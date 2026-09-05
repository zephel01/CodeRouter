"""Restore the files CodeRouter rewrote (v2.14.0).

Two commands in this codebase mutate files the operator owns:
``doctor --apply`` (providers.yaml, model-capabilities.yaml) and
``vscode-init`` (.vscode/settings.json, .envrc). Both already wrote a
``.bak`` sibling before touching anything — and then left the operator to
find it and copy it back by hand. The stated reasoning was that anyone
using git already has versioned history. That is true right up until the
file being rewritten is ``~/.coderouter-t/providers.yaml``, which is not in
anybody's repo, or until ``--force`` removes a line the operator wanted
(v2.11's ``.envrc`` incident, H-11).

So this module supplies the missing half: a way to get the old bytes back.

Swap, not overwrite
-------------------
Restoring does not discard the current file — it writes the current
contents back out as the new ``.bak`` before putting the old bytes in
place. Rollback therefore toggles: run it twice and you are where you
started. A one-way restore turns "I wanted to compare" into "I destroyed
the newer version", which is the same class of mistake this module exists
to undo.

Metadata
--------
Restores go through :func:`shutil.copy2`, so mode and mtime survive the
round trip. That matters for ``.envrc``, which is deliberately created
0600 because it carries ``ANTHROPIC_AUTH_TOKEN``; a restore that reset it
to the default umask would quietly widen a credential file.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "BACKUP_SUFFIX",
    "RestoreOutcome",
    "backup_for",
    "discover_managed_files",
    "exit_code_for_rollback",
    "format_rollback_report",
    "restore_file",
    "restore_many",
]

# Both writers use the same convention: the backup is the original name
# with ``.bak`` appended (``providers.yaml.bak``, ``settings.json.bak``),
# single generation, overwritten on each apply.
BACKUP_SUFFIX = ".bak"


def backup_for(path: str | os.PathLike[str]) -> Path:
    """Return the ``.bak`` sibling of ``path``."""
    p = Path(path)
    return p.with_name(p.name + BACKUP_SUFFIX)


@dataclass(frozen=True)
class RestoreOutcome:
    """What happened (or would happen) for one managed file.

    ``status`` is one of ``restored`` / ``would-restore`` / ``no-backup`` /
    ``identical`` / ``failed``. ``identical`` is reported rather than
    silently skipped: an operator who runs rollback and sees nothing wants
    to know the difference between "there was no backup" and "the backup
    already matches what is on disk".
    """

    path: Path
    backup: Path
    status: str
    detail: str = ""


def discover_managed_files(
    *,
    config_path: str | os.PathLike[str] | None = None,
    workspace: str | os.PathLike[str] | None = None,
) -> list[Path]:
    """List the files CodeRouter is known to rewrite, in restore order.

    ``config_path`` should be the path the loader actually read — pass it
    in rather than re-deriving it here, for the same reason
    ``doctor --apply`` delegates to ``resolve_config_path``: a second copy
    of the search order is exactly how the v2.13.0 CWD opt-in produced a
    file mismatch.

    The user-layer ``model-capabilities.yaml`` is always included because
    ``doctor --apply`` writes there regardless of which config was loaded.
    """
    paths: list[Path] = []
    if config_path:
        paths.append(Path(config_path).expanduser())
    paths.append(Path.home() / ".coderouter-t" / "model-capabilities.yaml")
    if workspace:
        root = Path(workspace).expanduser()
        paths.append(root / ".vscode" / "settings.json")
        paths.append(root / ".envrc")
    # Preserve order while dropping duplicates (config_path can legitimately
    # be the user-layer path in some setups).
    seen: set[Path] = set()
    unique: list[Path] = []
    for p in paths:
        if p not in seen:
            seen.add(p)
            unique.append(p)
    return unique


def restore_file(
    path: str | os.PathLike[str], *, dry_run: bool = False
) -> RestoreOutcome:
    """Swap ``path`` with its ``.bak`` sibling.

    The current contents become the new backup, so the operation is its
    own inverse. A missing backup is not an error — it just means this
    file was never rewritten by us.
    """
    target = Path(path).expanduser()
    backup = backup_for(target)

    if not backup.is_file():
        return RestoreOutcome(target, backup, "no-backup", "no .bak sibling found")

    backup_bytes = backup.read_bytes()
    current_bytes = target.read_bytes() if target.is_file() else None
    if current_bytes == backup_bytes:
        return RestoreOutcome(
            target, backup, "identical", "the backup already matches the live file"
        )

    if dry_run:
        return RestoreOutcome(
            target,
            backup,
            "would-restore",
            f"{len(backup_bytes)} bytes would replace "
            f"{'nothing (file absent)' if current_bytes is None else f'{len(current_bytes)} bytes'}",
        )

    try:
        # Write the current contents out as the new backup FIRST. If the
        # copy of the old bytes then fails, the operator has lost nothing:
        # both versions are still on disk.
        if current_bytes is not None:
            tmp = backup.with_name(backup.name + ".swap")
            shutil.copy2(target, tmp)
            shutil.copy2(backup, target)
            os.replace(tmp, backup)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup, target)
            backup.unlink()
    except OSError as exc:
        return RestoreOutcome(target, backup, "failed", str(exc))

    return RestoreOutcome(
        target,
        backup,
        "restored",
        "current contents kept as the new .bak — re-run to toggle back",
    )


def restore_many(
    paths: Iterable[str | os.PathLike[str]], *, dry_run: bool = False
) -> list[RestoreOutcome]:
    """Restore each path, continuing past files that have no backup."""
    return [restore_file(p, dry_run=dry_run) for p in paths]


_STATUS_GLYPH = {
    "restored": "RESTORED",
    "would-restore": "WOULD   ",
    "identical": "SAME    ",
    "no-backup": "SKIP    ",
    "failed": "FAILED  ",
}


def format_rollback_report(
    outcomes: Sequence[RestoreOutcome], *, dry_run: bool
) -> str:
    """Render outcomes for the terminal."""
    header = "coderouter rollback" + (" --dry-run" if dry_run else "")
    lines = ["", header, "=" * 52]
    for outcome in outcomes:
        glyph = _STATUS_GLYPH.get(outcome.status, "????    ")
        lines.append(f"[{glyph}] {outcome.path}")
        if outcome.detail:
            lines.append(f"           {outcome.detail}")
    restored = sum(o.status in {"restored", "would-restore"} for o in outcomes)
    failed = sum(o.status == "failed" for o in outcomes)
    lines.append("-" * 52)
    verb = "would restore" if dry_run else "restored"
    lines.append(f"{verb}: {restored} file(s); failed: {failed}")
    lines.append("")
    return "\n".join(lines)


def exit_code_for_rollback(outcomes: Sequence[RestoreOutcome]) -> int:
    """0 when something was (or would be) restored and nothing failed.

    A run where every file reports ``no-backup`` exits 2 — "nothing to do"
    is a different answer from "done", and a wrapper script should be able
    to tell them apart without parsing text.
    """
    if any(o.status == "failed" for o in outcomes):
        return 1
    if any(o.status in {"restored", "would-restore"} for o in outcomes):
        return 0
    return 2
