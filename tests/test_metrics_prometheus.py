"""Unit tests for :func:`coderouter.metrics.format_prometheus` (v1.5-B).

Pure-function tests over canned snapshot dicts — we don't need the
Collector here since the formatter is a pure ``dict → str``. An
end-to-end test (collector → formatter → ``/metrics`` endpoint) lives in
``test_metrics_endpoint.py``.
"""

from __future__ import annotations

from typing import Any

from coderouter.metrics import format_prometheus


def _empty_snapshot() -> dict[str, Any]:
    """Snapshot shape produced by a fresh MetricsCollector before any events."""
    return {
        "uptime_s": 0.0,
        "started_at": "1970-01-01T00:00:00",
        "startup": {},
        "counters": {
            "requests_total": 0,
            "chain_paid_gate_blocked_total": 0,
            "chain_uniform_auth_failure_total": 0,
            "provider_attempts": {},
            "provider_outcomes": {},
            "provider_skipped_paid": {},
            "provider_skipped_unknown": {},
            "capability_degraded": {},
            "output_filter_applied": {},
        },
        "providers": [],
        "recent": [],
    }


# ---------------------------------------------------------------------------
# Shape / envelope
# ---------------------------------------------------------------------------


def test_empty_snapshot_emits_help_and_type_for_every_metric() -> None:
    """Counters with zero samples still surface HELP / TYPE lines.

    Dashboards discover metrics from target metadata; emitting just
    HELP/TYPE for an empty counter lets them render "0" before the
    first event ever fires.
    """
    out = format_prometheus(_empty_snapshot())
    # Every metric name should appear at least in HELP + TYPE
    for metric in (
        "coderouter_uptime_seconds",
        "coderouter_requests_total",
        "coderouter_chain_paid_gate_blocked_total",
        "coderouter_chain_uniform_auth_failure_total",
        "coderouter_provider_attempts_total",
        "coderouter_provider_outcomes_total",
        "coderouter_provider_skipped_total",
        "coderouter_capability_degraded_total",
        "coderouter_output_filter_applied_total",
    ):
        assert f"# HELP {metric} " in out, f"missing HELP for {metric}"
        assert f"# TYPE {metric} " in out, f"missing TYPE for {metric}"


def test_output_ends_with_single_newline() -> None:
    out = format_prometheus(_empty_snapshot())
    assert out.endswith("\n")
    assert not out.endswith("\n\n")


def test_counter_names_end_with_total() -> None:
    out = format_prometheus(_empty_snapshot())
    # Every TYPE foo counter line must end in `_total` — the Prometheus
    # convention that makes counter vs gauge discoverable at a glance.
    for line in out.splitlines():
        if line.startswith("# TYPE ") and line.endswith(" counter"):
            name = line.split()[2]
            assert name.endswith("_total"), f"counter {name} missing _total suffix"


# ---------------------------------------------------------------------------
# Sample emission
# ---------------------------------------------------------------------------


def test_scalar_counter_emits_value_on_same_line() -> None:
    snap = _empty_snapshot()
    snap["counters"]["requests_total"] = 42
    out = format_prometheus(snap)
    assert "coderouter_requests_total 42" in out


def test_per_provider_attempts_uses_provider_label() -> None:
    snap = _empty_snapshot()
    snap["counters"]["provider_attempts"] = {"local": 5, "free-cloud": 2}
    out = format_prometheus(snap)
    assert 'coderouter_provider_attempts_total{provider="local"} 5' in out
    assert 'coderouter_provider_attempts_total{provider="free-cloud"} 2' in out


def test_provider_outcomes_uses_two_labels() -> None:
    snap = _empty_snapshot()
    snap["counters"]["provider_outcomes"] = {
        "local": {"ok": 3, "failed": 1},
        "free-cloud": {"failed_midstream": 1},
    }
    out = format_prometheus(snap)
    assert 'coderouter_provider_outcomes_total{provider="local",outcome="ok"} 3' in out
    assert 'coderouter_provider_outcomes_total{provider="local",outcome="failed"} 1' in out
    assert (
        'coderouter_provider_outcomes_total{provider="free-cloud",outcome="failed_midstream"} 1'
        in out
    )


def test_skipped_merges_paid_and_unknown_reasons() -> None:
    snap = _empty_snapshot()
    snap["counters"]["provider_skipped_paid"] = {"paid-cloud": 2}
    snap["counters"]["provider_skipped_unknown"] = {"typo": 1}
    out = format_prometheus(snap)
    assert (
        'coderouter_provider_skipped_total{provider="paid-cloud",reason="paid"} 2' in out
    )
    assert (
        'coderouter_provider_skipped_total{provider="typo",reason="unknown"} 1' in out
    )


def test_capability_and_filter_labels() -> None:
    snap = _empty_snapshot()
    snap["counters"]["capability_degraded"] = {"thinking": 2, "reasoning": 1}
    snap["counters"]["output_filter_applied"] = {"strip_thinking": 5}
    out = format_prometheus(snap)
    assert 'coderouter_capability_degraded_total{capability="thinking"} 2' in out
    assert 'coderouter_capability_degraded_total{capability="reasoning"} 1' in out
    assert 'coderouter_output_filter_applied_total{filter="strip_thinking"} 5' in out


def test_uptime_gauge_emits_numeric_value() -> None:
    snap = _empty_snapshot()
    snap["uptime_s"] = 123.456
    out = format_prometheus(snap)
    assert "# TYPE coderouter_uptime_seconds gauge" in out
    assert "coderouter_uptime_seconds 123.456" in out


# ---------------------------------------------------------------------------
# Escaping
# ---------------------------------------------------------------------------


def test_label_value_escapes_backslash_quote_newline() -> None:
    """Spec: label values escape ``\\``, ``"``, and newline; everything else literal."""
    snap = _empty_snapshot()
    # Pathological provider name — not realistic config, but we must not
    # produce a malformed exposition line if someone names a provider
    # ``weird"name\nwith\\slashes``.
    snap["counters"]["provider_attempts"] = {'weird"name\nwith\\slashes': 1}
    out = format_prometheus(snap)
    assert (
        'coderouter_provider_attempts_total{provider="weird\\"name\\nwith\\\\slashes"} 1'
        in out
    )


def test_dashes_in_label_values_are_literal() -> None:
    """Dashes are valid inside label values — only metric NAMES reject them.

    Regression guard: ``ollama-local`` / ``free-cloud`` should pass
    through untouched so operators can search by the exact provider name.
    """
    snap = _empty_snapshot()
    snap["counters"]["provider_attempts"] = {"ollama-local": 7}
    out = format_prometheus(snap)
    assert 'coderouter_provider_attempts_total{provider="ollama-local"} 7' in out
