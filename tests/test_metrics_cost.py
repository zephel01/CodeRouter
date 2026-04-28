"""Unit tests for v1.9-D Cost-aware Dashboard.

Three layers covered here:

  1. :func:`coderouter.cost.compute_cost_for_attempt` (pure function):
     correctly applies Anthropic's prompt-cache pricing model
     (cache_read at 10%, cache_creation at 125%) and computes the
     counterfactual savings figure used by the dashboard.
  2. :class:`MetricsCollector` dispatch: ``cache-observed`` events
     carrying ``cost_usd`` / ``cost_savings_usd`` are aggregated
     per-provider plus aggregate totals.
  3. Prometheus exposition: per-provider cost counters surface as
     float-valued metrics with ``_total`` suffix per convention.

End-to-end engine emission (timing + provider lookup) is exercised
by the existing ``test_fallback_cache_observed.py``; this module
isolates the cost-specific logic.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from coderouter.config.schemas import CostConfig
from coderouter.cost import CostBreakdown, compute_cost_for_attempt
from coderouter.logging import (
    configure_logging,
    get_logger,
    log_cache_observed,
)
from coderouter.metrics import (
    MetricsCollector,
    format_prometheus,
    install_collector,
    uninstall_collector,
)

# Anthropic Sonnet 4.x reference pricing for tests.
_ANTHROPIC_SONNET_COST = CostConfig(
    input_tokens_per_million=3.00,
    output_tokens_per_million=15.00,
    cache_read_discount=0.10,
    cache_creation_premium=1.25,
)


@pytest.fixture
def collector() -> Iterator[MetricsCollector]:
    """Fresh singleton per test (mirrors test_metrics_collector.py)."""
    uninstall_collector()
    configure_logging()
    yield install_collector(ring_size=16)
    uninstall_collector()


def _fire_cache_observed(**extra: Any) -> None:
    """Emit a ``cache-observed`` log line via the typed helper."""
    log_cache_observed(
        get_logger("test.metrics.cost"),
        provider=extra.get("provider", "test"),
        request_had_cache_control=extra.get("request_had_cache_control", False),
        outcome=extra.get("outcome", "no_cache"),
        cache_read_input_tokens=extra.get("cache_read_input_tokens", 0),
        cache_creation_input_tokens=extra.get("cache_creation_input_tokens", 0),
        input_tokens=extra.get("input_tokens", 0),
        output_tokens=extra.get("output_tokens", 0),
        streaming=extra.get("streaming", False),
        cost_usd=extra.get("cost_usd", 0.0),
        cost_savings_usd=extra.get("cost_savings_usd", 0.0),
    )


# ---------------------------------------------------------------------------
# compute_cost_for_attempt — pure function
# ---------------------------------------------------------------------------


def test_compute_cost_returns_zero_when_cost_config_is_none() -> None:
    """Local / unconfigured providers contribute zero to the cost dashboard."""
    out = compute_cost_for_attempt(
        None,
        input_tokens=1000,
        output_tokens=500,
        cache_read_input_tokens=200,
        cache_creation_input_tokens=100,
    )
    assert out == CostBreakdown()


def test_compute_cost_normal_input_output_no_cache() -> None:
    """Plain request with no cache → cost = input + output at normal rate.

    Anthropic Sonnet pricing: $3 / M input, $15 / M output.
    1000 input + 500 output → 0.003 + 0.0075 = 0.0105 USD.
    """
    out = compute_cost_for_attempt(
        _ANTHROPIC_SONNET_COST,
        input_tokens=1000,
        output_tokens=500,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    )
    assert out.input_usd == pytest.approx(0.003)
    assert out.output_usd == pytest.approx(0.0075)
    assert out.cache_read_usd == 0.0
    assert out.cache_creation_usd == 0.0
    assert out.total_usd == pytest.approx(0.0105)
    # No cache reads → no savings.
    assert out.savings_usd == 0.0


def test_compute_cost_cache_read_applies_discount() -> None:
    """Cache reads bill at ``cache_read_discount`` x normal input rate.

    1000 cache_read tokens at $3 / M x 0.10 discount = $0.0003 actual
    paid. Counterfactual (no-cache) would have been $0.003. Savings
    = $0.003 - $0.0003 = $0.0027.
    """
    out = compute_cost_for_attempt(
        _ANTHROPIC_SONNET_COST,
        input_tokens=0,
        output_tokens=0,
        cache_read_input_tokens=1000,
        cache_creation_input_tokens=0,
    )
    assert out.cache_read_usd == pytest.approx(0.0003)
    assert out.total_usd == pytest.approx(0.0003)
    assert out.savings_usd == pytest.approx(0.0027)


def test_compute_cost_cache_creation_applies_premium() -> None:
    """Cache creation bills at ``cache_creation_premium`` x normal input.

    1000 cache_creation tokens at $3 / M x 1.25 premium = $0.00375.
    Cache creation does NOT contribute to savings (it's a premium,
    not a discount).
    """
    out = compute_cost_for_attempt(
        _ANTHROPIC_SONNET_COST,
        input_tokens=0,
        output_tokens=0,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=1000,
    )
    assert out.cache_creation_usd == pytest.approx(0.00375)
    assert out.total_usd == pytest.approx(0.00375)
    # cache_creation is a premium, never a savings.
    assert out.savings_usd == 0.0


def test_compute_cost_combined_buckets() -> None:
    """All four token buckets together compose the total correctly.

    1000 input + 500 output + 200 cache_read + 100 cache_creation.
    Sonnet rates:
      input    : 1000 x 3 / 1e6        = 0.003
      output   : 500  x 15 / 1e6       = 0.0075
      read     : 200  x 3 / 1e6 x 0.10 = 0.00006
      creation : 100  x 3 / 1e6 x 1.25 = 0.000375
      total    = 0.010935
      savings  = 200 x 3 / 1e6 x 0.90  = 0.00054
    """
    out = compute_cost_for_attempt(
        _ANTHROPIC_SONNET_COST,
        input_tokens=1000,
        output_tokens=500,
        cache_read_input_tokens=200,
        cache_creation_input_tokens=100,
    )
    assert out.total_usd == pytest.approx(0.010935)
    assert out.savings_usd == pytest.approx(0.00054)


def test_compute_cost_negative_tokens_clamped_to_zero() -> None:
    """Defensive: negative token counts (malformed log) → zero contribution.

    Pins the safety net that prevents a corrupt log line from
    flipping the aggregate counters into negative territory.
    """
    out = compute_cost_for_attempt(
        _ANTHROPIC_SONNET_COST,
        input_tokens=-100,
        output_tokens=-50,
        cache_read_input_tokens=-10,
        cache_creation_input_tokens=-5,
    )
    assert out.total_usd == 0.0
    assert out.savings_usd == 0.0


def test_compute_cost_partial_config_only_input_rate_set() -> None:
    """Operator can declare only ``input_tokens_per_million`` — output is
    treated as zero rather than failing."""
    partial = CostConfig(input_tokens_per_million=2.0)  # output unset
    out = compute_cost_for_attempt(
        partial,
        input_tokens=1000,
        output_tokens=500,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    )
    assert out.input_usd == pytest.approx(0.002)
    assert out.output_usd == 0.0  # unset → 0 rate
    assert out.total_usd == pytest.approx(0.002)


# ---------------------------------------------------------------------------
# Collector dispatch — cost aggregation
# ---------------------------------------------------------------------------


def test_collector_aggregates_cost_per_provider(
    collector: MetricsCollector,
) -> None:
    """Multiple ``cache-observed`` events sum into per-provider totals."""
    _fire_cache_observed(
        provider="anthropic-direct",
        outcome="cache_hit",
        cost_usd=0.0030,
        cost_savings_usd=0.0027,
    )
    _fire_cache_observed(
        provider="anthropic-direct",
        outcome="no_cache",
        cost_usd=0.0105,
        cost_savings_usd=0.0,
    )
    _fire_cache_observed(
        provider="other-paid",
        outcome="no_cache",
        cost_usd=0.005,
        cost_savings_usd=0.0,
    )
    snap = collector.snapshot()
    counters = snap["counters"]
    assert counters["cost_total_usd"]["anthropic-direct"] == pytest.approx(0.0135)
    assert counters["cost_total_usd"]["other-paid"] == pytest.approx(0.005)
    assert counters["cost_savings_usd"]["anthropic-direct"] == pytest.approx(0.0027)
    # Aggregate across providers
    assert counters["cost_total_usd_aggregate"] == pytest.approx(0.0185)
    assert counters["cost_savings_usd_aggregate"] == pytest.approx(0.0027)


def test_collector_zero_cost_does_not_create_provider_entry(
    collector: MetricsCollector,
) -> None:
    """A provider whose cost is always 0 (local model) → no entry in the
    cost_total_usd dict (avoids cluttering the dashboard with $0.00 rows
    for local providers that operators don't care about cost-wise)."""
    _fire_cache_observed(
        provider="local-llama",
        outcome="no_cache",
        cost_usd=0.0,
        cost_savings_usd=0.0,
    )
    snap = collector.snapshot()
    assert "local-llama" not in snap["counters"]["cost_total_usd"]
    assert "local-llama" not in snap["counters"]["cost_savings_usd"]


def test_collector_provider_row_carries_cost_panel(
    collector: MetricsCollector,
) -> None:
    """Per-provider snapshot row includes a ``cost`` sub-dict for the dashboard.

    ``total_usd`` / ``savings_usd`` keys are always present (even at 0)
    so the dashboard can rely on the schema; the rendering layer
    decides whether to show "—" or "$0.00".
    """
    _fire_cache_observed(
        provider="anthropic-direct",
        outcome="cache_hit",
        cost_usd=0.0030,
        cost_savings_usd=0.0027,
    )
    snap = collector.snapshot()
    row = next(p for p in snap["providers"] if p["name"] == "anthropic-direct")
    assert "cost" in row
    assert row["cost"]["total_usd"] == pytest.approx(0.003)
    assert row["cost"]["savings_usd"] == pytest.approx(0.0027)


def test_collector_reset_clears_cost_state(
    collector: MetricsCollector,
) -> None:
    """``MetricsCollector.reset`` zeroes the v1.9-D cost totals."""
    _fire_cache_observed(provider="x", outcome="cache_hit", cost_usd=1.0)
    collector.reset()
    snap = collector.snapshot()
    counters = snap["counters"]
    assert counters["cost_total_usd"] == {}
    assert counters["cost_savings_usd"] == {}
    assert counters["cost_total_usd_aggregate"] == 0.0
    assert counters["cost_savings_usd_aggregate"] == 0.0


def test_collector_defensive_against_non_float_cost(
    collector: MetricsCollector,
) -> None:
    """Malformed cost values default to 0.0 rather than crashing.

    Same defensive contract as v1.9-A's token coercion: the metrics
    handler must never raise from a malformed log line.
    """
    get_logger("test").info(
        "cache-observed",
        extra={
            "provider": "weird",
            "outcome": "no_cache",
            "cost_usd": "not-a-number",
            "cost_savings_usd": None,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        },
    )
    snap = collector.snapshot()
    # Malformed values → no entry created (cost_usd > 0 gate)
    assert "weird" not in snap["counters"]["cost_total_usd"]


# ---------------------------------------------------------------------------
# Prometheus exposition — cost metrics
# ---------------------------------------------------------------------------


def _snapshot_with_cost(
    *,
    cost_total_usd: dict[str, float],
    cost_savings_usd: dict[str, float],
) -> dict[str, Any]:
    """Build a minimal snapshot exercising the v1.9-D cost counters."""
    return {
        "uptime_s": 1.0,
        "started_at": "2026-04-28T12:00:00",
        "startup": {},
        "counters": {
            "requests_total": 0,
            "chain_paid_gate_blocked_total": 0,
            "chain_uniform_auth_failure_total": 0,
            "auto_router_fallthrough_total": 0,
            "cache_read_tokens_total": 0,
            "cache_creation_tokens_total": 0,
            "provider_attempts": {},
            "provider_outcomes": {},
            "provider_skipped_paid": {},
            "provider_skipped_unknown": {},
            "capability_degraded": {},
            "output_filter_applied": {},
            "cache_read_tokens": {},
            "cache_creation_tokens": {},
            "cache_outcomes": {},
            "cost_total_usd": cost_total_usd,
            "cost_savings_usd": cost_savings_usd,
            "cost_total_usd_aggregate": sum(cost_total_usd.values()),
            "cost_savings_usd_aggregate": sum(cost_savings_usd.values()),
        },
        "providers": [],
        "recent": [],
    }


def test_prometheus_emits_cost_help_and_type_lines() -> None:
    out = format_prometheus(_snapshot_with_cost(cost_total_usd={}, cost_savings_usd={}))
    for metric in (
        "coderouter_cost_total_usd_total",
        "coderouter_cost_savings_usd_total",
    ):
        assert f"# HELP {metric} " in out
        assert f"# TYPE {metric} counter" in out


def test_prometheus_emits_per_provider_cost_samples() -> None:
    out = format_prometheus(
        _snapshot_with_cost(
            cost_total_usd={"anthropic-direct": 0.0135, "other-paid": 0.005},
            cost_savings_usd={"anthropic-direct": 0.0027},
        )
    )
    assert (
        'coderouter_cost_total_usd_total{provider="anthropic-direct"} 0.0135'
        in out
    )
    assert 'coderouter_cost_total_usd_total{provider="other-paid"} 0.005' in out
    assert (
        'coderouter_cost_savings_usd_total{provider="anthropic-direct"} 0.0027'
        in out
    )


def test_prometheus_cost_metrics_use_total_suffix() -> None:
    """v1.9-D cost counters follow the ``_total`` Prometheus convention."""
    out = format_prometheus(
        _snapshot_with_cost(
            cost_total_usd={"x": 1.0}, cost_savings_usd={"x": 0.5}
        )
    )
    for line in out.splitlines():
        if line.startswith("# TYPE coderouter_cost_") and line.endswith(" counter"):
            name = line.split()[2]
            assert name.endswith("_total"), f"counter {name} missing _total suffix"
