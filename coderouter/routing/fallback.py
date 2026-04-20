"""Sequential fallback engine.

Behavior (plan.md §7):
    1. Iterate the provider list of the chosen profile in order.
    2. Skip paid providers when ALLOW_PAID is false.
    3. Try generate() / stream() on each. If AdapterError(retryable=True) → next.
    4. If all providers fail, raise NoProvidersAvailableError.

Dual entry points (v0.3.x-1):
    The engine exposes both OpenAI-shaped (generate / stream) and
    Anthropic-shaped (generate_anthropic / stream_anthropic) methods. The
    Anthropic-shaped methods dispatch per-provider on `ProviderConfig.kind`:
        - kind="anthropic":    passthrough — no translation on either leg.
        - kind="openai_compat": translate AnthropicRequest → ChatRequest,
                               call the adapter, translate ChatResponse /
                               stream chunks back. Tool-call repair runs on
                               non-streaming responses; streaming tool-turns
                               are downgraded to non-stream internally
                               (v0.3-D strategy).

    Mixed chains are supported: a profile can list a native Anthropic
    provider first and fall through to an openai_compat provider second.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from coderouter.adapters.anthropic_native import AnthropicAdapter
from coderouter.adapters.base import (
    AdapterError,
    BaseAdapter,
    ChatRequest,
    ChatResponse,
    StreamChunk,
)
from coderouter.adapters.registry import build_adapter
from coderouter.config.schemas import CodeRouterConfig
from coderouter.logging import get_logger
from coderouter.translation import (
    AnthropicRequest,
    AnthropicResponse,
    AnthropicStreamEvent,
    stream_chat_to_anthropic_events,
    synthesize_anthropic_stream_from_response,
    to_anthropic_response,
    to_chat_request,
)

logger = get_logger(__name__)


class NoProvidersAvailableError(Exception):
    """Raised when every provider in the chain has failed (or was filtered out)."""

    def __init__(self, profile: str, errors: list[AdapterError]) -> None:
        self.profile = profile
        self.errors = errors
        detail = " | ".join(str(e) for e in errors) or "no providers eligible"
        super().__init__(f"profile={profile!r}: all providers failed: {detail}")


class MidStreamError(Exception):
    """Raised when a provider fails AFTER it has already emitted at least
    one chunk to the client. Fallback is not attempted (the client has
    received partial content, so switching providers would corrupt the
    stream). Callers should surface this as a terminal error event.
    """

    def __init__(self, provider: str, original: AdapterError) -> None:
        self.provider = provider
        self.original = original
        super().__init__(
            f"provider {provider!r} failed mid-stream: {original}"
        )


class FallbackEngine:
    def __init__(self, config: CodeRouterConfig) -> None:
        self.config = config
        # Cache adapters so we don't re-instantiate per request
        self._adapters: dict[str, BaseAdapter] = {
            p.name: build_adapter(p) for p in config.providers
        }

    def _resolve_chain(self, profile_name: str | None) -> list[BaseAdapter]:
        """Return the list of adapters to try, in order, for this profile."""
        chosen = profile_name or self.config.default_profile
        chain = self.config.profile_by_name(chosen)

        adapters: list[BaseAdapter] = []
        for prov_name in chain.providers:
            try:
                provider_cfg = self.config.provider_by_name(prov_name)
            except KeyError:
                logger.warning(
                    "skip-unknown-provider",
                    extra={"profile": chosen, "provider": prov_name},
                )
                continue
            if provider_cfg.paid and not self.config.allow_paid:
                logger.info(
                    "skip-paid-provider",
                    extra={"profile": chosen, "provider": prov_name},
                )
                continue
            adapters.append(self._adapters[prov_name])
        return adapters

    async def generate(self, request: ChatRequest) -> ChatResponse:
        adapters = self._resolve_chain(request.profile)
        errors: list[AdapterError] = []
        for adapter in adapters:
            logger.info(
                "try-provider",
                extra={"provider": adapter.name, "stream": False},
            )
            try:
                response = await adapter.generate(request)
                logger.info(
                    "provider-ok",
                    extra={"provider": adapter.name, "stream": False},
                )
                return response
            except AdapterError as exc:
                logger.warning(
                    "provider-failed",
                    extra={
                        "provider": adapter.name,
                        "status": exc.status_code,
                        "retryable": exc.retryable,
                        "error": str(exc)[:500],
                    },
                )
                errors.append(exc)
                if not exc.retryable:
                    break
        raise NoProvidersAvailableError(
            profile=request.profile or self.config.default_profile,
            errors=errors,
        )

    async def stream(self, request: ChatRequest) -> AsyncIterator[StreamChunk]:
        """Stream from the first provider that successfully starts streaming.

        Important: once we begin yielding chunks from an adapter, we cannot
        fall back mid-stream (the client has already received partial content).
        We only fall through if the *initial* response is an error.
        """
        adapters: list[BaseAdapter] = self._resolve_chain(request.profile)
        errors: list[AdapterError] = []
        for adapter in adapters:
            logger.info(
                "try-provider",
                extra={"provider": adapter.name, "stream": True},
            )
            stream_iter = adapter.stream(request)
            try:
                first = await anext(stream_iter)
            except StopAsyncIteration:
                # Adapter produced zero chunks — treat as failure, try next
                errors.append(
                    AdapterError(
                        "empty stream", provider=adapter.name, retryable=True
                    )
                )
                continue
            except AdapterError as exc:
                logger.warning(
                    "provider-failed",
                    extra={
                        "provider": adapter.name,
                        "status": exc.status_code,
                        "retryable": exc.retryable,
                        "error": str(exc)[:500],
                    },
                )
                errors.append(exc)
                if not exc.retryable:
                    break
                continue

            logger.info(
                "provider-ok",
                extra={"provider": adapter.name, "stream": True},
            )
            yield first
            # Mid-stream fallback guard: once the first byte is out the door,
            # any subsequent adapter exception is terminal — we cannot fall
            # back without risking duplicate / interleaved content reaching
            # the client.
            try:
                async for chunk in stream_iter:
                    yield chunk
            except AdapterError as exc:
                logger.warning(
                    "provider-failed-midstream",
                    extra={
                        "provider": adapter.name,
                        "status": exc.status_code,
                        "retryable": exc.retryable,
                        "error": str(exc)[:500],
                    },
                )
                raise MidStreamError(adapter.name, exc) from exc
            return

        raise NoProvidersAvailableError(
            profile=request.profile or self.config.default_profile,
            errors=errors,
        )

    # ==================================================================
    # Anthropic-shaped entry points (v0.3.x-1)
    # ==================================================================
    #
    # These exist so the /v1/messages ingress can route to a `kind:
    # "anthropic"` provider without a lossy round-trip through the
    # OpenAI-shaped internal format. Per-provider dispatch:
    #     - AnthropicAdapter: direct passthrough via generate_anthropic /
    #       stream_anthropic — no translation on either leg.
    #     - any other adapter: translate AnthropicRequest → ChatRequest,
    #       call the OpenAI-shaped methods, translate the result back.
    #       Tool-call repair + v0.3-D downgrade happen on this path.

    async def generate_anthropic(
        self, request: AnthropicRequest
    ) -> AnthropicResponse:
        """Non-streaming Anthropic request, per-provider dispatch."""
        adapters = self._resolve_chain(request.profile)
        errors: list[AdapterError] = []
        tool_names = [t.name for t in request.tools] if request.tools else None

        for adapter in adapters:
            is_native = isinstance(adapter, AnthropicAdapter)
            logger.info(
                "try-provider",
                extra={
                    "provider": adapter.name,
                    "stream": False,
                    "native_anthropic": is_native,
                },
            )
            try:
                if is_native:
                    resp = await adapter.generate_anthropic(request)
                else:
                    chat_req = to_chat_request(request)
                    chat_req.stream = False
                    chat_resp = await adapter.generate(chat_req)
                    resp = to_anthropic_response(
                        chat_resp, allowed_tool_names=tool_names
                    )
            except AdapterError as exc:
                logger.warning(
                    "provider-failed",
                    extra={
                        "provider": adapter.name,
                        "status": exc.status_code,
                        "retryable": exc.retryable,
                        "error": str(exc)[:500],
                    },
                )
                errors.append(exc)
                if not exc.retryable:
                    break
                continue

            logger.info(
                "provider-ok",
                extra={
                    "provider": adapter.name,
                    "stream": False,
                    "native_anthropic": is_native,
                },
            )
            return resp

        raise NoProvidersAvailableError(
            profile=request.profile or self.config.default_profile,
            errors=errors,
        )

    async def stream_anthropic(
        self, request: AnthropicRequest
    ) -> AsyncIterator[AnthropicStreamEvent]:
        """Streaming Anthropic request, per-provider dispatch.

        For non-native providers with tools declared, we use the v0.3-D
        downgrade path (run the request non-streaming internally, repair
        tool calls, then synthesize an Anthropic SSE event sequence) —
        the same logic that used to live in the ingress. Consolidating
        it here keeps the ingress thin and lets native providers bypass
        the downgrade entirely (Anthropic emits structured tool_use
        blocks natively, no repair needed).
        """
        adapters = self._resolve_chain(request.profile)
        errors: list[AdapterError] = []
        tool_names = [t.name for t in request.tools] if request.tools else None

        for adapter in adapters:
            is_native = isinstance(adapter, AnthropicAdapter)
            downgrading = (not is_native) and bool(request.tools)
            logger.info(
                "try-provider",
                extra={
                    "provider": adapter.name,
                    "stream": True,
                    "native_anthropic": is_native,
                    "downgrade": downgrading,
                },
            )

            # Stage 1: acquire an AnthropicStreamEvent iterator. Failures
            # here are candidates for fallback (no bytes have been sent to
            # the client yet).
            event_iter: AsyncIterator[AnthropicStreamEvent]
            first: AnthropicStreamEvent
            try:
                if is_native:
                    event_iter = adapter.stream_anthropic(request)
                    first = await anext(event_iter)
                elif downgrading:
                    # v0.3-D downgrade: run non-streaming, repair, replay.
                    chat_req = to_chat_request(request)
                    chat_req.stream = False
                    chat_resp = await adapter.generate(chat_req)
                    anth_resp = to_anthropic_response(
                        chat_resp, allowed_tool_names=tool_names
                    )
                    event_iter = synthesize_anthropic_stream_from_response(
                        anth_resp
                    )
                    first = await anext(event_iter)
                else:
                    chat_req = to_chat_request(request)
                    chat_req.stream = True
                    event_iter = stream_chat_to_anthropic_events(
                        adapter.stream(chat_req)
                    )
                    first = await anext(event_iter)
            except StopAsyncIteration:
                errors.append(
                    AdapterError(
                        "empty stream", provider=adapter.name, retryable=True
                    )
                )
                continue
            except AdapterError as exc:
                logger.warning(
                    "provider-failed",
                    extra={
                        "provider": adapter.name,
                        "status": exc.status_code,
                        "retryable": exc.retryable,
                        "error": str(exc)[:500],
                    },
                )
                errors.append(exc)
                if not exc.retryable:
                    break
                continue

            logger.info(
                "provider-ok",
                extra={
                    "provider": adapter.name,
                    "stream": True,
                    "native_anthropic": is_native,
                    "downgrade": downgrading,
                },
            )
            yield first
            # Mid-stream guard identical to stream() — any error after the
            # first event is terminal.
            try:
                async for ev in event_iter:
                    yield ev
            except AdapterError as exc:
                logger.warning(
                    "provider-failed-midstream",
                    extra={
                        "provider": adapter.name,
                        "status": exc.status_code,
                        "retryable": exc.retryable,
                        "error": str(exc)[:500],
                    },
                )
                raise MidStreamError(adapter.name, exc) from exc
            return

        raise NoProvidersAvailableError(
            profile=request.profile or self.config.default_profile,
            errors=errors,
        )
