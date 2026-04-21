"""Unit tests for ``coderouter.cli_stats`` data/render layer (v1.5-C).

We deliberately keep the curses driver (``run_tui``) out of the test
surface — it requires a real terminal. The pure ``dict → dataclass``
helpers (:func:`build_provider_rows`, :func:`build_gates_summary`,
:func:`build_recent_rows`, :func:`format_text`) are fully testable with
canned snapshot dicts, and cover the logic an operator actually cares
about (health tokens, percentages, failures-only filtering).

``fetch_snapshot`` is tested via monkeypatched :mod:`urllib.request` so
the tests don't need a running HTTP server.
"""

from __future__ import annotations

import io
import json
from typing import Any

import pytest

from coderouter import cli_stats
from coderouter.cli_stats import (
    FetchError,
    ProviderRow,
    build_gates_summary,
    build_provider_rows,
    build_recent_rows,
    fetch_snapshot,
    format_text,
    main,
)

# ---------------------------------------------------------------------------
# Snapshot fixtures
# ---------------------------------------------------------------------------


def _empty_snapshot() -> dict[str, Any]:
    """Shape returned by a fresh MetricsCollector before any events."""
    return {
        "uptime_s": 0.0,
        "started_at": "1970-01-01T00:00:00",
        "startup": {"default_profile": "default"},
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
        "config": {"default_profile": "default", "allow_paid": False, "providers": []},
    }


def _populated_snapshot() -> dict[str, Any]:
    """Two-provider snapshot with a mix of ok / failed / midstream events.

    Mirrors the shape we'd observe after a real fallback sequence: one
    healthy local provider and one free-cloud that fell over with a
    mid-stream disconnect.
    """
    snap = _empty_snapshot()
    snap["uptime_s"] = 125.5
    snap["counters"]["requests_total"] = 4
    snap["counters"]["chain_paid_gate_blocked_total"] = 1
    snap["counters"]["capability_degraded"] = {"thinking": 2}
    snap["counters"]["output_filter_applied"] = {"strip_thinking": 3}
    snap["counters"]["provider_outcomes"] = {
        "local": {"ok": 3, "failed": 1},
        "free-cloud": {"failed_midstream": 1},
    }
    snap["providers"] = [
        {
            "name": "local",
            "attempts": 4,
            "outcomes": {"ok": 3, "failed": 1},
            "last_error": {"status": 429, "retryable": True, "error": "rate_limit"},
        },
        {
            "name": "free-cloud",
            "attempts": 1,
            "outcomes": {"failed_midstream": 1},
            "last_error": {"status": 502, "retryable": False, "error": "bad_gateway"},
        },
    ]
    snap["recent"] = [
        {
            "ts": "2026-04-21T10:15:03",
            "event": "try-provider",
            "provider": "local",
            "stream": True,
        },
        {
            "ts": "2026-04-21T10:15:04",
            "event": "provider-ok",
            "provider": "local",
            "stream": True,
        },
        {
            "ts": "2026-04-21T10:15:05",
            "event": "provider-failed",
            "provider": "local",
            "stream": False,
            "status": 429,
            "retryable": True,
        },
        {
            "ts": "2026-04-21T10:15:06",
            "event": "provider-failed-midstream",
            "provider": "free-cloud",
            "stream": True,
            "status": 502,
            "retryable": False,
        },
    ]
    return snap


# ---------------------------------------------------------------------------
# build_provider_rows
# ---------------------------------------------------------------------------


def test_build_provider_rows_sorts_alphabetically() -> None:
    """Provider order is stable regardless of snapshot key order."""
    rows = build_provider_rows(_populated_snapshot())
    assert [r.name for r in rows] == ["free-cloud", "local"]


def test_build_provider_rows_computes_ok_rate() -> None:
    """``ok_rate_pct`` rounds to integer and handles zero-attempt providers."""
    rows = {r.name: r for r in build_provider_rows(_populated_snapshot())}
    assert rows["local"].ok_rate_pct == 75  # 3/4
    # free-cloud: 0 ok / 1 attempt → 0%
    assert rows["free-cloud"].ok_rate_pct == 0


def test_ok_rate_pct_100_when_no_attempts() -> None:
    """Zero-attempt row shows 100 so the UI doesn't flash 0% for unused providers."""
    row = ProviderRow(
        name="unused",
        attempts=0,
        ok=0,
        failed=0,
        failed_midstream=0,
        last_error="-",
        health="gray",
    )
    assert row.ok_rate_pct == 100


def test_build_provider_rows_health_tokens() -> None:
    """Health derivation: midstream → red, <80% → red, <95% → yellow, else green."""
    rows = {r.name: r for r in build_provider_rows(_populated_snapshot())}
    # local at 75% ok → yellow? Actually 75% < 80% → red
    assert rows["local"].health == "red"
    # free-cloud has a midstream fail → always red
    assert rows["free-cloud"].health == "red"


def test_build_provider_rows_health_green_for_healthy_provider() -> None:
    """A provider with ≥95% success rate should show green."""
    snap = _empty_snapshot()
    snap["providers"] = [
        {"name": "local", "attempts": 100, "outcomes": {"ok": 98, "failed": 2}}
    ]
    rows = {r.name: r for r in build_provider_rows(snap)}
    assert rows["local"].health == "green"


def test_build_provider_rows_health_yellow_band() -> None:
    """80% ≤ rate < 95% maps to yellow."""
    snap = _empty_snapshot()
    snap["providers"] = [
        {"name": "local", "attempts": 10, "outcomes": {"ok": 9, "failed": 1}}
    ]
    rows = {r.name: r for r in build_provider_rows(snap)}
    assert rows["local"].health == "yellow"


def test_build_provider_rows_last_error_formatting() -> None:
    """``last_error`` column renders ``<status> <error>`` when both present."""
    rows = {r.name: r for r in build_provider_rows(_populated_snapshot())}
    assert "429" in rows["local"].last_error
    assert "rate_limit" in rows["local"].last_error


def test_build_provider_rows_last_error_dash_when_missing() -> None:
    """Providers without a prior error show ``-`` to keep column width stable."""
    snap = _empty_snapshot()
    snap["providers"] = [
        {"name": "x", "attempts": 1, "outcomes": {"ok": 1}},
    ]
    rows = {r.name: r for r in build_provider_rows(snap)}
    assert rows["x"].last_error == "-"


# ---------------------------------------------------------------------------
# build_gates_summary
# ---------------------------------------------------------------------------


def test_build_gates_summary_totals() -> None:
    """Gates summary aggregates failures across all providers."""
    gates = build_gates_summary(_populated_snapshot())
    assert gates.total_requests == 4
    # local.failed=1 + free-cloud.failed_midstream=1
    assert gates.total_failed == 2
    assert gates.paid_gate_blocked == 1


def test_build_gates_summary_fallback_rate_zero_when_no_requests() -> None:
    """No division-by-zero surface — 0/0 is reported as 0.0."""
    gates = build_gates_summary(_empty_snapshot())
    assert gates.fallback_rate_pct == 0.0


def test_build_gates_summary_fallback_rate_computed() -> None:
    """2 failures / 4 requests = 50.0%."""
    gates = build_gates_summary(_populated_snapshot())
    assert gates.fallback_rate_pct == pytest.approx(50.0)


def test_build_gates_summary_breakdowns_preserved() -> None:
    """The capability / filter breakdowns are carried through verbatim."""
    gates = build_gates_summary(_populated_snapshot())
    assert gates.degraded_breakdown == {"thinking": 2}
    assert gates.degraded_total == 2
    assert gates.filters_breakdown == {"strip_thinking": 3}
    assert gates.filters_applied_total == 3


# ---------------------------------------------------------------------------
# build_recent_rows
# ---------------------------------------------------------------------------


def test_build_recent_rows_default_returns_all() -> None:
    """Without filtering, every ring entry surfaces."""
    rows = build_recent_rows(_populated_snapshot())
    assert len(rows) == 4
    assert rows[0].event == "try-provider"


def test_build_recent_rows_failures_only_filters_happy_path() -> None:
    """``failures_only`` keeps only ``provider-failed*`` events."""
    rows = build_recent_rows(_populated_snapshot(), failures_only=True)
    assert len(rows) == 2
    assert all(r.is_failure for r in rows)
    assert {r.event for r in rows} == {"provider-failed", "provider-failed-midstream"}


def test_build_recent_rows_strips_date_from_ts() -> None:
    """The TUI cell is narrow — we keep only HH:MM:SS from the ISO timestamp."""
    rows = build_recent_rows(_populated_snapshot())
    assert rows[0].ts == "10:15:03"


def test_recent_row_status_text_mappings() -> None:
    """Each event type maps to a human-readable short label."""
    rows = {r.event: r for r in build_recent_rows(_populated_snapshot())}
    assert rows["try-provider"].status_text == "try"
    assert rows["provider-ok"].status_text == "ok"
    # failures include status codes where available
    assert rows["provider-failed"].status_text == "FAIL (429)"
    assert rows["provider-failed-midstream"].status_text == "FAIL (502)"


def test_recent_row_status_text_without_status_is_fail() -> None:
    """Failed event without status still shows a clean ``FAIL`` label."""
    snap = _empty_snapshot()
    snap["recent"] = [
        {"ts": "2026-04-21T10:00:00", "event": "provider-failed", "provider": "x"},
    ]
    row = build_recent_rows(snap)[0]
    assert row.status_text == "FAIL"


# ---------------------------------------------------------------------------
# v1.5-E: display_timezone conversion in build_recent_rows
# ---------------------------------------------------------------------------


def test_build_recent_rows_converts_ts_to_configured_tz() -> None:
    """``config.display_timezone`` rewrites the ``ts`` column in the target zone.

    Asia/Tokyo is UTC+9 year-round (no DST), so a deterministic 10:15:03
    UTC ring entry renders as 19:15:03 — a concrete assertion rather
    than a tautological "same zone → same time" check.
    """
    snap = _populated_snapshot()
    snap["config"]["display_timezone"] = "Asia/Tokyo"
    rows = build_recent_rows(snap)
    # 10:15:03 UTC + 9h = 19:15:03 JST
    assert rows[0].ts == "19:15:03"


def test_build_recent_rows_unset_tz_keeps_utc_slice() -> None:
    """No ``display_timezone`` → naive ``HH:MM:SS`` UTC slice (pre-v1.5-E behavior).

    Guards the "feature off by default" contract — existing snapshots
    from a v1.5-D server must continue to render as they did.
    """
    snap = _populated_snapshot()
    # Ensure display_timezone is absent
    snap["config"].pop("display_timezone", None)
    rows = build_recent_rows(snap)
    assert rows[0].ts == "10:15:03"


def test_build_recent_rows_none_tz_keeps_utc_slice() -> None:
    """Explicit ``None`` also falls back to UTC — mirrors the unset path.

    Important because :func:`/metrics.json` surfaces
    ``display_timezone: None`` verbatim when the config field is unset,
    so downstream consumers must treat both shapes identically.
    """
    snap = _populated_snapshot()
    snap["config"]["display_timezone"] = None
    rows = build_recent_rows(snap)
    assert rows[0].ts == "10:15:03"


def test_build_recent_rows_malformed_tz_falls_back_to_utc_slice() -> None:
    """An unresolvable zone name must not crash the renderer.

    The server-side validator rejects bad zones at load time, but a
    stale snapshot or a downgraded server could still ship one. The
    client-side fallback keeps the table readable.
    """
    snap = _populated_snapshot()
    snap["config"]["display_timezone"] = "Not/A_Zone"
    rows = build_recent_rows(snap)
    assert rows[0].ts == "10:15:03"


def test_build_recent_rows_malformed_ts_still_renders() -> None:
    """A ring entry with a garbled ``ts`` field falls back to the raw slice.

    Defense-in-depth: if some future event shape sneaks in a non-ISO
    timestamp, the recent-events panel still renders instead of
    500'ing the whole TUI frame.
    """
    snap = _empty_snapshot()
    snap["config"]["display_timezone"] = "Asia/Tokyo"
    snap["recent"] = [
        {"ts": "not-a-timestamp", "event": "provider-ok", "provider": "local"},
    ]
    rows = build_recent_rows(snap)
    assert rows[0].ts == "not-a-timestamp"


def test_format_text_header_includes_tz_label() -> None:
    """``--once`` header surfaces the configured TZ so piped output is self-describing."""
    snap = _populated_snapshot()
    snap["config"]["display_timezone"] = "Asia/Tokyo"
    out = format_text(snap)
    assert "tz: Asia/Tokyo" in out


def test_format_text_header_defaults_tz_to_utc() -> None:
    """Unset ``display_timezone`` renders as ``tz: UTC`` in the header.

    Explicit label — readers of a piped dump shouldn't have to guess
    which zone the Recent column is in, and UTC is the honest default.
    """
    snap = _populated_snapshot()
    snap["config"].pop("display_timezone", None)
    out = format_text(snap)
    assert "tz: UTC" in out


# ---------------------------------------------------------------------------
# format_text (--once mode)
# ---------------------------------------------------------------------------


def test_format_text_contains_header_and_panel_titles() -> None:
    """One-shot dump carries Providers / Fallback & Gates / Recent sections."""
    out = format_text(_populated_snapshot())
    assert "profile: default" in out
    assert "Providers" in out
    assert "Fallback & Gates" in out
    assert "Recent" in out
    assert out.endswith("\n")


def test_format_text_empty_snapshot_notes_no_data() -> None:
    """Empty snapshot produces a graceful "no data" output."""
    out = format_text(_empty_snapshot())
    assert "(no requests seen yet)" in out
    assert "(no events yet)" in out


def test_format_text_includes_breakdown_when_present() -> None:
    """Capability / filter breakdowns appear in parentheses."""
    out = format_text(_populated_snapshot())
    assert "thinking:2" in out
    assert "strip_thinking:3" in out


def test_format_text_provider_rows_present() -> None:
    """Each provider appears as its own row with attempts + ok%."""
    out = format_text(_populated_snapshot())
    assert "local" in out
    assert "free-cloud" in out


# ---------------------------------------------------------------------------
# fetch_snapshot
# ---------------------------------------------------------------------------


class _FakeResponse:
    """Minimal context-manager stand-in for urllib's HTTPResponse."""

    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *a: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def test_fetch_snapshot_returns_parsed_dict(monkeypatch: pytest.MonkeyPatch) -> None:
    """Happy path: 200 with valid JSON body."""
    payload = {"uptime_s": 1.0, "counters": {}}

    def _fake_urlopen(url: str, timeout: float = 0) -> _FakeResponse:
        return _FakeResponse(json.dumps(payload).encode("utf-8"))

    monkeypatch.setattr("coderouter.cli_stats.urllib.request.urlopen", _fake_urlopen)
    result = fetch_snapshot("http://irrelevant/metrics.json")
    assert isinstance(result, dict)
    assert result["uptime_s"] == 1.0


def test_fetch_snapshot_wraps_url_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Transport errors become a ``FetchError`` (never raise to the caller)."""
    import urllib.error

    def _raise(url: str, timeout: float = 0) -> _FakeResponse:
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr("coderouter.cli_stats.urllib.request.urlopen", _raise)
    result = fetch_snapshot("http://127.0.0.1:1/metrics.json")
    assert isinstance(result, FetchError)
    assert "connection refused" in result.message


def test_fetch_snapshot_wraps_invalid_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-JSON response → FetchError with a parse reason."""

    def _fake_urlopen(url: str, timeout: float = 0) -> _FakeResponse:
        return _FakeResponse(b"<html>oops</html>")

    monkeypatch.setattr("coderouter.cli_stats.urllib.request.urlopen", _fake_urlopen)
    result = fetch_snapshot("http://irrelevant/metrics.json")
    assert isinstance(result, FetchError)
    assert "invalid JSON" in result.message


def test_fetch_snapshot_wraps_non_dict_response(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the server returns a JSON list or string, we surface a typed error.

    ``/metrics.json`` guarantees a dict envelope — anything else is a
    server contract violation that the TUI should show rather than
    silently render as an empty dashboard.
    """

    def _fake_urlopen(url: str, timeout: float = 0) -> _FakeResponse:
        return _FakeResponse(b"[1, 2, 3]")

    monkeypatch.setattr("coderouter.cli_stats.urllib.request.urlopen", _fake_urlopen)
    result = fetch_snapshot("http://irrelevant/metrics.json")
    assert isinstance(result, FetchError)
    assert "non-object" in result.message


# ---------------------------------------------------------------------------
# main() dispatch
# ---------------------------------------------------------------------------


def test_main_once_mode_prints_format_text(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--once`` path: fetch → format_text → stdout, exit 0."""
    monkeypatch.setattr(
        "coderouter.cli_stats.fetch_snapshot",
        lambda url, timeout_s=2.0: _populated_snapshot(),
    )
    rc = main("http://irrelevant", interval=1.0, once=True)
    assert rc == 0
    out = capsys.readouterr().out
    assert "profile: default" in out
    assert "local" in out


def test_main_once_mode_reports_fetch_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Fetch failure in --once mode surfaces on stderr and exits 2."""
    monkeypatch.setattr(
        "coderouter.cli_stats.fetch_snapshot",
        lambda url, timeout_s=2.0: FetchError("server down"),
    )
    rc = main("http://irrelevant", interval=1.0, once=True)
    assert rc == 2
    err = capsys.readouterr().err
    assert "server down" in err


def test_main_routes_to_once_when_stdout_is_not_tty(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Piped stdout (e.g. ``| grep``) falls back to --once automatically."""
    monkeypatch.setattr(
        "coderouter.cli_stats.fetch_snapshot",
        lambda url, timeout_s=2.0: _empty_snapshot(),
    )
    # sys.stdout in pytest is usually a non-tty capture buffer, but pin
    # it explicitly so the test doesn't depend on pytest internals.
    monkeypatch.setattr("sys.stdout", io.StringIO())
    rc = main("http://irrelevant", interval=1.0, once=False)
    assert rc == 0


# ---------------------------------------------------------------------------
# Internal helpers — exercised for coverage and contract
# ---------------------------------------------------------------------------


def test_fmt_uptime_bands() -> None:
    """Uptime humanization: seconds / minutes / hours branches."""
    assert cli_stats._fmt_uptime(5) == "5s"
    assert cli_stats._fmt_uptime(65) == "1m 05s"
    assert cli_stats._fmt_uptime(3725) == "1h 02m"


def test_truncate_adds_ellipsis_when_over_budget() -> None:
    """Over-budget strings end in an ellipsis; under-budget pass through."""
    assert cli_stats._truncate("short", 10) == "short"
    assert cli_stats._truncate("this is too long", 8) == "this is\u2026"


def test_compute_health_branches() -> None:
    """Each health branch maps to the expected token."""
    assert cli_stats._compute_health(attempts=0, ok=0, failed_midstream=0) == "gray"
    assert cli_stats._compute_health(attempts=1, ok=0, failed_midstream=1) == "red"
    assert cli_stats._compute_health(attempts=100, ok=99, failed_midstream=0) == "green"
    assert cli_stats._compute_health(attempts=100, ok=85, failed_midstream=0) == "yellow"
    assert cli_stats._compute_health(attempts=100, ok=10, failed_midstream=0) == "red"


def test_format_last_error_variants() -> None:
    """All last_error shapes render a useful string."""
    assert cli_stats._format_last_error(None) == "-"
    assert cli_stats._format_last_error({"status": 500}) == "status=500"
    assert cli_stats._format_last_error({"error": "boom"}) == "boom"
    assert cli_stats._format_last_error({"status": 502, "error": "bad"}) == "502 bad"
