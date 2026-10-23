"""Common intermediate format + BaseAdapter ABC.

The shape mirrors OpenAI's Chat Completions API since memo.txt §2.4 chose
OpenAI-compat as the standard ingress. v0.2+ will add a separate Anthropic
adapter that converts Messages API into / out of this same format.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

from coderouter.config.schemas import ProviderConfig
from coderouter.errors import CodeRouterError


class Message(BaseModel):
    """A single chat message in OpenAI Chat Completions shape.

    Mirrors the OpenAI wire format (role + content, plus tool-call
    fields for assistant/tool turns). ``content`` is ``None`` on
    assistant messages that carry only ``tool_calls`` — the OpenAI
    spec allows this, and the Anthropic→OpenAI translation in
    :mod:`coderouter.translation.convert` emits it for tool-use turns.
    """

    model_config = ConfigDict(extra="allow")

    role: Literal["system", "user", "assistant", "tool"]
    # OpenAI spec allows content: null on assistant messages that carry only
    # tool_calls. Anthropic → OpenAI translation also produces this when an
    # assistant turn has only tool_use blocks (no text).
    content: str | list[dict[str, Any]] | None = None
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: list[dict[str, Any]] | None = None


class ChatRequest(BaseModel):
    """An inbound OpenAI-shaped request to the engine.

    Accepts the standard OpenAI Chat Completions fields plus the
    CodeRouter-specific ``profile`` extension (carried in the body as
    ``{"profile": "fast"}``; excluded from any upstream serialization
    via ``Field(exclude=True)``). ``extra="allow"`` lets callers pass
    provider-specific knobs (e.g. Ollama's ``think: false``) straight
    through without a schema bump.
    """

    model_config = ConfigDict(extra="allow")

    model: str | None = None
    messages: list[Message]
    stream: bool = False
    temperature: float | None = None
    max_tokens: int | None = None
    top_p: float | None = None
    stop: list[str] | None = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: Any | None = None

    # CodeRouter-specific extension (not sent upstream)
    profile: str | None = Field(default=None, exclude=True)


class ChatResponse(BaseModel):
    """A non-streaming response in OpenAI Chat Completions shape."""

    model_config = ConfigDict(extra="allow")

    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: list[dict[str, Any]]
    usage: dict[str, Any] | None = None

    # Routing metadata — added by CodeRouter, not from upstream
    coderouter_provider: str | None = Field(default=None)


class StreamChunk(BaseModel):
    """A single SSE chunk in OpenAI streaming format."""

    model_config = ConfigDict(extra="allow")

    id: str
    object: str = "chat.completion.chunk"
    created: int
    model: str
    choices: list[dict[str, Any]]
    # Present on the trailing chunk when a provider honors
    # `stream_options.include_usage=true`. Also populated by the
    # Anthropic→OpenAI reverse translation in
    # coderouter.translation.convert when mirroring `message_delta`
    # usage into an OpenAI stream.
    usage: dict[str, Any] | None = None


#: v2.16.0: max chars of an upstream error body an :class:`AdapterError`
#: keeps. Error bodies are small; the cap only bounds a misbehaving upstream
#: that answers an error status with a large HTML page.
UPSTREAM_BODY_CAP = 4096


class AdapterError(CodeRouterError):
    """Raised when a provider call fails in a way the fallback engine should retry on."""

    def __init__(
        self,
        message: str,
        *,
        provider: str,
        status_code: int | None = None,
        retryable: bool = True,
        upstream_body: str | None = None,
        retry_after: str | None = None,
    ) -> None:
        """Construct an AdapterError.

        Args:
            message: Human-readable failure reason.
            provider: The ``ProviderConfig.name`` that failed — used by
                the fallback engine's log trail and by tests that assert
                WHICH provider raised.
            status_code: HTTP status code when the failure originated
                from an upstream response. ``None`` for transport /
                JSON-parse / pre-flight failures.
            retryable: When True, the fallback engine may try the next
                provider in the chain. When False, the engine stops
                and surfaces the error as a terminal failure.
            upstream_body: v2.16.0 — the upstream's error body (capped at
                :data:`UPSTREAM_BODY_CAP` chars), kept so the ingress can
                hand the upstream's own wording back to the client.
                Claude Code recovers from several 400s (rejected beta
                headers, tool search, context overflow) only when it can
                read the upstream message. Never logged by this class.
            retry_after: v2.16.0 — the upstream's ``Retry-After`` header
                value, verbatim, when it sent one.
        """
        super().__init__(message)
        self.provider = provider
        self.status_code = status_code
        self.retryable = retryable
        self.upstream_body = (
            upstream_body[:UPSTREAM_BODY_CAP] if upstream_body is not None else None
        )
        self.retry_after = retry_after

    def __str__(self) -> str:
        """Render as ``[provider status=NNN] message`` for log trails."""
        sc = f" status={self.status_code}" if self.status_code is not None else ""
        return f"[{self.provider}{sc}] {super().__str__()}"


# v2.15.0 (stream-truncation): raised by an adapter when the upstream SSE
# stream ended *without* its protocol terminator (``message_stop`` on the
# Anthropic wire, ``data: [DONE]`` / a ``finish_reason`` on the OpenAI wire).
#
# HTTP-level breakage (timeout, ``httpx.RemoteProtocolError``) already became a
# plain ``AdapterError`` in both adapters. This subclass covers the layer
# mismatch the transport cannot see: the HTTP body ended cleanly, but the *LLM
# protocol* carried inside it was still mid-message.
#
# It subclasses ``AdapterError`` on purpose — every existing engine branch
# (``except AdapterError``) keeps working untouched, and the L2/L4/L5/L6 guards
# learn from the failure automatically. The subclass only adds identity, so
# ``classify_adapter_error`` can label the hop ``stream-truncated`` instead of
# the generic ``upstream-error``.
#
# Raised only when the active profile sets ``stream_truncation_action: error``.
# Under ``off`` (the default) and ``warn`` the adapter returns normally and the
# legacy terminator-synthesis path in ``coderouter.translation.convert`` (H6 /
# M9) runs byte-for-byte as before.
class StreamTruncatedError(AdapterError):
    """Upstream SSE stream ended without its protocol terminator event."""

    def __init__(
        self,
        message: str,
        *,
        provider: str,
        tool_call_in_flight: bool = False,
    ) -> None:
        """Construct a truncation error.

        Args:
            message: Human-readable reason. Never contains upstream body text.
            provider: The ``ProviderConfig.name`` whose stream was cut.
            tool_call_in_flight: True when the stream was cut while a
                ``tool_use`` / ``tool_calls`` block was still open, i.e. the
                accumulated argument JSON is certainly incomplete. Carried for
                observability only — it never changes control flow.
        """
        super().__init__(message, provider=provider, status_code=None, retryable=True)
        self.tool_call_in_flight = tool_call_in_flight


# v2.17.0: the provider did not produce its first real streamed output within
# ``ProviderConfig.ttft_timeout_s``. Raised by the engine (not the adapter),
# before any byte reached the client, so the chain falls back normally and the
# self-healing guards learn from it like any other retryable failure.
class TTFTTimeoutError(AdapterError):
    """No first token within ``ttft_timeout_s``."""

    def __init__(self, *, provider: str, ttft_timeout_s: float) -> None:
        super().__init__(
            f"timeout: no first token within {ttft_timeout_s:g}s (ttft_timeout_s)",
            provider=provider,
            status_code=None,
            retryable=True,
        )
        self.ttft_timeout_s = ttft_timeout_s


# v0.6-B: per-call overrides resolved from the active profile. The engine
# builds one instance per request (since a profile is invariant across its
# chain) and threads it through every adapter call on that chain. Adapters
# use :meth:`effective_timeout` / :meth:`effective_append_system_prompt` to
# pick the winning value (profile override > provider default).
#
# Design notes:
#   - Both fields are Optional. ``None`` means "leave the provider default
#     alone" — so ``ProviderCallOverrides()`` is a safe no-op default and
#     legacy call sites that pass nothing keep their old behavior.
#   - ``append_system_prompt=""`` is a meaningful explicit value: "for
#     this profile, clear the provider's directive". The adapter must
#     distinguish ``None`` (no override) from ``""`` (override-to-empty).
#
# v2.15.0 (stream-truncation): ``stream_truncation_action`` rides the same
# channel. It is a *profile*-level knob but has to be read inside the adapter's
# SSE parse loop, and ``ProviderCallOverrides`` is the only object the engine
# already threads into every adapter call. Its default of ``"off"`` keeps
# ``ProviderCallOverrides()`` (and every legacy call site that passes nothing)
# on the pre-v2.15.0 behavior.
class ProviderCallOverrides(BaseModel):
    """Per-call provider overrides, resolved from the active profile."""

    model_config = ConfigDict(extra="forbid")

    timeout_s: float | None = None
    append_system_prompt: str | None = None
    stream_truncation_action: Literal["off", "warn", "error"] = "off"


class BaseAdapter(ABC):
    """Provider-specific adapter. Subclasses implement HTTP plumbing."""

    def __init__(self, config: ProviderConfig) -> None:
        """Bind the adapter to a :class:`ProviderConfig`.

        Subclasses do not need to override this. A single shared
        :class:`httpx.AsyncClient` is created lazily on first use (see
        :meth:`client`) and reused across every call on this adapter so
        the connection pool, HTTP keep-alive, and TLS session are all
        reused. Per-call timeouts are still honored by passing
        ``timeout=`` to ``client.post`` / ``client.stream`` — they do
        NOT require a fresh client.
        """
        self.config = config
        # Shared client, created lazily inside the running event loop the
        # first time an adapter method needs it (see ``client``). Kept as
        # ``None`` until then so constructing an adapter has no I/O cost and
        # is safe outside an event loop (e.g. at import / config time).
        self._client: httpx.AsyncClient | None = None

    @property
    def name(self) -> str:
        """Shortcut for ``self.config.name`` — used in log trails and errors."""
        return self.config.name

    # ---- H3: shared HTTP client (connection-pool / keep-alive reuse) ----
    def client(self) -> httpx.AsyncClient:
        """Return the shared :class:`httpx.AsyncClient`, creating it lazily.

        The client is created on first use so construction happens inside
        the running event loop and carries no I/O cost at adapter-build
        time. Subsequent calls return the same instance, which is what
        lets httpx reuse pooled connections, keep-alive, and the TLS
        session across requests.

        No default timeout is baked in here: every call site passes an
        explicit per-call ``timeout=`` (resolved from the active profile
        via :meth:`effective_timeout`), so leaving the client timeout
        unset avoids a surprising default clamping long-running calls.
        """
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=None)
        return self._client

    async def aclose(self) -> None:
        """Close the shared HTTP client and drop the reference.

        Idempotent: safe to call when no client was ever created, and
        safe to call more than once. Invoked from the app lifespan
        shutdown path so pooled connections are released cleanly rather
        than left to garbage collection. After ``aclose`` a later call
        re-creates the client on demand via :meth:`client`.
        """
        client = self._client
        self._client = None
        if client is not None:
            await client.aclose()

    # ---- v0.6-B override resolution helpers -----------------------------
    def effective_timeout(self, overrides: ProviderCallOverrides | None) -> float:
        """Profile override wins when set; else provider default."""
        if overrides is not None and overrides.timeout_s is not None:
            return overrides.timeout_s
        return self.config.timeout_s

    def effective_append_system_prompt(self, overrides: ProviderCallOverrides | None) -> str | None:
        """Profile override replaces provider directive when set.

        ``None`` means no override → fall through to provider. ``""``
        (explicit empty) means "clear the provider directive for this
        profile" → return None so the caller skips injection entirely.
        """
        if overrides is not None and overrides.append_system_prompt is not None:
            return overrides.append_system_prompt or None
        return self.config.append_system_prompt

    def effective_stream_truncation_action(
        self, overrides: ProviderCallOverrides | None
    ) -> str:
        """v2.15.0: resolve ``stream_truncation_action`` for this call.

        There is no per-provider counterpart — the knob lives on the profile
        only — so this is just a null-safe read. ``None`` overrides (legacy
        call sites, unit tests constructing adapters directly) resolve to
        ``"off"``, which is the pre-v2.15.0 behavior.
        """
        if overrides is None:
            return "off"
        return overrides.stream_truncation_action

    @abstractmethod
    async def healthcheck(self) -> bool:
        """Lightweight check that the upstream is reachable. Return True if healthy."""

    @abstractmethod
    async def generate(
        self,
        request: ChatRequest,
        *,
        overrides: ProviderCallOverrides | None = None,
    ) -> ChatResponse:
        """Non-streaming completion. Raise AdapterError on failure.

        ``overrides`` carries profile-level timeouts / directives (v0.6-B).
        Legacy callers that pass nothing keep the pre-v0.6-B behavior.
        """

    @abstractmethod
    def stream(
        self,
        request: ChatRequest,
        *,
        overrides: ProviderCallOverrides | None = None,
    ) -> AsyncIterator[StreamChunk]:
        """Streaming completion. Yield StreamChunks. Raise AdapterError on failure."""
