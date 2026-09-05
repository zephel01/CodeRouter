"""Tests for ``coderouter rollback`` and the guarded apply (v2.14.0).

Two properties matter here and neither is about the happy path:

1. A restore is a **swap**, so running it twice is a no-op overall. That
   is what makes it safe to run when you are not sure which version you
   want.
2. A ``doctor --apply`` that fails partway must leave **nothing** behind.
   Before this change, a write failure on the second target file left the
   first one rewritten, with no command able to name or undo it.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from coderouter.rollback import (
    backup_for,
    discover_managed_files,
    exit_code_for_rollback,
    format_rollback_report,
    restore_file,
    restore_many,
)


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# restore_file
# ---------------------------------------------------------------------------


def test_backup_for_appends_the_suffix_to_the_full_name() -> None:
    """``providers.yaml`` → ``providers.yaml.bak``, not ``providers.bak``."""
    assert backup_for("/tmp/providers.yaml").name == "providers.yaml.bak"


def test_restore_puts_the_old_bytes_back(tmp_path: Path) -> None:
    target = _write(tmp_path / "providers.yaml", "new\n")
    _write(tmp_path / "providers.yaml.bak", "old\n")
    outcome = restore_file(target)
    assert outcome.status == "restored"
    assert target.read_text() == "old\n"


def test_restore_is_its_own_inverse(tmp_path: Path) -> None:
    """Run it twice and you are exactly where you started."""
    target = _write(tmp_path / "providers.yaml", "new\n")
    backup = _write(tmp_path / "providers.yaml.bak", "old\n")
    restore_file(target)
    assert (target.read_text(), backup.read_text()) == ("old\n", "new\n")
    restore_file(target)
    assert (target.read_text(), backup.read_text()) == ("new\n", "old\n")


def test_dry_run_touches_nothing(tmp_path: Path) -> None:
    target = _write(tmp_path / "providers.yaml", "new\n")
    backup = _write(tmp_path / "providers.yaml.bak", "old\n")
    outcome = restore_file(target, dry_run=True)
    assert outcome.status == "would-restore"
    assert target.read_text() == "new\n"
    assert backup.read_text() == "old\n"


def test_missing_backup_is_reported_not_raised(tmp_path: Path) -> None:
    target = _write(tmp_path / "providers.yaml", "new\n")
    outcome = restore_file(target)
    assert outcome.status == "no-backup"
    assert target.read_text() == "new\n"


def test_identical_backup_is_distinguished_from_no_backup(tmp_path: Path) -> None:
    """An operator seeing "nothing happened" needs to know which case it was."""
    target = _write(tmp_path / "providers.yaml", "same\n")
    _write(tmp_path / "providers.yaml.bak", "same\n")
    assert restore_file(target).status == "identical"


def test_restore_recreates_a_deleted_file(tmp_path: Path) -> None:
    """``--force`` that removed a file entirely is still recoverable."""
    _write(tmp_path / ".envrc.bak", "export FOO=1\n")
    outcome = restore_file(tmp_path / ".envrc")
    assert outcome.status == "restored"
    assert (tmp_path / ".envrc").read_text() == "export FOO=1\n"


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes only")
def test_restore_preserves_the_file_mode(tmp_path: Path) -> None:
    """.envrc is created 0600 because it carries a token — keep it that way."""
    target = _write(tmp_path / ".envrc", "new\n")
    backup = _write(tmp_path / ".envrc.bak", "old\n")
    backup.chmod(0o600)
    target.chmod(0o644)
    restore_file(target)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_restore_many_continues_past_a_file_with_no_backup(tmp_path: Path) -> None:
    a = _write(tmp_path / "a.yaml", "new\n")
    _write(tmp_path / "a.yaml.bak", "old\n")
    b = _write(tmp_path / "b.yaml", "untouched\n")
    outcomes = restore_many([a, b])
    assert [o.status for o in outcomes] == ["restored", "no-backup"]
    assert b.read_text() == "untouched\n"


# ---------------------------------------------------------------------------
# Discovery, exit codes, formatting
# ---------------------------------------------------------------------------


def test_discovery_includes_the_user_layer_capabilities_file() -> None:
    """doctor --apply writes there regardless of which config was loaded."""
    paths = discover_managed_files(config_path="/etc/coderouter/providers.yaml")
    assert Path.home() / ".coderouter-t" / "model-capabilities.yaml" in paths


def test_discovery_adds_the_workspace_outputs(tmp_path: Path) -> None:
    paths = discover_managed_files(config_path=None, workspace=tmp_path)
    assert tmp_path / ".vscode" / "settings.json" in paths
    assert tmp_path / ".envrc" in paths


def test_discovery_does_not_repeat_a_path() -> None:
    shared = Path.home() / ".coderouter-t" / "model-capabilities.yaml"
    paths = discover_managed_files(config_path=shared)
    assert paths.count(shared) == 1


def test_exit_code_distinguishes_nothing_to_do_from_success(tmp_path: Path) -> None:
    target = _write(tmp_path / "a.yaml", "new\n")
    assert exit_code_for_rollback(restore_many([target])) == 2  # no backup
    _write(tmp_path / "a.yaml.bak", "old\n")
    assert exit_code_for_rollback(restore_many([target])) == 0


def test_exit_code_is_1_when_a_restore_fails(tmp_path: Path) -> None:
    from coderouter.rollback import RestoreOutcome

    failed = RestoreOutcome(tmp_path / "x", tmp_path / "x.bak", "failed", "boom")
    assert exit_code_for_rollback([failed]) == 1


def test_report_names_every_file_it_touched(tmp_path: Path) -> None:
    target = _write(tmp_path / "providers.yaml", "new\n")
    _write(tmp_path / "providers.yaml.bak", "old\n")
    text = format_rollback_report(restore_many([target]), dry_run=False)
    assert "providers.yaml" in text
    assert "restored: 1 file(s)" in text


# ---------------------------------------------------------------------------
# The guarded apply helper (the end-to-end case lives in test_doctor_apply.py,
# next to the _FakeReport harness that can drive the real write loop)
# ---------------------------------------------------------------------------


def test_undo_partial_apply_restores_and_deletes(tmp_path: Path) -> None:
    """The helper is what turns a mid-loop failure into a clean no-op."""
    from coderouter.doctor_apply import _undo_partial_apply

    overwritten = _write(tmp_path / "providers.yaml", "HALF-APPLIED\n")
    _write(tmp_path / "providers.yaml.bak", "ORIGINAL\n")
    created = _write(tmp_path / "model-capabilities.yaml", "brand new\n")

    reverted = _undo_partial_apply(
        {str(overwritten): str(tmp_path / "providers.yaml.bak")}, [created]
    )

    assert reverted == 2
    assert overwritten.read_text() == "ORIGINAL\n"
    assert not created.exists()


def test_undo_partial_apply_is_best_effort(tmp_path: Path) -> None:
    """A rollback that raised would mask the write error the user needs."""
    from coderouter.doctor_apply import _undo_partial_apply

    missing = tmp_path / "gone.yaml"
    reverted = _undo_partial_apply(
        {str(missing): str(tmp_path / "does-not-exist.bak")},
        [tmp_path / "also-missing.yaml"],
    )
    # The unlink of a non-existent path succeeds (missing_ok), the copy does
    # not — but neither raises.
    assert reverted == 1
