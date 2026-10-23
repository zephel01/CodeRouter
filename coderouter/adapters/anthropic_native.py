"""Native Anthropic Messages API adapter (v0.3.x-1, extended in v0.4-A).

A passthrough adapter that speaks the Anthropic wire format directly, used
when the Anthropic ingress routes to a `kind: "anthropic"` provider (most
commonly api.anthropic.com itself, but also any server that speaks the
Anthropic Messages protocol — e.g. AWS Bedrock's Anthropic shim).

Design decisions:
    - No SDK dependency (plan.md §5.4): all calls are plain httpx.
    - v0.3.x-1 introduced `generate_anthropic` / `stream_anthropic` as the
      native passthrough entry points for the Anthropic ingress.
    - v0.4-A fills in the OpenAI-shaped `generate` / `stream` methods via
      reverse translation (ChatRequest ↔ AnthropicRequest). That means a
      `kind: anthropic` provider is now reachable from both /v1/messages
      (native passthrough) AND /v1/chat/completions (reverse-translated).
    - Streaming is parse-based (event/data → AnthropicStreamEvent) rather
      than pure byte passthrough. This preserves the v0.3-B mid-stream
      guard: upstream errors that surface after the first chunk still
      raise AdapterError, which the engine converts to MidStreamError.

Auth:
    Anthropic uses `x-api-key`, NOT `Authorization: Bearer`. `api_key_env`
    in ProviderConfig names the env var holding the key (typically
    ANTHROPIC_API_KEY).

    `anthropic-version` header defaults to "2023-06-01" and can be
    overridden via `provider.extra_body["anthropic_version"]` in
    providers.yaml for users on a pinned minor version.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
from pydantic import ValidationError

from coderouter.adapters.base import (
    AdapterError,
    BaseAdapter,
    ChatRequest,
    ChatResponse,
    ProviderCallOverrides,
    StreamChunk,
    StreamTruncatedError,
)
from coderouter.credentials import resolve_provider_credential
from coderouter.logging import (
    get_logger,
    log_output_filter_applied,
    log_stream_truncation_detected,
)
from coderouter.output_filters import OutputFilterChain
from coderouter.translation.anthropic import (
    AnthropicRequest,
    AnthropicResponse,
    AnthropicStreamEvent,
)

# NOTE: `coderouter.translation.convert` is intentionally NOT imported here
# at module scope (see H-7). The import chain is:
#   translation/__init__.py -> convert.py -> adapters/__init__.py
#     -> registry.py -> anthropic_native.py -> translation.convert (again)
# A top-level import here closes that cycle and makes standalone imports of
# `coderouter.translation.*` fail with a partial-initialization ImportError
# unless `coderouter.adapters` happened to be imported first. The three
# symbols used from `convert` are imported lazily inside `generate()` /
# `stream()` instead, which breaks the cycle without changing behavior.

logger = get_logger(__name__)

# Mirror openai_compat._RETRYABLE_STATUSES — same reasoning applies.
# 404: upstream may not have the requested model → next provider.
# 408 / 504: timeouts. 425: too early. 429: rate limit. 5xx: upstream errors.
_RETRYABLE_STATUSES = {404, 408, 425, 429, 500, 502, 503, 504}

_DEFAULT_ANTHROPIC_VERSION = "2023-06-01"


class _AnthropicTerminatorTracker:
    """v2.15.0: watch an Anthropic SSE event stream for its terminator.

    The parse loop in :meth:`AnthropicAdapter.stream_anthropic` used to be
    stateless across events — ``current_event`` was reset on every blank
    line and nothing remembered whether the stream had ended *properly*.
    That made "upstream sent 30 deltas and hung up" indistinguishable from
    "upstream sent the whole message". This tracker is the missing memory.

    What counts as a terminator (deliberately generous, to keep the false
    positive rate down — see ``stream_truncation_action`` in
    ``config/schemas.py``):

    * ``message_stop`` — the canonical Anthropic terminator.
    * ``message_delta`` carrying a non-null ``stop_reason`` — the message is
      semantically complete even if the provider omits ``message_stop``.
      Some Anthropic-compatible local servers do exactly that, and their
      content is *not* truncated, so flagging them would be a false positive.
    * a top-level ``error`` event — the upstream ended the stream on purpose
      and the frame is forwarded to the client verbatim (current behavior).
      Reporting truncation on top would double-report one failure.

    ``open_block_type`` tracks the currently-open content block so a stream
    cut in the middle of a ``tool_use`` block's ``input_json_delta`` can be
    reported as such: that is the case where the translation layer's
    terminator synthesis closes a block whose argument JSON is provably
    incomplete.
    """

    __slots__ = ("events", "open_block_type", "saw_start", "saw_terminator")

    def __init__(self) -> None:
        """Start with nothing observed."""
        self.saw_terminator: bool = False
        self.saw_start: bool = False
        self.events: int = 0
        self.open_block_type: str | None = None

    def observe(self, event_type: str, data: Any) -> None:
        """Fold one parsed SSE event into the tracked state.

        ``data`` is whatever ``json.loads`` produced for the block, so it is
        not guaranteed to be a mapping; non-dict payloads are treated as
        empty rather than raising inside a streaming hot path.
        """
        self.events += 1
        if not isinstance(data, dict):
            data = {}
        if event_type == "message_start":
            self.saw_start = True
        elif event_type == "content_block_start":
            block = data.get("content_block")
            self.open_block_type = block.get("type") if isinstance(block, dict) else None
        elif event_type == "content_block_stop":
            self.open_block_type = None
        elif event_type == "message_delta":
            delta = data.get("delta")
            if isinstance(delta, dict) and delta.get("stop_reason"):
                self.saw_terminator = True
        elif event_type in {"message_stop", "error"}:
            self.saw_terminator = True

    @property
    def tool_call_in_flight(self) -> bool:
        """True when a ``tool_use`` block was still open at end-of-stream."""
        return self.open_block_type == "tool_use"


def anthropic_messages_url(base_url: str) -> str:
    """Resolve the ``/v1/messages`` endpoint URL from a ``base_url``.

    ``base_url`` may be given with or without a trailing ``/v1`` — users
    may point it at either ``https://api.anthropic.com`` or
    ``https://api.anthropic.com/v1`` (LM Studio and similar servers
    commonly advertise the latter). We normalize by stripping a trailing
    ``/v1`` so appending ``/v1/messages`` always yields a valid,
    non-duplicated URL.

    Shared by :class:`AnthropicAdapter` and the continuous-probe guard so
    both hit the exact same endpoint (see bug H4: a mismatch made the
    probe target ``/v1/v1/messages`` and 404 on healthy backends).
    """
    base = base_url.rstrip("/")
    if base.endswith("/v1"):
        base = base[: -len("/v1")]
    return f"{base}/v1/messages"


class AnthropicAdapter(BaseAdapter):
    """Native Anthropic Messages API adapter (passthrough).

    The new methods ``generate_anthropic`` / ``stream_anthropic`` speak the
    Anthropic wire format end-to-end. The OpenAI-shaped ``generate`` /
    ``stream`` inherited contract raises a non-retryable error — if you
    want to reach Anthropic via /v1/chat/completions, configure an
    OpenRouter (or similar) `openai_compat` provider instead.
    """

    # ------------------------------------------------------------------
    # HTTP plumbing
    # ------------------------------------------------------------------

    def _url(self) -> str:
        """Resolve the ``/v1/messages`` endpoint URL from ``base_url``.

        ``base_url`` may be given with or without a trailing ``/v1`` —
        we normalize by stripping it so appending ``/v1/messages``
        always yields a valid URL. Delegates to the shared
        :func:`anthropic_messages_url` helper.
        """
        return anthropic_messages_url(str(self.config.base_url))

    def _headers(self, request: AnthropicRequest | None = None) -> dict[str, str]:
        """Build Anthropic-native HTTP headers, including beta-header forwarding.

        Always sets ``anthropic-version`` (configurable via ``extra_body``)
        and, when an API key is configured, ``x-api-key``. When the
        caller passes an ``AnthropicRequest`` that carries an
        ``anthropic-beta`` header value, it is forwarded verbatim so
        beta-gated body fields (``context_management`` etc.) survive.
        """
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "User-Agent": "CodeRouter/0.1",
            "anthropic-version": str(
                self.config.extra_body.get("anthropic_version", _DEFAULT_ANTHROPIC_VERSION)
            ),
        }
        # v2.14.0: see openai_compat._headers — same resolver, so an
        # Anthropic-protocol provider can also ride a CLI session token.
        api_key = resolve_provider_credential(self.config)
        if api_key:
            headers["x-api-key"] = api_key
        # v0.4-D: forward the client's anthropic-beta header verbatim.
        # This is what unlocks beta-gated body fields Claude Code relies on
        # (context_management, newer cache_control / thinking variants).
        # When the entry point is `/v1/chat/completions` (reverse
        # translation), the request won't carry one and we skip the header
        # — OpenAI clients wouldn't know what to put there anyway.
        if request is not None and request.anthropic_beta:
            headers["anthropic-beta"] = request.anthropic_beta
        return headers

    def _payload(self, req: AnthropicRequest, *, stream: bool) -> dict[str, Any]:
        """Serialize the AnthropicRequest to an outbound JSON body.

        The provider's configured `model` ALWAYS wins — the client-sent
        `model` field is treated as a routing placeholder (same policy as
        the OpenAI-compat adapter; see plan.md routing rules).
        """
        # Start from extra_body so client fields can override vendor defaults.
        # `anthropic_version` is a header, not body — strip it here.
        body: dict[str, Any] = {
            k: v for k, v in self.config.extra_body.items() if k != "anthropic_version"
        }

        dumped = req.model_dump(exclude_none=True)
        # `profile` is CodeRouter-only; `model` comes from provider config.
        dumped.pop("profile", None)
        dumped.pop("model", None)

        body.update(dumped)
        body["model"] = self.config.model
        body["stream"] = stream
        return body

    # ------------------------------------------------------------------
    # BaseAdapter contract
    # ------------------------------------------------------------------

    async def healthcheck(self) -> bool:
        """Cheapest meaningful probe: POST /v1/messages with 1 token cap.

        Anthropic doesn't expose a public /models list, and a GET to / or
        /v1/messages returns 405. A minimal POST is the least-bad signal
        that auth works and the endpoint is reachable.
        """
        try:
            resp = await self.client().post(
                self._url(),
                headers=self._headers(),
                json={
                    "model": self.config.model,
                    "max_tokens": 1,
                    "messages": [{"role": "user", "content": "ping"}],
                },
                timeout=5.0,
            )
            # 200 is clearly healthy. 4xx auth errors indicate the
            # endpoint is reachable even if the key is bad — still a
            # "the server answered" signal for healthcheck purposes.
            # 5xx is upstream trouble.
            return resp.status_code < 500
        except httpx.HTTPError:
            return False

    async def generate(
        self,
        request: ChatRequest,
        *,
        overrides: ProviderCallOverrides | None = None,
    ) -> ChatResponse:
        """OpenAI-shaped generate (v0.4-A): reverse-translate → native call → back.

        ChatRequest is converted to AnthropicRequest via
        ``to_anthropic_request``, the native ``generate_anthropic`` path
        handles the HTTP call (including retryable status mapping), and
        the AnthropicResponse is converted back to ChatResponse via
        ``to_chat_response``. The ``coderouter_provider`` tag is preserved
        on both sides.
        """
        # Local import to avoid a circular import at module load time (H-7):
        # convert.py transitively imports coderouter.adapters, which imports
        # this module. Importing convert.py here (call time, not load time)
        # keeps `coderouter.translation.*` importable standalone.
        from coderouter.translation.convert import to_anthropic_request, to_chat_response

        anth_req = to_anthropic_request(request)
        anth_resp = await self.generate_anthropic(anth_req, overrides=overrides)
        chat_resp = to_chat_response(anth_resp)
        # generate_anthropic stamps coderouter_provider on the Anthropic
        # response; to_chat_response forwards it. Re-assert for safety
        # in case a caller constructed a response without the tag.
        if not chat_resp.coderouter_provider:
            chat_resp.coderouter_provider = self.name
        return chat_resp

    async def stream(
        self,
        request: ChatRequest,
        *,
        overrides: ProviderCallOverrides | None = None,
    ) -> AsyncIterator[StreamChunk]:
        """OpenAI-shaped stream (v0.4-A): reverse-translate → native SSE → chunks.

        Mirrors ``generate()``: ChatRequest → AnthropicRequest via
        ``to_anthropic_request``, the native ``stream_anthropic`` yields
        AnthropicStreamEvents, and ``stream_anthropic_to_chat_chunks``
        re-segments those into OpenAI StreamChunks on the fly.

        Error semantics preserve the v0.3-B mid-stream guard:
            - Initial upstream failure → AdapterError propagates to the
              engine's fallback path.
            - Mid-stream failure (after first chunk) → AdapterError is
              re-raised by the engine as MidStreamError.
            - Anthropic ``event: error`` → translator raises
              AdapterError(retryable=False), same treatment as above.
        """
        # Local import to avoid a circular import at module load time (H-7);
        # see the matching comment in generate() above.
        from coderouter.translation.convert import (
            stream_anthropic_to_chat_chunks,
            to_anthropic_request,
        )

        anth_req = to_anthropic_request(request)
        events = self.stream_anthropic(anth_req, overrides=overrides)
        async for chunk in stream_anthropic_to_chat_chunks(events, provider_name=self.name):
            yield chunk

    # ------------------------------------------------------------------
    # v1.0-A: output_filters helpers (native Anthropic streaming)
    # ------------------------------------------------------------------

    def _process_stream_event_for_filters(
        self,
        event: AnthropicStreamEvent,
        *,
        chains: dict[int, OutputFilterChain],
        logged_flag: list[bool],
    ) -> list[AnthropicStreamEvent]:
        """Apply the configured output_filters chain to a parsed SSE event.

        Returns the list of events to yield downstream:
            - ``content_block_start`` (type=text) → create a fresh chain
              keyed by block index, return [event].
            - ``content_block_delta`` (type=text_delta) → filter the
              delta text in place (may produce an empty string that the
              downstream pass-through still emits; clients tolerate it).
            - ``content_block_stop`` → flush the chain for this index.
              If the flush produced tail text (the safe-to-emit suffix
              the filter had been holding), a synthetic
              ``content_block_delta`` event is prepended so the client
              sees every byte that was not part of a matched tag, then
              the original ``content_block_stop`` follows.
            - All other event types pass through unchanged.

        Logs ``output-filter-applied`` exactly once per stream (via the
        ``logged_flag`` mutable cell; same dedupe shape as v0.5-C's
        reasoning-strip log).
        """
        if not self.config.output_filters:
            return [event]

        data = event.data

        if event.type == "content_block_start":
            block = data.get("content_block") or {}
            if isinstance(block, dict) and block.get("type") == "text":
                idx = data.get("index", 0)
                chains[idx] = OutputFilterChain(self.config.output_filters)
            return [event]

        if event.type == "content_block_delta":
            delta = data.get("delta") or {}
            if isinstance(delta, dict) and delta.get("type") == "text_delta":
                idx = data.get("index", 0)
                chain = chains.get(idx)
                if chain is not None:
                    text = delta.get("text", "")
                    if isinstance(text, str) and text:
                        delta["text"] = chain.feed(text)
            return [event]

        if event.type == "content_block_stop":
            idx = data.get("index", 0)
            chain = chains.pop(idx, None)
            if chain is None:
                return [event]
            tail = chain.feed("", eof=True)
            events: list[AnthropicStreamEvent] = []
            if tail:
                events.append(
                    AnthropicStreamEvent(
                        type="content_block_delta",
                        data={
                            "type": "content_block_delta",
                            "index": idx,
                            "delta": {"type": "text_delta", "text": tail},
                        },
                    )
                )
            events.append(event)
            if chain.any_applied and not logged_flag[0]:
                log_output_filter_applied(
                    logger,
                    provider=self.name,
                    filters=chain.applied_filters(),
                    streaming=True,
                )
                logged_flag[0] = True
            return events

        return [event]

    # ------------------------------------------------------------------
    # Native Anthropic entry points (v0.3.x-1)
    # ------------------------------------------------------------------

    async def generate_anthropic(
        self,
        request: AnthropicRequest,
        *,
        overrides: ProviderCallOverrides | None = None,
    ) -> AnthropicResponse:
        """Non-streaming passthrough: AnthropicRequest → AnthropicResponse."""
        url = self._url()
        payload = self._payload(request, stream=False)
        timeout = self.effective_timeout(overrides)

        try:
            resp = await self.client().post(
                url, json=payload, headers=self._headers(request), timeout=timeout
            )
        except httpx.TimeoutException as exc:
            raise AdapterError(
                f"timeout contacting {url}", provider=self.name, retryable=True
            ) from exc
        except httpx.HTTPError as exc:
            raise AdapterError(
                f"transport error: {exc}", provider=self.name, retryable=True
            ) from exc

        if resp.status_code >= 400:
            raise AdapterError(
                f"{resp.status_code} from upstream: {resp.text[:200]}",
                provider=self.name,
                status_code=resp.status_code,
                retryable=resp.status_code in _RETRYABLE_STATUSES,
                upstream_body=resp.text,
                retry_after=resp.headers.get("retry-after"),
            )

        try:
            data = resp.json()
        except json.JSONDecodeError as exc:
            raise AdapterError(
                f"invalid JSON from upstream: {exc}",
                provider=self.name,
                retryable=False,
            ) from exc

        # v1.0-A: apply output_filters to every text content block. One
        # fresh chain per block so `<think>...</think>` state from block N
        # never bleeds into block N+1 (matters when Anthropic thinking +
        # text blocks coexist, or with future multi-block responses).
        if self.config.output_filters:
            any_block_modified = False
            applied_names: list[str] = []
            blocks = data.get("content")
            if isinstance(blocks, list):
                for block in blocks:
                    if (
                        isinstance(block, dict)
                        and block.get("type") == "text"
                        and isinstance(block.get("text"), str)
                        and block["text"]
                    ):
                        chain = OutputFilterChain(self.config.output_filters)
                        block["text"] = chain.feed(block["text"], eof=True)
                        if chain.any_applied:
                            any_block_modified = True
                            for n in chain.applied_filters():
                                if n not in applied_names:
                                    applied_names.append(n)
            if any_block_modified:
                log_output_filter_applied(
                    logger,
                    provider=self.name,
                    filters=applied_names,
                    streaming=False,
                )

        # Tag with provider metadata and return. Unknown Anthropic fields
        # (future additions like thinking blocks) pass through via
        # extra="allow" on AnthropicResponse.
        data["coderouter_provider"] = self.name
        # M6: guard the model validation. A 200 body that isn't a valid
        # Anthropic Messages response (missing ``content``/``id``, an
        # error envelope returned with a 200, etc.) would otherwise raise
        # a bare pydantic ValidationError that escapes the engine's
        # AdapterError-based retry/fallback path. Convert to a retryable
        # AdapterError so the chain can fall through (mirrors the
        # invalid-JSON branch above, but retryable — a malformed shape is
        # usually transient upstream noise rather than a config fault).
        try:
            return AnthropicResponse.model_validate(data)
        except ValidationError as exc:
            raise AdapterError(
                f"malformed response shape from upstream: {exc}",
                provider=self.name,
                retryable=True,
            ) from exc

    async def stream_anthropic(
        self,
        request: AnthropicRequest,
        *,
        overrides: ProviderCallOverrides | None = None,
    ) -> AsyncIterator[AnthropicStreamEvent]:
        """Streaming passthrough: Anthropic SSE → AnthropicStreamEvent iterator.

        Parses the upstream SSE stream (event/data pairs) and yields each
        event as a typed AnthropicStreamEvent. The ingress re-serializes
        these back to the wire, so events are round-tripped through the
        same structure the non-native path produces.

        Upstream errors after the first event raise AdapterError; the
        FallbackEngine converts those to MidStreamError (v0.3-B).
        """
        url = self._url()
        payload = self._payload(request, stream=True)
        timeout = self.effective_timeout(overrides)

        # v1.0-A: per-block filter chains (keyed by content block index)
        # + a mutable one-cell flag that lets the filter helper log
        # exactly once per stream without ping-ponging state through
        # every yield site.
        filter_chains: dict[int, OutputFilterChain] = {}
        logged_flag: list[bool] = [False]

        # v2.15.0 (stream-truncation): remember whether the upstream ever
        # terminated its message. Resolved once per call — the knob is a
        # profile-level setting threaded in through ProviderCallOverrides.
        truncation_action = self.effective_stream_truncation_action(overrides)
        terminator = _AnthropicTerminatorTracker()

        try:
            # H3: stream over the shared client (pool / keep-alive / TLS
            # reuse). Only the ``stream(...)`` context is scoped here; the
            # client persists and is closed via ``aclose`` on shutdown. The
            # ``async with`` releases the response's borrowed connection even
            # if the caller abandons this generator mid-stream — no GC leak.
            async with self.client().stream(
                "POST", url, json=payload, headers=self._headers(request), timeout=timeout
            ) as resp:
                if resp.status_code >= 400:
                    body = await resp.aread()
                    raise AdapterError(
                        f"{resp.status_code} from upstream: {body[:200]!r}",
                        provider=self.name,
                        status_code=resp.status_code,
                        retryable=resp.status_code in _RETRYABLE_STATUSES,
                        upstream_body=body.decode("utf-8", errors="replace"),
                        retry_after=resp.headers.get("retry-after"),
                    )

                # Anthropic SSE block shape:
                #   event: <event-type>
                #   data: <json>
                #   <blank line>
                # We buffer per-line and emit on blank-line boundary.
                current_event: str | None = None
                data_lines: list[str] = []

                async for line in resp.aiter_lines():
                    if line == "":
                        # End of block — flush if well-formed.
                        if current_event is not None and data_lines:
                            data_str = "\n".join(data_lines)
                            try:
                                data_obj = json.loads(data_str)
                            except json.JSONDecodeError:
                                # Skip malformed blocks rather than
                                # abort the whole stream.
                                current_event = None
                                data_lines = []
                                continue
                            terminator.observe(current_event, data_obj)
                            for out_event in self._process_stream_event_for_filters(
                                AnthropicStreamEvent(type=current_event, data=data_obj),
                                chains=filter_chains,
                                logged_flag=logged_flag,
                            ):
                                yield out_event
                        current_event = None
                        data_lines = []
                        continue

                    if line.startswith(":"):
                        # SSE comment / heartbeat
                        continue
                    if line.startswith("event:"):
                        current_event = line[len("event:") :].strip()
                    elif line.startswith("data:"):
                        data_lines.append(line[len("data:") :].lstrip())
                    # Silently ignore other field names (id:, retry:) —
                    # Anthropic doesn't use them today.

                # Trailing block without terminating blank line.
                if current_event is not None and data_lines:
                    data_str = "\n".join(data_lines)
                    try:
                        data_obj = json.loads(data_str)
                    except json.JSONDecodeError:
                        data_obj = None
                    if data_obj is not None:
                        terminator.observe(current_event, data_obj)
                        for out_event in self._process_stream_event_for_filters(
                            AnthropicStreamEvent(type=current_event, data=data_obj),
                            chains=filter_chains,
                            logged_flag=logged_flag,
                        ):
                            yield out_event

            # v2.15.0 (stream-truncation): the upstream line iterator is
            # exhausted. Reaching here with no terminator observed means the
            # HTTP body ended cleanly while the Anthropic message was still
            # open — the exact case transport-level error handling below
            # cannot see (``httpx.RemoteProtocolError`` and timeouts already
            # raise). Under ``off`` this block is inert, which is what keeps
            # the default byte-for-byte identical to v2.14.0.
            if truncation_action != "off" and not terminator.saw_terminator:
                log_stream_truncation_detected(
                    logger,
                    self.name,
                    action=truncation_action,
                    wire="anthropic",
                    events_forwarded=terminator.events,
                    saw_stream_start=terminator.saw_start,
                    tool_call_in_flight=terminator.tool_call_in_flight,
                )
                if truncation_action == "error":
                    raise StreamTruncatedError(
                        "upstream stream truncated (no message_stop terminator)",
                        provider=self.name,
                        tool_call_in_flight=terminator.tool_call_in_flight,
                    )
        except httpx.TimeoutException as exc:
            raise AdapterError(
                f"timeout streaming from {url}", provider=self.name, retryable=True
            ) from exc
        except httpx.HTTPError as exc:
            raise AdapterError(
                f"transport error: {exc}", provider=self.name, retryable=True
            ) from exc
