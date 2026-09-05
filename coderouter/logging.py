"""Tiny structured-logging helper.

We don't pull in structlog/loguru — see plan.md §5.4. stdlib logging + a
custom formatter that emits JSON lines is enough for v0.1.

v0.5.1 additions
    ``CapabilityDegradedReason`` / ``CapabilityDegradedPayload`` /
    ``log_capability_degraded`` are the typed contract for the
    ``capability-degraded`` log line (v0.5 gate trio). They live here —
    rather than in ``coderouter/routing/capability.py`` where they fit
    semantically — because (a) importing anything from the ``routing``
    package eagerly triggers ``routing/__init__.py`` which pulls
    ``FallbackEngine`` and creates a cycle with adapter modules that
    want to emit the same log, and (b) logging.py is a dependency-free
    leaf, so it is the safest home for a cross-cutting log shape.
    ``capability.py`` re-exports all three for discoverability.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any, Literal, TypedDict

from coderouter.secret_redaction import install_secret_filter


class JsonLineFormatter(logging.Formatter):
    """Emit each record as a single JSON line."""

    def format(self, record: logging.LogRecord) -> str:
        """Render a LogRecord as a single-line JSON string.

        Standard ``logging`` attributes (levelname, funcName, lineno, …)
        are whitelisted out; everything attached via ``extra={...}`` is
        included, so structured calls like
        ``logger.info("evt", extra={"provider": "ollama"})`` surface
        ``"provider": "ollama"`` verbatim in the output line.
        """
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, datefmt="%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # Pick up custom attributes attached via `extra={...}`
        for key, value in record.__dict__.items():
            if key in {
                "args",
                "asctime",
                "created",
                "exc_info",
                "exc_text",
                "filename",
                "funcName",
                "levelname",
                "levelno",
                "lineno",
                "message",
                "module",
                "msecs",
                "msg",
                "name",
                "pathname",
                "process",
                "processName",
                "relativeCreated",
                "stack_info",
                "thread",
                "threadName",
                "taskName",
            }:
                continue
            payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


# Marker attribute stamped on the handler this module installs. Used by
# configure_logging to remove ONLY its own prior handler on reconfigure,
# rather than nuking every handler on the root logger. Removing all
# handlers was a M4 bug: a second create_app() in the same process would
# also detach the MetricsCollector / audit / request-log handlers that
# other subsystems had attached, silently killing metrics and the JSONL
# journals from the second app onward.
_CODEROUTER_LOG_HANDLER_MARKER: str = "_coderouter_json_line_handler"


def configure_logging(level: str = "INFO") -> None:
    """Install JSON-line logging on the root logger. Idempotent.

    Only the handler this function previously installed (identified by the
    :data:`_CODEROUTER_LOG_HANDLER_MARKER` attribute) is removed on
    reconfigure. Handlers attached by other subsystems — the
    :class:`~coderouter.metrics.collector.MetricsCollector`, the audit /
    request-log JSONL handlers — are left in place so a second
    ``create_app()`` in the same process does not silently detach them.
    """
    root = logging.getLogger()
    root.setLevel(level.upper())
    # Avoid duplicate handlers on reload — but only remove OUR own prior
    # handler, never handlers other subsystems attached (M4).
    for h in list(root.handlers):
        if getattr(h, _CODEROUTER_LOG_HANDLER_MARKER, False):
            root.removeHandler(h)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonLineFormatter())
    # v2.14.0: scrub registered credentials before the formatter runs. The
    # filter is attached per-handler (not to the root logger) because logger
    # filters do not apply to records propagated up from child loggers —
    # every emitting module in this codebase uses its own module logger.
    install_secret_filter(handler)
    setattr(handler, _CODEROUTER_LOG_HANDLER_MARKER, True)
    root.addHandler(handler)


def get_logger(name: str) -> logging.Logger:
    """Alias for :func:`logging.getLogger` — exists so modules can import
    from :mod:`coderouter.logging` without reaching into stdlib directly,
    keeping future logger customization (tags, adapters, …) to one line.
    """
    return logging.getLogger(name)


# ---------------------------------------------------------------------------
# v0.5.1: capability-degraded log shape
#
# Single chokepoint for the log line emitted by the v0.5 capability gates
# (thinking / cache_control / reasoning). See module docstring above for
# why this lives in logging.py rather than in capability.py.
# ---------------------------------------------------------------------------

CapabilityDegradedReason = Literal[
    "provider-does-not-support",
    "translation-lossy",
    "non-standard-field",
    "unsupported-backend",
]
"""Why a capability was degraded.

- ``provider-does-not-support``: the provider's wire format would 400 on
  the field. v0.5-A thinking gate; request-side strip happens before the
  call.
- ``translation-lossy``: the field has no equivalent in the target wire
  format so it is dropped during translation. v0.5-B cache_control;
  observability only — no strip happens inside the gate itself (the
  translation layer already drops the marker).
- ``non-standard-field``: upstream emits a field that is not in the spec
  the ingress speaks, so we strip it on the response-side boundary.
  v0.5-C reasoning field.
- ``unsupported-backend``: the request asks for a semantic the target
  backend does not honor (S2 forced ``tool_choice``); with action
  ``warn`` the request is sent unchanged, with ``emulate`` a separate
  ``tool-choice-emulated`` line records the substituted directive.
"""


class CapabilityDegradedPayload(TypedDict):
    """Structured shape of the ``capability-degraded`` log record.

    Fields
        provider: the ``name:`` of the ProviderConfig that degraded — so
            operators can correlate with the ``provider-failed`` /
            ``provider-ok`` lines sharing that key.
        dropped: list of capability names affected. Single-element today
            (``["thinking"]`` / ``["cache_control"]`` / ``["reasoning"]``)
            but typed as a list so a single call can report multiple
            simultaneous drops in the future without a schema break.
        reason: see ``CapabilityDegradedReason``.
    """

    provider: str
    dropped: list[str]
    reason: CapabilityDegradedReason


def log_capability_degraded(
    logger: logging.Logger,
    *,
    provider: str,
    dropped: list[str],
    reason: CapabilityDegradedReason,
) -> None:
    """Emit a ``capability-degraded`` log record with the unified shape.

    Single chokepoint for the log. Keyword-only args force callers through
    the TypedDict contract at the static-type level. The ``logger``
    argument is passed in so the record's ``logger`` name (captured by
    JsonLineFormatter) reflects the site of the degradation — request-side
    gates emit under ``coderouter.routing.fallback``, response-side under
    ``coderouter.adapters.openai_compat``. That distinction is useful
    when reading the log alongside the surrounding ``try-provider`` /
    ``provider-ok`` trail.
    """
    payload: CapabilityDegradedPayload = {
        "provider": provider,
        "dropped": dropped,
        "reason": reason,
    }
    logger.info("capability-degraded", extra=payload)


# ---------------------------------------------------------------------------
# v0.6-C: chain-paid-gate-blocked log shape
#
# Motivation (plan.md §9.3 #3, "宣言的 ALLOW_PAID gate"):
#   v0.1 already filters ``paid: true`` providers from the chain when
#   ``allow_paid=False`` (per-provider INFO ``skip-paid-provider``), but
#   when the gate ends up filtering the ENTIRE chain to empty, the
#   operator-visible symptom is a generic ``NoProvidersAvailableError``.
#   A dedicated aggregate warn makes the gate "declarative" in the same
#   sense as v0.5's capability gates: the rule is visible in one line.
#
# Scope:
#   - Fires once per request (the 4 engine entry points), only when the
#     chain resolves to ZERO adapters AND at least one provider was
#     filtered out by the paid gate. Mixed chains where at least one
#     free provider survives stay quiet — they proceed into the normal
#     try-provider / provider-failed trail.
#   - ``skip-paid-provider`` is still emitted per-provider at INFO so
#     per-provider traceability is intact. This warn sits at a coarser
#     granularity (one line per blocked chain).
# ---------------------------------------------------------------------------

_DEFAULT_PAID_GATE_HINT: str = (
    "set ALLOW_PAID=true, mark a provider paid=false, "
    "or add a free provider to this profile's chain"
)


class ChainPaidGateBlockedPayload(TypedDict):
    """Structured shape of the ``chain-paid-gate-blocked`` log record.

    Fields
        profile: the active profile name (resolved, not user-supplied —
            so after falling back to ``default_profile``).
        blocked_providers: names of providers on this chain that were
            ``paid: true`` and filtered out by the gate. Order matches
            their position in the chain (same as what the ``skip-paid-
            provider`` INFO lines report individually).
        hint: a one-line remediation suggestion — stable text so it can
            be grepped, overridable at the call site when context-
            specific advice is warranted.
    """

    profile: str
    blocked_providers: list[str]
    hint: str


def log_chain_paid_gate_blocked(
    logger: logging.Logger,
    *,
    profile: str,
    blocked_providers: list[str],
    hint: str = _DEFAULT_PAID_GATE_HINT,
) -> None:
    """Emit a ``chain-paid-gate-blocked`` warn with the unified shape.

    Single chokepoint mirroring :func:`log_capability_degraded`. Warn
    level (not info) because an empty chain is always a config problem
    the operator needs to see — whereas the per-provider
    ``skip-paid-provider`` can stay info (the chain as a whole may still
    be viable).
    """
    payload: ChainPaidGateBlockedPayload = {
        "profile": profile,
        "blocked_providers": blocked_providers,
        "hint": hint,
    }
    logger.warning(
        "chain-paid-gate-blocked",
        extra=payload,
    )


# ---------------------------------------------------------------------------
# v1.10: budget-gate log shapes (skip-budget-exceeded / chain-budget-exceeded)
#
# Mirrors the v0.6-C paid-gate pattern exactly: per-provider INFO event
# for each provider that the chain resolver skipped because its
# current-month running cost reached or exceeded the configured
# ``cost.monthly_budget_usd``, plus a chain-level WARN aggregate when
# the budget gate filters the entire chain to zero adapters.
#
# Scope:
#   - ``skip-budget-exceeded`` fires per-provider once per chain
#     resolution. Useful for the cost dashboard's "budget pressure"
#     panel + Prometheus counter mirror of the paid-gate counter.
#   - ``chain-budget-exceeded`` fires once per request, only when the
#     budget gate left ZERO providers usable. Warn-level because, like
#     the paid-gate, an empty chain is a configuration / spend
#     problem the operator must see.
# ---------------------------------------------------------------------------


_DEFAULT_BUDGET_GATE_HINT: str = (
    "raise ``cost.monthly_budget_usd`` for at least one provider, "
    "wait for the calendar-month rollover, or restart the process to "
    "zero the in-memory running totals"
)


class SkipBudgetExceededPayload(TypedDict):
    """Structured shape of the ``skip-budget-exceeded`` log record.

    Fields
        provider: name of the provider being skipped.
        profile: active profile resolving the chain (resolved name,
            not user-supplied — so after falling back to
            ``default_profile``).
        monthly_budget_usd: configured cap from
            :class:`coderouter.config.schemas.CostConfig`.
        current_total_usd: per-provider running USD total for the
            current calendar-month, observed at the time of the
            skip decision (rounded to 6 decimal places to match the
            cost dashboard's display precision).
        month: ``YYYY-MM`` UTC bucket the totals belong to. Useful
            for cross-referencing with the dashboard / external
            cost reports across month boundaries.
    """

    provider: str
    profile: str
    monthly_budget_usd: float
    current_total_usd: float
    month: str


class ChainBudgetExceededPayload(TypedDict):
    """Structured shape of the ``chain-budget-exceeded`` log record.

    Same layout as :class:`ChainPaidGateBlockedPayload` (profile +
    blocked list + hint) so dashboards / log aggregators can render
    both with the same template.
    """

    profile: str
    blocked_providers: list[str]
    month: str
    hint: str


def log_skip_budget_exceeded(
    logger: logging.Logger,
    *,
    provider: str,
    profile: str,
    monthly_budget_usd: float,
    current_total_usd: float,
    month: str,
) -> None:
    """Emit a ``skip-budget-exceeded`` info line for one filtered provider.

    Info level (not warn) — same severity as ``skip-paid-provider``.
    The chain may still be viable; the chain-level warn fires only
    when ALL providers are filtered out.
    """
    payload: SkipBudgetExceededPayload = {
        "provider": provider,
        "profile": profile,
        "monthly_budget_usd": monthly_budget_usd,
        "current_total_usd": round(current_total_usd, 6),
        "month": month,
    }
    logger.info("skip-budget-exceeded", extra=payload)


def log_chain_budget_exceeded(
    logger: logging.Logger,
    *,
    profile: str,
    blocked_providers: list[str],
    month: str,
    hint: str = _DEFAULT_BUDGET_GATE_HINT,
) -> None:
    """Emit a ``chain-budget-exceeded`` warn aggregate.

    Single chokepoint mirroring :func:`log_chain_paid_gate_blocked`.
    Warn level because an empty chain is always a config / spend
    problem the operator must see.
    """
    payload: ChainBudgetExceededPayload = {
        "profile": profile,
        "blocked_providers": blocked_providers,
        "month": month,
        "hint": hint,
    }
    logger.warning("chain-budget-exceeded", extra=payload)


# ---------------------------------------------------------------------------
# v1.9-E phase 2 (L2): memory-pressure log shapes
#
# Three event lanes mirror the paid-gate / budget-gate triplet:
#   * ``memory-pressure-detected``    — info: an OOM-coded failure was
#                                        observed; the provider has been
#                                        marked pressured (when action=skip)
#                                        or is logged-only (when action=warn).
#   * ``skip-memory-pressure``         — info: chain resolver filtered a
#                                        provider out because it's still in
#                                        cooldown.
#   * ``chain-memory-pressure-blocked``— warn: the L2 gate filtered every
#                                        provider out (chain empty). Operator
#                                        must see this — every backend in
#                                        this profile is OOM at once.
# ---------------------------------------------------------------------------


_DEFAULT_MEMORY_PRESSURE_HINT: str = (
    "shorten the prompt, switch to a smaller model, increase backend RAM/VRAM, "
    "or wait for the cooldown window to elapse"
)


class MemoryPressureDetectedPayload(TypedDict):
    """Structured shape of the ``memory-pressure-detected`` log record.

    Fields
        provider: the provider whose error body matched an OOM phrase.
        profile: active profile name (resolved, not user-supplied).
        action: configured ``memory_pressure_action`` for this profile
            (``warn`` or ``skip``). ``off`` never fires this event.
        cooldown_s: configured cooldown window in seconds. Echoed
            even when ``action=warn`` (no cooldown is applied) so
            downstream tooling can correlate with the action.
        error: short prefix of the upstream error message that
            triggered the detection (truncated to keep the log
            line bounded; full body lives in the prior
            ``provider-failed`` line).
    """

    provider: str
    profile: str
    action: str
    cooldown_s: int
    error: str


class SkipMemoryPressurePayload(TypedDict):
    """Structured shape of the ``skip-memory-pressure`` log record.

    Fields
        provider: the provider being skipped.
        profile: active profile resolving the chain.
        seconds_until_eligible: how many seconds remain in the
            cooldown window (rounded to int). 0 implies "this is
            the call that just released the entry" — which the
            tracker filters out, so the value is always >= 1 in
            practice.
    """

    provider: str
    profile: str
    seconds_until_eligible: int


class ChainMemoryPressureBlockedPayload(TypedDict):
    """Structured shape of the ``chain-memory-pressure-blocked`` log record.

    Same field set as :class:`ChainPaidGateBlockedPayload` /
    :class:`ChainBudgetExceededPayload` so dashboards / log aggregators
    can render all three with the same template.
    """

    profile: str
    blocked_providers: list[str]
    hint: str


def log_memory_pressure_detected(
    logger: logging.Logger,
    *,
    provider: str,
    profile: str,
    action: str,
    cooldown_s: int,
    error: str,
) -> None:
    """Emit a ``memory-pressure-detected`` info line.

    Info level (not warn) — the engine still has the chain to fall
    through to. The chain-level warn fires only when the gate
    filters every provider out.
    """
    payload: MemoryPressureDetectedPayload = {
        "provider": provider,
        "profile": profile,
        "action": action,
        "cooldown_s": cooldown_s,
        "error": error[:200],
    }
    logger.info("memory-pressure-detected", extra=payload)


def log_skip_memory_pressure(
    logger: logging.Logger,
    *,
    provider: str,
    profile: str,
    seconds_until_eligible: int,
) -> None:
    """Emit a ``skip-memory-pressure`` info line.

    Mirror of :func:`log_skip_budget_exceeded`. Info level — the
    chain may still be viable.
    """
    payload: SkipMemoryPressurePayload = {
        "provider": provider,
        "profile": profile,
        "seconds_until_eligible": seconds_until_eligible,
    }
    logger.info("skip-memory-pressure", extra=payload)


def log_chain_memory_pressure_blocked(
    logger: logging.Logger,
    *,
    profile: str,
    blocked_providers: list[str],
    hint: str = _DEFAULT_MEMORY_PRESSURE_HINT,
) -> None:
    """Emit a ``chain-memory-pressure-blocked`` warn aggregate.

    Mirrors :func:`log_chain_paid_gate_blocked` — warn level
    because every provider being OOM at once is a real operator-
    visible problem.
    """
    payload: ChainMemoryPressureBlockedPayload = {
        "profile": profile,
        "blocked_providers": blocked_providers,
        "hint": hint,
    }
    logger.warning("chain-memory-pressure-blocked", extra=payload)


# ---------------------------------------------------------------------------
# v1.9-E phase 2 (L5): backend-health log shapes
#
# Two event lanes:
#   * ``backend-health-changed`` — info: a provider's state machine
#                                  transitioned (HEALTHY ↔ DEGRADED ↔
#                                  UNHEALTHY). Operator-visible
#                                  diagnostic; quiet when the provider
#                                  is steadily HEALTHY.
#   * ``demote-unhealthy-provider``— info: chain resolver moved an
#                                    UNHEALTHY provider to the back of
#                                    the chain. Mirror of v1.9-C
#                                    adaptive's demotion log but
#                                    state-machine-driven, not
#                                    rolling-window-driven.
#
# No chain-level warn for L5 — even when every provider in the chain
# is UNHEALTHY, the resolver still attempts them all (best-effort);
# the existing ``chain-uniform-auth-failure`` and
# ``provider-failed`` log trails surface the cascade.
# ---------------------------------------------------------------------------


class BackendHealthChangedPayload(TypedDict):
    """Structured shape of the ``backend-health-changed`` log record.

    Fields
        provider: the provider whose state transitioned.
        profile: active profile resolving the chain.
        old_state / new_state: the two endpoints of the transition.
            String-typed (not the ``HealthState`` Literal) so the
            log payload survives JSON round-trip without typing
            tooling complaining at the consumer site.
        consecutive_failures: counter snapshot at transition time.
            Useful for diagnosing "how many failures did it take to
            cross the threshold" without grepping the full
            ``provider-failed`` trail.
    """

    provider: str
    profile: str
    old_state: str
    new_state: str
    consecutive_failures: int


class DemoteUnhealthyProviderPayload(TypedDict):
    """Structured shape of the ``demote-unhealthy-provider`` log record."""

    provider: str
    profile: str


def log_backend_health_changed(
    logger: logging.Logger,
    *,
    provider: str,
    profile: str,
    old_state: str,
    new_state: str,
    consecutive_failures: int,
) -> None:
    """Emit a ``backend-health-changed`` info line on a state transition."""
    payload: BackendHealthChangedPayload = {
        "provider": provider,
        "profile": profile,
        "old_state": old_state,
        "new_state": new_state,
        "consecutive_failures": consecutive_failures,
    }
    logger.info("backend-health-changed", extra=payload)


def log_demote_unhealthy_provider(
    logger: logging.Logger,
    *,
    provider: str,
    profile: str,
) -> None:
    """Emit a ``demote-unhealthy-provider`` info line.

    Fires per chain-resolve when an UNHEALTHY provider is moved to
    the back. Quiet when no demotion happens (state-machine
    UNHEALTHY but action != demote → no log).
    """
    payload: DemoteUnhealthyProviderPayload = {
        "provider": provider,
        "profile": profile,
    }
    logger.info("demote-unhealthy-provider", extra=payload)


# ---------------------------------------------------------------------------
# v2.x: ``skip`` backend-health action log shapes
# ---------------------------------------------------------------------------


class SkipUnhealthyProviderPayload(TypedDict):
    """Structured shape of the ``skip-unhealthy-provider`` log record."""

    provider: str
    profile: str


class ChainAllUnhealthyLastResortPayload(TypedDict):
    """Structured shape of the ``chain-all-unhealthy-last-resort`` log record."""

    profile: str
    providers: list[str]


def log_skip_unhealthy_provider(
    logger: logging.Logger,
    *,
    provider: str,
    profile: str,
) -> None:
    """Emit a ``skip-unhealthy-provider`` info line.

    Fires per chain-resolve when the ``skip`` action filters an UNHEALTHY
    provider out of the chain (and the half-open trial is not currently
    allowed through). Quiet when a half-open trial lets the provider through
    — that resolve looks identical to a healthy one.
    """
    payload: SkipUnhealthyProviderPayload = {
        "provider": provider,
        "profile": profile,
    }
    logger.info("skip-unhealthy-provider", extra=payload)


def log_chain_all_unhealthy_last_resort(
    logger: logging.Logger,
    *,
    profile: str,
    providers: list[str],
) -> None:
    """Emit a ``chain-all-unhealthy-last-resort`` info line.

    Fires once per chain-resolve when the ``skip`` action would have filtered
    out every provider — rather than fail the request outright, the engine
    falls back to the unfiltered chain and attempts everyone. ``providers`` is
    the last-resort chain (original order) so the operator can see exactly what
    is being tried despite all members being UNHEALTHY.
    """
    payload: ChainAllUnhealthyLastResortPayload = {
        "profile": profile,
        "providers": providers,
    }
    logger.info("chain-all-unhealthy-last-resort", extra=payload)


# ---------------------------------------------------------------------------
# v2.0-J: self-healing log shapes
# ---------------------------------------------------------------------------


class SelfHealingExcludePayload(TypedDict):
    """Structured shape of the ``self-healing-exclude`` log record."""

    provider: str
    profile: str
    consecutive_failures: int


class SelfHealingRestorePayload(TypedDict):
    """Structured shape of the ``self-healing-restore`` log record."""

    provider: str
    profile: str
    excluded_duration_s: float


class SelfHealingRestartPayload(TypedDict):
    """Structured shape of the ``self-healing-restart`` log record."""

    provider: str
    command: str
    success: bool
    error: str | None


class SelfHealingRecoveryProbePayload(TypedDict):
    """Structured shape of the ``self-healing-recovery-probe`` log record."""

    provider: str
    success: bool
    next_interval_s: float
    latency_ms: float


def log_self_healing_exclude(
    logger: logging.Logger,
    *,
    provider: str,
    profile: str,
    consecutive_failures: int,
) -> None:
    """Emit when a provider is excluded from the chain by self-healing."""
    payload: SelfHealingExcludePayload = {
        "provider": provider,
        "profile": profile,
        "consecutive_failures": consecutive_failures,
    }
    logger.warning("self-healing-exclude", extra=payload)


def log_self_healing_restore(
    logger: logging.Logger,
    *,
    provider: str,
    profile: str,
    excluded_duration_s: float,
) -> None:
    """Emit when a previously excluded provider is restored to the chain."""
    payload: SelfHealingRestorePayload = {
        "provider": provider,
        "profile": profile,
        "excluded_duration_s": round(excluded_duration_s, 1),
    }
    logger.info("self-healing-restore", extra=payload)


def log_self_healing_restart(
    logger: logging.Logger,
    *,
    provider: str,
    command: str,
    success: bool,
    error: str | None = None,
) -> None:
    """Emit after attempting to restart a provider's backend process."""
    payload: SelfHealingRestartPayload = {
        "provider": provider,
        "command": command,
        "success": success,
        "error": error,
    }
    level = logging.INFO if success else logging.WARNING
    logger.log(level, "self-healing-restart", extra=payload)


def log_self_healing_recovery_probe(
    logger: logging.Logger,
    *,
    provider: str,
    success: bool,
    next_interval_s: float,
    latency_ms: float,
) -> None:
    """Emit after each recovery probe attempt for an excluded provider."""
    payload: SelfHealingRecoveryProbePayload = {
        "provider": provider,
        "success": success,
        "next_interval_s": round(next_interval_s, 1),
        "latency_ms": round(latency_ms, 1),
    }
    logger.info("self-healing-recovery-probe", extra=payload)


# ---------------------------------------------------------------------------
# v1.0-A: output-filter-applied log shape
#
# Motivation (plan.md §10.2 "出力クリーニング" / retrospective v0.7 "transformation
# には probe が伴う"):
#   ``output_filters`` is an operator opt-in (declared in providers.yaml)
#   rather than a passive / silent strip, so it does not fit the
#   ``capability-degraded`` vocabulary — nothing is "degraded" when a user
#   explicitly asked for scrubbing. A dedicated typed log line keeps the
#   observability surface legible (grep for ``output-filter-applied`` to
#   see exactly when a filter fired, for which provider, via which
#   filters).
#
# Scope:
#   - Fires ONCE per generate()/stream() call (log-once, mirroring the
#     v0.5-C reasoning-strip dedupe).
#   - Only fires when at least one filter actually modified the stream.
#     A chain configured but never triggered stays quiet.
# ---------------------------------------------------------------------------


class OutputFilterAppliedPayload(TypedDict):
    """Structured shape of the ``output-filter-applied`` log record.

    Fields
        provider: the ``name:`` of the ProviderConfig whose adapter ran
            the chain — correlates with surrounding ``provider-ok`` /
            ``provider-failed`` log lines.
        filters: names of filters that actually modified the stream
            (subset of the configured chain, preserving declaration
            order). Single-entry today when only ``strip_thinking``
            triggers, multi-entry once an operator enables two+.
        streaming: True if emitted from the streaming path, False from
            non-streaming. Lets a log-reading operator distinguish
            "filter fired mid-stream" from "filter fired on the final
            body" without cross-referencing the surrounding request
            metadata.
    """

    provider: str
    filters: list[str]
    streaming: bool


def log_output_filter_applied(
    logger: logging.Logger,
    *,
    provider: str,
    filters: list[str],
    streaming: bool,
) -> None:
    """Emit an ``output-filter-applied`` info record.

    Single chokepoint mirroring :func:`log_capability_degraded`.
    Called at most once per request/stream — adapter threads a
    dedupe flag on the enclosing call. ``filters`` SHOULD be the subset
    that actually modified text (see ``OutputFilterChain.applied_filters``),
    not the declared chain — so a chain of ``[strip_thinking,
    strip_stop_markers]`` where only the first triggers logs
    ``filters=["strip_thinking"]``.
    """
    payload: OutputFilterAppliedPayload = {
        "provider": provider,
        "filters": filters,
        "streaming": streaming,
    }
    logger.info(
        "output-filter-applied",
        extra=payload,
    )


# ---------------------------------------------------------------------------
# v1.7-B: chain-claude-code-suitability-degraded log shape
#
# Motivation (plan.md §11.B.4 #2):
#   v1.6.2 documented in docs/guides/troubleshooting.md §4-1 that putting
#   Llama-3.3-70B at the head of a Claude-Code-facing chain causes
#   over-eager tool invocation (small talk like ``こんにちは`` getting
#   rewritten to ``Skill(hello)`` calls). Docs alone require the operator
#   to know to read troubleshooting.md; v1.7-B promotes that hint to a
#   structured, automatic startup WARN whenever a profile whose name
#   starts with ``claude-code`` contains a provider declared
#   ``claude_code_suitability: degraded`` in the capability registry.
#
# Scope:
#   - Fires ONCE per such profile at app startup (during the FastAPI
#     lifespan), before any request is served.
#   - Only fires for profiles whose name starts with ``claude-code``
#     (case-sensitive prefix). A user with a ``writing`` profile
#     containing Llama-3.3-70B stays quiet — the model is fine outside
#     the agentic harness.
#   - The operator can opt OUT by declaring
#     ``claude_code_suitability: ok`` for the matching glob in
#     ``~/.coderouter-t/model-capabilities.yaml`` (user rules win against
#     bundled rules in the registry's first-match-per-flag walk).
# ---------------------------------------------------------------------------


_DEFAULT_CLAUDE_CODE_SUITABILITY_HINT: str = (
    "move the degraded provider(s) to the tail of the chain or replace "
    "with an agentic-coding-tuned model (e.g. qwen3-coder-480b-a35b-instruct); "
    "see docs/guides/troubleshooting.md §4-1"
)


class ChainClaudeCodeSuitabilityDegradedPayload(TypedDict):
    """Structured shape of the ``chain-claude-code-suitability-degraded`` log.

    Fields
        profile: the profile name flagged. Always starts with
            ``claude-code`` (the gate's prefix filter).
        degraded_providers: provider ``name:`` values whose ``model:`` was
            looked up in the capability registry and resolved to
            ``claude_code_suitability == "degraded"``. Order matches their
            position in the chain.
        degraded_models: corresponding ``model:`` strings, parallel to
            ``degraded_providers`` (same length, same order). Carried
            separately so a log reader can grep for the model family
            without having to cross-reference providers.yaml.
        hint: a one-line remediation suggestion. Stable text so it can be
            grepped, overridable at the call site if context warrants.
    """

    profile: str
    degraded_providers: list[str]
    degraded_models: list[str]
    hint: str


def log_chain_claude_code_suitability_degraded(
    logger: logging.Logger,
    *,
    profile: str,
    degraded_providers: list[str],
    degraded_models: list[str],
    hint: str = _DEFAULT_CLAUDE_CODE_SUITABILITY_HINT,
) -> None:
    """Emit a ``chain-claude-code-suitability-degraded`` warn.

    Single chokepoint mirroring :func:`log_chain_paid_gate_blocked`. Warn
    level (not info) because mis-routing small talk to a Skill() call is
    a user-visible behavior break that operators should see immediately
    — but it does not block startup (the chain still works, just sub-
    optimally for the agentic-coding harness).
    """
    payload: ChainClaudeCodeSuitabilityDegradedPayload = {
        "profile": profile,
        "degraded_providers": degraded_providers,
        "degraded_models": degraded_models,
        "hint": hint,
    }
    logger.warning(
        "chain-claude-code-suitability-degraded",
        extra=payload,
    )


# ---------------------------------------------------------------------------
# v1.9-A: cache-observed log shape (Cache Observability)
#
# Motivation (docs/inside/future.md §5.1):
#   Anthropic prompt caching is the single biggest cost / latency lever a
#   Claude Code user has, but until v1.9-A there is no record kept of how
#   often it actually fires through CodeRouter. The MetricsCollector has
#   ``provider-ok`` events but nothing in them carries token-level
#   cache accounting. v1.9-A adds a structured ``cache-observed`` log
#   line emitted from the engine's success path so every successful
#   ``provider-ok`` is paired with a cache observation when the response
#   carried any token usage data.
#
# Why a separate event (not piggyback on ``provider-ok``)
#   1. ``provider-ok`` already has stable typed extras consumed by the
#      v1.5-A MetricsCollector dispatch table; bolting cache fields onto
#      it would force every downstream consumer (collector, JSONL mirror,
#      tests) to re-validate the new shape.
#   2. cache observation does not always fire (streaming path defers
#      usage to ``message_delta`` events; openai_compat upstreams don't
#      return cache fields). A dedicated event lets us model "cache info
#      missing" as ``outcome=unknown`` without polluting ``provider-ok``.
#   3. Forward compat: a future ``cache-observed`` from a doctor cache
#      probe (planned for v1.9-B) reuses the same shape, no schema split.
#
# Four-class outcome (vs LiteLLM's three-class)
#   future.md §3 documents the LiteLLM ``cache_creation_input_tokens``
#   undercounting bug — they bucket "cache miss" and "no cache_control"
#   together, which makes "is my cache_control even being sent?" hard to
#   answer from their dashboard. CodeRouter splits these from day one:
#       cache_hit       cache_read_input_tokens > 0
#       cache_creation  cache_creation_input_tokens > 0 (and not hit)
#       no_cache        usage present, both cache fields 0/missing
#       unknown         response carried no usage at all (streaming /
#                       openai_compat / pre-v1.9-A upstreams)
#   "no_cache" further splits at the call site into "request lacked
#   cache_control" vs "request had cache_control but provider stripped
#   it" — but the latter is already captured by the existing
#   ``capability-degraded`` (reason=translation-lossy) event, so this
#   event keeps the bucket flat.
# ---------------------------------------------------------------------------


CacheOutcome = Literal["cache_hit", "cache_creation", "no_cache", "unknown"]
"""Four-class cache observation outcome.

- ``cache_hit``: ``cache_read_input_tokens > 0`` — a previously-cached
  prefix was reused. The savings figure is ``cache_read_input_tokens``
  (charged at ~10% of normal input rate).
- ``cache_creation``: ``cache_creation_input_tokens > 0`` and not a hit.
  The first call after a cache_control marker plants the cache; this
  call paid the writeback cost (charged at ~125% of normal input).
- ``no_cache``: usage was returned but both cache fields were 0 or
  absent. Either the request had no ``cache_control`` markers, or the
  marker was sent but the upstream did not honor it (the latter case
  already triggers ``capability-degraded`` so it is not double-counted
  here).
- ``unknown``: the response carried no usage block at all. Common for
  streaming responses (usage arrives via ``message_delta`` events the
  v1.9-A path does not aggregate yet) and for ``openai_compat``
  upstreams converted via ``to_anthropic_response`` (the OpenAI
  Chat Completions wire has no cache fields).
"""


class CacheObservedPayload(TypedDict):
    """Structured shape of the ``cache-observed`` log record.

    Fields
        provider: the ``name:`` of the ProviderConfig that handled the
            request — same key used by ``provider-ok`` so log lines join
            cleanly on it.
        request_had_cache_control: True iff the inbound Anthropic request
            carried any ``cache_control`` marker (system / tools /
            messages). Lets dashboards distinguish "client didn't ask
            for caching" from "client asked but nothing was cached".
        outcome: one of :data:`CacheOutcome`. See module-level comment for
            the four-class rationale.
        cache_read_input_tokens: as reported by the upstream usage block
            (Anthropic native or LM Studio /v1/messages). 0 when missing.
        cache_creation_input_tokens: as reported by the upstream usage
            block. 0 when missing.
        input_tokens: total prompt tokens charged at normal rate (cache
            tokens are reported separately by the spec).
        output_tokens: completion tokens.
        streaming: whether the response was returned via the streaming
            path. Streaming aggregation lands in v1.9-B; for v1.9-A,
            ``streaming=True`` always pairs with ``outcome=unknown``.
        cost_usd: v1.9-D — total USD cost charged for this attempt
            (sum of all four token buckets at their respective rates,
            using the provider's :class:`coderouter.config.schemas.CostConfig`).
            ``0.0`` when the provider has no cost configured (typical
            for local models).
        cost_savings_usd: v1.9-D — counterfactual "no-cache" delta:
            what the operator *would have* paid for ``cache_read_input_tokens``
            at full input rate, minus what they actually paid at the
            discounted rate. ``0.0`` when there were no cache reads
            or no cost is configured. Always >= 0.
    """

    provider: str
    request_had_cache_control: bool
    outcome: CacheOutcome
    cache_read_input_tokens: int
    cache_creation_input_tokens: int
    input_tokens: int
    output_tokens: int
    streaming: bool
    cost_usd: float
    cost_savings_usd: float
    # v2.6 language-tax track (optional; default 0.0 / 1.0 at the emit
    # site keeps pre-v2.6 callers and log consumers working unchanged).
    language_tax_usd: float
    language_tax_multiplier: float


def log_cache_observed(
    logger: logging.Logger,
    *,
    provider: str,
    request_had_cache_control: bool,
    outcome: CacheOutcome,
    cache_read_input_tokens: int,
    cache_creation_input_tokens: int,
    input_tokens: int,
    output_tokens: int,
    streaming: bool,
    cost_usd: float = 0.0,
    cost_savings_usd: float = 0.0,
    language_tax_usd: float = 0.0,
    language_tax_multiplier: float = 1.0,
) -> None:
    """Emit a ``cache-observed`` info record with the unified shape.

    Single chokepoint mirroring :func:`log_capability_degraded`. Info
    level (not warn) — a cache miss is rarely an error; cache_read of 0
    on the first call of a new conversation is the steady-state.
    Operators surface anomalies via the dashboard's per-provider hit-
    rate panel rather than per-line warnings.

    Caller responsibility: derive ``outcome`` from the token fields.
    Helper :func:`classify_cache_outcome` exists for callers that just
    have the raw usage dict. v1.9-D adds optional ``cost_usd`` /
    ``cost_savings_usd`` parameters that default to 0.0 — pre-v1.9-D
    callers continue to work and simply don't contribute to the cost
    aggregator.
    """
    payload: CacheObservedPayload = {
        "provider": provider,
        "request_had_cache_control": request_had_cache_control,
        "outcome": outcome,
        "cache_read_input_tokens": cache_read_input_tokens,
        "cache_creation_input_tokens": cache_creation_input_tokens,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "streaming": streaming,
        "cost_usd": cost_usd,
        "cost_savings_usd": cost_savings_usd,
        "language_tax_usd": language_tax_usd,
        "language_tax_multiplier": language_tax_multiplier,
    }
    logger.info("cache-observed", extra=payload)


# ---------------------------------------------------------------------------
# v1.9-E (L3): tool-loop-detected log shape (Long-run Guards / loop detection)
#
# Motivation (docs/inside/future.md §5.3.2):
#   Long-running agent sessions can fall into "stuck loops" where the
#   assistant repeatedly calls the same tool with identical args
#   because it can't make progress. The guard inspects the assistant
#   tool_use history in the inbound request and, when the same call
#   repeats above the configured threshold, emits this log line.
#
#   The log line is the ``warn`` action's only effect. The ``inject``
#   and ``break`` actions both fire this log too (so dashboards can
#   surface every detection regardless of the action), but they also
#   modify the outbound request / response.
#
# Why warn-level
#   Mid-session loop detection is operationally significant — it
#   indicates the agent ran out of progress signal — but rarely a
#   crisis (the agent often unsticks itself on the next turn). Warn
#   matches the existing severity of ``chain-paid-gate-blocked`` and
#   ``chain-claude-code-suitability-degraded``: visible in the default
#   log level, but not error-level.
# ---------------------------------------------------------------------------


class ToolLoopDetectedPayload(TypedDict):
    """Structured shape of the ``tool-loop-detected`` log record.

    Fields
        profile: the profile name in effect for this request. Lets
            dashboards filter loop detections per-profile.
        tool_name: the tool that was called repeatedly.
        repeat_count: how many consecutive identical calls were
            observed in the assistant's recent history. Always >=
            ``threshold`` (the threshold is the trigger condition).
        threshold: the configured ``tool_loop_threshold`` for this
            profile — surfaced in the log so operators can correlate
            against their config without grep'ing providers.yaml.
        window: the configured ``tool_loop_window`` (how many recent
            tool_use blocks were inspected).
        action: the configured ``tool_loop_action`` (``warn`` /
            ``inject`` / ``break``). Lets a single log query
            distinguish "fired but only logged" from "fired and broke
            the request".
    """

    profile: str
    tool_name: str
    repeat_count: int
    threshold: int
    window: int
    action: Literal["warn", "inject", "break"]


def log_tool_loop_detected(
    logger: logging.Logger,
    *,
    profile: str,
    tool_name: str,
    repeat_count: int,
    threshold: int,
    window: int,
    action: Literal["warn", "inject", "break"],
) -> None:
    """Emit a ``tool-loop-detected`` warn record with the unified shape.

    Single chokepoint mirroring :func:`log_capability_degraded`. Warn
    level — see module-level rationale.
    """
    payload: ToolLoopDetectedPayload = {
        "profile": profile,
        "tool_name": tool_name,
        "repeat_count": repeat_count,
        "threshold": threshold,
        "window": window,
        "action": action,
    }
    logger.warning("tool-loop-detected", extra=payload)


def classify_cache_outcome(
    *,
    usage_present: bool,
    cache_read_input_tokens: int,
    cache_creation_input_tokens: int,
) -> CacheOutcome:
    """Bucket a usage block into one of the four :data:`CacheOutcome` values.

    Order matters: a response carrying both a read and a creation count
    (rare but possible when a cached prefix is extended with a fresh
    cache_control marker on the same call) is classified as
    ``cache_hit`` because the read indicates the primary cache mechanism
    fired. The creation count still rolls up into the per-provider
    ``cache_creation_total`` counter via the collector, so no token is
    lost from the accounting.
    """
    if not usage_present:
        return "unknown"
    if cache_read_input_tokens > 0:
        return "cache_hit"
    if cache_creation_input_tokens > 0:
        return "cache_creation"
    return "no_cache"


# ---------------------------------------------------------------------------
# v2.0-F (L1): context budget guard logging
# ---------------------------------------------------------------------------


class ContextBudgetWarningPayload(TypedDict):
    """Structured shape of the ``context-budget-warning`` log record."""

    provider: str
    profile: str
    estimated_tokens: int
    max_context_tokens: int
    usage_ratio: float
    action: str


def log_context_budget_warning(
    logger: logging.Logger,
    *,
    provider: str,
    profile: str,
    estimated_tokens: int,
    max_context_tokens: int,
    usage_ratio: float,
    action: str,
) -> None:
    """Emit a ``context-budget-warning`` warning line.

    Warning level because this indicates the request is approaching a
    failure boundary. Unlike memory-pressure (where the chain can fall
    through), context overflow affects the entire request regardless of
    which provider serves it.
    """
    payload: ContextBudgetWarningPayload = {
        "provider": provider,
        "profile": profile,
        "estimated_tokens": estimated_tokens,
        "max_context_tokens": max_context_tokens,
        "usage_ratio": round(usage_ratio, 3),
        "action": action,
    }
    logger.warning("context-budget-warning", extra=payload)


class ContextBudgetTrimmedPayload(TypedDict):
    """Structured shape of the ``context-budget-trimmed`` log record."""

    provider: str
    profile: str
    messages_removed: int
    messages_before: int
    messages_after: int
    estimated_tokens_before: int
    estimated_tokens_after: int
    max_context_tokens: int


def log_context_budget_trimmed(
    logger: logging.Logger,
    *,
    provider: str,
    profile: str,
    messages_removed: int,
    messages_before: int,
    messages_after: int,
    estimated_tokens_before: int,
    estimated_tokens_after: int,
    max_context_tokens: int,
) -> None:
    """Emit a ``context-budget-trimmed`` info line.

    Info level (not warn) because the trim action successfully
    resolved the budget pressure — the request will proceed with
    reduced context. The warning was already emitted by
    :func:`log_context_budget_warning`.
    """
    payload: ContextBudgetTrimmedPayload = {
        "provider": provider,
        "profile": profile,
        "messages_removed": messages_removed,
        "messages_before": messages_before,
        "messages_after": messages_after,
        "estimated_tokens_before": estimated_tokens_before,
        "estimated_tokens_after": estimated_tokens_after,
        "max_context_tokens": max_context_tokens,
    }
    logger.info("context-budget-trimmed", extra=payload)


# ---------------------------------------------------------------------------
# v2.0-G (L4): Drift detection logging
# ---------------------------------------------------------------------------


def log_drift_detected(
    logger: logging.Logger,
    *,
    provider: str,
    profile: str,
    severity: str,
    reason: str,
    action: str,
    signals: dict[str, float],
) -> None:
    """Emit a ``drift-detected`` warning line.

    Fired when the drift detector finds quality degradation in the
    provider's rolling response window.
    """
    logger.warning(
        "drift-detected",
        extra={
            "provider": provider,
            "profile": profile,
            "severity": severity,
            "reason": reason,
            "action": action,
            "signals": signals,
        },
    )


def log_drift_promoted(
    logger: logging.Logger,
    *,
    provider: str,
    profile: str,
    demoted_to_rank: int,
    cooldown_s: int,
) -> None:
    """Emit a ``drift-promoted`` info line.

    Fired when a drifted provider is demoted in the chain and a
    different provider takes over as primary.
    """
    logger.info(
        "drift-promoted",
        extra={
            "provider": provider,
            "profile": profile,
            "demoted_to_rank": demoted_to_rank,
            "cooldown_s": cooldown_s,
        },
    )


def log_drift_reload_attempted(
    logger: logging.Logger,
    *,
    provider: str,
    success: bool,
) -> None:
    """Emit a ``drift-reload-attempted`` info line.

    Fired after attempting an Ollama KV cache flush (keep_alive=0).
    """
    logger.info(
        "drift-reload-attempted",
        extra={
            "provider": provider,
            "success": success,
        },
    )


def log_drift_recovered(
    logger: logging.Logger,
    *,
    provider: str,
    profile: str,
    after_s: float,
) -> None:
    """Emit a ``drift-recovered`` info line.

    Fired when a previously-drifted provider's cooldown expires and its
    rank is restored.
    """
    logger.info(
        "drift-recovered",
        extra={
            "provider": provider,
            "profile": profile,
            "after_s": round(after_s, 1),
        },
    )


# ---------------------------------------------------------------------------
# v2.0-H (L6): Partial stitch surfaced logging
# ---------------------------------------------------------------------------


def log_partial_stitch_surfaced(
    logger: logging.Logger,
    *,
    provider: str,
    profile: str,
    text_blocks: int,
    text_length: int,
) -> None:
    """Emit a ``partial-stitch-surfaced`` info line.

    Fired when a mid-stream failure is gracefully terminated with partial
    content delivered to the client (partial_stitch_action=surface).
    """
    logger.info(
        "partial-stitch-surfaced",
        extra={
            "provider": provider,
            "profile": profile,
            "text_blocks": text_blocks,
            "text_length": text_length,
        },
    )


# ---------------------------------------------------------------------------
# ⑧ (empty-response): per-request empty-response fallback log shape
#
# Fired when a provider returns a 200 with content-empty output (no
# tool_use and no non-whitespace text). The single event lane carries the
# ``action`` (warn|fallback) that triggered it, whether the empty response
# came from the streaming or non-streaming path, and ``chain_exhausted``
# — True only when every provider in the chain returned empty and the last
# empty response is being returned to the client as-is.
# ---------------------------------------------------------------------------


def log_empty_response_detected(
    logger: logging.Logger,
    provider: str,
    *,
    action: str,
    stream: bool,
    chain_exhausted: bool = False,
) -> None:
    """Emit an ``empty-response-detected`` warn line.

    Fired when ``empty_response_action`` is ``warn`` or ``fallback`` and a
    provider returns a structurally-valid but content-empty response. The
    ``action`` field records which knob value produced the line; ``stream``
    distinguishes the SSE path from the non-streaming path; ``chain_exhausted``
    is True only when the whole chain returned empty and the last empty
    response is being returned unchanged.
    """
    logger.warning(
        "empty-response-detected",
        extra={
            "provider": provider,
            "action": action,
            "stream": stream,
            "chain_exhausted": chain_exhausted,
        },
    )


# ---------------------------------------------------------------------------
# v2.15.0 (stream-truncation): upstream SSE ended without a terminator
#
# Fired from the adapter's SSE parse loop when ``stream_truncation_action``
# is ``warn`` or ``error`` and the upstream line iterator was exhausted
# without a ``message_stop`` (Anthropic wire) / ``[DONE]`` or a
# ``finish_reason`` (OpenAI wire).
#
# ``action`` records which knob value produced the line, so a dashboard can
# separate "we only measured it" (``warn``) from "we acted on it" (``error``).
# ``events_forwarded`` is how many events/chunks the adapter had already
# yielded — 0 means nothing reached the engine and a clean fallback is still
# possible. ``tool_call_in_flight`` is the sharp edge: True means a
# ``tool_use`` / ``tool_calls`` block was still open, so whatever the
# translation layer synthesizes closes a block whose argument JSON is
# provably incomplete.
# ---------------------------------------------------------------------------


def log_stream_truncation_detected(
    logger: logging.Logger,
    provider: str,
    *,
    action: str,
    wire: str,
    events_forwarded: int,
    saw_stream_start: bool,
    tool_call_in_flight: bool = False,
) -> None:
    """Emit a ``stream-truncation-detected`` warn line.

    Args:
        logger: Target logger.
        provider: ``ProviderConfig.name`` whose stream was cut.
        action: The ``stream_truncation_action`` value in effect (``warn``
            or ``error`` — ``off`` never reaches here).
        wire: ``anthropic`` or ``openai`` — which SSE dialect was being
            parsed, i.e. which terminator went missing.
        events_forwarded: Count of events/chunks already yielded downstream.
        saw_stream_start: Whether the opening event (``message_start`` /
            first data chunk) had arrived at all. False means the upstream
            produced nothing before closing.
        tool_call_in_flight: Whether a tool-call block was still open.
    """
    logger.warning(
        "stream-truncation-detected",
        extra={
            "provider": provider,
            "action": action,
            "wire": wire,
            "events_forwarded": events_forwarded,
            "saw_stream_start": saw_stream_start,
            "tool_call_in_flight": tool_call_in_flight,
        },
    )


# ---------------------------------------------------------------------------
# v2.0-I: Continuous probe log shapes
#
# Two event lanes mirror the backend-health triplet:
#   * ``probe-completed``       — info: a single provider probe finished
#                                  (success or failure). Quiet in normal
#                                  operation; operators grep for these to
#                                  diagnose individual backend issues.
#   * ``probe-round-completed`` — info: one full sweep across all probed
#                                  providers finished. Summary counter for
#                                  dashboards to render "probes/min" and
#                                  "failure ratio per round".
# ---------------------------------------------------------------------------


class ProbeCompletedPayload(TypedDict):
    """Structured shape of the ``probe-completed`` log record.

    Fields
        provider: the provider that was probed.
        success: whether the 1-token probe request succeeded.
        latency_ms: round-trip time of the probe in milliseconds.
        error: short error message (truncated) when success=False;
            None on success.
        model_name: model name extracted from the probe response,
            if available.
    """

    provider: str
    success: bool
    latency_ms: float
    error: str | None
    model_name: str | None


class ProbeRoundCompletedPayload(TypedDict):
    """Structured shape of the ``probe-round-completed`` log record.

    Fields
        providers_probed: number of providers probed in this round.
        failures: number of probe failures in this round.
    """

    providers_probed: int
    failures: int


def log_probe_completed(
    logger: logging.Logger,
    *,
    provider: str,
    success: bool,
    latency_ms: float,
    error: str | None = None,
    model_name: str | None = None,
) -> None:
    """Emit a ``probe-completed`` info line for one provider probe.

    Single chokepoint mirroring :func:`log_capability_degraded`. Info
    level — individual probes are diagnostic noise at normal operation;
    operators filter on ``success=False`` for alerting.
    """
    payload: ProbeCompletedPayload = {
        "provider": provider,
        "success": success,
        "latency_ms": round(latency_ms, 1),
        "error": error,
        "model_name": model_name,
    }
    logger.info("probe-completed", extra=payload)


def log_probe_round_completed(
    logger: logging.Logger,
    *,
    providers_probed: int,
    failures: int,
) -> None:
    """Emit a ``probe-round-completed`` info line summarizing one sweep.

    Info level — fires once per probe interval (default 60s). Dashboards
    render this as "probes/min" and "failure ratio" without needing to
    aggregate the individual ``probe-completed`` lines.
    """
    payload: ProbeRoundCompletedPayload = {
        "providers_probed": providers_probed,
        "failures": failures,
    }
    logger.info("probe-round-completed", extra=payload)


class ProbeCapabilitiesDriftPayload(TypedDict):
    """Structured shape of the ``probe-capabilities-drift`` log record.

    Fields
        provider: the provider whose probe response model mismatched.
        configured_model: the model string in providers.yaml.
        observed_model: the model name returned by the probe response.
        in_registry: whether the observed model has an entry in the
            capability registry.
    """

    provider: str
    configured_model: str
    observed_model: str
    in_registry: bool


def log_probe_capabilities_drift(
    logger: logging.Logger,
    *,
    provider: str,
    configured_model: str,
    observed_model: str,
    in_registry: bool,
) -> None:
    """Emit a ``probe-capabilities-drift`` warning line.

    Fired when the continuous probe detects that the model name returned
    by the provider differs from the configured model. This can indicate
    a model swap (Ollama auto-updated), a misconfiguration, or a new
    model that the capability registry doesn't know about yet.

    Warn level — model drift can cause subtle behavior changes (e.g. a
    model that supports thinking being replaced by one that doesn't).
    """
    payload: ProbeCapabilitiesDriftPayload = {
        "provider": provider,
        "configured_model": configured_model,
        "observed_model": observed_model,
        "in_registry": in_registry,
    }
    logger.warning("probe-capabilities-drift", extra=payload)


# ---------------------------------------------------------------------------
# v2.15.0: fallback reason logging
#
# One ``fallback-occurred`` line per hop. Fires only when the chain
# actually moves — a request served by its first provider produces no
# line at all, so this adds zero volume to the healthy path.
#
# The vocabulary of ``reason`` is defined in
# :mod:`coderouter.routing.fallback_trace` (a single canonical set that
# also names the pre-existing ``provider-does-not-support`` /
# ``translation-lossy`` degrade reasons, which never appear here — a
# degrade keeps the request on the same provider).
#
# ``to_provider`` is ``None`` when the hop is the chain's last one and no
# successor was ever attempted (chain exhausted). The line is still
# emitted: "we left A because of R and had nowhere to go" is the single
# most diagnostic event in the whole engine.
# ---------------------------------------------------------------------------


class FallbackOccurredPayload(TypedDict):
    """Structured shape of the ``fallback-occurred`` log record.

    Fields
        provider: the provider being left. Duplicated from
            ``from_provider`` under the field name every other engine
            event uses, so existing per-provider log queries keep working
            without learning a new key.
        from_provider: the provider being left.
        to_provider: the provider taking over, or None if the chain was
            exhausted with no successor.
        reason: canonical fallback reason (see ``fallback_trace``).
        detail: short human-readable context (HTTP status, truncated
            error text), or None.
        profile: the resolved profile name, when known.
        stream: True when the hop happened on a streaming request.
        pre_attempt: True when the provider was filtered out during chain
            resolution (budget / health / paid gate) and therefore never
            actually called; False when it was called and failed.
        hop_index: 0-based position of this hop within the request.
    """

    provider: str
    from_provider: str
    to_provider: str | None
    reason: str
    detail: str | None
    profile: str | None
    stream: bool
    pre_attempt: bool
    hop_index: int


def log_fallback_occurred(
    logger: logging.Logger,
    *,
    from_provider: str,
    to_provider: str | None,
    reason: str,
    detail: str | None = None,
    profile: str | None = None,
    stream: bool = False,
    pre_attempt: bool = False,
    hop_index: int = 0,
) -> None:
    """Emit a ``fallback-occurred`` warning line for one chain transition.

    Warn level, matching ``provider-failed`` — a fallback is a
    degradation of the intended routing even when the request ultimately
    succeeds, and operators grep for it when a profile's primary provider
    stops earning its place at the front of the chain.
    """
    payload: FallbackOccurredPayload = {
        "provider": from_provider,
        "from_provider": from_provider,
        "to_provider": to_provider,
        "reason": reason,
        "detail": detail,
        "profile": profile,
        "stream": stream,
        "pre_attempt": pre_attempt,
        "hop_index": hop_index,
    }
    logger.warning("fallback-occurred", extra=payload)
