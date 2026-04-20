#!/usr/bin/env python3
"""OpenRouter free-tier roster diff (v0.5-D).

Weekly cron candidate. GETs ``https://openrouter.ai/api/v1/models``,
filters to the free tier, compares against the committed snapshot,
appends any differences to ``CHANGES.md``, and updates ``latest.json``.

Design goals (from v0.4 retrospective §Follow-ons):
    - **Proactive**: surface free-tier withdrawals *before* users hit
      them. v0.4-B was reactive — ``deepseek-r1:free`` had already
      vanished from the roster when we did the audit, which is how it
      became the motivating case.
    - **Zero coupling to the coderouter package.** This script imports
      only the standard library + ``httpx``. It does not load
      ``providers.yaml``, instantiate adapters, or know about profiles.
      That keeps the cron safe to run even when coderouter itself is
      mid-change, and lets operators run it anywhere httpx is
      installed (not only in a configured coderouter checkout).
    - **Human-readable diff log in git.** ``CHANGES.md`` is an
      append-only log in markdown — ``git log -p`` on that file is
      the primary audit trail for roster churn.

First-run semantics
    The first invocation (no ``latest.json`` present) writes the
    initial snapshot but does **not** emit an "Added: N models"
    section — that would be a noise spike unrelated to actual churn.
    ``CHANGES.md`` starts tracking diffs from the second run onward.

CLI
    python scripts/openrouter_roster_diff.py             # normal run
    python scripts/openrouter_roster_diff.py --dry-run   # fetch, diff, but don't write
    python scripts/openrouter_roster_diff.py --url URL   # override endpoint
    python scripts/openrouter_roster_diff.py --snapshot PATH --changes PATH

Exit codes
    0: success (diff may or may not be empty)
    2: HTTP fetch failed (transport error, 4xx, 5xx)

Idempotence
    The script is safe to re-run back-to-back. If no changes happened
    between runs, ``CHANGES.md`` is unchanged and ``latest.json`` is
    rewritten with a refreshed ``fetched_at`` only. Git diff on that
    file will show just the timestamp flip, which we tolerate as the
    "this cron did run" signal.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"

# Default paths are resolved relative to the repo root (= parent of this file's
# parent). Overridable via CLI for tests / alternative layouts.
_REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SNAPSHOT = _REPO_ROOT / "docs" / "openrouter-roster" / "latest.json"
DEFAULT_CHANGES = _REPO_ROOT / "docs" / "openrouter-roster" / "CHANGES.md"

_CHANGES_HEADER = (
    "# OpenRouter free-tier roster — change log\n"
    "\n"
    "Appended by `scripts/openrouter_roster_diff.py` on each run. Newest\n"
    "entries appear at the top. Each section records a delta between two\n"
    "consecutive snapshots — not a cumulative list of free-tier models.\n"
    "For the current list, see `latest.json` in this directory.\n"
    "\n"
)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RosterEntry:
    """One free-tier model as tracked across snapshots.

    The shape is deliberately narrow: we only persist fields that
    affect routing decisions (context window) or cost (pricing). Human-
    readable names / descriptions / provider metadata drift too often
    to be useful signal for churn detection.
    """

    id: str
    context_length: int | None
    pricing_prompt: str
    pricing_completion: str


@dataclass
class RosterDiff:
    """Difference between two consecutive snapshots.

    Categories are orthogonal: a model that went from free to paid
    shows up in ``removed`` (it's no longer in the free filter), not
    ``pricing_changed`` (which only tracks price deltas *within* the
    free set, e.g. a pricing string normalization from ``"0"`` to
    ``"0.00"``).
    """

    added: list[RosterEntry] = field(default_factory=list)
    removed: list[RosterEntry] = field(default_factory=list)
    pricing_changed: list[tuple[RosterEntry, RosterEntry]] = field(
        default_factory=list
    )
    context_changed: list[tuple[RosterEntry, RosterEntry]] = field(
        default_factory=list
    )

    def is_empty(self) -> bool:
        return not (
            self.added
            or self.removed
            or self.pricing_changed
            or self.context_changed
        )


# ---------------------------------------------------------------------------
# Pure parsing / filtering / diffing
# ---------------------------------------------------------------------------


def parse_models(raw: dict[str, Any]) -> list[RosterEntry]:
    """Parse OpenRouter's ``/api/v1/models`` response into entries.

    Response shape (abbreviated, verified 2026-04)::

        {
          "data": [
            {
              "id": "openai/gpt-oss-120b:free",
              "context_length": 32768,
              "pricing": {"prompt": "0", "completion": "0", ...},
              ...
            }
          ]
        }

    Unparseable entries (missing ``id``, malformed shapes) are skipped
    silently — a weekly cron that hard-fails on a single bad row is
    more annoying than useful.
    """
    data = raw.get("data", [])
    out: list[RosterEntry] = []
    for m in data:
        if not isinstance(m, dict):
            continue
        model_id = m.get("id")
        if not model_id or not isinstance(model_id, str):
            continue
        pricing = m.get("pricing") or {}
        ctx = m.get("context_length")
        try:
            context_length = int(ctx) if ctx is not None else None
        except (TypeError, ValueError):
            context_length = None
        out.append(
            RosterEntry(
                id=model_id,
                context_length=context_length,
                pricing_prompt=str(pricing.get("prompt", "")),
                pricing_completion=str(pricing.get("completion", "")),
            )
        )
    return out


def _is_zero_price(price: str) -> bool:
    """True iff the string parses as a zero decimal."""
    try:
        return float(price) == 0.0
    except (TypeError, ValueError):
        return False


def is_free(entry: RosterEntry) -> bool:
    """True iff both prompt and completion pricing are zero.

    We deliberately do NOT trust the ``:free`` id suffix — it's a
    naming convention on OpenRouter, not a wire contract. Some models
    have had the suffix while charging nonzero completion (edge cases
    during rollout), and future free models may drop the suffix. The
    pricing fields are the authoritative signal.
    """
    return _is_zero_price(entry.pricing_prompt) and _is_zero_price(
        entry.pricing_completion
    )


def filter_free(entries: list[RosterEntry]) -> list[RosterEntry]:
    return [e for e in entries if is_free(e)]


def diff_rosters(
    old: list[RosterEntry], new: list[RosterEntry]
) -> RosterDiff:
    """Compute the additive / subtractive / modified delta.

    Pure function of two input lists. Returns entries in deterministic
    sorted-by-id order so CHANGES.md is reproducible.
    """
    old_by_id = {e.id: e for e in old}
    new_by_id = {e.id: e for e in new}

    added = [
        new_by_id[mid]
        for mid in sorted(new_by_id.keys() - old_by_id.keys())
    ]
    removed = [
        old_by_id[mid]
        for mid in sorted(old_by_id.keys() - new_by_id.keys())
    ]

    pricing_changed: list[tuple[RosterEntry, RosterEntry]] = []
    context_changed: list[tuple[RosterEntry, RosterEntry]] = []
    for mid in sorted(new_by_id.keys() & old_by_id.keys()):
        o, n = old_by_id[mid], new_by_id[mid]
        if (o.pricing_prompt, o.pricing_completion) != (
            n.pricing_prompt,
            n.pricing_completion,
        ):
            pricing_changed.append((o, n))
        if o.context_length != n.context_length:
            context_changed.append((o, n))

    return RosterDiff(
        added=added,
        removed=removed,
        pricing_changed=pricing_changed,
        context_changed=context_changed,
    )


# ---------------------------------------------------------------------------
# Rendering + I/O
# ---------------------------------------------------------------------------


def format_markdown(diff: RosterDiff, *, now: datetime) -> str:
    """Render a diff as a prepend-able markdown section.

    Layout::

        ## 2026-04-20T12:34:56Z

        ### Removed (N) [warning marker]
        - `<id>` (was: ctx=N, prompt=P, completion=C)

        ### Added (N)
        - `<id>` (ctx=N)

        ### Pricing changed (N)
        - `<id>`: prompt $old → $new, completion $old → $new

        ### Context changed (N)
        - `<id>`: old → new

    Removed goes first because that's the most operationally urgent
    category (free-tier withdrawal = user-visible breakage). Added is
    next, then the softer "still present but changed" categories.
    Empty categories are omitted. Returns the empty string when the
    diff is empty.
    """
    if diff.is_empty():
        return ""
    ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    lines: list[str] = [f"## {ts}", ""]

    if diff.removed:
        lines.append(f"### Removed ({len(diff.removed)}) ⚠️")
        lines.append("")
        for e in diff.removed:
            lines.append(
                f"- `{e.id}` (was: ctx={e.context_length}, "
                f"prompt={e.pricing_prompt}, completion={e.pricing_completion})"
            )
        lines.append("")
    if diff.added:
        lines.append(f"### Added ({len(diff.added)})")
        lines.append("")
        for e in diff.added:
            lines.append(f"- `{e.id}` (ctx={e.context_length})")
        lines.append("")
    if diff.pricing_changed:
        lines.append(f"### Pricing changed ({len(diff.pricing_changed)})")
        lines.append("")
        for o, n in diff.pricing_changed:
            lines.append(
                f"- `{n.id}`: prompt {o.pricing_prompt} → {n.pricing_prompt}, "
                f"completion {o.pricing_completion} → {n.pricing_completion}"
            )
        lines.append("")
    if diff.context_changed:
        lines.append(f"### Context changed ({len(diff.context_changed)})")
        lines.append("")
        for o, n in diff.context_changed:
            lines.append(
                f"- `{n.id}`: {o.context_length} → {n.context_length}"
            )
        lines.append("")

    return "\n".join(lines) + "\n"


def load_snapshot(path: Path) -> list[RosterEntry]:
    """Load a previously-saved snapshot. Returns [] when the file is
    absent (first-run case handled by caller)."""
    if not path.exists():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    entries_raw = raw.get("entries") or []
    out: list[RosterEntry] = []
    for e in entries_raw:
        if not isinstance(e, dict) or "id" not in e:
            continue
        out.append(
            RosterEntry(
                id=e["id"],
                context_length=e.get("context_length"),
                pricing_prompt=e.get("pricing_prompt", ""),
                pricing_completion=e.get("pricing_completion", ""),
            )
        )
    return out


def save_snapshot(
    path: Path, entries: list[RosterEntry], *, fetched_at: datetime
) -> None:
    """Atomic write via ``tmp → replace``. Entries are id-sorted so
    repeated runs with unchanged rosters produce byte-identical files
    (apart from the ``fetched_at`` timestamp)."""
    payload = {
        "schema_version": 1,
        "fetched_at": fetched_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "entries": [
            {
                "id": e.id,
                "context_length": e.context_length,
                "pricing_prompt": e.pricing_prompt,
                "pricing_completion": e.pricing_completion,
            }
            for e in sorted(entries, key=lambda x: x.id)
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)


def prepend_changes(changes_path: Path, section: str) -> None:
    """Prepend ``section`` below the markdown header.

    Prepend (not append) so the newest run is at the top — matches
    the convention in CHANGELOG.md and makes ``less CHANGES.md``
    immediately useful without scrolling.

    No-op when ``section`` is empty (i.e., diff was empty).
    """
    if not section:
        return
    changes_path.parent.mkdir(parents=True, exist_ok=True)
    if changes_path.exists():
        existing = changes_path.read_text(encoding="utf-8")
        # Split: header up to first "## " (exclusive) + rest.
        idx = existing.find("\n## ")
        if idx == -1:
            # No previous sections yet — header only. Append.
            new_content = existing.rstrip() + "\n\n" + section
        else:
            new_content = (
                existing[: idx + 1] + section + existing[idx + 1 :]
            )
    else:
        new_content = _CHANGES_HEADER + section
    changes_path.write_text(new_content, encoding="utf-8")


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def run(
    *,
    url: str,
    snapshot: Path,
    changes: Path,
    dry_run: bool,
    client: httpx.Client | None = None,
    now: datetime | None = None,
) -> RosterDiff:
    """Full run: fetch → parse → filter → diff → (maybe) persist.

    ``client`` and ``now`` are injectable for tests. Callers that don't
    pass them get a default 30s-timeout client and UTC wall time.

    Returns the diff so tests / cron drivers can inspect without
    re-reading the just-written files.
    """
    own_client = client is None
    if client is None:
        client = httpx.Client(timeout=30.0)
    try:
        resp = client.get(url)
        resp.raise_for_status()
        raw = resp.json()
    finally:
        if own_client:
            client.close()

    new_entries = filter_free(parse_models(raw))
    snapshot_existed = snapshot.exists()
    old_entries = load_snapshot(snapshot)
    diff = diff_rosters(old_entries, new_entries)

    if dry_run:
        return diff

    effective_now = now or datetime.now(UTC)
    save_snapshot(snapshot, new_entries, fetched_at=effective_now)
    # Only log deltas once a baseline exists — otherwise the first run
    # would emit "Added: N" for every free model, which is the
    # baseline itself, not actual churn.
    if snapshot_existed:
        prepend_changes(changes, format_markdown(diff, now=effective_now))
    return diff


def _format_summary(diff: RosterDiff) -> str:
    """Compact one-line summary for cron stdout."""
    if diff.is_empty():
        return "no changes"
    parts: list[str] = []
    if diff.added:
        parts.append(f"+{len(diff.added)}")
    if diff.removed:
        parts.append(f"-{len(diff.removed)}")
    if diff.pricing_changed:
        parts.append(f"Δprice={len(diff.pricing_changed)}")
    if diff.context_changed:
        parts.append(f"Δctx={len(diff.context_changed)}")
    return "changes: " + " ".join(parts)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=(
            "Fetch OpenRouter /api/v1/models, filter to free tier, "
            "diff against committed snapshot, record changes."
        )
    )
    p.add_argument(
        "--url",
        default=OPENROUTER_MODELS_URL,
        help=f"override endpoint (default: {OPENROUTER_MODELS_URL})",
    )
    p.add_argument(
        "--snapshot",
        type=Path,
        default=DEFAULT_SNAPSHOT,
        help="path to snapshot JSON",
    )
    p.add_argument(
        "--changes",
        type=Path,
        default=DEFAULT_CHANGES,
        help="path to CHANGES.md log",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="fetch and diff but do not write any files",
    )
    args = p.parse_args(argv)

    try:
        diff = run(
            url=args.url,
            snapshot=args.snapshot,
            changes=args.changes,
            dry_run=args.dry_run,
        )
    except httpx.HTTPError as exc:
        print(f"fetch failed: {exc}", file=sys.stderr)
        return 2

    print(_format_summary(diff))
    # Removed models are the operationally interesting category —
    # surface each one on stdout so a cron-mail consumer sees them
    # without grep'ing CHANGES.md.
    for e in diff.removed:
        print(f"  REMOVED: {e.id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
