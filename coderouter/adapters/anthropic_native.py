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

from coderouter.adapters.base import (
    AdapterError,
    BaseAdapter,
    ChatRequest,
    ChatResponse,
    ProviderCallOverrides,
    StreamChunk,
)
from coderouter.config.loader import resolve_api_key
from coderouter.logging import get_logger, log_output_filter_applied
from coderouter.output_filters import OutputFilterChain
from coderouter.translation.anthropic import (
    AnthropicRequest,
    AnthropicResponse,
    AnthropicStreamEvent,
)
from coderouter.translation.convert import (
    stream_anthropic_to_chat_chunks,
    to_anthropic_request,
    to_chat_response,
)

logger = get_logger(__name__)

# Mirror openai_compat._RETRYABLE_STATUSES — same reasoning applies.
# 404: upstream may not have the requested model → next provider.
# 408 / 504: timeouts. 425: too early. 429: rate limit. 5xx: upstream errors.
_RETRYABLE_STATUSES = {404, 408, 425, 429, 500, 502, 503, 504}

_DEFAULT_ANTHROPIC_VERSION = "2023-06-01"


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
        base = str(self.config.base_url).rstrip("/")
        # Users may point base_url at either `https://api.anthropic.com`
        # or `https://api.anthropic.com/v1`. We normalize to the former so
        # we can always append /v1/messages.
        if base.endswith("/v1"):
            base = base[: -len("/v1")]
        return f"{base}/v1/messages"

    def _headers(self, request: AnthropicRequest | None = None) -> dict[str, str]:
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "User-Agent": "CodeRouter/0.1",
            "anthropic-version": str(
                self.config.extra_body.get("anthropic_version", _DEFAULT_ANTHROPIC_VERSION)
            ),
        }
        api_key = resolve_api_key(self.config.api_key_env)
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
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post(
                    self._url(),
                    headers=self._headers(),
                    json={
                        "model": self.config.model,
                        "max_tokens": 1,
                        "messages": [{"role": "user", "content": "ping"}],
                    },
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
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(url, json=payload, headers=self._headers(request))
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
        return AnthropicResponse.model_validate(data)

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

        try:
            async with (
                httpx.AsyncClient(timeout=timeout) as client,
                client.stream("POST", url, json=payload, headers=self._headers(request)) as resp,
            ):
                if resp.status_code >= 400:
                    body = await resp.aread()
                    raise AdapterError(
                        f"{resp.status_code} from upstream: {body[:200]!r}",
                        provider=self.name,
                        status_code=resp.status_code,
                        retryable=resp.status_code in _RETRYABLE_STATUSES,
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
                        for out_event in self._process_stream_event_for_filters(
                            AnthropicStreamEvent(type=current_event, data=data_obj),
                            chains=filter_chains,
                            logged_flag=logged_flag,
                        ):
                            yield out_event
        except httpx.TimeoutException as exc:
            raise AdapterError(
                f"timeout streaming from {url}", provider=self.name, retryable=True
            ) from exc
        except httpx.HTTPError as exc:
            raise AdapterError(
                f"transport error: {exc}", provider=self.name, retryable=True
            ) from exc
