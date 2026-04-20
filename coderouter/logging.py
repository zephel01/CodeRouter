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


class JsonLineFormatter(logging.Formatter):
    """Emit each record as a single JSON line."""

    def format(self, record: logging.LogRecord) -> str:
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


def configure_logging(level: str = "INFO") -> None:
    """Install JSON-line logging on the root logger. Idempotent."""
    root = logging.getLogger()
    root.setLevel(level.upper())
    # Avoid duplicate handlers on reload
    for h in list(root.handlers):
        root.removeHandler(h)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonLineFormatter())
    root.addHandler(handler)


def get_logger(name: str) -> logging.Logger:
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
