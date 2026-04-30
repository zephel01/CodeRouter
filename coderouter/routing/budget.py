"""v1.10: per-provider monthly USD budget tracking.

The :class:`BudgetTracker` is the enforcement counterpart to v1.9-D's
cost dashboard — v1.9-D *observes* spend per provider; v1.10 lets
operators *cap* it via ``providers.cost.monthly_budget_usd`` and have
the chain resolver skip providers that have hit their cap.

Why a standalone tracker
========================

The :class:`coderouter.metrics.collector.MetricsCollector` already
maintains lifetime ``cost_total_usd`` per provider, but the budget
tracker needs:

  1. **Calendar-month windowing** — the dashboard's lifetime totals
     are useful for cost reporting but unsuitable for billing-cycle
     enforcement.
  2. **A simpler API surface** — the engine's chain resolver only
     needs ``is_over_budget`` and ``record``. Reaching into the
     collector's broader counter set would couple the engine to the
     observability infrastructure (which is intentionally an
     observer-only attachment, never a control-plane component).
  3. **Independent locking** — the collector's lock is held during
     event dispatch on every log record. The budget check runs at
     chain-resolution time, well outside the collector's hot path.

So the tracker is a small, focused module with its own lock and a
narrow API.

Persistence
===========

In-memory only. State zeroes out on:

  * Process restart — natural for the 5-deps invariant; persistent
    budget state would mean adding sqlite/disk/Redis dependencies.
  * UTC calendar-month rollover — billing-cycle reset is the whole
    point of "monthly" budget.

Operators who need durable cross-restart budget state can layer
external monitoring on the v1.9-D cost dashboard's
``cost_total_usd`` panel (the lifetime figure persists in the
running collector for the entire process lifetime, which covers
typical multi-day server runs).

Concurrency
===========

The tracker is safe to share across asyncio tasks within one event
loop and across worker threads — every public method holds an
``RLock`` for the body of its read/write. Pure functions
(:meth:`current_month`, :meth:`total_for_provider`) still take the
lock to ensure they observe a consistent month/total pair (the
month-rollover sweep mutates both fields).
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime


def _utc_month_key(now: datetime | None = None) -> str:
    """Return the current UTC calendar-month key as ``"YYYY-MM"``.

    ``now`` is exposed so tests can pin a deterministic month boundary
    without monkey-patching ``datetime.now`` globally. Production calls
    pass ``None`` and the tracker uses the live UTC clock.
    """
    if now is None:
        now = datetime.now(UTC)
    return f"{now.year:04d}-{now.month:02d}"


class BudgetTracker:
    """In-memory per-provider current-month USD running total.

    Public API:

    - :meth:`record(provider, cost_usd)` — fold one attempt's cost
      into the provider's current-month total.
    - :meth:`is_over_budget(provider, budget_usd)` — True iff
      provider's current-month total is ``>=`` budget. Exclusive of
      the boundary on the strict-less branch (i.e. ``>=`` is
      "blocked"); the engine treats ``is_over_budget == True`` as a
      hard skip.
    - :meth:`current_month()` — string YYYY-MM the tracker is
      currently bucketing into. Useful for log payloads and tests.
    - :meth:`total_for_provider(provider)` — the per-provider current-
      month running total in USD.
    - :meth:`reset()` — zero everything immediately. Mainly for tests
      and for a future ``coderouter stats --budget reset`` admin
      command.

    Month rollover is "lazy": every public call observes the current
    UTC month and resets the per-provider dict if the cached month no
    longer matches. There is no background timer.
    """

    def __init__(self) -> None:
        self._lock: threading.RLock = threading.RLock()
        self._month: str = _utc_month_key()
        self._totals: dict[str, float] = {}

    # ------------------------------------------------------------------
    # Mutating API
    # ------------------------------------------------------------------

    def record(
        self,
        provider: str,
        cost_usd: float,
        *,
        now: datetime | None = None,
    ) -> None:
        """Add ``cost_usd`` to ``provider``'s current-month total.

        Negative or zero costs are accepted but contribute nothing —
        the engine never emits negatives, but defensive handling
        keeps a malformed cost from silently corrupting the running
        total. ``now`` is exposed for tests; production passes None.
        """
        if cost_usd <= 0.0:
            return
        with self._lock:
            self._roll_if_needed(now)
            self._totals[provider] = self._totals.get(provider, 0.0) + cost_usd

    def reset(self) -> None:
        """Zero all running totals and re-snap the month to current UTC."""
        with self._lock:
            self._totals.clear()
            self._month = _utc_month_key()

    # ------------------------------------------------------------------
    # Read-only API
    # ------------------------------------------------------------------

    def is_over_budget(
        self,
        provider: str,
        budget_usd: float,
        *,
        now: datetime | None = None,
    ) -> bool:
        """True iff ``provider``'s current-month running total is at or
        above ``budget_usd``.

        ``>=`` (not strict ``>``) so a provider that exactly hits its
        budget is considered exhausted. This matches the conservative
        interpretation: when the operator says "5 USD/month", landing
        at 5.00 is the line — the next call should not bill.

        The boundary check observes the latest UTC month, so a budget
        check across a month boundary correctly resets to a fresh
        bucket and reports the freshly-reset total.
        """
        with self._lock:
            self._roll_if_needed(now)
            return self._totals.get(provider, 0.0) >= budget_usd

    def current_month(self, *, now: datetime | None = None) -> str:
        """Return the active YYYY-MM key, advancing the bucket if needed."""
        with self._lock:
            self._roll_if_needed(now)
            return self._month

    def total_for_provider(
        self, provider: str, *, now: datetime | None = None
    ) -> float:
        """Return ``provider``'s current-month running total (USD)."""
        with self._lock:
            self._roll_if_needed(now)
            return self._totals.get(provider, 0.0)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _roll_if_needed(self, now: datetime | None) -> None:
        """Reset ``_totals`` if the UTC calendar month has changed.

        Caller MUST hold ``self._lock``.
        """
        current = _utc_month_key(now)
        if current != self._month:
            self._totals.clear()
            self._month = current


__all__ = ["BudgetTracker"]
