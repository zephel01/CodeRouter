"""v2.15.0: request-scoped fallback trace — *why* did the chain move?

Before v2.15.0 the engine could tell an operator (via ``try-provider`` /
``provider-failed`` log lines) that a fallback happened, but nothing tied
the two together into a single "we left A for B because of R" record, and
nothing at all reached the *client*. This module is that missing record.

Vocabulary
==========

``reason`` already existed in the codebase as a keyword argument on
:func:`coderouter.routing.capability.log_capability_degraded`
(``provider-does-not-support`` / ``translation-lossy`` /
``unsupported-backend``) and on the drift verdict. Those are **degrade**
reasons: the request stays on the same provider, something is stripped.
v2.15.0 keeps those strings verbatim (they are re-exported here as
``REASON_PROVIDER_DOES_NOT_SUPPORT`` etc. so there is one place to read
the whole vocabulary) and adds the **fallback** reasons — the ones that
actually move the request to a different provider.

Only fallback reasons ever produce a :class:`FallbackHop`. Degrade
reasons never do, by construction: nothing moved.

Fallback reasons split in two families:

*Pre-attempt* (the provider was filtered out of the chain during
:meth:`FallbackEngine._resolve_chain`, so it was never called):
``paid-gate`` / ``budget-exceeded`` / ``memory-pressure`` /
``backend-unhealthy`` / ``self-healing-excluded`` / ``unknown-provider``.

*Attempt-failure* (the provider was called and did not deliver):
``timeout`` / ``rate-limit`` / ``auth`` / ``upstream-5xx`` /
``upstream-4xx`` / ``connection`` / ``upstream-error`` /
``empty-response`` / ``empty-stream``.

Request scoping
===============

The trace lives in a :class:`~contextvars.ContextVar`, the same isolation
mechanism the drift verdict (``_drift_verdict_ctx``) and the M11 prepared
dispatch (``_prepared_dispatch_ctx``) already use in
:mod:`coderouter.routing.fallback`. Starlette runs each request handler in
its own asyncio task, so one request never sees another's hops, and the
value cannot go stale across requests (the ContextVar defaults to
``None``).

Two entry points, deliberately different:

* :func:`begin_fallback_trace` — installs a *fresh* trace. Called by the
  engine's dispatch entry points. ``keep_existing=True`` preserves a trace
  the ingress's ``apply_context_budget`` pre-pass already started for the
  same request (chain-resolve skips are recorded there, before dispatch).
* :func:`current_fallback_trace` — read-only lookup. The ingress uses it
  after dispatch to build the ``X-CodeRouter-Fallback-*`` headers and the
  ``coderouter_fallback`` SSE metadata event.

Ordering / shape contract (what a client can rely on)
=====================================================

For a request that fell back twice::

    X-CodeRouter-Fallback-From:   local
    X-CodeRouter-Fallback-To:     openrouter
    X-CodeRouter-Fallback-Reason: timeout,upstream-5xx
    X-CodeRouter-Fallback-Chain:  local>ollama>openrouter

``Chain`` is the providers in try-order; ``Reason`` is the reason for each
*departure*, in the same order. ``len(Chain) == len(Reason) + 1`` when the
chain finally produced an answer, and ``len(Chain) == len(Reason)`` when
the whole chain was exhausted (there was no provider to hand off to, so
``X-CodeRouter-Fallback-To`` is omitted). ``From`` is the first provider
that was abandoned, ``To`` the one that ultimately served.

None of these headers are emitted when no fallback happened — the
zero-fallback path is byte-for-byte unchanged from v2.14.0.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from coderouter.adapters.base import AdapterError

# ---------------------------------------------------------------------------
# Canonical reason vocabulary
# ---------------------------------------------------------------------------

# -- pre-attempt (chain-resolve filters; provider was never called) ---------
REASON_PAID_GATE: Final = "paid-gate"
REASON_BUDGET_EXCEEDED: Final = "budget-exceeded"
REASON_MEMORY_PRESSURE: Final = "memory-pressure"
REASON_BACKEND_UNHEALTHY: Final = "backend-unhealthy"
REASON_SELF_HEALING_EXCLUDED: Final = "self-healing-excluded"
REASON_UNKNOWN_PROVIDER: Final = "unknown-provider"

# -- attempt failures (provider was called) --------------------------------
REASON_TIMEOUT: Final = "timeout"
REASON_RATE_LIMIT: Final = "rate-limit"
REASON_AUTH: Final = "auth"
REASON_UPSTREAM_5XX: Final = "upstream-5xx"
REASON_UPSTREAM_4XX: Final = "upstream-4xx"
REASON_CONNECTION: Final = "connection"
REASON_UPSTREAM_ERROR: Final = "upstream-error"
REASON_EMPTY_RESPONSE: Final = "empty-response"
REASON_EMPTY_STREAM: Final = "empty-stream"
# v2.15.0: the upstream SSE stream ended without its protocol terminator —
# distinct from ``empty-stream`` (nothing at all arrived) and from
# ``connection`` (the transport itself broke). Produced only when the profile
# sets ``stream_truncation_action: error``.
REASON_STREAM_TRUNCATED: Final = "stream-truncated"
# v2.17.0: no first real output within the provider's ``ttft_timeout_s``.
REASON_TTFT_TIMEOUT: Final = "ttft-timeout"

# -- degrade reasons (pre-existing strings; never produce a hop) -----------
# Kept here so the whole ``reason`` vocabulary is readable in one place.
REASON_PROVIDER_DOES_NOT_SUPPORT: Final = "provider-does-not-support"
REASON_TRANSLATION_LOSSY: Final = "translation-lossy"
REASON_UNSUPPORTED_BACKEND: Final = "unsupported-backend"

PRE_ATTEMPT_REASONS: Final[frozenset[str]] = frozenset(
    {
        REASON_PAID_GATE,
        REASON_BUDGET_EXCEEDED,
        REASON_MEMORY_PRESSURE,
        REASON_BACKEND_UNHEALTHY,
        REASON_SELF_HEALING_EXCLUDED,
        REASON_UNKNOWN_PROVIDER,
    }
)

ATTEMPT_FAILURE_REASONS: Final[frozenset[str]] = frozenset(
    {
        REASON_TIMEOUT,
        REASON_RATE_LIMIT,
        REASON_AUTH,
        REASON_UPSTREAM_5XX,
        REASON_UPSTREAM_4XX,
        REASON_CONNECTION,
        REASON_UPSTREAM_ERROR,
        REASON_EMPTY_RESPONSE,
        REASON_EMPTY_STREAM,
        REASON_STREAM_TRUNCATED,
        REASON_TTFT_TIMEOUT,
    }
)

FALLBACK_REASONS: Final[frozenset[str]] = PRE_ATTEMPT_REASONS | ATTEMPT_FAILURE_REASONS

DEGRADE_REASONS: Final[frozenset[str]] = frozenset(
    {
        REASON_PROVIDER_DOES_NOT_SUPPORT,
        REASON_TRANSLATION_LOSSY,
        REASON_UNSUPPORTED_BACKEND,
    }
)


# ---------------------------------------------------------------------------
# Header names — follow the v2.0-F / v2.0-G convention already in
# ``ingress/anthropic_routes.py`` (``X-CodeRouter-Context-Budget`` /
# ``X-CodeRouter-Drift``): ``X-CodeRouter-`` prefix, Train-Case suffix,
# short scalar value.
# ---------------------------------------------------------------------------

HEADER_FALLBACK_FROM: Final = "X-CodeRouter-Fallback-From"
HEADER_FALLBACK_TO: Final = "X-CodeRouter-Fallback-To"
HEADER_FALLBACK_REASON: Final = "X-CodeRouter-Fallback-Reason"
HEADER_FALLBACK_CHAIN: Final = "X-CodeRouter-Fallback-Chain"

# SSE metadata event name for the streaming path. Mirrors the existing
# ``coderouter_partial`` extension event (v2.0-H) — a client-optional
# trailing frame, emitted after ``message_stop``, that spec-compliant
# Anthropic SDKs ignore.
SSE_FALLBACK_EVENT: Final = "coderouter_fallback"

# Provider names and reasons are config identifiers, but a header value has
# to stay in the printable-ASCII token range no matter what a user typed in
# their YAML. Anything outside the allowlist collapses to ``_``.
_SAFE_CHARS: Final[frozenset[str]] = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
)


def _sanitize(value: str) -> str:
    """Reduce ``value`` to a header-safe token.

    Keeps ``[A-Za-z0-9._-]`` and replaces everything else with ``_`` so a
    provider named ``my provider`` or one carrying non-ASCII can never
    produce a malformed (or header-injecting) response header.
    """
    return "".join(ch if ch in _SAFE_CHARS else "_" for ch in value) or "_"


def describe_adapter_error(exc: AdapterError) -> str:
    """Build the short, non-leaking ``detail`` string for a failed attempt.

    Deliberately structural: the upstream's error *text* stays in the
    existing ``provider-failed`` warn line (which passes through the
    secret-redaction filter) and never reaches a response header or an SSE
    event. Here we only report the HTTP status, or the transport shape
    when there was no response at all.
    """
    if exc.status_code is not None:
        return f"status={exc.status_code}"
    reason = classify_adapter_error(exc)
    if reason == REASON_STREAM_TRUNCATED:
        # Not a transport fault — the HTTP body ended cleanly; it was the
        # LLM protocol inside it that never closed.
        return "no-terminator"
    if reason == REASON_TTFT_TIMEOUT:
        return "no-first-token"
    return f"transport={reason}"


def classify_adapter_error(exc: AdapterError) -> str:
    """Map an :class:`AdapterError` onto a canonical fallback reason.

    Status codes are the primary signal. Adapters raise with
    ``status_code=None`` for pre-response failures (httpx timeout,
    transport error, JSON decode), so those are disambiguated by the
    message text the adapters already write — ``timeout ...`` and
    ``transport error: ...`` are stable prefixes in both
    ``adapters/openai_compat.py`` and ``adapters/anthropic_native.py``.

    v2.15.0: ``StreamTruncatedError`` is checked first and by type, not by
    message text. It carries ``status_code=None`` and would otherwise land in
    the generic ``upstream-error`` bucket, hiding the one failure mode this
    reason exists to name. The import is function-local because
    ``adapters.base`` is a TYPE_CHECKING-only import at module scope here
    (the runtime import order would otherwise cycle through the registry).
    """
    from coderouter.adapters.base import StreamTruncatedError, TTFTTimeoutError

    if isinstance(exc, StreamTruncatedError):
        return REASON_STREAM_TRUNCATED
    if isinstance(exc, TTFTTimeoutError):
        return REASON_TTFT_TIMEOUT
    status = exc.status_code
    if status is None:
        message = str(exc).lower()
        if "timeout" in message or "timed out" in message:
            return REASON_TIMEOUT
        if "transport error" in message or "connect" in message:
            return REASON_CONNECTION
        return REASON_UPSTREAM_ERROR
    if status == 408:
        return REASON_TIMEOUT
    if status == 429:
        return REASON_RATE_LIMIT
    if status in {401, 403}:
        return REASON_AUTH
    if status >= 500:
        return REASON_UPSTREAM_5XX
    if status >= 400:
        return REASON_UPSTREAM_4XX
    return REASON_UPSTREAM_ERROR


@dataclass(frozen=True)
class FallbackHop:
    """One provider→provider transition and the reason behind it.

    ``to_provider`` is ``None`` until the *next* provider is actually
    attempted (see :meth:`FallbackTrace.record_attempt`); it stays ``None``
    forever when the chain was exhausted without a successor.
    """

    from_provider: str
    reason: str
    to_provider: str | None = None
    detail: str | None = None
    stream: bool = False
    pre_attempt: bool = False

    def as_dict(self) -> dict[str, Any]:
        """Serializable view, used by the SSE metadata event."""
        return {
            "from": self.from_provider,
            "to": self.to_provider,
            "reason": self.reason,
            "detail": self.detail,
            "stream": self.stream,
            "pre_attempt": self.pre_attempt,
        }


@dataclass
class FallbackTrace:
    """Ordered record of every fallback that happened while serving one request.

    Mutated in place by the engine. The ingress only ever reads it, so the
    object can be shared safely between the request-handler context and the
    SSE-iterator context (which inherits a copy of the same ContextVar
    mapping, and therefore the same object).
    """

    hops: list[FallbackHop] = field(default_factory=list)
    attempts: list[str] = field(default_factory=list)
    profile: str | None = None
    #: Set once the engine has emitted the ``fallback-occurred`` log lines
    #: for this request, so a dispatch path with several terminal returns
    #: cannot log the same hop trail twice.
    logged: bool = False

    # -- recording ---------------------------------------------------------

    def record_skip(
        self, provider: str, reason: str, *, detail: str | None = None
    ) -> None:
        """Record a provider filtered out of the chain before it was called."""
        self.hops.append(
            FallbackHop(
                from_provider=provider,
                reason=reason,
                detail=detail,
                pre_attempt=True,
            )
        )

    def record_failure(
        self,
        provider: str,
        reason: str,
        *,
        detail: str | None = None,
        stream: bool = False,
    ) -> None:
        """Record a provider that was called and did not deliver."""
        self.hops.append(
            FallbackHop(
                from_provider=provider,
                reason=reason,
                detail=detail,
                stream=stream,
                pre_attempt=False,
            )
        )

    def record_attempt(self, provider: str) -> None:
        """Record that ``provider`` is being tried, resolving pending hops.

        Every hop still waiting for a successor (``to_provider is None``)
        gets ``provider`` filled in. Consecutive skips therefore all point
        at the same provider that finally ran, which is exactly the
        narrative an operator wants: "budget blocked local and health
        blocked ollama, so openrouter served it".
        """
        self.attempts.append(provider)
        self.resolve_pending(provider)

    def resolve_pending(self, provider: str) -> None:
        """Point every still-unresolved hop at ``provider``.

        Split out of :meth:`record_attempt` because the *streaming* ingress
        needs the answer earlier than the first attempt: an SSE response
        commits its HTTP headers before the engine's generator runs a
        single line, so ``X-CodeRouter-Fallback-To`` would always be
        missing if only ``record_attempt`` could fill it. The engine calls
        this from ``apply_context_budget`` — the one place where the chain
        is fully ordered (adaptive reorder, drift demotion and capability
        bucketing have all been applied) and its head is therefore
        genuinely the provider that will be tried first.

        Idempotent and monotonic: only hops holding ``None`` are touched,
        so a later ``record_attempt`` can never rewrite an answer this
        already gave, and the two mechanisms compose instead of fighting.
        """
        for index, hop in enumerate(self.hops):
            if hop.to_provider is None:
                self.hops[index] = FallbackHop(
                    from_provider=hop.from_provider,
                    reason=hop.reason,
                    to_provider=provider,
                    detail=hop.detail,
                    stream=hop.stream,
                    pre_attempt=hop.pre_attempt,
                )

    # -- reading -----------------------------------------------------------

    @property
    def occurred(self) -> bool:
        """True iff at least one fallback hop was recorded."""
        return bool(self.hops)

    @property
    def chain(self) -> list[str]:
        """Providers in try-order, including the one that finally served."""
        if not self.hops:
            return list(self.attempts)
        names = [hop.from_provider for hop in self.hops]
        final = self.hops[-1].to_provider
        if final is not None:
            names.append(final)
        return names

    @property
    def reasons(self) -> list[str]:
        """Departure reason for each hop, in order."""
        return [hop.reason for hop in self.hops]

    @property
    def from_provider(self) -> str | None:
        """The first provider that was abandoned."""
        return self.hops[0].from_provider if self.hops else None

    @property
    def to_provider(self) -> str | None:
        """The provider that ultimately served, or ``None`` if none did."""
        return self.hops[-1].to_provider if self.hops else None

    def header_values(self) -> dict[str, str]:
        """Build the ``X-CodeRouter-Fallback-*`` response headers.

        Returns an empty dict when no fallback occurred, so the caller can
        merge unconditionally and the zero-fallback response keeps exactly
        the headers it had before v2.15.0.
        """
        if not self.hops:
            return {}
        headers: dict[str, str] = {
            HEADER_FALLBACK_FROM: _sanitize(self.hops[0].from_provider),
            HEADER_FALLBACK_REASON: ",".join(_sanitize(r) for r in self.reasons),
            HEADER_FALLBACK_CHAIN: ">".join(_sanitize(p) for p in self.chain),
        }
        final = self.hops[-1].to_provider
        if final is not None:
            headers[HEADER_FALLBACK_TO] = _sanitize(final)
        return headers

    def as_event_payload(self) -> dict[str, Any]:
        """Build the ``coderouter_fallback`` SSE metadata event body."""
        return {
            "type": SSE_FALLBACK_EVENT,
            "from": self.from_provider,
            "to": self.to_provider,
            "reason": self.reasons,
            "chain": self.chain,
            "hops": [hop.as_dict() for hop in self.hops],
        }


# ---------------------------------------------------------------------------
# Request-scoped storage
# ---------------------------------------------------------------------------

_fallback_trace_ctx: ContextVar[FallbackTrace | None] = ContextVar(
    "coderouter_fallback_trace", default=None
)


def begin_fallback_trace(
    *, keep_existing: bool = False, profile: str | None = None
) -> FallbackTrace:
    """Install (or reuse) the trace for the request being dispatched.

    ``keep_existing=True`` is used by the engine entry points when the M11
    prepared dispatch was consumed: that proves the ingress already called
    ``apply_context_budget`` for *this* request, so the trace it started
    (holding the chain-resolve skips) must be kept rather than replaced.

    In every other case a fresh trace is installed, which is what makes
    successive direct engine calls in one context (unit tests, scripts)
    independent instead of accumulating hops forever.
    """
    existing = _fallback_trace_ctx.get()
    if keep_existing and existing is not None:
        if profile is not None:
            existing.profile = profile
        return existing
    trace = FallbackTrace(profile=profile)
    _fallback_trace_ctx.set(trace)
    return trace


def ensure_fallback_trace(*, profile: str | None = None) -> FallbackTrace:
    """Return the current trace, creating one only if none exists.

    Used by chain resolution, which runs both inside dispatch (a trace
    already exists) and from the ingress's ``apply_context_budget``
    pre-pass (it does not yet).
    """
    existing = _fallback_trace_ctx.get()
    if existing is not None:
        if profile is not None and existing.profile is None:
            existing.profile = profile
        return existing
    trace = FallbackTrace(profile=profile)
    _fallback_trace_ctx.set(trace)
    return trace


def current_fallback_trace() -> FallbackTrace | None:
    """Return the trace for the current request, or ``None``.

    Read-only accessor for the ingress. ``None`` means "nothing recorded"
    — either no fallback machinery ran (a stubbed engine in a test) or the
    engine never started a trace. Callers must treat it as "no fallback".
    """
    return _fallback_trace_ctx.get()


def reset_fallback_trace() -> None:
    """Clear the current trace. Test helper; not used in the request path."""
    _fallback_trace_ctx.set(None)
