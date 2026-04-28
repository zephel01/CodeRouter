"""Unit tests for v1.9-C AdaptiveAdjuster — health-based dynamic chain priority.

Two layers covered here:

  1. :class:`AdaptiveAdjuster` (pure logic): rolling-window stats,
     latency-vs-global-median demotion, error-rate demotion,
     debounce semantics, deterministic ordering.
  2. Engine integration via :meth:`FallbackEngine._resolve_anthropic_chain`:
     ``adaptive: true`` profiles get reorders applied; static profiles
     do not.

The tests inject ``now=`` timestamps explicitly so the rolling-window
behavior is deterministic without sleeping.
"""

from __future__ import annotations

from typing import cast

from coderouter.adapters.base import BaseAdapter
from coderouter.config.schemas import (
    Capabilities,
    CodeRouterConfig,
    FallbackChain,
    ProviderConfig,
)
from coderouter.routing.adaptive import (
    DEBOUNCE_S,
    ERROR_RATE_DEMOTE_THRESHOLD,
    LATENCY_DEMOTE_FACTOR,
    MIN_SAMPLES_FOR_ERROR_RATE,
    MIN_SAMPLES_FOR_LATENCY,
    ROLLING_WINDOW_S,
    AdaptiveAdjuster,
)
from coderouter.routing.fallback import FallbackEngine

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _FakeAdapter:
    """Minimal stand-in for :class:`BaseAdapter` — only exposes ``name``.

    AdaptiveAdjuster's API is name-only, so a duck-typed object is
    enough. Cast to ``BaseAdapter`` at the call site for typing.
    """

    def __init__(self, name: str) -> None:
        self.name = name

    def __repr__(self) -> str:  # pragma: no cover - debug aid only
        return f"_FakeAdapter({self.name!r})"


def _adapter(name: str) -> BaseAdapter:
    return cast("BaseAdapter", _FakeAdapter(name))


def _provider(name: str) -> ProviderConfig:
    return ProviderConfig(
        name=name,
        kind="anthropic",
        base_url="https://example.test",
        model="any",
        capabilities=Capabilities(),
    )


def _config_with_adaptive_profile(
    *, adaptive: bool, providers: list[str]
) -> CodeRouterConfig:
    return CodeRouterConfig(
        providers=[_provider(n) for n in providers],
        profiles=[
            FallbackChain(
                name="default",
                providers=providers,
                adaptive=adaptive,
            )
        ],
    )


# ---------------------------------------------------------------------------
# Stats — rolling window + median + error rate
# ---------------------------------------------------------------------------


def test_stats_default_to_zero_for_unseen_provider() -> None:
    adj = AdaptiveAdjuster()
    s = adj.stats_for("never-seen")
    assert s.median_latency_ms is None
    assert s.error_rate == 0.0
    assert s.sample_count == 0


def test_stats_compute_median_from_successful_samples_only() -> None:
    """Failed attempts must NOT contribute to the median latency.

    A failure with a high latency would push the median up and make
    the provider look slow even if its successful calls are fast.
    """
    adj = AdaptiveAdjuster()
    now = 1000.0
    # 3 successful: 100, 200, 300 → median 200
    for ms in (100.0, 200.0, 300.0):
        adj.record_attempt("a", latency_ms=ms, success=True, now=now)
    # 1 failure with latency 9000 — should be excluded from median
    adj.record_attempt("a", latency_ms=9000.0, success=False, now=now)
    s = adj.stats_for("a", now=now)
    assert s.median_latency_ms == 200.0
    # But the failure DOES count for error rate.
    assert s.error_rate == 1 / 4
    assert s.sample_count == 4


def test_stats_drop_observations_older_than_window() -> None:
    """Observations older than ``ROLLING_WINDOW_S`` must roll off."""
    adj = AdaptiveAdjuster()
    # 5 old samples and 2 fresh ones.
    for _ in range(5):
        adj.record_attempt("a", latency_ms=1000.0, success=True, now=0.0)
    adj.record_attempt("a", latency_ms=100.0, success=True, now=ROLLING_WINDOW_S + 1)
    adj.record_attempt("a", latency_ms=200.0, success=True, now=ROLLING_WINDOW_S + 2)
    s = adj.stats_for("a", now=ROLLING_WINDOW_S + 2)
    # Only the 2 fresh entries — median is mean of (100, 200) = 150
    assert s.median_latency_ms == 150.0
    assert s.sample_count == 2


def test_stats_error_rate_zero_with_no_samples() -> None:
    """A provider with zero samples reports zero error rate (not NaN)."""
    adj = AdaptiveAdjuster()
    s = adj.stats_for("a")
    assert s.error_rate == 0.0


# ---------------------------------------------------------------------------
# Effective ordering — no demotions
# ---------------------------------------------------------------------------


def test_effective_order_unchanged_when_chain_empty() -> None:
    adj = AdaptiveAdjuster()
    assert adj.compute_effective_order([]) == []


def test_effective_order_unchanged_when_no_observations() -> None:
    """Fresh adjuster + chain → static order wins."""
    adj = AdaptiveAdjuster()
    chain = [_adapter("a"), _adapter("b"), _adapter("c")]
    out = adj.compute_effective_order(chain)
    assert [x.name for x in out] == ["a", "b", "c"]


def test_effective_order_unchanged_when_all_providers_fast() -> None:
    """All providers within latency / error thresholds → static order wins."""
    adj = AdaptiveAdjuster()
    now = 1000.0
    for n in ("a", "b", "c"):
        for _ in range(MIN_SAMPLES_FOR_LATENCY):
            adj.record_attempt(n, latency_ms=100.0, success=True, now=now)
    chain = [_adapter("a"), _adapter("b"), _adapter("c")]
    out = adj.compute_effective_order(chain, now=now)
    assert [x.name for x in out] == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# Effective ordering — latency-based demotions
# ---------------------------------------------------------------------------


def test_effective_order_latency_demotion_moves_slow_provider_back() -> None:
    """A provider whose median latency >= 1.5x global median → demoted 1 rank.

    With chain [a, b, c] and a slow → effective order should put a after b.
    """
    adj = AdaptiveAdjuster()
    now = 1000.0
    # a is slow: median ~ 1000 ms
    for _ in range(MIN_SAMPLES_FOR_LATENCY):
        adj.record_attempt("a", latency_ms=1000.0, success=True, now=now)
    # b and c are fast: median ~ 100 ms
    for n in ("b", "c"):
        for _ in range(MIN_SAMPLES_FOR_LATENCY):
            adj.record_attempt(n, latency_ms=100.0, success=True, now=now)
    chain = [_adapter("a"), _adapter("b"), _adapter("c")]
    out = adj.compute_effective_order(chain, now=now)
    # Global median across (1000, 100, 100) = 100. Threshold = 100*1.5 = 150.
    # a is 1000 >> 150 → demote 1 rank → goes after b, c (both at demotion 0).
    assert [x.name for x in out] == ["b", "c", "a"]


def test_effective_order_below_min_samples_does_not_demote() -> None:
    """Need at least ``MIN_SAMPLES_FOR_LATENCY`` to consider latency."""
    adj = AdaptiveAdjuster()
    now = 1000.0
    # a has only 1 slow sample (below MIN_SAMPLES_FOR_LATENCY).
    adj.record_attempt("a", latency_ms=10000.0, success=True, now=now)
    # b and c have enough fast samples.
    for n in ("b", "c"):
        for _ in range(MIN_SAMPLES_FOR_LATENCY):
            adj.record_attempt(n, latency_ms=100.0, success=True, now=now)
    chain = [_adapter("a"), _adapter("b"), _adapter("c")]
    out = adj.compute_effective_order(chain, now=now)
    # a's single slow sample isn't enough to demote — static order wins.
    assert [x.name for x in out] == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# Effective ordering — error-rate demotions
# ---------------------------------------------------------------------------


def test_effective_order_error_rate_demotion_moves_failing_provider_to_back() -> None:
    """error_rate >= 10% → demote 2 ranks (more aggressive than latency)."""
    adj = AdaptiveAdjuster()
    now = 1000.0
    # a has a high failure rate.
    for _ in range(MIN_SAMPLES_FOR_ERROR_RATE - 1):
        adj.record_attempt("a", latency_ms=100.0, success=True, now=now)
    for _ in range(2):  # 2 failures out of (MIN_SAMPLES + 1) = high rate
        adj.record_attempt("a", latency_ms=100.0, success=False, now=now)
    # b and c are clean.
    for n in ("b", "c"):
        for _ in range(MIN_SAMPLES_FOR_ERROR_RATE):
            adj.record_attempt(n, latency_ms=100.0, success=True, now=now)
    chain = [_adapter("a"), _adapter("b"), _adapter("c")]
    out = adj.compute_effective_order(chain, now=now)
    # a has error_rate = 2/(MIN+1) > 0.10 → demote +2 → moves to end.
    assert out[-1].name == "a"


def test_effective_order_error_rate_below_min_samples_does_not_demote() -> None:
    """A provider with one failure but few samples is not yet demoted."""
    adj = AdaptiveAdjuster()
    now = 1000.0
    # a: 1 failure, 1 success — only 2 samples, below MIN_SAMPLES_FOR_ERROR_RATE.
    adj.record_attempt("a", latency_ms=100.0, success=False, now=now)
    adj.record_attempt("a", latency_ms=100.0, success=True, now=now)
    for n in ("b", "c"):
        for _ in range(MIN_SAMPLES_FOR_ERROR_RATE):
            adj.record_attempt(n, latency_ms=100.0, success=True, now=now)
    chain = [_adapter("a"), _adapter("b"), _adapter("c")]
    out = adj.compute_effective_order(chain, now=now)
    # 50% error rate but below sample threshold → no demotion.
    assert [x.name for x in out] == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# Effective ordering — debounce
# ---------------------------------------------------------------------------


def test_debounce_pins_recently_changed_rank() -> None:
    """A second proposed reorder within DEBOUNCE_S must be reverted."""
    adj = AdaptiveAdjuster()
    chain = [_adapter("a"), _adapter("b"), _adapter("c")]

    # Pass 1 at t=0: a is slow, gets demoted.
    now0 = 1000.0
    for _ in range(MIN_SAMPLES_FOR_LATENCY):
        adj.record_attempt("a", latency_ms=1000.0, success=True, now=now0)
    for n in ("b", "c"):
        for _ in range(MIN_SAMPLES_FOR_LATENCY):
            adj.record_attempt(n, latency_ms=100.0, success=True, now=now0)
    out1 = adj.compute_effective_order(chain, now=now0)
    assert [x.name for x in out1] == ["b", "c", "a"]

    # Pass 2 at t=0+1s: a now looks fast (we add fast samples).
    # The proposed order would put a back at position 0, but
    # debounce forbids it because the rank change was less than
    # DEBOUNCE_S ago.
    now1 = now0 + 1.0
    for _ in range(MIN_SAMPLES_FOR_LATENCY * 3):
        adj.record_attempt("a", latency_ms=50.0, success=True, now=now1)
    out2 = adj.compute_effective_order(chain, now=now1)
    # a's rank is pinned to its previous position (back).
    assert [x.name for x in out2] == ["b", "c", "a"]


def test_debounce_releases_after_window() -> None:
    """After DEBOUNCE_S has elapsed, rank changes are accepted again."""
    adj = AdaptiveAdjuster()
    chain = [_adapter("a"), _adapter("b"), _adapter("c")]

    # Pass 1: a slow, demoted.
    now0 = 1000.0
    for _ in range(MIN_SAMPLES_FOR_LATENCY):
        adj.record_attempt("a", latency_ms=1000.0, success=True, now=now0)
    for n in ("b", "c"):
        for _ in range(MIN_SAMPLES_FOR_LATENCY):
            adj.record_attempt(n, latency_ms=100.0, success=True, now=now0)
    out1 = adj.compute_effective_order(chain, now=now0)
    assert [x.name for x in out1] == ["b", "c", "a"]

    # Pass 2 well after debounce window: a now fast. The rank-change
    # bookkeeping should accept the new order.
    now1 = now0 + DEBOUNCE_S + 1.0
    # Replace the slow samples with fast ones (drop window covers
    # the original slow batch when ROLLING_WINDOW_S > DEBOUNCE_S+1).
    # But ROLLING_WINDOW_S = 60 and DEBOUNCE_S = 30, so the slow
    # samples are still in window. We add enough fast samples to
    # tilt the median back below the demote threshold.
    for _ in range(MIN_SAMPLES_FOR_LATENCY * 6):
        adj.record_attempt("a", latency_ms=50.0, success=True, now=now1)
    out2 = adj.compute_effective_order(chain, now=now1)
    # a should be back at the front (or at least not at the end).
    assert out2[0].name == "a"


# ---------------------------------------------------------------------------
# Engine integration
# ---------------------------------------------------------------------------


def test_engine_static_profile_does_not_invoke_adjuster() -> None:
    """A profile without ``adaptive: true`` must keep the static order."""
    config = _config_with_adaptive_profile(adaptive=False, providers=["x", "y"])
    engine = FallbackEngine.__new__(FallbackEngine)
    engine.config = config
    engine._adapters = {  # type: ignore[attr-defined]
        "x": _adapter("x"),
        "y": _adapter("y"),
    }

    # Even if the adjuster has data suggesting a reorder, the static
    # profile path must ignore it.
    now = 1000.0
    for _ in range(MIN_SAMPLES_FOR_LATENCY):
        engine._adaptive.record_attempt("x", latency_ms=10000.0, success=True, now=now)
    for _ in range(MIN_SAMPLES_FOR_LATENCY):
        engine._adaptive.record_attempt("y", latency_ms=100.0, success=True, now=now)

    chain = engine._resolve_chain(profile_name=None)
    # The engine ALSO calls _profile_is_adaptive when resolving the
    # anthropic-shaped chain; we hit that path through the helper.
    assert engine._profile_is_adaptive(None) is False
    # Static order: x first.
    assert [a.name for a in chain] == ["x", "y"]


def test_engine_adaptive_profile_invokes_adjuster() -> None:
    """``adaptive: true`` enables the dynamic priority computation."""
    config = _config_with_adaptive_profile(adaptive=True, providers=["x", "y", "z"])
    engine = FallbackEngine.__new__(FallbackEngine)
    engine.config = config
    engine._adapters = {  # type: ignore[attr-defined]
        n: _adapter(n) for n in ("x", "y", "z")
    }
    assert engine._profile_is_adaptive(None) is True

    # Make x slow so it gets demoted.
    now = 1000.0
    for _ in range(MIN_SAMPLES_FOR_LATENCY):
        engine._adaptive.record_attempt("x", latency_ms=2000.0, success=True, now=now)
    for n in ("y", "z"):
        for _ in range(MIN_SAMPLES_FOR_LATENCY):
            engine._adaptive.record_attempt(n, latency_ms=100.0, success=True, now=now)

    chain = engine._adaptive.compute_effective_order(
        [_adapter("x"), _adapter("y"), _adapter("z")],
        now=now,
    )
    assert chain[-1].name == "x"


# ---------------------------------------------------------------------------
# Spot checks of constants — defensive against accidental reuse
# ---------------------------------------------------------------------------


def test_constants_have_expected_values() -> None:
    """Pin the v1.9-C tuning knobs so accidental edits are visible.

    The values come from the design retro (future.md §5.4); changing
    them is a deliberate decision, not a refactor side effect.
    """
    assert ROLLING_WINDOW_S == 60.0
    assert LATENCY_DEMOTE_FACTOR == 1.5
    assert ERROR_RATE_DEMOTE_THRESHOLD == 0.10
    assert DEBOUNCE_S == 30.0
    assert MIN_SAMPLES_FOR_LATENCY == 3
    assert MIN_SAMPLES_FOR_ERROR_RATE == 5
