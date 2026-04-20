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
                "args", "asctime", "created", "exc_info", "exc_text", "filename",
                "funcName", "levelname", "levelno", "lineno", "message", "module",
                "msecs", "msg", "name", "pathname", "process", "processName",
                "relativeCreated", "stack_info", "thread", "threadName", "taskName",
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
    logger.info("capability-degraded", extra=payload)  # type: ignore[arg-type]
