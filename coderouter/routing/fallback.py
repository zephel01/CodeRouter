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
from typing import Final

from coderouter.adapters.anthropic_native import AnthropicAdapter
from coderouter.adapters.base import (
    AdapterError,
    BaseAdapter,
    ChatRequest,
    ChatResponse,
    ProviderCallOverrides,
    StreamChunk,
)
from coderouter.adapters.registry import build_adapter
from coderouter.config.schemas import CodeRouterConfig
from coderouter.logging import get_logger, log_chain_paid_gate_blocked
from coderouter.routing.capability import (
    anthropic_request_has_cache_control,
    anthropic_request_requires_thinking,
    log_capability_degraded,
    provider_supports_cache_control,
    provider_supports_thinking,
    strip_thinking,
)
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
        super().__init__(f"provider {provider!r} failed mid-stream: {original}")


# ---------------------------------------------------------------------------
# v0.5.1 A-3: "probable misconfig" warn
#
# Motivation (from v0.5-verify.md §Follow-ons, 2026-04-20 re-verify):
#   The first verify run hit OpenRouter with a mis-read env var and got
#   401 back. The single-provider chain short-circuited as it should, but
#   the surface error was just "all providers failed" — operators had to
#   grep the ``provider-failed`` line to spot the common 401 in the
#   `error` field. A one-line warn at the aggregate level turns that
#   grep-and-diagnose into a directly-readable hint.
#
# Scope:
#   - Fires only when EVERY attempt in the chain returned the SAME
#     non-retryable auth status (401 or 403). A mixed chain (one 401 +
#     one 429, etc.) is ambiguous and stays quiet; so does any chain
#     where at least one error was retryable (transient / rate-limit).
#   - Auth-only by design. 400 "model not found" is also non-retryable
#     but reflects a config-vs-upstream-reality mismatch that a generic
#     "probable misconfig" hint would mis-diagnose. Widening later is
#     cheap if we see the need.
#   - Fires for single-provider chains too (the verify scenario). "Every
#     attempt" is trivially all attempts when there is one.
# ---------------------------------------------------------------------------

_AUTH_STATUS_CODES: Final[frozenset[int]] = frozenset({401, 403})


def _warn_if_uniform_auth_failure(errors: list[AdapterError], *, profile: str) -> None:
    """Emit a ``chain-uniform-auth-failure`` warn when the whole chain 401/403'd.

    Called from each of the four ``raise NoProvidersAvailableError`` sites
    right before the raise. No-op when:
        - ``errors`` is empty (nothing was attempted — e.g. every provider
          was filtered out by paid-blocking).
        - The first error's status is not in ``_AUTH_STATUS_CODES``.
        - Any error has a different status_code, or is retryable.

    The log is intentionally separate from the raised exception (which
    stays unchanged for API stability) — it sits alongside the
    ``provider-failed`` lines and gives operators a single-line diagnosis
    without changing the ingress response shape.
    """
    if not errors:
        return
    status = errors[0].status_code
    if status not in _AUTH_STATUS_CODES:
        return
    for exc in errors:
        if exc.status_code != status or exc.retryable:
            return
    logger.warning(
        "chain-uniform-auth-failure",
        extra={
            "profile": profile,
            "status": status,
            "count": len(errors),
            "providers": [exc.provider for exc in errors],
            "hint": "probable-misconfig",
        },
    )


class FallbackEngine:
    def __init__(self, config: CodeRouterConfig) -> None:
        self.config = config
        # Cache adapters so we don't re-instantiate per request
        self._adapters: dict[str, BaseAdapter] = {
            p.name: build_adapter(p) for p in config.providers
        }

    def _resolve_profile_overrides(self, profile_name: str | None) -> ProviderCallOverrides:
        """v0.6-B: build the ProviderCallOverrides for the active profile.

        Invariant across every adapter call on one chain (profiles are
        immutable per request), so callers resolve this once at the top of
        each engine method and pass to every adapter invocation.
        """
        chosen = profile_name or self.config.default_profile
        profile = self.config.profile_by_name(chosen)
        return ProviderCallOverrides(
            timeout_s=profile.timeout_s,
            append_system_prompt=profile.append_system_prompt,
        )

    def _resolve_chain(self, profile_name: str | None) -> list[BaseAdapter]:
        """Return the list of adapters to try, in order, for this profile.

        v0.6-C declarative ALLOW_PAID gate: when the paid gate filters
        the chain to zero adapters, emit ``chain-paid-gate-blocked`` at
        warn level via :func:`log_chain_paid_gate_blocked`. Per-provider
        ``skip-paid-provider`` info lines are still emitted (one per
        blocked provider) so per-provider traceability is intact; the
        warn sits at chain granularity for operator diagnosis.
        """
        chosen = profile_name or self.config.default_profile
        chain = self.config.profile_by_name(chosen)

        adapters: list[BaseAdapter] = []
        blocked_by_paid: list[str] = []
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
                blocked_by_paid.append(prov_name)
                continue
            adapters.append(self._adapters[prov_name])

        # v0.6-C: aggregate warn fires ONLY when the paid gate left the
        # chain empty. A mixed chain where at least one free provider
        # survives stays quiet (the normal try-provider / provider-
        # failed trail already narrates what happened).
        if not adapters and blocked_by_paid:
            log_chain_paid_gate_blocked(
                logger,
                profile=chosen,
                blocked_providers=blocked_by_paid,
            )
        return adapters

    def _resolve_anthropic_chain(self, request: AnthropicRequest) -> list[tuple[BaseAdapter, bool]]:
        """Resolve a chain, annotating each adapter with a ``will_degrade`` flag.

        v0.5-A capability gate: when ``request`` carries ``thinking: {type:
        enabled}`` and a provider does not support it (per
        ``provider_supports_thinking``), we still include that provider in
        the chain — it becomes a degraded-fallback. The block will be
        stripped before the call and a ``capability-degraded`` log line
        will fire. Capable providers are pulled to the front (stable sort)
        so the user's ordering is preserved within each bucket.

        Returns a list of ``(adapter, will_degrade)`` pairs in the order
        they should be tried. When the request has no capability
        requirement, all entries have ``will_degrade=False`` and the order
        matches ``_resolve_chain``.
        """
        base = self._resolve_chain(request.profile)
        if not anthropic_request_requires_thinking(request):
            return [(a, False) for a in base]

        capable: list[tuple[BaseAdapter, bool]] = []
        degraded: list[tuple[BaseAdapter, bool]] = []
        for adapter in base:
            if provider_supports_thinking(adapter.config):
                capable.append((adapter, False))
            else:
                degraded.append((adapter, True))
        return capable + degraded

    async def generate(self, request: ChatRequest) -> ChatResponse:
        adapters = self._resolve_chain(request.profile)
        overrides = self._resolve_profile_overrides(request.profile)
        errors: list[AdapterError] = []
        for adapter in adapters:
            logger.info(
                "try-provider",
                extra={"provider": adapter.name, "stream": False},
            )
            try:
                response = await adapter.generate(request, overrides=overrides)
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
        profile = request.profile or self.config.default_profile
        _warn_if_uniform_auth_failure(errors, profile=profile)
        raise NoProvidersAvailableError(profile=profile, errors=errors)

    async def stream(self, request: ChatRequest) -> AsyncIterator[StreamChunk]:
        """Stream from the first provider that successfully starts streaming.

        Important: once we begin yielding chunks from an adapter, we cannot
        fall back mid-stream (the client has already received partial content).
        We only fall through if the *initial* response is an error.
        """
        adapters: list[BaseAdapter] = self._resolve_chain(request.profile)
        overrides = self._resolve_profile_overrides(request.profile)
        errors: list[AdapterError] = []
        for adapter in adapters:
            logger.info(
                "try-provider",
                extra={"provider": adapter.name, "stream": True},
            )
            stream_iter = adapter.stream(request, overrides=overrides)
            try:
                first = await anext(stream_iter)
            except StopAsyncIteration:
                # Adapter produced zero chunks — treat as failure, try next
                errors.append(AdapterError("empty stream", provider=adapter.name, retryable=True))
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

        profile = request.profile or self.config.default_profile
        _warn_if_uniform_auth_failure(errors, profile=profile)
        raise NoProvidersAvailableError(profile=profile, errors=errors)

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

    async def generate_anthropic(self, request: AnthropicRequest) -> AnthropicResponse:
        """Non-streaming Anthropic request, per-provider dispatch."""
        chain = self._resolve_anthropic_chain(request)
        overrides = self._resolve_profile_overrides(request.profile)
        errors: list[AdapterError] = []
        tool_names = [t.name for t in request.tools] if request.tools else None

        for adapter, will_degrade in chain:
            is_native = isinstance(adapter, AnthropicAdapter)
            effective_request = request
            if will_degrade:
                # v0.5-A: strip unsupported blocks before handing to this
                # provider and emit a structured log so operators can see
                # the downgrade after the fact. Today only `thinking` is
                # gated; the list is surfaced in the log for forward-compat.
                effective_request = strip_thinking(request)
                log_capability_degraded(
                    logger,
                    provider=adapter.name,
                    dropped=["thinking"],
                    reason="provider-does-not-support",
                )
            # v0.5-B: observability-only gate for cache_control. The
            # field is silently dropped during Anthropic → OpenAI
            # translation for openai_compat providers — no strip is
            # needed here (to_chat_request already handles it) and no
            # chain reorder is done (user ordering preserved). We just
            # emit a log line so operators can see the lossiness.
            if anthropic_request_has_cache_control(request) and not provider_supports_cache_control(
                adapter.config
            ):
                log_capability_degraded(
                    logger,
                    provider=adapter.name,
                    dropped=["cache_control"],
                    reason="translation-lossy",
                )
            logger.info(
                "try-provider",
                extra={
                    "provider": adapter.name,
                    "stream": False,
                    "native_anthropic": is_native,
                    "degraded": will_degrade,
                },
            )
            try:
                if is_native:
                    resp = await adapter.generate_anthropic(effective_request, overrides=overrides)
                else:
                    chat_req = to_chat_request(effective_request)
                    chat_req.stream = False
                    chat_resp = await adapter.generate(chat_req, overrides=overrides)
                    resp = to_anthropic_response(chat_resp, allowed_tool_names=tool_names)
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

        profile = request.profile or self.config.default_profile
        _warn_if_uniform_auth_failure(errors, profile=profile)
        raise NoProvidersAvailableError(profile=profile, errors=errors)

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
        chain = self._resolve_anthropic_chain(request)
        overrides = self._resolve_profile_overrides(request.profile)
        errors: list[AdapterError] = []
        tool_names = [t.name for t in request.tools] if request.tools else None

        for adapter, will_degrade in chain:
            is_native = isinstance(adapter, AnthropicAdapter)
            downgrading = (not is_native) and bool(request.tools)
            effective_request = request
            if will_degrade:
                effective_request = strip_thinking(request)
                log_capability_degraded(
                    logger,
                    provider=adapter.name,
                    dropped=["thinking"],
                    reason="provider-does-not-support",
                )
            # v0.5-B: mirror of the non-streaming path — see comment there.
            if anthropic_request_has_cache_control(request) and not provider_supports_cache_control(
                adapter.config
            ):
                log_capability_degraded(
                    logger,
                    provider=adapter.name,
                    dropped=["cache_control"],
                    reason="translation-lossy",
                )
            logger.info(
                "try-provider",
                extra={
                    "provider": adapter.name,
                    "stream": True,
                    "native_anthropic": is_native,
                    "downgrade": downgrading,
                    "degraded": will_degrade,
                },
            )

            # Stage 1: acquire an AnthropicStreamEvent iterator. Failures
            # here are candidates for fallback (no bytes have been sent to
            # the client yet).
            event_iter: AsyncIterator[AnthropicStreamEvent]
            first: AnthropicStreamEvent
            try:
                if is_native:
                    event_iter = adapter.stream_anthropic(effective_request, overrides=overrides)
                    first = await anext(event_iter)
                elif downgrading:
                    # v0.3-D downgrade: run non-streaming, repair, replay.
                    chat_req = to_chat_request(effective_request)
                    chat_req.stream = False
                    chat_resp = await adapter.generate(chat_req, overrides=overrides)
                    anth_resp = to_anthropic_response(chat_resp, allowed_tool_names=tool_names)
                    event_iter = synthesize_anthropic_stream_from_response(anth_resp)
                    first = await anext(event_iter)
                else:
                    chat_req = to_chat_request(effective_request)
                    chat_req.stream = True
                    event_iter = stream_chat_to_anthropic_events(
                        adapter.stream(chat_req, overrides=overrides)
                    )
                    first = await anext(event_iter)
            except StopAsyncIteration:
                errors.append(AdapterError("empty stream", provider=adapter.name, retryable=True))
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

        profile = request.profile or self.config.default_profile
        _warn_if_uniform_auth_failure(errors, profile=profile)
        raise NoProvidersAvailableError(profile=profile, errors=errors)
