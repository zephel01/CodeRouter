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
from coderouter.errors import CodeRouterError
from coderouter.logging import (
    classify_cache_outcome,
    get_logger,
    log_cache_observed,
    log_chain_paid_gate_blocked,
)
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


# ---------------------------------------------------------------------------
# v1.9-A: cache observation helper
#
# Single chokepoint that turns a successful AnthropicResponse into a
# ``cache-observed`` log line. Lives at module scope (not on the engine
# class) so unit tests can feed a synthetic response without spinning up
# a fallback engine.
#
# We pass `request_had_cache_control` in from the caller (already
# computed in v0.5-B for the capability-degraded gate) so we don't
# re-walk the AnthropicRequest tree twice per call.
# ---------------------------------------------------------------------------


def _emit_cache_observed(
    response: AnthropicResponse,
    *,
    provider: str,
    request_had_cache_control: bool,
    streaming: bool,
) -> None:
    """Extract usage / cache fields from an AnthropicResponse and log them.

    The Anthropic ``usage`` block carries cache_read_input_tokens /
    cache_creation_input_tokens via the ``extra="allow"`` config on
    :class:`AnthropicUsage` — the engine never had to care about them
    until v1.9-A. We pull them out of ``model_extra`` rather than typing
    them into the schema because (a) the openai_compat → anthropic
    converter zero-fills usage so ``input_tokens`` / ``output_tokens``
    are always present, but cache fields land only on native
    Anthropic / LM Studio /v1/messages responses, and (b) future
    Anthropic API additions (e.g. ephemeral_5m vs ephemeral_1h
    breakdowns) extend ``model_extra`` without a schema change.

    The ``streaming=True`` arg path is reserved — v1.9-A does not yet
    aggregate ``message_delta`` events, so streaming responses always
    record ``outcome=unknown`` (per :data:`coderouter.logging.CacheOutcome`
    docstring). Streaming aggregation lands in v1.9-B.
    """
    usage = response.usage
    extra = usage.model_extra or {}
    raw_read = extra.get("cache_read_input_tokens", 0)
    raw_creation = extra.get("cache_creation_input_tokens", 0)
    cache_read = raw_read if isinstance(raw_read, int) else 0
    cache_creation = raw_creation if isinstance(raw_creation, int) else 0
    # ``usage_present`` is True if either usage was populated by the
    # upstream OR derived in conversion. We treat any non-zero token
    # count as evidence the upstream answered with usage info; an
    # all-zero usage from the openai_compat converter is treated as
    # "unknown" so the no_cache bucket only counts real cache misses.
    usage_present = (
        usage.input_tokens > 0
        or usage.output_tokens > 0
        or cache_read > 0
        or cache_creation > 0
    )
    outcome = classify_cache_outcome(
        usage_present=usage_present,
        cache_read_input_tokens=cache_read,
        cache_creation_input_tokens=cache_creation,
    )
    log_cache_observed(
        logger,
        provider=provider,
        request_had_cache_control=request_had_cache_control,
        outcome=outcome,
        cache_read_input_tokens=cache_read,
        cache_creation_input_tokens=cache_creation,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        streaming=streaming,
    )


class NoProvidersAvailableError(CodeRouterError):
    """Raised when every provider in the chain has failed (or was filtered out)."""

    def __init__(self, profile: str, errors: list[AdapterError]) -> None:
        """Construct with the resolved profile name and per-provider errors.

        ``errors`` may be empty when every provider was filtered out
        before a call was attempted (e.g. the paid-gate blocked the
        whole chain); in that case the rendered message falls back to
        ``"no providers eligible"``.
        """
        self.profile = profile
        self.errors = errors
        detail = " | ".join(str(e) for e in errors) or "no providers eligible"
        super().__init__(f"profile={profile!r}: all providers failed: {detail}")


class MidStreamError(CodeRouterError):
    """Raised when a provider fails AFTER it has already emitted at least
    one chunk to the client. Fallback is not attempted (the client has
    received partial content, so switching providers would corrupt the
    stream). Callers should surface this as a terminal error event.
    """

    def __init__(self, provider: str, original: AdapterError) -> None:
        """Wrap the underlying :class:`AdapterError` with the provider name.

        The ingress layer catches this and converts it into an in-stream
        ``event: error`` (never a 5xx) because HTTP headers have already
        shipped by the time we know the stream failed.
        """
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
    """Sequential fallback router — the core of CodeRouter.

    Holds the resolved :class:`CodeRouterConfig` plus a pre-built adapter
    per provider (adapters are cheap but constructing them per-request
    would repeatedly re-read provider config). Exposes four entry
    points: :meth:`generate` / :meth:`stream` for OpenAI-shaped requests,
    :meth:`generate_anthropic` / :meth:`stream_anthropic` for Anthropic
    Messages API requests. See the module docstring for the per-kind
    translation behavior.
    """

    def __init__(self, config: CodeRouterConfig) -> None:
        """Pre-build one adapter per configured provider.

        Adapters are stateless with respect to requests (all state is
        held in the per-call ``ProviderCallOverrides``), so caching by
        provider name across requests is safe and avoids the cost of
        re-parsing YAML / re-resolving env vars on every request.
        """
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
        """Non-streaming OpenAI-shaped generation with sequential fallback.

        Walks the chain in order, returning the first provider's response.
        On retryable :class:`AdapterError` (transport failure, rate
        limit, upstream 5xx, etc.) the loop advances; on non-retryable
        errors it breaks immediately. When every provider has been tried
        without success, raises :class:`NoProvidersAvailableError` with
        the full per-provider error list so the ingress layer can
        surface a single 502.
        """
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
        # v1.9-A: cache observation needs to know whether the request
        # ever asked for caching. Compute once; the v0.5-B gate uses the
        # same value below for the capability-degraded log.
        request_had_cache_control = anthropic_request_has_cache_control(request)

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
            if request_had_cache_control and not provider_supports_cache_control(
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
                # `is_native` is the same test as this `isinstance`; we do
                # it directly here so mypy narrows `adapter` to
                # AnthropicAdapter inside the branch (BaseAdapter itself
                # does not declare the Anthropic-shaped methods).
                if isinstance(adapter, AnthropicAdapter):
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
            # v1.9-A: pair every successful Anthropic response with a
            # cache-observed log line. Native Anthropic / LM Studio
            # /v1/messages report cache_read_input_tokens /
            # cache_creation_input_tokens via usage.model_extra;
            # openai_compat-converted responses fall through to
            # outcome=unknown.
            _emit_cache_observed(
                resp,
                provider=adapter.name,
                request_had_cache_control=request_had_cache_control,
                streaming=False,
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
                # See the non-streaming branch above: `is_native` and this
                # isinstance test are the same check; we do it inline so
                # mypy narrows for stream_anthropic (not on BaseAdapter).
                if isinstance(adapter, AnthropicAdapter):
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
