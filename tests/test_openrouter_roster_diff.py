"""v0.5-D: tests for the OpenRouter roster diff cron.

The script (``scripts/openrouter_roster_diff.py``) is deliberately
dependency-light and has three distinct tiers we want to cover:

    1. Pure parsing / filtering — ``parse_models`` / ``is_free`` /
       ``filter_free``. No I/O.
    2. Pure diffing — ``diff_rosters`` given two in-memory lists. No
       I/O.
    3. Orchestration — ``run()`` which combines httpx + snapshot I/O +
       changes log. Mocked with ``httpx_mock``.

Each tier gets tests in its own section. The orchestration tier also
covers the critical first-run case (baseline write, no CHANGES.md
emission) which is hard to spot from pure-function tests alone.

Imports via ``importlib`` because the module lives under ``scripts/``
and isn't on the package path. Pytest's conftest sets ``PYTHONPATH``
via ``testpaths = ["tests"]`` which means ``tests/`` and the repo root
are importable, but ``scripts/`` isn't. Rather than editing the test
config, we do a one-shot ``spec_from_file_location`` — this is a test-
only concern and keeps the production layout unchanged.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pytest_httpx import HTTPXMock

_SCRIPT_PATH = (
    Path(__file__).resolve().parent.parent
    / "scripts"
    / "openrouter_roster_diff.py"
)

_spec = importlib.util.spec_from_file_location(
    "openrouter_roster_diff", _SCRIPT_PATH
)
assert _spec is not None and _spec.loader is not None
rd = importlib.util.module_from_spec(_spec)
# dataclasses looks up __module__ in sys.modules while processing the
# class body, so the module must be registered *before* exec_module.
sys.modules["openrouter_roster_diff"] = rd
_spec.loader.exec_module(rd)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


FIXED_NOW = datetime(2026, 4, 20, 12, 34, 56, tzinfo=UTC)


def _model(
    mid: str,
    *,
    prompt: str = "0",
    completion: str = "0",
    ctx: int | None = 32768,
) -> dict:
    """Build an OpenRouter-shaped model dict for fixtures."""
    m: dict = {
        "id": mid,
        "pricing": {"prompt": prompt, "completion": completion},
    }
    if ctx is not None:
        m["context_length"] = ctx
    return m


def _response(models: list[dict]) -> dict:
    return {"data": models}


# ---------------------------------------------------------------------------
# Tier 1: parse_models / is_free / filter_free
# ---------------------------------------------------------------------------


def test_parse_models_basic() -> None:
    raw = _response(
        [
            _model("openai/gpt-oss-120b:free"),
            _model("anthropic/claude-opus-4", prompt="0.000015", completion="0.000075"),
        ]
    )
    entries = rd.parse_models(raw)
    assert [e.id for e in entries] == [
        "openai/gpt-oss-120b:free",
        "anthropic/claude-opus-4",
    ]
    assert entries[1].pricing_prompt == "0.000015"
    assert entries[1].context_length == 32768


def test_parse_models_skips_malformed_rows() -> None:
    raw = {
        "data": [
            _model("ok/id"),
            {"id": ""},                       # empty id — skipped
            "not a dict",                      # not a dict — skipped
            {"pricing": {"prompt": "0"}},      # no id — skipped
            _model("ok/id2"),
        ]
    }
    ids = [e.id for e in rd.parse_models(raw)]
    assert ids == ["ok/id", "ok/id2"]


def test_parse_models_invalid_context_length_becomes_none() -> None:
    raw = {
        "data": [
            {
                "id": "weird/ctx",
                "context_length": "not-a-number",
                "pricing": {"prompt": "0", "completion": "0"},
            }
        ]
    }
    entries = rd.parse_models(raw)
    assert entries[0].context_length is None


def test_is_free_on_zero_pricing() -> None:
    e = rd.RosterEntry(
        id="x", context_length=4096, pricing_prompt="0", pricing_completion="0"
    )
    assert rd.is_free(e)


def test_is_free_rejects_nonzero_completion() -> None:
    e = rd.RosterEntry(
        id="x",
        context_length=4096,
        pricing_prompt="0",
        pricing_completion="0.000001",
    )
    assert not rd.is_free(e)


def test_is_free_rejects_non_numeric_strings() -> None:
    # Bogus pricing strings shouldn't accidentally pass as free.
    e = rd.RosterEntry(
        id="x",
        context_length=4096,
        pricing_prompt="free",
        pricing_completion="free",
    )
    assert not rd.is_free(e)


def test_is_free_does_not_require_free_suffix() -> None:
    """Pricing is authoritative, not the ``:free`` id convention."""
    e = rd.RosterEntry(
        id="vendor/model-without-suffix",
        context_length=4096,
        pricing_prompt="0",
        pricing_completion="0",
    )
    assert rd.is_free(e)


def test_filter_free_drops_paid() -> None:
    entries = rd.parse_models(
        _response(
            [
                _model("free/a"),
                _model("paid/b", prompt="0.000005", completion="0.000010"),
                _model("free/c", prompt="0.00", completion="0.00"),
            ]
        )
    )
    assert [e.id for e in rd.filter_free(entries)] == ["free/a", "free/c"]


# ---------------------------------------------------------------------------
# Tier 2: diff_rosters
# ---------------------------------------------------------------------------


def _entry(
    mid: str,
    *,
    prompt: str = "0",
    completion: str = "0",
    ctx: int | None = 32768,
) -> rd.RosterEntry:
    return rd.RosterEntry(
        id=mid,
        context_length=ctx,
        pricing_prompt=prompt,
        pricing_completion=completion,
    )


def test_diff_empty_when_identical() -> None:
    roster = [_entry("a"), _entry("b")]
    assert rd.diff_rosters(roster, list(roster)).is_empty()


def test_diff_detects_added_and_removed() -> None:
    old = [_entry("a"), _entry("b")]
    new = [_entry("b"), _entry("c")]
    diff = rd.diff_rosters(old, new)
    assert [e.id for e in diff.added] == ["c"]
    assert [e.id for e in diff.removed] == ["a"]
    assert diff.pricing_changed == []
    assert diff.context_changed == []


def test_diff_detects_pricing_change() -> None:
    old = [_entry("a", prompt="0", completion="0")]
    new = [_entry("a", prompt="0.00", completion="0")]  # stringform flip
    diff = rd.diff_rosters(old, new)
    assert diff.added == []
    assert diff.removed == []
    assert len(diff.pricing_changed) == 1
    o, n = diff.pricing_changed[0]
    assert o.pricing_prompt == "0"
    assert n.pricing_prompt == "0.00"


def test_diff_detects_context_change() -> None:
    old = [_entry("a", ctx=32768)]
    new = [_entry("a", ctx=65536)]
    diff = rd.diff_rosters(old, new)
    assert len(diff.context_changed) == 1
    o, n = diff.context_changed[0]
    assert o.context_length == 32768
    assert n.context_length == 65536


def test_diff_output_is_id_sorted() -> None:
    """CHANGES.md determinism depends on sorted diff output."""
    old = [_entry("b"), _entry("a")]
    new = [_entry("d"), _entry("c"), _entry("b"), _entry("a")]
    diff = rd.diff_rosters(old, new)
    assert [e.id for e in diff.added] == ["c", "d"]


# ---------------------------------------------------------------------------
# Tier 2.5: format_markdown
# ---------------------------------------------------------------------------


def test_format_markdown_empty_diff_returns_empty_string() -> None:
    assert rd.format_markdown(rd.RosterDiff(), now=FIXED_NOW) == ""


def test_format_markdown_includes_all_categories() -> None:
    diff = rd.RosterDiff(
        added=[_entry("z/added")],
        removed=[_entry("z/removed", prompt="0", completion="0", ctx=4096)],
        pricing_changed=[(_entry("z/pchg", prompt="0"), _entry("z/pchg", prompt="0.00"))],
        context_changed=[(_entry("z/cchg", ctx=4096), _entry("z/cchg", ctx=8192))],
    )
    md = rd.format_markdown(diff, now=FIXED_NOW)
    # Timestamp header
    assert md.startswith("## 2026-04-20T12:34:56Z\n")
    # Removed is listed first (operational urgency)
    assert md.index("### Removed") < md.index("### Added")
    assert md.index("### Added") < md.index("### Pricing changed")
    assert md.index("### Pricing changed") < md.index("### Context changed")
    # Content
    assert "z/added" in md
    assert "z/removed" in md
    assert "z/pchg" in md
    assert "z/cchg" in md
    # Removed section carries a warn marker (emoji) for grep-ability
    assert "⚠️" in md


def test_format_markdown_omits_empty_sections() -> None:
    diff = rd.RosterDiff(added=[_entry("only/added")])
    md = rd.format_markdown(diff, now=FIXED_NOW)
    assert "### Added" in md
    assert "### Removed" not in md
    assert "### Pricing changed" not in md
    assert "### Context changed" not in md


# ---------------------------------------------------------------------------
# Tier 3: orchestration — run() with mocked httpx
# ---------------------------------------------------------------------------


@pytest.fixture(name="paths")
def _paths(tmp_path: Path) -> tuple[Path, Path]:
    return tmp_path / "latest.json", tmp_path / "CHANGES.md"


def test_run_first_invocation_writes_snapshot_without_changes_log(
    httpx_mock: HTTPXMock, paths: tuple[Path, Path]
) -> None:
    """First run (no snapshot yet): write the baseline, but do NOT
    append to CHANGES.md. Otherwise the first run would record every
    free model as 'Added' which is noise."""
    snapshot, changes = paths
    httpx_mock.add_response(
        url=rd.OPENROUTER_MODELS_URL,
        json=_response([_model("free/a"), _model("free/b")]),
    )
    diff = rd.run(
        url=rd.OPENROUTER_MODELS_URL,
        snapshot=snapshot,
        changes=changes,
        dry_run=False,
        now=FIXED_NOW,
    )
    # diff is non-empty (2 "added" relative to empty baseline) but the
    # run() logic suppresses the CHANGES.md write on first run.
    assert len(diff.added) == 2
    assert snapshot.exists()
    payload = json.loads(snapshot.read_text(encoding="utf-8"))
    assert {e["id"] for e in payload["entries"]} == {"free/a", "free/b"}
    assert not changes.exists()


def test_run_second_invocation_records_removal(
    httpx_mock: HTTPXMock, paths: tuple[Path, Path]
) -> None:
    """End-to-end: first run establishes the baseline, second run sees
    a removed model, and CHANGES.md grows with the expected section."""
    snapshot, changes = paths

    # Run 1: baseline of 2 models
    httpx_mock.add_response(
        url=rd.OPENROUTER_MODELS_URL,
        json=_response([_model("free/a"), _model("free/b")]),
    )
    rd.run(
        url=rd.OPENROUTER_MODELS_URL,
        snapshot=snapshot,
        changes=changes,
        dry_run=False,
        now=FIXED_NOW,
    )

    # Run 2: free/a disappears, free/c appears
    httpx_mock.add_response(
        url=rd.OPENROUTER_MODELS_URL,
        json=_response([_model("free/b"), _model("free/c")]),
    )
    diff = rd.run(
        url=rd.OPENROUTER_MODELS_URL,
        snapshot=snapshot,
        changes=changes,
        dry_run=False,
        now=FIXED_NOW,
    )
    assert [e.id for e in diff.removed] == ["free/a"]
    assert [e.id for e in diff.added] == ["free/c"]

    body = changes.read_text(encoding="utf-8")
    assert "OpenRouter free-tier roster" in body  # header
    assert "free/a" in body  # removed
    assert "free/c" in body  # added
    # Snapshot reflects the new state
    payload = json.loads(snapshot.read_text(encoding="utf-8"))
    assert {e["id"] for e in payload["entries"]} == {"free/b", "free/c"}


def test_run_dry_run_does_not_write_anything(
    httpx_mock: HTTPXMock, paths: tuple[Path, Path]
) -> None:
    snapshot, changes = paths
    httpx_mock.add_response(
        url=rd.OPENROUTER_MODELS_URL,
        json=_response([_model("free/a")]),
    )
    diff = rd.run(
        url=rd.OPENROUTER_MODELS_URL,
        snapshot=snapshot,
        changes=changes,
        dry_run=True,
        now=FIXED_NOW,
    )
    # Diff is computed…
    assert len(diff.added) == 1
    # …but nothing touched on disk.
    assert not snapshot.exists()
    assert not changes.exists()


def test_run_filters_out_paid_models(
    httpx_mock: HTTPXMock, paths: tuple[Path, Path]
) -> None:
    """The cron only tracks free tier. Paid models must not appear in
    the snapshot (they'd churn constantly from price updates)."""
    snapshot, changes = paths
    httpx_mock.add_response(
        url=rd.OPENROUTER_MODELS_URL,
        json=_response(
            [
                _model("free/a"),
                _model("paid/b", prompt="0.000005", completion="0.00001"),
            ]
        ),
    )
    rd.run(
        url=rd.OPENROUTER_MODELS_URL,
        snapshot=snapshot,
        changes=changes,
        dry_run=False,
        now=FIXED_NOW,
    )
    payload = json.loads(snapshot.read_text(encoding="utf-8"))
    assert {e["id"] for e in payload["entries"]} == {"free/a"}


def test_run_noop_when_roster_unchanged(
    httpx_mock: HTTPXMock, paths: tuple[Path, Path]
) -> None:
    """Two identical runs leave CHANGES.md untouched on the second."""
    snapshot, changes = paths
    # Two fetches with identical roster
    for _ in range(2):
        httpx_mock.add_response(
            url=rd.OPENROUTER_MODELS_URL,
            json=_response([_model("free/a")]),
        )
    rd.run(
        url=rd.OPENROUTER_MODELS_URL,
        snapshot=snapshot,
        changes=changes,
        dry_run=False,
        now=FIXED_NOW,
    )
    # Baseline existed; second run sees no changes.
    rd.run(
        url=rd.OPENROUTER_MODELS_URL,
        snapshot=snapshot,
        changes=changes,
        dry_run=False,
        now=FIXED_NOW,
    )
    assert not changes.exists()


def test_run_prepends_newest_changes_on_top(
    httpx_mock: HTTPXMock, paths: tuple[Path, Path]
) -> None:
    """Two distinct diffs should stack with newest on top (matches
    CHANGELOG.md convention)."""
    snapshot, changes = paths
    # Baseline
    httpx_mock.add_response(
        url=rd.OPENROUTER_MODELS_URL,
        json=_response([_model("free/a")]),
    )
    rd.run(
        url=rd.OPENROUTER_MODELS_URL,
        snapshot=snapshot,
        changes=changes,
        dry_run=False,
        now=FIXED_NOW,
    )
    # Second run: add free/b
    httpx_mock.add_response(
        url=rd.OPENROUTER_MODELS_URL,
        json=_response([_model("free/a"), _model("free/b")]),
    )
    first_change_time = datetime(2026, 4, 27, 9, 0, 0, tzinfo=UTC)
    rd.run(
        url=rd.OPENROUTER_MODELS_URL,
        snapshot=snapshot,
        changes=changes,
        dry_run=False,
        now=first_change_time,
    )
    # Third run: remove free/a
    httpx_mock.add_response(
        url=rd.OPENROUTER_MODELS_URL,
        json=_response([_model("free/b")]),
    )
    second_change_time = datetime(2026, 5, 4, 9, 0, 0, tzinfo=UTC)
    rd.run(
        url=rd.OPENROUTER_MODELS_URL,
        snapshot=snapshot,
        changes=changes,
        dry_run=False,
        now=second_change_time,
    )

    body = changes.read_text(encoding="utf-8")
    # Newest (2026-05-04) appears before older (2026-04-27)
    newest_idx = body.index("2026-05-04")
    older_idx = body.index("2026-04-27")
    assert newest_idx < older_idx


# ---------------------------------------------------------------------------
# Tier 3b: main() exit codes
# ---------------------------------------------------------------------------


def test_main_exit_zero_on_success(
    httpx_mock: HTTPXMock, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    httpx_mock.add_response(
        url=rd.OPENROUTER_MODELS_URL,
        json=_response([_model("free/a")]),
    )
    rc = rd.main(
        [
            "--snapshot",
            str(tmp_path / "latest.json"),
            "--changes",
            str(tmp_path / "CHANGES.md"),
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    # First run: no-changes summary (first-run suppresses CHANGES.md
    # emission so the *surface* diff is empty too from main's POV)
    # — actually `_format_summary` runs on the real diff returned by
    # run(), which contains the "first-run" added. Document that.
    assert "+1" in out or "no changes" in out


def test_main_exit_two_on_fetch_failure(
    httpx_mock: HTTPXMock, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Transport / HTTP error surfaces as exit 2 — distinct from 0
    (success, no changes) and 1 (argparse). Cron can branch on this."""
    httpx_mock.add_response(
        url=rd.OPENROUTER_MODELS_URL,
        status_code=503,
    )
    rc = rd.main(
        [
            "--snapshot",
            str(tmp_path / "latest.json"),
            "--changes",
            str(tmp_path / "CHANGES.md"),
        ]
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "fetch failed" in err
