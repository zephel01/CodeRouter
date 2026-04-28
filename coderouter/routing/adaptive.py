"""Adaptive routing — health-based dynamic chain priority (v1.9-C).

Bridges the static profile chain (operator-declared in providers.yaml)
with live observed health (latency / error rate) so a normally-fast
provider that's currently slow gets temporarily demoted, and a slow
provider that's currently fast gets nothing — order-preservation is
the default.

Where this fits relative to L5 (v1.9-E)
=======================================

  * **L5 backend health** is *binary* (HEALTHY / UNHEALTHY). It swaps
    a crashed provider out of the chain entirely until it recovers.
  * **C adaptive** is *gradient* (continuous latency + error-rate
    deltas). It reorders within the still-functioning set during
    normal operation, when no provider is hard-down.

Both consume the same per-provider observation stream
(``record_attempt``); the dispatch logic differs.

Design constraints (docs/inside/future.md §5.4)
================================================

  1. **Opt-in per profile** — ``FallbackChain.adaptive: bool = False``.
     The default behavior is unchanged: static chain ordering wins
     and adaptive doesn't even instantiate. Operators who want it
     toggle the flag on profiles where chain reorders are tolerable
     (typically ``coding`` / ``general``; almost never on profiles
     that route to a paid endpoint where cost is the override).
  2. **Order preservation by default** — when no provider crosses a
     threshold, the static chain wins. Adaptive should add zero
     surprise during normal operation.
  3. **Debounced demotions** — a single provider can only be re-
     prioritized once per 30 s. Without this the chain flips on
     every transient blip and the fallback semantics get muddled
     (especially with health-streak reporting on ``provider-failed``).
  4. **Stable demotion math** — when multiple providers cross a
     threshold simultaneously, the resulting order is deterministic
     (same input → same output across calls) so debugging stays sane.

Threshold defaults
==================

  * ``latency_factor = 1.5`` — provider's median latency must be
    ≥ 1.5x the global median to demote 1 step. The reference is the
    median across all *capable* providers in this profile; a
    1-provider chain effectively disables latency-based demotion
    (everyone is "fast" by definition).
  * ``error_rate_threshold = 0.10`` — 10% rolling-window error rate
    triggers a 2-step demotion. The threshold is intentionally lax
    (10% is "noticeably bad" for a backend, not "transiently shaky")
    to avoid demoting on a single rate-limit spike.
  * ``rolling_window_s = 60`` — observations older than 60 s are
    dropped. 60 s matches the typical Claude Code agent step cadence
    while staying responsive to backend regressions.

The thresholds are constants in this module rather than profile
fields because they're system-wide tuning knobs that operators
should rarely touch — the 1.5x / 10% / 60 s tuple comes from the
v1.9 design retro and is documented in future.md §5.4 for review.
"""

from __future__ import annotations

import statistics
import threading
import time
from collections import deque
from dataclasses import dataclass, field

from coderouter.adapters.base import BaseAdapter
from coderouter.logging import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ROLLING_WINDOW_S: float = 60.0
"""Observations older than this drop out of the window."""

LATENCY_DEMOTE_FACTOR: float = 1.5
"""Median latency at or above ``LATENCY_DEMOTE_FACTOR x global_median``
demotes the provider one rank."""

ERROR_RATE_DEMOTE_THRESHOLD: float = 0.10
"""Rolling-window error rate at or above this demotes two ranks."""

DEBOUNCE_S: float = 30.0
"""Minimum seconds between rank changes for a single provider.

Without this, a chain can oscillate every request when a provider's
median latency drifts back and forth across the threshold. The
debounce window is intentionally larger than ``ROLLING_WINDOW_S /
2`` so the rolling sample has time to refresh before reconsideration.
"""

MIN_SAMPLES_FOR_LATENCY: int = 3
"""Require at least this many successful samples before considering
latency-based demotion. Below this the median is too noisy to rely on."""

MIN_SAMPLES_FOR_ERROR_RATE: int = 5
"""Require at least this many total samples before considering
error-rate demotion. Below this the rate is too noisy."""


# ---------------------------------------------------------------------------
# Per-provider rolling stats
# ---------------------------------------------------------------------------


@dataclass
class _ProviderObservation:
    """One attempt observation."""

    ts_monotonic: float
    latency_ms: float | None
    """``None`` for failures whose total wall time isn't a useful
    latency signal (auth failure / transport error). Successful and
    timeout-flavored failures both carry a number."""

    success: bool


@dataclass
class ProviderStats:
    """Aggregated stats for one provider over the rolling window.

    Computed lazily; consumers ask for ``median_latency_ms`` /
    ``error_rate`` and the adjuster recomputes them at call time.
    """

    median_latency_ms: float | None
    error_rate: float
    """Failures / total samples in the window. ``0.0`` when no
    samples (so a fresh provider isn't marked unhealthy by default)."""

    sample_count: int
    """Total observations in the window — used to gate demotions
    behind ``MIN_SAMPLES_FOR_*``."""


# ---------------------------------------------------------------------------
# AdaptiveAdjuster — main public class
# ---------------------------------------------------------------------------


@dataclass
class _AdjusterState:
    """Mutable per-provider state inside the adjuster.

    Held in a dict keyed by provider name. Each entry carries the
    rolling observation buffer plus debounce bookkeeping.
    """

    observations: deque[_ProviderObservation] = field(default_factory=deque)
    last_rank_change_ts: float = 0.0
    """Monotonic timestamp of the last time this provider's effective
    rank changed. ``0.0`` (the epoch) means "never changed", so any
    new change is allowed immediately on the first divergence."""

    last_committed_rank: int | None = None
    """Effective-order rank produced by the most recent
    :meth:`AdaptiveAdjuster.compute_effective_order` call. ``None``
    until the first call. Debounce compares the new proposed rank
    against this value, not against the static-chain rank, so a
    flip in either direction (demote → promote OR promote → demote)
    is suppressed within the debounce window."""


class AdaptiveAdjuster:
    """Per-process adaptive routing engine.

    Single instance owned by the FallbackEngine; thread-safe via an
    internal lock so concurrent ingress requests can record + read
    without corrupting the rolling buffers.

    The adjuster is intentionally **stateless across process restarts**
    — observations live in memory and reset on bounce. This keeps
    behavior predictable (no surprise from a stale on-disk window
    that disagrees with the current chain) and avoids a persistence
    surface that would have to evolve with rolling-window tuning.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._state: dict[str, _AdjusterState] = {}

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def record_attempt(
        self,
        provider: str,
        *,
        latency_ms: float | None,
        success: bool,
        now: float | None = None,
    ) -> None:
        """Append one observation for ``provider``.

        ``latency_ms`` is the wall-clock duration the engine measured
        from "send request" to "received response (or error)". For
        auth failures / immediate transport errors where the latency
        carries no useful signal, callers may pass ``None`` and the
        observation contributes only to the error-rate counter, not
        the latency median.

        ``now`` is the monotonic timestamp; defaulted via
        :func:`time.monotonic` so production callers don't need to
        supply it. Tests inject deterministic values.
        """
        ts = now if now is not None else time.monotonic()
        with self._lock:
            entry = self._state.setdefault(provider, _AdjusterState())
            entry.observations.append(
                _ProviderObservation(
                    ts_monotonic=ts,
                    latency_ms=latency_ms,
                    success=success,
                )
            )
            # Prune old observations eagerly on append. Cheap (deque
            # popleft) and keeps the buffer bounded without a
            # background reaper.
            self._prune(entry, now=ts)

    def _prune(self, entry: _AdjusterState, *, now: float) -> None:
        """Drop observations older than ``ROLLING_WINDOW_S`` from the head."""
        cutoff = now - ROLLING_WINDOW_S
        while entry.observations and entry.observations[0].ts_monotonic < cutoff:
            entry.observations.popleft()

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def stats_for(self, provider: str, *, now: float | None = None) -> ProviderStats:
        """Compute current rolling stats for ``provider``.

        Returns zero-sample defaults when the provider has never been
        seen — equivalent to a fresh, healthy provider. Callers that
        want to distinguish "never seen" from "seen and healthy" can
        check ``sample_count == 0``.
        """
        ts = now if now is not None else time.monotonic()
        with self._lock:
            entry = self._state.get(provider)
            if entry is None:
                return ProviderStats(
                    median_latency_ms=None,
                    error_rate=0.0,
                    sample_count=0,
                )
            self._prune(entry, now=ts)
            obs = list(entry.observations)

        latencies = [
            o.latency_ms for o in obs if o.latency_ms is not None and o.success
        ]
        median = statistics.median(latencies) if latencies else None
        total = len(obs)
        failures = sum(1 for o in obs if not o.success)
        error_rate = (failures / total) if total else 0.0
        return ProviderStats(
            median_latency_ms=median,
            error_rate=error_rate,
            sample_count=total,
        )

    # ------------------------------------------------------------------
    # Effective ordering
    # ------------------------------------------------------------------

    def compute_effective_order(
        self,
        adapters: list[BaseAdapter],
        *,
        now: float | None = None,
    ) -> list[BaseAdapter]:
        """Return the ``adapters`` list reordered by adaptive demotions.

        Algorithm
        ---------

        1. For each adapter, gather its current ``ProviderStats``.
        2. Compute the global median latency across all providers
           with sufficient samples (``MIN_SAMPLES_FOR_LATENCY``).
           This is the reference point for the latency factor.
        3. For each adapter, compute a **demotion** integer:

              error_rate ≥ ERROR_RATE_DEMOTE_THRESHOLD → +2
              median ≥ global x LATENCY_DEMOTE_FACTOR  → +1

           Both can fire (max +3 demotions). Static-position-stable
           secondary sort means same-demotion entries keep their
           original relative order.
        4. Re-sort: ``key=(demotion, original_index)``. Lower values
           win the front of the chain.
        5. **Debounce**: providers whose effective rank changed within
           the past ``DEBOUNCE_S`` seconds are pinned to their
           previous position. The pinning happens AFTER the sort so
           operators can see "what the algorithm wanted to do" via
           a parallel call to :meth:`stats_for`. v1.9-C MVP applies
           debounce by holding the last-emitted order in process
           memory; restart wipes the debounce window.

        Side effects
        ------------

        Calls :func:`logger.info` with the structured event
        ``adaptive-routing-applied`` whenever the resulting order
        differs from the input order. Stays quiet when the chain is
        unchanged (the common case).

        The function does not mutate the adjuster state itself
        beyond pruning expired observations — debounce is enforced
        via the ``last_rank_change_ts`` field which is updated only
        when the new rank actually shipped.
        """
        ts = now if now is not None else time.monotonic()
        if not adapters:
            return adapters

        # Gather stats + raw demotion values.
        stats_per_provider: dict[str, ProviderStats] = {}
        for adapter in adapters:
            stats_per_provider[adapter.name] = self.stats_for(adapter.name, now=ts)

        # Compute the latency reference from providers with enough samples.
        latency_samples = [
            s.median_latency_ms
            for s in stats_per_provider.values()
            if s.median_latency_ms is not None
            and s.sample_count >= MIN_SAMPLES_FOR_LATENCY
        ]
        global_median = (
            statistics.median(latency_samples) if latency_samples else None
        )

        # Compute proposed demotions per adapter, in original order.
        proposed: list[tuple[BaseAdapter, int, int]] = []  # (adapter, demotion, idx)
        for idx, adapter in enumerate(adapters):
            s = stats_per_provider[adapter.name]
            demotion = 0
            if (
                global_median is not None
                and s.median_latency_ms is not None
                and s.sample_count >= MIN_SAMPLES_FOR_LATENCY
                and s.median_latency_ms >= global_median * LATENCY_DEMOTE_FACTOR
            ):
                demotion += 1
            if (
                s.sample_count >= MIN_SAMPLES_FOR_ERROR_RATE
                and s.error_rate >= ERROR_RATE_DEMOTE_THRESHOLD
            ):
                demotion += 2
            proposed.append((adapter, demotion, idx))

        # Sort by (demotion, original_index) — stable secondary keeps
        # same-bucket adapters in their declared order.
        proposed.sort(key=lambda t: (t[1], t[2]))
        proposed_order = [a for a, _d, _i in proposed]

        # Apply debounce: revert any provider whose rank just changed
        # but whose last_rank_change_ts is too recent.
        with self._lock:
            committed_order = self._apply_debounce(
                static_order=adapters,
                proposed_order=proposed_order,
                now=ts,
            )

        # Emit log when something actually moved.
        if [a.name for a in committed_order] != [a.name for a in adapters]:
            logger.info(
                "adaptive-routing-applied",
                extra={
                    "static_order": [a.name for a in adapters],
                    "effective_order": [a.name for a in committed_order],
                    "stats": {
                        name: {
                            "median_latency_ms": s.median_latency_ms,
                            "error_rate": round(s.error_rate, 4),
                            "sample_count": s.sample_count,
                        }
                        for name, s in stats_per_provider.items()
                    },
                },
            )

        return committed_order

    def _apply_debounce(
        self,
        *,
        static_order: list[BaseAdapter],
        proposed_order: list[BaseAdapter],
        now: float,
    ) -> list[BaseAdapter]:
        """Pin providers whose rank flipped within ``DEBOUNCE_S``.

        Comparison reference:
            For each provider, the "current rank" is the one we
            committed on the most recent call (``last_committed_rank``)
            — *not* the static-chain rank. This way both directions
            of change (demote → promote AND promote → demote) are
            debounced symmetrically: a provider that was demoted
            10 s ago cannot be re-promoted until the window elapses,
            even if its observed health has now bounced back.

            Providers without a previous commit (first call after
            startup, or never reordered) use the static rank as the
            comparison reference, so first-time demotions are
            always allowed.

        Implementation note:
            Two-pass algorithm. We identify all "want-to-revert"
            providers based on debounce, then build the committed
            order honoring those reverts plus the proposed rank for
            the rest. Reverting in-place during the identification
            pass would re-invalidate adjacent positions because the
            rank dict is global to the chain.
        """
        static_rank = {a.name: i for i, a in enumerate(static_order)}
        proposed_rank = {a.name: i for i, a in enumerate(proposed_order)}

        def _reference_rank(name: str) -> int:
            """Rank used for the change-detection comparison."""
            entry = self._state.get(name)
            if entry is not None and entry.last_committed_rank is not None:
                return entry.last_committed_rank
            return static_rank[name]

        # Identify reverts.
        reverts: dict[str, int] = {}  # name -> rank to pin to
        for adapter in proposed_order:
            ref_rank = _reference_rank(adapter.name)
            if proposed_rank[adapter.name] == ref_rank:
                continue  # rank unchanged → no debounce concern
            entry = self._state.setdefault(adapter.name, _AdjusterState())
            if entry.last_rank_change_ts == 0.0:
                # first-ever change is always allowed
                continue
            if now - entry.last_rank_change_ts < DEBOUNCE_S:
                reverts[adapter.name] = ref_rank

        if not reverts:
            # No reverts — commit the proposed order verbatim.
            self._commit_ranks(proposed_order, now=now, static_rank=static_rank)
            return proposed_order

        # Some entries were reverted. Build a new order: reverted
        # entries take their reference rank, others take proposed.
        keyed: list[tuple[BaseAdapter, int]] = []
        for adapter in static_order:
            rank = (
                reverts[adapter.name]
                if adapter.name in reverts
                else proposed_rank[adapter.name]
            )
            keyed.append((adapter, rank))
        keyed.sort(key=lambda t: t[1])
        committed = [a for a, _r in keyed]
        self._commit_ranks(committed, now=now, static_rank=static_rank)
        return committed

    def _commit_ranks(
        self,
        order: list[BaseAdapter],
        *,
        now: float,
        static_rank: dict[str, int],
    ) -> None:
        """Persist the just-committed order's per-provider state.

        Updates ``last_committed_rank`` for every adapter and
        ``last_rank_change_ts`` for those whose rank actually changed
        from their previous reference. Called once per
        :meth:`compute_effective_order` invocation, after debounce
        has resolved.
        """
        for new_rank, adapter in enumerate(order):
            entry = self._state.setdefault(adapter.name, _AdjusterState())
            previous = entry.last_committed_rank
            if previous is None:
                # First commit: only mark a change vs. static rank.
                if new_rank != static_rank[adapter.name]:
                    entry.last_rank_change_ts = now
            elif previous != new_rank:
                entry.last_rank_change_ts = now
            entry.last_committed_rank = new_rank
