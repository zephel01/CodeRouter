"""In-memory metrics collector — a ``logging.Handler`` that taps structured logs.

Design (plan.md §12.3):
    - v1.5-A ships the collector + ``/metrics.json`` endpoint only.
      Prometheus exposition, JSONL persistence, CLI TUI, and HTML dashboard
      land in v1.5-B/C/D.
    - Collection is a **pure log tap**: we install ourselves on the root
      logger via :func:`install_collector` and inspect each ``LogRecord``.
      Adapter / routing code stays untouched — v0.5's ``capability-
      degraded`` gate, v0.6's ``chain-paid-gate-blocked`` warn, v0.7's
      ``output-filter-applied`` info line are all already structured with
      typed extras, so the collector just dispatches on ``record.msg``.
    - Storage is in-memory only: counters (``collections.Counter``),
      per-provider last-error snapshots, and a fixed-size ``deque`` of
      recent events. Re-start clears the state — JSONL persistence lands
      in v1.5-B (``CODEROUTER_EVENTS_PATH``).

Thread safety
    ``logging.Handler.emit`` can be invoked from any thread (Python's
    logging module acquires ``handler.lock`` itself). We additionally
    guard the mutable state with an ``RLock`` so ``snapshot()`` — which
    the FastAPI event loop calls — sees a consistent view. The lock is
    held only for the small mutation sites.

Event inventory (dispatch table in :meth:`MetricsCollector._dispatch`)
    ``try-provider``             → ``requests_total`` + ``provider_attempts``
    ``provider-ok``              → ``provider_outcomes[provider]["ok"]``
    ``provider-failed``          → ``provider_outcomes[provider]["failed"]``
                                   + last_error[provider]
    ``provider-failed-midstream``→ ``provider_outcomes[...]["failed_midstream"]``
    ``skip-paid-provider``       → ``provider_skipped_paid``
    ``skip-unknown-provider``    → ``provider_skipped_unknown``
    ``capability-degraded``      → ``capability_degraded[capability]``
    ``output-filter-applied``    → ``output_filter_applied[filter]``
    ``chain-paid-gate-blocked``  → ``chain_paid_gate_blocked_total``
    ``chain-uniform-auth-failure``→ ``chain_uniform_auth_failure_total``
    ``auto-router-fallthrough``  → ``auto_router_fallthrough_total``
    ``cache-observed`` (v1.9-A)  → ``cache_*`` per-provider counters
                                   (read tokens, creation tokens,
                                    outcome 4-class breakdown)
    ``coderouter-startup``       → ``startup_info`` (stored for the UI header)

    Unrecognized events are ignored (forward-compat: adding a new log
    event never breaks the collector).
"""

from __future__ import annotations

import contextlib
import logging
import os
import threading
import time
from collections import Counter, deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from coderouter.logging import JsonLineFormatter

# Default ring-buffer size. Chosen to match a ~2-second refresh at 100 RPS
# without blowing memory; overridable via the :class:`MetricsCollector`
# constructor for tests.
_DEFAULT_RING_SIZE: Final[int] = 256

# Truncate ``error`` strings stored in the last-error snapshot. The raw
# log already truncates at 500 chars; 200 is plenty for dashboard display
# and keeps the snapshot dict small when many providers have errors.
_LAST_ERROR_MAX_CHARS: Final[int] = 200


def _utc_now_iso() -> str:
    """Current UTC time as ``YYYY-MM-DDTHH:MM:SS`` (no microseconds, no TZ suffix).

    Matches the format :class:`coderouter.logging.JsonLineFormatter` uses
    for its ``ts`` field, so the recent-events ring reads the same way as
    the stderr log stream.
    """
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")


class MetricsCollector(logging.Handler):
    """``logging.Handler`` subclass that aggregates metrics from log records.

    One instance per process (see :func:`install_collector`). Thread-safe
    mutation of the internal counters/ring buffer via ``self._lock``.
    ``emit()`` is the hot path — it runs on every log record — so it
    stays branchless outside the event dispatch table.
    """

    def __init__(self, *, ring_size: int = _DEFAULT_RING_SIZE) -> None:
        """Construct an empty collector.

        ``ring_size`` is the maximum number of recent events retained for
        the dashboard's "Recent Requests" panel. Older events roll off
        FIFO. Default 256 balances "enough history for a human glance" vs
        memory; tests pass smaller values to keep assertions tight.
        """
        super().__init__(level=logging.DEBUG)
        self._lock = threading.RLock()
        self._started_monotonic: float = time.monotonic()
        self._started_at: str = _utc_now_iso()

        # Counters — monotone, process-lifetime.
        self._requests_total: int = 0
        self._provider_attempts: Counter[str] = Counter()
        # nested: provider -> outcome -> count
        self._provider_outcomes: dict[str, Counter[str]] = {}
        self._provider_skipped_paid: Counter[str] = Counter()
        self._provider_skipped_unknown: Counter[str] = Counter()
        self._capability_degraded: Counter[str] = Counter()
        self._output_filter_applied: Counter[str] = Counter()
        self._chain_paid_gate_blocked_total: int = 0
        self._chain_uniform_auth_failure_total: int = 0
        # v1.6-B: classifier ran, no user rule matched, and the
        # ``default_rule_profile`` was returned instead. Surfaced as its own
        # Prometheus counter so operators can watch the fall-through rate as
        # a stability signal on custom rulesets.
        self._auto_router_fallthrough_total: int = 0

        # v1.9-A: cache observability. Per-provider token totals + 4-class
        # outcome counters. The 4-class split avoids the LiteLLM
        # cache_creation_input_tokens undercounting bug (future.md §3) by
        # keeping "no_cache" (request lacked / lost cache_control) distinct
        # from "unknown" (response carried no usage at all).
        self._cache_read_tokens: Counter[str] = Counter()
        self._cache_creation_tokens: Counter[str] = Counter()
        # nested: provider -> outcome -> count, where outcome is one of
        # CacheOutcome (cache_hit / cache_creation / no_cache / unknown).
        self._cache_outcomes: dict[str, Counter[str]] = {}
        # Aggregate (sum across providers) — kept inline so snapshot()
        # doesn't have to re-fold every read. Cheap to maintain on every
        # event; expensive to recompute on every /metrics.json scrape.
        self._cache_read_tokens_total: int = 0
        self._cache_creation_tokens_total: int = 0

        # Last-error snapshot per provider (overwrites previous). Enables the
        # dashboard's "last error" column without scanning the ring.
        self._last_error: dict[str, dict[str, Any]] = {}

        # Recent events ring. Each entry is a flat dict ready for JSON.
        self._recent: deque[dict[str, Any]] = deque(maxlen=ring_size)

        # Populated by coderouter-startup — lets /metrics.json surface
        # "which providers does this server know about" without re-reading
        # YAML.
        self._startup_info: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Handler API
    # ------------------------------------------------------------------

    def emit(self, record: logging.LogRecord) -> None:
        """Dispatch a log record into the counter/ring updates.

        Unknown event names are silently ignored so adding a new log line
        elsewhere in the codebase never breaks metrics. Exceptions inside
        dispatch are swallowed via :meth:`handleError` per the Handler
        contract (we must never let metrics blow up a log call).
        """
        try:
            self._dispatch(record)
        except Exception:  # pragma: no cover - defensive
            self.handleError(record)

    def _dispatch(self, record: logging.LogRecord) -> None:
        """Event name → counter/ring mutation. Called under ``self._lock``."""
        event = record.msg
        if not isinstance(event, str):
            return
        extras = record.__dict__
        with self._lock:
            if event == "try-provider":
                self._requests_total += 1
                provider = _str(extras.get("provider"))
                self._provider_attempts[provider] += 1
                self._push_recent(event, extras, record)
            elif event == "provider-ok":
                provider = _str(extras.get("provider"))
                self._provider_outcomes.setdefault(provider, Counter())["ok"] += 1
                self._push_recent(event, extras, record)
            elif event == "provider-failed":
                provider = _str(extras.get("provider"))
                self._provider_outcomes.setdefault(provider, Counter())["failed"] += 1
                self._last_error[provider] = _make_last_error(extras, record)
                self._push_recent(event, extras, record)
            elif event == "provider-failed-midstream":
                provider = _str(extras.get("provider"))
                self._provider_outcomes.setdefault(provider, Counter())[
                    "failed_midstream"
                ] += 1
                self._last_error[provider] = _make_last_error(extras, record)
                self._push_recent(event, extras, record)
            elif event == "skip-paid-provider":
                provider = _str(extras.get("provider"))
                self._provider_skipped_paid[provider] += 1
            elif event == "skip-unknown-provider":
                provider = _str(extras.get("provider"))
                self._provider_skipped_unknown[provider] += 1
            elif event == "capability-degraded":
                dropped = extras.get("dropped") or []
                if isinstance(dropped, list):
                    for cap in dropped:
                        if isinstance(cap, str):
                            self._capability_degraded[cap] += 1
            elif event == "output-filter-applied":
                filters = extras.get("filters") or []
                if isinstance(filters, list):
                    for name in filters:
                        if isinstance(name, str):
                            self._output_filter_applied[name] += 1
            elif event == "chain-paid-gate-blocked":
                self._chain_paid_gate_blocked_total += 1
            elif event == "chain-uniform-auth-failure":
                self._chain_uniform_auth_failure_total += 1
            elif event == "auto-router-fallthrough":
                # Every call into ``classify()`` that exits via the
                # default-rule branch (no user/bundled rule matched, or
                # ``auto_router.disabled: true``) bumps this counter.
                self._auto_router_fallthrough_total += 1
            elif event == "cache-observed":
                # v1.9-A: per-provider cache token + 4-class outcome.
                # Defensive int-coerce — log extras are typed via
                # CacheObservedPayload at the source but the handler
                # contract still lets us receive anything, and we never
                # want a malformed log line to crash the metrics tap.
                provider = _str(extras.get("provider"))
                read_raw = extras.get("cache_read_input_tokens", 0)
                creation_raw = extras.get("cache_creation_input_tokens", 0)
                read = read_raw if isinstance(read_raw, int) else 0
                creation = creation_raw if isinstance(creation_raw, int) else 0
                outcome = _str(extras.get("outcome"))
                self._cache_read_tokens[provider] += read
                self._cache_creation_tokens[provider] += creation
                self._cache_read_tokens_total += read
                self._cache_creation_tokens_total += creation
                if outcome:
                    self._cache_outcomes.setdefault(provider, Counter())[outcome] += 1
            elif event == "coderouter-startup":
                # Snapshot a subset — startup payload contains lists that are
                # safe to surface to /metrics.json. Version / providers /
                # profiles / default_profile is all the dashboard needs.
                self._startup_info = {
                    "version": _str(extras.get("version")),
                    "providers": list(extras.get("providers") or []),
                    "profiles": list(extras.get("profiles") or []),
                    "default_profile": _str(extras.get("default_profile")),
                    "allow_paid": bool(extras.get("allow_paid")),
                    "mode_source": _str(extras.get("mode_source")),
                }

    def _push_recent(
        self, event: str, extras: dict[str, Any], record: logging.LogRecord
    ) -> None:
        """Append a minimal record to the ring buffer.

        We keep the shape flat and only surface whitelisted fields — this
        is what the dashboard renders, and avoids leaking transient log
        attributes (``msecs``, ``thread``, etc.) that would just bloat
        the payload.
        """
        entry: dict[str, Any] = {
            "ts": _record_ts_iso(record),
            "event": event,
        }
        for key in ("provider", "stream", "status", "retryable"):
            if key in extras and extras[key] is not None:
                entry[key] = extras[key]
        self._recent.append(entry)

    # ------------------------------------------------------------------
    # Read API
    # ------------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """Return the current metrics as a JSON-safe dict.

        Shape is stable within v1.5 (may evolve with a semver-compatible
        additive bump). Keys absent from one install but present in
        another just mean "that event never fired this process lifetime".
        """
        with self._lock:
            providers = sorted(
                set(self._provider_attempts)
                | set(self._provider_outcomes)
                | set(self._last_error)
                | set(self._cache_read_tokens)
                | set(self._cache_creation_tokens)
                | set(self._cache_outcomes)
            )
            provider_rows = [
                {
                    "name": name,
                    "attempts": self._provider_attempts.get(name, 0),
                    "outcomes": dict(self._provider_outcomes.get(name, Counter())),
                    "last_error": self._last_error.get(name),
                    # v1.9-A: per-row cache panel. ``hit_rate`` is None
                    # rather than 0.0 when no observations have happened
                    # yet — keeps the dashboard from rendering a flat 0%
                    # for an idle provider that never had a chance to be
                    # measured.
                    "cache": _make_cache_row(
                        name,
                        self._cache_outcomes.get(name, Counter()),
                        self._cache_read_tokens.get(name, 0),
                        self._cache_creation_tokens.get(name, 0),
                    ),
                }
                for name in providers
            ]
            return {
                "uptime_s": round(time.monotonic() - self._started_monotonic, 3),
                "started_at": self._started_at,
                "startup": dict(self._startup_info),
                "counters": {
                    "requests_total": self._requests_total,
                    "chain_paid_gate_blocked_total": self._chain_paid_gate_blocked_total,
                    "chain_uniform_auth_failure_total": self._chain_uniform_auth_failure_total,
                    "auto_router_fallthrough_total": self._auto_router_fallthrough_total,
                    # v1.9-A: aggregate cache totals (sum across providers).
                    # Per-provider numbers live on the provider rows.
                    "cache_read_tokens_total": self._cache_read_tokens_total,
                    "cache_creation_tokens_total": self._cache_creation_tokens_total,
                    "provider_attempts": dict(self._provider_attempts),
                    "provider_outcomes": {
                        name: dict(counter)
                        for name, counter in self._provider_outcomes.items()
                    },
                    "provider_skipped_paid": dict(self._provider_skipped_paid),
                    "provider_skipped_unknown": dict(self._provider_skipped_unknown),
                    "capability_degraded": dict(self._capability_degraded),
                    "output_filter_applied": dict(self._output_filter_applied),
                    # v1.9-A: per-provider cache token totals + outcome
                    # breakdown. The Prometheus exposition flattens these
                    # into ``coderouter_cache_*_total{provider,...}``.
                    "cache_read_tokens": dict(self._cache_read_tokens),
                    "cache_creation_tokens": dict(self._cache_creation_tokens),
                    "cache_outcomes": {
                        name: dict(counter)
                        for name, counter in self._cache_outcomes.items()
                    },
                },
                "providers": provider_rows,
                "recent": list(self._recent),
            }

    # ------------------------------------------------------------------
    # Test hook
    # ------------------------------------------------------------------

    def reset(self) -> None:
        """Drop all accumulated state. Tests call this between runs.

        Not part of the ingress contract — production code should never
        need to reset live metrics; a bounce of the process is the right
        seam when operators want a clean slate.
        """
        with self._lock:
            self._requests_total = 0
            self._provider_attempts.clear()
            self._provider_outcomes.clear()
            self._provider_skipped_paid.clear()
            self._provider_skipped_unknown.clear()
            self._capability_degraded.clear()
            self._output_filter_applied.clear()
            self._chain_paid_gate_blocked_total = 0
            self._chain_uniform_auth_failure_total = 0
            self._auto_router_fallthrough_total = 0
            # v1.9-A
            self._cache_read_tokens.clear()
            self._cache_creation_tokens.clear()
            self._cache_outcomes.clear()
            self._cache_read_tokens_total = 0
            self._cache_creation_tokens_total = 0
            self._last_error.clear()
            self._recent.clear()
            self._startup_info.clear()
            self._started_monotonic = time.monotonic()
            self._started_at = _utc_now_iso()


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_collector_lock: Final[threading.RLock] = threading.RLock()
_collector: MetricsCollector | None = None

# v1.5-B: optional JSONL mirror. Env-gated via ``CODEROUTER_EVENTS_PATH``.
# Stored as a module global so ``uninstall_collector`` can detach it in
# tandem with the MetricsCollector (keeps test isolation honest).
_JSONL_ENV_VAR: Final[str] = "CODEROUTER_EVENTS_PATH"
_jsonl_handler: logging.FileHandler | None = None


def install_collector(*, ring_size: int = _DEFAULT_RING_SIZE) -> MetricsCollector:
    """Attach a :class:`MetricsCollector` to the root logger. Idempotent.

    Called from :func:`coderouter.ingress.app.create_app` at lifespan
    startup. Subsequent calls return the same instance — so tests that
    build multiple FastAPI apps don't stack duplicate handlers. The
    handler is installed alongside the existing
    :class:`JsonLineFormatter` stderr handler; logging to stderr
    continues unchanged.

    v1.5-B side-effect: when ``$CODEROUTER_EVENTS_PATH`` is set at install
    time, a JSONL mirror handler is attached too (see
    :func:`_install_jsonl_mirror`). The env is read once; operators who
    want to toggle mid-process must restart — which matches the "restart
    to reset" policy the rest of the lifecycle follows.
    """
    global _collector
    with _collector_lock:
        if _collector is None:
            _collector = MetricsCollector(ring_size=ring_size)
            logging.getLogger().addHandler(_collector)
            _install_jsonl_mirror()
        return _collector


def get_collector() -> MetricsCollector:
    """Return the installed collector, auto-installing if absent.

    Allows ``/metrics.json`` to respond even when the ingress lifespan
    hasn't fired yet (e.g. inside FastAPI TestClient where the lifespan
    is async and may not have run before the first request). The auto-
    install is equivalent to an explicit ``install_collector()`` call.
    """
    return install_collector()


def uninstall_collector() -> None:
    """Detach the collector from the root logger. Tests use this for isolation.

    Clears the module-level singleton so the next :func:`install_collector`
    builds a fresh instance. Not called from production code — a process
    restart is the right seam there. The JSONL mirror (v1.5-B) is
    detached and closed in the same call so file handles don't leak
    between tests.
    """
    global _collector
    with _collector_lock:
        if _collector is not None:
            with contextlib.suppress(ValueError):  # pragma: no cover - already detached
                logging.getLogger().removeHandler(_collector)
            _collector = None
        _uninstall_jsonl_mirror()


def _install_jsonl_mirror() -> None:
    """Attach a JSONL file handler if ``$CODEROUTER_EVENTS_PATH`` is set.

    Read once at install time. The handler uses the same
    :class:`JsonLineFormatter` as the stderr handler, so the mirror file
    is byte-for-byte equivalent to what the operator sees on stderr
    (except for the file rotation policy, which is delegated to external
    ``logrotate`` — stdlib's ``RotatingFileHandler`` was rejected as
    extra complexity for v1.5-B per plan.md §12.3.3).

    Path expansion:
        - ``~`` is expanded via :func:`os.path.expanduser`.
        - Parent directories are created if missing (``parents=True``).

    Idempotency is enforced at the outer :func:`install_collector` seam
    (the module-level ``_collector`` check); this helper assumes a clean
    slate at call time.
    """
    global _jsonl_handler
    raw_path = os.environ.get(_JSONL_ENV_VAR, "").strip()
    if not raw_path:
        return
    path = Path(os.path.expanduser(raw_path))
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(path, mode="a", encoding="utf-8", delay=True)
    handler.setFormatter(JsonLineFormatter())
    handler.setLevel(logging.DEBUG)
    logging.getLogger().addHandler(handler)
    _jsonl_handler = handler


def _uninstall_jsonl_mirror() -> None:
    """Detach + close the JSONL handler if one is attached.

    Called by :func:`uninstall_collector` for test isolation. Safe to
    call when no handler is attached (no-op).
    """
    global _jsonl_handler
    if _jsonl_handler is None:
        return
    with contextlib.suppress(ValueError):  # pragma: no cover - already detached
        logging.getLogger().removeHandler(_jsonl_handler)
    with contextlib.suppress(Exception):  # pragma: no cover - best-effort cleanup
        _jsonl_handler.close()
    _jsonl_handler = None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _str(value: Any) -> str:
    """Coerce a possibly-``None`` log extra to a string.

    Log extras are typed ``str`` by convention (see
    :class:`coderouter.logging.CapabilityDegradedPayload` and friends),
    but the handler contract lets us receive anything — coerce
    defensively so counter keys stay hashable ``str``.
    """
    return "" if value is None else str(value)


def _record_ts_iso(record: logging.LogRecord) -> str:
    """Format the record's timestamp in the same shape as JsonLineFormatter.

    Uses the record's ``created`` epoch-seconds instead of calling
    ``datetime.now()`` so the recent-events ring and the stderr log line
    for the same event carry identical timestamps.
    """
    return datetime.fromtimestamp(record.created, tz=UTC).strftime(
        "%Y-%m-%dT%H:%M:%S"
    )


def _make_last_error(extras: dict[str, Any], record: logging.LogRecord) -> dict[str, Any]:
    """Build the per-provider last-error snapshot.

    Trims the error message to ``_LAST_ERROR_MAX_CHARS`` (the raw log
    already truncates at 500, but dashboard real estate is tight).
    """
    error_text = _str(extras.get("error"))
    if len(error_text) > _LAST_ERROR_MAX_CHARS:
        error_text = error_text[:_LAST_ERROR_MAX_CHARS] + "…"
    status = extras.get("status")
    retryable = extras.get("retryable")
    return {
        "ts": _record_ts_iso(record),
        "status": status if isinstance(status, int) else None,
        "retryable": bool(retryable) if retryable is not None else None,
        "error": error_text,
    }


def _make_cache_row(
    _name: str,
    outcomes: Counter[str],
    read_tokens: int,
    creation_tokens: int,
) -> dict[str, Any]:
    """Build the per-provider cache panel for the snapshot.

    Hit rate is computed as ``cache_hit / (cache_hit + cache_creation +
    no_cache)`` — ``unknown`` is excluded from the denominator because
    the response carried no usage at all (counting it would dilute the
    rate with provider/configuration noise rather than actual cache
    behavior).

    Returns ``None``-bearing fields when no observations exist yet, so
    the dashboard can render "—" rather than a deceptive 0%.
    """
    total_observed = (
        outcomes.get("cache_hit", 0)
        + outcomes.get("cache_creation", 0)
        + outcomes.get("no_cache", 0)
    )
    if total_observed == 0:
        hit_rate: float | None = None
    else:
        hit_rate = round(outcomes.get("cache_hit", 0) / total_observed, 4)
    return {
        "read_tokens": read_tokens,
        "creation_tokens": creation_tokens,
        "outcomes": dict(outcomes),
        "hit_rate": hit_rate,
        "observations": (
            sum(outcomes.values()) if outcomes else 0
        ),
    }
