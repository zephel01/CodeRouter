"""Unit tests for v1.9-A Cache Observability — collector + snapshot shape.

Validates that the new ``cache-observed`` log event flows through the
:class:`MetricsCollector` dispatch table (see
``coderouter/metrics/collector.py`` v1.9-A additions) and surfaces the
expected per-provider + aggregate fields in :meth:`MetricsCollector.snapshot`.

The four-class outcome (``cache_hit`` / ``cache_creation`` / ``no_cache``
/ ``unknown``) is what differentiates the CodeRouter cache picture from
LiteLLM's — see ``docs/inside/future.md`` §3 for the underlying
LiteLLM ``cache_creation_input_tokens`` undercounting bug.

End-to-end emission from the engine is exercised in
``test_fallback_cache_observed.py``; this module isolates the collector.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from coderouter.logging import (
    classify_cache_outcome,
    configure_logging,
    get_logger,
    log_cache_observed,
)
from coderouter.metrics import (
    MetricsCollector,
    install_collector,
    uninstall_collector,
)


@pytest.fixture
def collector() -> Iterator[MetricsCollector]:
    """Fresh singleton per test (mirrors test_metrics_collector.py)."""
    uninstall_collector()
    configure_logging()
    yield install_collector(ring_size=16)
    uninstall_collector()


def _fire_cache_observed(**extra: Any) -> None:
    """Emit a ``cache-observed`` structured log line through the helper."""
    log_cache_observed(
        get_logger("test.metrics.cache"),
        provider=extra.get("provider", "test"),
        request_had_cache_control=extra.get("request_had_cache_control", False),
        outcome=extra.get("outcome", "unknown"),
        cache_read_input_tokens=extra.get("cache_read_input_tokens", 0),
        cache_creation_input_tokens=extra.get("cache_creation_input_tokens", 0),
        input_tokens=extra.get("input_tokens", 0),
        output_tokens=extra.get("output_tokens", 0),
        streaming=extra.get("streaming", False),
    )


# ---------------------------------------------------------------------------
# classify_cache_outcome — pure-function helper
# ---------------------------------------------------------------------------


def test_classify_cache_outcome_unknown_when_no_usage() -> None:
    """No usage block at all → ``unknown`` (not ``no_cache``).

    Distinguishing these is the entire point of the 4-class split: a
    streaming response that hasn't aggregated ``message_delta`` events
    yet is ``unknown``, NOT ``no_cache`` — counting it as ``no_cache``
    would dilute the hit rate with provider/configuration noise.
    """
    assert (
        classify_cache_outcome(
            usage_present=False,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        )
        == "unknown"
    )


def test_classify_cache_outcome_hit_takes_precedence_over_creation() -> None:
    """Both read AND creation counts present → ``cache_hit``.

    Rare but possible on long conversations where a cached prefix is
    extended with a fresh ``cache_control`` marker on the same call.
    The creation tokens still roll into the per-provider creation
    counter, so no token is lost from the accounting.
    """
    assert (
        classify_cache_outcome(
            usage_present=True,
            cache_read_input_tokens=512,
            cache_creation_input_tokens=128,
        )
        == "cache_hit"
    )


def test_classify_cache_outcome_creation_when_only_creation() -> None:
    assert (
        classify_cache_outcome(
            usage_present=True,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=2048,
        )
        == "cache_creation"
    )


def test_classify_cache_outcome_no_cache_when_usage_present_but_zero() -> None:
    """Usage block present but cache fields 0/missing → ``no_cache``.

    This is the case where the request lacked ``cache_control`` — or
    sent it but the upstream did not honor it — and is what gets
    bucketed against the hit-rate denominator.
    """
    assert (
        classify_cache_outcome(
            usage_present=True,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        )
        == "no_cache"
    )


# ---------------------------------------------------------------------------
# Collector dispatch
# ---------------------------------------------------------------------------


def test_cache_observed_increments_per_provider_token_counters(
    collector: MetricsCollector,
) -> None:
    """``cache-observed`` with non-zero tokens accumulates per-provider."""
    _fire_cache_observed(
        provider="anthropic-direct",
        outcome="cache_hit",
        cache_read_input_tokens=300,
        cache_creation_input_tokens=0,
        input_tokens=12,
        output_tokens=8,
        request_had_cache_control=True,
    )
    _fire_cache_observed(
        provider="anthropic-direct",
        outcome="cache_creation",
        cache_read_input_tokens=0,
        cache_creation_input_tokens=1500,
        input_tokens=1500,
        output_tokens=4,
        request_had_cache_control=True,
    )

    snap = collector.snapshot()
    counters = snap["counters"]
    assert counters["cache_read_tokens"] == {"anthropic-direct": 300}
    assert counters["cache_creation_tokens"] == {"anthropic-direct": 1500}
    # Aggregate totals across providers
    assert counters["cache_read_tokens_total"] == 300
    assert counters["cache_creation_tokens_total"] == 1500


def test_cache_observed_records_outcome_breakdown(
    collector: MetricsCollector,
) -> None:
    """4-class outcome counter is per-provider AND in the snapshot."""
    for outcome, kwargs in [
        ("cache_hit", {"cache_read_input_tokens": 200}),
        ("cache_hit", {"cache_read_input_tokens": 100}),
        ("cache_creation", {"cache_creation_input_tokens": 1024}),
        ("no_cache", {}),
        ("unknown", {}),
    ]:
        _fire_cache_observed(provider="lmstudio", outcome=outcome, **kwargs)

    snap = collector.snapshot()
    breakdown = snap["counters"]["cache_outcomes"]["lmstudio"]
    assert breakdown == {
        "cache_hit": 2,
        "cache_creation": 1,
        "no_cache": 1,
        "unknown": 1,
    }


def test_cache_observed_per_provider_row_carries_cache_panel(
    collector: MetricsCollector,
) -> None:
    """Each provider row gets a ``cache`` sub-dict with hit_rate + tokens.

    Verifies the v1.9-A snapshot extension that powers the dashboard's
    per-provider cache panel.
    """
    # 2 hits, 1 creation, 1 no_cache → hit_rate = 2/4 = 0.5
    for outcome, kwargs in [
        ("cache_hit", {"cache_read_input_tokens": 200}),
        ("cache_hit", {"cache_read_input_tokens": 200}),
        ("cache_creation", {"cache_creation_input_tokens": 600}),
        ("no_cache", {}),
        # Two ``unknown`` are excluded from the hit_rate denominator so
        # this test pins that behavior — the hit rate stays at 0.5
        # despite the additional observations.
        ("unknown", {}),
        ("unknown", {}),
    ]:
        _fire_cache_observed(provider="lmstudio-9b", outcome=outcome, **kwargs)

    snap = collector.snapshot()
    row = next(p for p in snap["providers"] if p["name"] == "lmstudio-9b")
    cache = row["cache"]
    assert cache["read_tokens"] == 400
    assert cache["creation_tokens"] == 600
    assert cache["hit_rate"] == 0.5
    assert cache["observations"] == 6  # all 6 events including the unknowns


def test_cache_observed_hit_rate_is_none_with_no_observations(
    collector: MetricsCollector,
) -> None:
    """A provider with no observations gets ``hit_rate=None`` (not 0.0).

    This is what lets the dashboard render "—" instead of a deceptive
    flat 0% for an idle provider that never had a chance to measure.
    """
    # Bump only attempts (no cache-observed event) so the provider
    # appears in the row list but has no cache data.
    get_logger("test").info("try-provider", extra={"provider": "idle", "stream": False})

    snap = collector.snapshot()
    row = next(p for p in snap["providers"] if p["name"] == "idle")
    assert row["cache"]["hit_rate"] is None
    assert row["cache"]["observations"] == 0


def test_cache_observed_only_unknown_keeps_hit_rate_none(
    collector: MetricsCollector,
) -> None:
    """All-``unknown`` observations keep ``hit_rate=None``.

    This pins the rule that ``unknown`` is not a denominator entry —
    a provider where every response is streaming (and hence ``unknown``
    today, until v1.9-B aggregates ``message_delta``) shouldn't be
    plotted at 0% hit rate. It's "no signal", not "all misses".
    """
    for _ in range(3):
        _fire_cache_observed(provider="streaming-only", outcome="unknown")

    snap = collector.snapshot()
    row = next(p for p in snap["providers"] if p["name"] == "streaming-only")
    assert row["cache"]["hit_rate"] is None


def test_cache_observed_reset_clears_state(collector: MetricsCollector) -> None:
    """:meth:`MetricsCollector.reset` zeroes the v1.9-A counters."""
    _fire_cache_observed(
        provider="any",
        outcome="cache_hit",
        cache_read_input_tokens=99,
    )
    collector.reset()
    snap = collector.snapshot()
    assert snap["counters"]["cache_read_tokens"] == {}
    assert snap["counters"]["cache_creation_tokens"] == {}
    assert snap["counters"]["cache_outcomes"] == {}
    assert snap["counters"]["cache_read_tokens_total"] == 0
    assert snap["counters"]["cache_creation_tokens_total"] == 0


def test_cache_observed_defensive_against_non_int_tokens(
    collector: MetricsCollector,
) -> None:
    """Malformed log extras don't crash the metrics tap.

    The handler contract says we never let a metrics dispatch raise,
    even on a malformed event. Token fields arriving as strings (or
    None) should be coerced to 0 rather than blowing up the Counter
    arithmetic.
    """
    # We bypass the typed helper here to inject a malformed extra.
    get_logger("test.metrics.cache").info(
        "cache-observed",
        extra={
            "provider": "weird",
            "outcome": "cache_hit",
            "cache_read_input_tokens": "not-an-int",  # malformed
            "cache_creation_input_tokens": None,  # missing
        },
    )

    snap = collector.snapshot()
    # Malformed values coerce to 0 → counter still records 0 for the
    # provider, outcome still bumps. No exception escaped.
    assert snap["counters"]["cache_read_tokens"].get("weird", 0) == 0
    assert snap["counters"]["cache_outcomes"]["weird"]["cache_hit"] == 1
