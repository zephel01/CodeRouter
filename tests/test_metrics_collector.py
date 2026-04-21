"""Unit tests for :class:`coderouter.metrics.MetricsCollector` (v1.5-A).

Drives the collector through the same log-event vocabulary the runtime
uses (``try-provider`` / ``provider-ok`` / ``provider-failed`` / ...) and
asserts that :meth:`MetricsCollector.snapshot` surfaces the expected
counter / last-error / ring-buffer shape. No FastAPI involved — the
endpoint integration is tested in ``test_metrics_endpoint.py``.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from coderouter.logging import (
    configure_logging,
    get_logger,
    log_capability_degraded,
    log_chain_paid_gate_blocked,
    log_output_filter_applied,
)
from coderouter.metrics import (
    MetricsCollector,
    get_collector,
    install_collector,
    uninstall_collector,
)


@pytest.fixture
def collector() -> Iterator[MetricsCollector]:
    """Fresh singleton per test.

    ``install_collector`` is idempotent, so we uninstall before + after to
    guarantee a clean slate (tests from earlier modules may have already
    attached a handler via ``create_app``). ``configure_logging`` runs
    too — production code calls it inside ``create_app`` before attaching
    the collector, so mirroring that order here keeps the root logger at
    INFO (otherwise stdlib defaults to WARNING and our info-level events
    get filtered before the handler sees them).
    """
    uninstall_collector()
    configure_logging()
    yield install_collector(ring_size=16)
    uninstall_collector()


def _fire(event: str, **extra: Any) -> None:
    """Emit a structured log line the collector will pick up."""
    get_logger("test.metrics").info(event, extra=extra)


def _fire_warn(event: str, **extra: Any) -> None:
    """Emit a warn-level structured log line."""
    get_logger("test.metrics").warning(event, extra=extra)


# ---------------------------------------------------------------------------
# Singleton semantics
# ---------------------------------------------------------------------------


def test_install_is_idempotent() -> None:
    uninstall_collector()
    first = install_collector()
    second = install_collector()
    assert first is second
    uninstall_collector()


def test_get_collector_auto_installs() -> None:
    uninstall_collector()
    c = get_collector()
    assert isinstance(c, MetricsCollector)
    uninstall_collector()


# ---------------------------------------------------------------------------
# Counter dispatch
# ---------------------------------------------------------------------------


def test_try_provider_bumps_requests_and_attempts(collector: MetricsCollector) -> None:
    _fire("try-provider", provider="local", stream=False)
    _fire("try-provider", provider="local", stream=False)
    _fire("try-provider", provider="free-cloud", stream=True)

    snap = collector.snapshot()
    assert snap["counters"]["requests_total"] == 3
    assert snap["counters"]["provider_attempts"] == {"local": 2, "free-cloud": 1}


def test_provider_ok_and_failed_split_outcomes(collector: MetricsCollector) -> None:
    _fire("try-provider", provider="local", stream=False)
    _fire("provider-ok", provider="local", stream=False)
    _fire("try-provider", provider="local", stream=False)
    _fire_warn(
        "provider-failed",
        provider="local",
        status=429,
        retryable=True,
        error="rate_limit",
    )

    snap = collector.snapshot()
    outcomes = snap["counters"]["provider_outcomes"]["local"]
    assert outcomes == {"ok": 1, "failed": 1}

    # last_error carries the truncated error + retryable flag
    last = snap["providers"][0]["last_error"]
    assert last["status"] == 429
    assert last["retryable"] is True
    assert last["error"] == "rate_limit"


def test_provider_failed_midstream_tracked_separately(collector: MetricsCollector) -> None:
    _fire_warn(
        "provider-failed-midstream",
        provider="local",
        status=500,
        retryable=False,
        error="connection reset",
    )
    outcomes = collector.snapshot()["counters"]["provider_outcomes"]["local"]
    assert outcomes == {"failed_midstream": 1}


def test_skip_paid_and_unknown_counters(collector: MetricsCollector) -> None:
    _fire("skip-paid-provider", profile="default", provider="paid-cloud")
    _fire("skip-paid-provider", profile="default", provider="paid-cloud")
    _fire_warn("skip-unknown-provider", profile="default", provider="typo")

    snap = collector.snapshot()
    assert snap["counters"]["provider_skipped_paid"] == {"paid-cloud": 2}
    assert snap["counters"]["provider_skipped_unknown"] == {"typo": 1}


def test_capability_degraded_uses_typed_helper(collector: MetricsCollector) -> None:
    logger = get_logger("test.metrics")
    log_capability_degraded(
        logger,
        provider="local",
        dropped=["thinking"],
        reason="provider-does-not-support",
    )
    log_capability_degraded(
        logger,
        provider="local",
        dropped=["thinking", "reasoning"],
        reason="translation-lossy",
    )
    # counts are per-capability — the two calls collectively dropped
    # thinking twice and reasoning once.
    degraded = collector.snapshot()["counters"]["capability_degraded"]
    assert degraded == {"thinking": 2, "reasoning": 1}


def test_output_filter_applied_uses_typed_helper(collector: MetricsCollector) -> None:
    logger = get_logger("test.metrics")
    log_output_filter_applied(
        logger, provider="local", filters=["strip_thinking"], streaming=False
    )
    log_output_filter_applied(
        logger,
        provider="local",
        filters=["strip_thinking", "strip_stop_markers"],
        streaming=True,
    )
    applied = collector.snapshot()["counters"]["output_filter_applied"]
    assert applied == {"strip_thinking": 2, "strip_stop_markers": 1}


def test_chain_paid_gate_blocked_counter(collector: MetricsCollector) -> None:
    log_chain_paid_gate_blocked(
        get_logger("test.metrics"),
        profile="default",
        blocked_providers=["paid-cloud"],
    )
    assert collector.snapshot()["counters"]["chain_paid_gate_blocked_total"] == 1


def test_chain_uniform_auth_failure_counter(collector: MetricsCollector) -> None:
    _fire_warn(
        "chain-uniform-auth-failure",
        profile="default",
        status=401,
        count=2,
        providers=["a", "b"],
        hint="probable-misconfig",
    )
    assert collector.snapshot()["counters"]["chain_uniform_auth_failure_total"] == 1


def test_startup_snapshot_populated(collector: MetricsCollector) -> None:
    _fire(
        "coderouter-startup",
        version="1.0.1",
        providers=["local", "free-cloud"],
        profiles=["default"],
        default_profile="default",
        allow_paid=False,
        mode_source="config",
    )
    startup = collector.snapshot()["startup"]
    assert startup["version"] == "1.0.1"
    assert startup["providers"] == ["local", "free-cloud"]
    assert startup["default_profile"] == "default"
    assert startup["allow_paid"] is False


# ---------------------------------------------------------------------------
# Recent ring buffer
# ---------------------------------------------------------------------------


def test_recent_ring_captures_request_flow(collector: MetricsCollector) -> None:
    _fire("try-provider", provider="local", stream=False)
    _fire("provider-ok", provider="local", stream=False)
    _fire("try-provider", provider="free-cloud", stream=True)
    _fire_warn(
        "provider-failed",
        provider="free-cloud",
        status=429,
        retryable=True,
        error="rate_limit",
    )

    recent = collector.snapshot()["recent"]
    events = [entry["event"] for entry in recent]
    assert events == [
        "try-provider",
        "provider-ok",
        "try-provider",
        "provider-failed",
    ]
    # Failed entry carries status + retryable; skipped events (non-
    # try/ok/failed) don't populate the ring at all.
    assert recent[-1]["status"] == 429
    assert recent[-1]["retryable"] is True


def test_recent_ring_bounded(collector: MetricsCollector) -> None:
    # Fixture installed with ring_size=16; fire 20 events and expect the
    # oldest 4 to roll off.
    for i in range(20):
        _fire("try-provider", provider=f"p{i}", stream=False)
    recent = collector.snapshot()["recent"]
    assert len(recent) == 16
    assert recent[0]["provider"] == "p4"
    assert recent[-1]["provider"] == "p19"


# ---------------------------------------------------------------------------
# Forward-compat + isolation
# ---------------------------------------------------------------------------


def test_unknown_event_is_ignored(collector: MetricsCollector) -> None:
    _fire("totally-new-event-from-the-future", foo="bar")
    snap = collector.snapshot()
    assert snap["counters"]["requests_total"] == 0
    assert snap["recent"] == []


def test_non_string_msg_is_ignored(collector: MetricsCollector) -> None:
    # Some callers log integers / objects. LogRecord.getMessage() stringifies
    # them, but our dispatch keys off record.msg; anything non-str gets
    # skipped safely.
    get_logger("test.metrics").info(123)
    assert collector.snapshot()["counters"]["requests_total"] == 0


def test_reset_clears_state(collector: MetricsCollector) -> None:
    _fire("try-provider", provider="local", stream=False)
    assert collector.snapshot()["counters"]["requests_total"] == 1
    collector.reset()
    assert collector.snapshot()["counters"]["requests_total"] == 0
    assert collector.snapshot()["recent"] == []
