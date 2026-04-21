"""``coderouter stats`` — CLI / TUI for the metrics endpoint (v1.5-C).

This module is split into three layers so the core logic is testable
without a terminal:

    1. **Data fetch** (:func:`fetch_snapshot`) — stdlib ``urllib`` GET of
       ``/metrics.json``. Returns parsed dict or ``None`` on transport /
       parse error. No curses, no sleeping.
    2. **Pure render layer** (:func:`build_provider_rows`,
       :func:`build_gates_summary`, :func:`build_recent_rows`,
       :func:`format_text`) — canonical ``dict → dataclass / str``
       transforms. No I/O, no side effects — they just take a snapshot
       and produce display-ready shapes.
    3. **Drivers** (:func:`run_once`, :func:`run_tui`) — the actual user-
       facing execution. ``run_once`` prints a single plain-text dump
       (for scripts / non-tty); ``run_tui`` is the curses loop.

Why stdlib-only (no ``rich`` / ``textual``)
    plan.md §12.3.5: dependencies go up with every library we add, and
    the design memo explicitly calls out "stdlib ``curses`` + 1s clear-
    redraw". ``curses`` is in stdlib on macOS and Linux (Windows would
    need ``windows-curses`` but CodeRouter is primarily a local-dev tool
    for Unix shells). ``rich`` adds ~500kB and a dependency, for which
    the payoff — prettier borders — is not worth the 5-dep budget.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

# Matches ``coderouter serve --host 127.0.0.1 --port 4000`` from cli.py.
# A deliberate choice to leave the default localhost-only — remote
# scraping goes through the Prometheus endpoint (v1.5-B), not this TUI.
DEFAULT_URL: Final[str] = "http://127.0.0.1:4000/metrics.json"

# 1-second refresh (design memo §12.3.5). Short enough to feel live, long
# enough that the HTTP overhead is negligible even on busy machines.
DEFAULT_INTERVAL_S: Final[float] = 1.0

# A 2-second HTTP timeout — the endpoint is on localhost, so anything
# slower than that means the server is wedged and the TUI should surface
# that rather than hang.
_FETCH_TIMEOUT_S: Final[float] = 2.0

# Thresholds for the provider-health dot. Derived from "what an operator
# expects of a healthy provider" rather than a statistical definition —
# anything below 95% success merits attention.
_HEALTH_GREEN_MIN_RATE: Final[float] = 0.95
_HEALTH_YELLOW_MIN_RATE: Final[float] = 0.80


# ---------------------------------------------------------------------------
# Structured display types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProviderRow:
    """One row in the "Providers" panel of the TUI.

    Fields map 1:1 to columns shown in the wireframe (§12.3.5.1). The
    ``health`` string is a stable token (``green`` / ``yellow`` / ``red``
    / ``gray``) that the driver maps to a curses color pair — keeping it
    a token rather than an int means tests don't need curses constants.
    """

    name: str
    attempts: int
    ok: int
    failed: int
    failed_midstream: int
    last_error: str  # short one-line description or "-"
    health: str  # "green" | "yellow" | "red" | "gray"

    @property
    def ok_rate_pct(self) -> int:
        """Rounded success percentage (``ok / attempts × 100``).

        Returns 100 when ``attempts`` is zero so the UI shows a neutral
        value rather than ``0%`` for a provider that hasn't been tried.
        """
        if self.attempts <= 0:
            return 100
        return round(self.ok * 100 / self.attempts)


@dataclass(frozen=True)
class GatesSummary:
    """Aggregate counters shown in the "Fallback & Gates" panel.

    Everything is a scalar — this panel is a glance surface ("are any
    gates firing?"), not a drill-down. The per-provider breakdown lives
    in :class:`ProviderRow` and the recent ring.
    """

    total_requests: int
    total_failed: int
    fallback_rate_pct: float  # (failed / total_requests * 100), 0.0 when no reqs
    paid_gate_blocked: int
    degraded_total: int
    degraded_breakdown: dict[str, int]  # capability → count
    filters_applied_total: int
    filters_breakdown: dict[str, int]  # filter name → count


@dataclass(frozen=True)
class RecentRow:
    """One entry from the recent-events ring buffer.

    Driver decides coloring based on ``event`` (e.g. ``provider-failed``
    gets a red cell on the status column).
    """

    ts: str  # "HH:MM:SS" extracted from the full ts
    event: str
    provider: str
    stream: bool | None
    status: int | None  # only populated for provider-failed*
    is_failure: bool

    @property
    def status_text(self) -> str:
        """Short human label for the status column.

        Maps each event to one of: ``ok`` (provider-ok), ``try``
        (try-provider), ``FAIL`` / ``FAIL (<status>)`` for the failed
        variants. The driver uses this in the recent panel cell.
        """
        if self.event == "provider-ok":
            return "ok"
        if self.event == "try-provider":
            return "try"
        if self.event.startswith("provider-failed"):
            return f"FAIL ({self.status})" if self.status is not None else "FAIL"
        return self.event


# ---------------------------------------------------------------------------
# 1. Data fetch
# ---------------------------------------------------------------------------


@dataclass
class FetchError:
    """Structured error from :func:`fetch_snapshot`.

    Carries a short one-line message suitable for the TUI's status bar.
    Separating this from "no data yet" (``None``) lets the driver show
    "connecting…" on first fetch vs. "server down — retrying" on later
    fetches.
    """

    message: str


def fetch_snapshot(
    url: str, *, timeout_s: float = _FETCH_TIMEOUT_S
) -> dict[str, Any] | FetchError:
    """HTTP GET + JSON parse. Pure w.r.t. snapshot dict semantics.

    Returns the parsed dict on success; otherwise a :class:`FetchError`
    with a compact, operator-readable reason. Never raises — the TUI
    loop relies on this swallowing transport hiccups (``ConnRefused``
    when the server is starting, for example) without crashing.
    """
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as resp:  # noqa: S310
            raw = resp.read()
    except urllib.error.URLError as exc:
        return FetchError(f"connection failed: {exc.reason}")
    except TimeoutError:  # pragma: no cover - only reproducible under load
        return FetchError("timeout")
    except Exception as exc:  # pragma: no cover - defensive catch-all
        return FetchError(f"fetch failed: {exc}")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return FetchError(f"invalid JSON: {exc.msg}")
    if not isinstance(data, dict):
        return FetchError("unexpected non-object response")
    return data


# ---------------------------------------------------------------------------
# 2. Pure render layer
# ---------------------------------------------------------------------------


def build_provider_rows(snapshot: dict[str, Any]) -> list[ProviderRow]:
    """Transform the snapshot's providers[] into display rows, sorted by name.

    We sort alphabetically (not by attempts) so re-runs show providers in
    a stable order — operators glancing at the TUI benefit from muscle
    memory ("local is the first row") over a "hottest-first" ordering
    that bounces around.
    """
    rows: list[ProviderRow] = []
    for entry in snapshot.get("providers", []):
        name = str(entry.get("name", ""))
        attempts = int(entry.get("attempts", 0))
        outcomes = entry.get("outcomes", {}) or {}
        ok = int(outcomes.get("ok", 0))
        failed = int(outcomes.get("failed", 0))
        failed_midstream = int(outcomes.get("failed_midstream", 0))

        last_error_raw = entry.get("last_error")
        last_error = _format_last_error(last_error_raw)
        health = _compute_health(attempts=attempts, ok=ok, failed_midstream=failed_midstream)

        rows.append(
            ProviderRow(
                name=name,
                attempts=attempts,
                ok=ok,
                failed=failed,
                failed_midstream=failed_midstream,
                last_error=last_error,
                health=health,
            )
        )
    rows.sort(key=lambda r: r.name)
    return rows


def build_gates_summary(snapshot: dict[str, Any]) -> GatesSummary:
    """Collapse the counters block into the one-glance panel dataclass.

    ``fallback_rate_pct`` is computed as failures / total — when total
    is 0 we return 0.0 (not a 0/0 division) to match the TUI's "no data
    yet" state.
    """
    counters = snapshot.get("counters", {}) or {}
    total_requests = int(counters.get("requests_total", 0))
    # Sum of failed + failed_midstream across all providers.
    total_failed = 0
    for outcomes in (counters.get("provider_outcomes", {}) or {}).values():
        total_failed += int(outcomes.get("failed", 0))
        total_failed += int(outcomes.get("failed_midstream", 0))
    fallback_rate_pct = (
        (total_failed * 100 / total_requests) if total_requests > 0 else 0.0
    )
    degraded_breakdown = dict(counters.get("capability_degraded", {}) or {})
    filters_breakdown = dict(counters.get("output_filter_applied", {}) or {})
    return GatesSummary(
        total_requests=total_requests,
        total_failed=total_failed,
        fallback_rate_pct=fallback_rate_pct,
        paid_gate_blocked=int(counters.get("chain_paid_gate_blocked_total", 0)),
        degraded_total=sum(degraded_breakdown.values()),
        degraded_breakdown=degraded_breakdown,
        filters_applied_total=sum(filters_breakdown.values()),
        filters_breakdown=filters_breakdown,
    )


def build_recent_rows(
    snapshot: dict[str, Any], *, failures_only: bool = False
) -> list[RecentRow]:
    """Transform the recent ring into display rows.

    ``failures_only`` drives the ``[f]`` key binding in the TUI — when
    on, only ``provider-failed*`` events are returned so an operator
    debugging a fallback chain can scroll without noise from the "happy
    path" ok lines.

    v1.5-E: when ``snapshot["config"]["display_timezone"]`` is set to an
    IANA zone name, the ``ts`` column is rendered in that zone. Unset →
    the raw UTC ``HH:MM:SS`` from the ring, matching pre-v1.5-E output.
    A malformed zone (shouldn't happen — the server-side validator
    rejects them — but the snapshot might predate the caller's
    upgrade) silently falls back to UTC so the table still renders.
    """
    config = snapshot.get("config", {}) or {}
    tz_name = config.get("display_timezone")
    target_tz: ZoneInfo | None
    if isinstance(tz_name, str) and tz_name:
        try:
            target_tz = ZoneInfo(tz_name)
        except ZoneInfoNotFoundError:
            target_tz = None
    else:
        target_tz = None

    rows: list[RecentRow] = []
    for entry in snapshot.get("recent", []) or []:
        event = str(entry.get("event", ""))
        is_failure = event.startswith("provider-failed")
        if failures_only and not is_failure:
            continue
        ts_full = str(entry.get("ts", ""))
        ts_display = _format_ts_in_tz(ts_full, target_tz)
        status_raw = entry.get("status")
        stream = entry.get("stream")
        rows.append(
            RecentRow(
                ts=ts_display,
                event=event,
                provider=str(entry.get("provider", "")),
                stream=bool(stream) if isinstance(stream, bool) else None,
                status=int(status_raw) if isinstance(status_raw, int) else None,
                is_failure=is_failure,
            )
        )
    return rows


def _format_ts_in_tz(ts_full: str, tz: ZoneInfo | None) -> str:
    """Render a ``YYYY-MM-DDTHH:MM:SS`` UTC stamp as ``HH:MM:SS`` in ``tz``.

    When ``tz`` is ``None`` (unset / typo / zoneinfo missing), strips the
    date prefix and returns the naive UTC time — this preserves the
    pre-v1.5-E behavior. Anything that fails to parse as an ISO datetime
    also falls back to the naive slice so the column never blanks.
    """
    naive = ts_full.split("T", 1)[-1] if "T" in ts_full else ts_full
    if tz is None or not ts_full:
        return naive
    try:
        dt_utc = datetime.fromisoformat(ts_full).replace(tzinfo=timezone.utc)
    except ValueError:
        return naive
    return dt_utc.astimezone(tz).strftime("%H:%M:%S")


def format_text(snapshot: dict[str, Any], *, width: int = 80) -> str:
    """Render a snapshot as a plain-text dump — used by ``--once`` mode.

    Three blocks mirroring the TUI panels (Providers / Gates / Recent).
    Kept as one function because ``--once`` is supposed to be a single
    atomic output that a shell script can grep without thinking about
    layout — splitting into more functions would gain nothing.
    """
    startup = snapshot.get("startup", {}) or {}
    config = snapshot.get("config", {}) or {}
    profile = str(
        startup.get("default_profile") or config.get("default_profile") or "?"
    )
    uptime_s = float(snapshot.get("uptime_s", 0.0))
    gates = build_gates_summary(snapshot)
    providers = build_provider_rows(snapshot)
    recent = build_recent_rows(snapshot)

    # v1.5-E: surface the configured display TZ next to uptime so a
    # reader of piped ``coderouter stats --once`` output can tell which
    # zone the ``Recent`` column is rendered in. Falls back to ``UTC``
    # when unset, matching the dashboard header convention.
    tz_label = str(config.get("display_timezone") or "UTC")
    lines: list[str] = []
    lines.append(
        f"coderouter stats — profile: {profile}  uptime: {_fmt_uptime(uptime_s)}  "
        f"requests: {gates.total_requests}  tz: {tz_label}"
    )
    lines.append("-" * min(width, 80))
    lines.append("Providers")
    lines.append(
        f"  {'name':<22} {'att':>5} {'ok%':>5} {'failed':>7} {'last error':<25}"
    )
    if not providers:
        lines.append("  (no requests seen yet)")
    for prov in providers:
        lines.append(
            f"  {prov.name:<22} {prov.attempts:>5} {prov.ok_rate_pct:>5} "
            f"{prov.failed + prov.failed_midstream:>7} "
            f"{_truncate(prov.last_error, 25):<25}"
        )
    lines.append("")
    lines.append("Fallback & Gates")
    lines.append(
        f"  fallback rate:         {gates.fallback_rate_pct:5.1f}%  "
        f"({gates.total_failed}/{gates.total_requests})"
    )
    lines.append(f"  paid-gate blocked:     {gates.paid_gate_blocked}")
    lines.append(
        f"  capability degraded:   {gates.degraded_total}"
        + (
            f"  ({_fmt_breakdown(gates.degraded_breakdown)})"
            if gates.degraded_breakdown
            else ""
        )
    )
    lines.append(
        f"  output-filter applied: {gates.filters_applied_total}"
        + (
            f"  ({_fmt_breakdown(gates.filters_breakdown)})"
            if gates.filters_breakdown
            else ""
        )
    )
    lines.append("")
    lines.append("Recent")
    if not recent:
        lines.append("  (no events yet)")
    for rec in recent[-10:]:
        lines.append(
            f"  {rec.ts:<8}  {rec.provider:<22} {rec.status_text}"
        )
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# 3. Drivers
# ---------------------------------------------------------------------------


def run_once(url: str) -> int:
    """Fetch once, print the plain-text dump, exit.

    Also used automatically when stdout is not a TTY so ``coderouter
    stats | grep foo`` works in scripts. Exit code: 0 on success, 2 on
    fetch failure (matches the doctor "needs-tuning" convention of
    "non-fatal but actionable").
    """
    snap = fetch_snapshot(url)
    if isinstance(snap, FetchError):
        print(f"coderouter stats: {snap.message}", file=sys.stderr)
        return 2
    sys.stdout.write(format_text(snap))
    return 0


def run_tui(url: str, *, interval_s: float = DEFAULT_INTERVAL_S) -> int:  # pragma: no cover
    """curses driver. Refreshes every ``interval_s`` seconds.

    Not unit-tested because curses needs a real terminal — we keep this
    function thin (data fetch + :mod:`curses` bookkeeping) and push the
    content construction into :func:`build_provider_rows` &c., which
    ARE tested. Manual QA: ``coderouter stats`` against a running server
    shows the 4-panel layout described in plan.md §12.3.5.1.

    Imports ``curses`` lazily so the test runner on a non-tty CI doesn't
    eagerly import it (avoids a potential ImportError on systems
    without the terminfo lib — Alpine / some minimal containers).
    """
    import curses

    def _driver(stdscr: Any) -> int:
        curses.curs_set(0)
        curses.use_default_colors()
        _init_color_pairs(curses)
        # halfdelay: getch blocks up to N tenths of a second before
        # returning -1. halfdelay(10) → 1-second cap, matching interval.
        curses.halfdelay(max(1, int(interval_s * 10)))

        paused = False
        failures_only = False
        last_snap: dict[str, Any] | FetchError = FetchError("connecting…")
        while True:
            if not paused:
                last_snap = fetch_snapshot(url)
            stdscr.erase()
            height, width = stdscr.getmaxyx()
            if isinstance(last_snap, FetchError):
                _draw_error_screen(curses, stdscr, last_snap, width=width)
            else:
                _draw_frame(
                    curses,
                    stdscr,
                    snapshot=last_snap,
                    width=width,
                    height=height,
                    paused=paused,
                    failures_only=failures_only,
                )
            stdscr.refresh()
            ch = stdscr.getch()
            if ch in (ord("q"), ord("Q")):
                return 0
            if ch in (ord("p"), ord("P")):
                paused = not paused
            if ch in (ord("r"), ord("R")):
                last_snap = fetch_snapshot(url)
            if ch in (ord("f"), ord("F")):
                failures_only = not failures_only
            # KEY_RESIZE handled implicitly: next loop re-reads getmaxyx

    return int(curses.wrapper(_driver))


def main(argv_url: str, *, interval: float, once: bool) -> int:
    """Entry called from :mod:`coderouter.cli`.

    Non-tty stdout → ``--once`` mode regardless of the flag, so piping
    to grep / redirecting to a file works without an extra flag. A
    deliberate user-facing ``--once`` still wins when the shell IS a TTY
    and the caller wants a single snapshot anyway.
    """
    if once or not sys.stdout.isatty():
        return run_once(argv_url)
    return run_tui(argv_url, interval_s=interval)


# ---------------------------------------------------------------------------
# Curses drawing helpers (imported lazily inside run_tui so this module
# stays importable on systems without curses — e.g. windows CI runners).
# ---------------------------------------------------------------------------


_COLOR_GREEN_PAIR = 1
_COLOR_YELLOW_PAIR = 2
_COLOR_RED_PAIR = 3
_COLOR_GRAY_PAIR = 4
_COLOR_DIM_PAIR = 5


def _init_color_pairs(curses: Any) -> None:  # pragma: no cover - curses-only
    """Install the 5 color pairs the TUI uses.

    Paired with :data:`_COLOR_*_PAIR` constants above so the draw
    helpers can reference them without importing ``curses`` at module
    scope. ``use_default_colors`` was already called in the driver, so
    ``-1`` means "terminal default" (honors the user's color scheme).
    """
    curses.init_pair(_COLOR_GREEN_PAIR, curses.COLOR_GREEN, -1)
    curses.init_pair(_COLOR_YELLOW_PAIR, curses.COLOR_YELLOW, -1)
    curses.init_pair(_COLOR_RED_PAIR, curses.COLOR_RED, -1)
    curses.init_pair(_COLOR_GRAY_PAIR, curses.COLOR_WHITE, -1)
    curses.init_pair(_COLOR_DIM_PAIR, -1, -1)


def _color_for_health(curses: Any, token: str) -> int:  # pragma: no cover - curses-only
    """Map a health token (``green``/``yellow``/``red``/``gray``) to a curses attr."""
    if token == "green":
        return int(curses.color_pair(_COLOR_GREEN_PAIR))
    if token == "yellow":
        return int(curses.color_pair(_COLOR_YELLOW_PAIR))
    if token == "red":
        return int(curses.color_pair(_COLOR_RED_PAIR) | curses.A_BOLD)
    return int(curses.color_pair(_COLOR_GRAY_PAIR) | curses.A_DIM)


def _draw_frame(  # pragma: no cover - curses-only
    curses: Any,
    stdscr: Any,
    *,
    snapshot: dict[str, Any],
    width: int,
    height: int,
    paused: bool,
    failures_only: bool,
) -> None:
    """Render one frame of the 4-panel layout.

    Driver holds the bookkeeping (paused / failures_only) and passes
    them in; this function is purely draw-from-state. Height/width come
    from ``getmaxyx`` each frame, so KEY_RESIZE is handled implicitly.
    """
    startup = snapshot.get("startup", {}) or {}
    config = snapshot.get("config", {}) or {}
    profile = str(
        startup.get("default_profile") or config.get("default_profile") or "?"
    )
    uptime = _fmt_uptime(float(snapshot.get("uptime_s", 0.0)))
    tz_label = str(config.get("display_timezone") or "UTC")
    gates = build_gates_summary(snapshot)
    providers = build_provider_rows(snapshot)
    recent = build_recent_rows(snapshot, failures_only=failures_only)

    row = 0
    header = (
        f" coderouter stats  profile: {profile}  uptime: {uptime}  "
        f"requests: {gates.total_requests}  tz: {tz_label} "
    )
    stdscr.addnstr(row, 0, header.ljust(width), width, curses.A_REVERSE)
    row += 1
    stdscr.addnstr(
        row,
        0,
        " providers ".ljust(width, "─"),
        width,
        curses.A_BOLD,
    )
    row += 1
    stdscr.addnstr(
        row,
        0,
        f"  {'provider':<22} {'att':>5} {'ok%':>5} {'failed':>7} {'last error':<25}",
        width,
        curses.A_DIM,
    )
    row += 1
    for pr in providers:
        if row >= height - 2:
            break
        line = (
            f"  {pr.name:<22} {pr.attempts:>5} {pr.ok_rate_pct:>4}% "
            f"{pr.failed + pr.failed_midstream:>7} {_truncate(pr.last_error, 25):<25}"
        )
        stdscr.addnstr(row, 0, line.ljust(width), width, _color_for_health(curses, pr.health))
        row += 1

    row += 1
    if row >= height - 2:
        return
    stdscr.addnstr(row, 0, " fallback / gates ".ljust(width, "─"), width, curses.A_BOLD)
    row += 1
    rate = gates.fallback_rate_pct
    rate_color = (
        _COLOR_GREEN_PAIR if rate < 5 else _COLOR_YELLOW_PAIR if rate < 20 else _COLOR_RED_PAIR
    )
    stdscr.addnstr(
        row,
        0,
        f"  fallback rate:         {rate:5.1f}%  ({gates.total_failed}/{gates.total_requests})",
        width,
        int(curses.color_pair(rate_color)),
    )
    row += 1
    stdscr.addnstr(row, 0, f"  paid-gate blocked:     {gates.paid_gate_blocked}", width)
    row += 1
    stdscr.addnstr(
        row,
        0,
        f"  capability degraded:   {gates.degraded_total}"
        + (f"  ({_fmt_breakdown(gates.degraded_breakdown)})" if gates.degraded_breakdown else ""),
        width,
    )
    row += 1
    stdscr.addnstr(
        row,
        0,
        f"  output-filter applied: {gates.filters_applied_total}"
        + (f"  ({_fmt_breakdown(gates.filters_breakdown)})" if gates.filters_breakdown else ""),
        width,
    )
    row += 2

    if row >= height - 2:
        return
    title = " recent (failures only) " if failures_only else " recent "
    stdscr.addnstr(row, 0, title.ljust(width, "─"), width, curses.A_BOLD)
    row += 1
    for rr in recent[-(height - row - 2) :]:
        if row >= height - 1:
            break
        attr = int(curses.color_pair(_COLOR_RED_PAIR) | curses.A_BOLD) if rr.is_failure else 0
        line = f"  {rr.ts:<8}  {rr.provider:<22} {rr.status_text}"
        stdscr.addnstr(row, 0, line.ljust(width), width, attr)
        row += 1

    # Footer / keybind hints
    footer = f" [q]uit  [r]efresh  [p]ause{' ✔' if paused else ''}  [f]ailures{' ✔' if failures_only else ''} "
    stdscr.addnstr(height - 1, 0, footer.ljust(width), width, curses.A_REVERSE)


def _draw_error_screen(  # pragma: no cover - curses-only
    curses: Any, stdscr: Any, err: FetchError, *, width: int
) -> None:
    """Minimal error display — shown until a fetch succeeds."""
    stdscr.addnstr(0, 0, " coderouter stats ".ljust(width), width, curses.A_REVERSE)
    stdscr.addnstr(
        2,
        2,
        f"cannot reach metrics endpoint: {err.message}",
        width - 2,
        int(curses.color_pair(_COLOR_RED_PAIR)),
    )
    stdscr.addnstr(
        4,
        2,
        "  - is the server running? try: coderouter serve",
        width - 2,
        curses.A_DIM,
    )
    stdscr.addnstr(5, 2, "  - check --url", width - 2, curses.A_DIM)


# ---------------------------------------------------------------------------
# Tiny internal helpers
# ---------------------------------------------------------------------------


def _compute_health(*, attempts: int, ok: int, failed_midstream: int) -> str:
    """Derive a health token from counters.

    ``failed_midstream`` is promoted to "red" regardless of the overall
    rate — a single mid-stream failure means the client saw a partial
    response, which is the kind of incident that should draw an
    operator's eye immediately even if total volume is low.
    """
    if attempts <= 0:
        return "gray"
    if failed_midstream > 0:
        return "red"
    rate = ok / attempts
    if rate >= _HEALTH_GREEN_MIN_RATE:
        return "green"
    if rate >= _HEALTH_YELLOW_MIN_RATE:
        return "yellow"
    return "red"


def _format_last_error(raw: Any) -> str:
    """Render the per-provider last-error dict as a short one-liner.

    ``raw`` is the dict snapshot shape ``{status, retryable, error}`` or
    ``None``. Returns ``"-"`` for "no error yet" so column width stays
    stable.
    """
    if not isinstance(raw, dict):
        return "-"
    status = raw.get("status")
    error = str(raw.get("error") or "")
    if status is not None and error:
        return f"{status} {_truncate(error, 40)}"
    if error:
        return _truncate(error, 40)
    if status is not None:
        return f"status={status}"
    return "-"


def _fmt_uptime(seconds: float) -> str:
    """Humanize uptime: "12s", "3m 04s", "1h 23m"."""
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        m, r = divmod(s, 60)
        return f"{m}m {r:02d}s"
    h, r = divmod(s, 3600)
    m = r // 60
    return f"{h}h {m:02d}m"


def _fmt_breakdown(counter: dict[str, int]) -> str:
    """Render a breakdown dict as ``k:v  k2:v2`` with stable key order."""
    return "  ".join(f"{k}:{v}" for k, v in sorted(counter.items()))


def _truncate(text: str, max_chars: int) -> str:
    """Trim text with an ellipsis when it exceeds ``max_chars``.

    Used for the last-error column and recent-event error fields —
    the single ``…`` terminator matches :mod:`coderouter.metrics.collector`'s
    internal truncation so the TUI output is byte-aligned with the
    snapshot payload.
    """
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1] + "…"
