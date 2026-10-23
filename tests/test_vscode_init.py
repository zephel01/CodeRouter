"""v2.10-A: unit tests for ``coderouter vscode-init``.

Two layers of tests here:

1. ``run_vscode_init`` — the module entry point. Exercised via
   :func:`coderouter.vscode_init.run_vscode_init` directly, no argparse
   in the loop. Covers: fresh workspace, merge into existing
   ``settings.json``, conflict detection, ``--force`` overwrite,
   ``--dry-run`` byte-parity, ``.envrc`` generation, ``--profile``
   propagation, and OS-key coverage (osx / linux / windows all written
   regardless of the current machine's OS).

2. ``cli.main(["vscode-init", ...])`` — the CLI wrapper. Only pinning
   the argparse plumbing: flag names, exit-code propagation, missing
   target directory. The actual scaffolding behavior is covered by
   layer 1.

The tests deliberately avoid any real filesystem side-effects outside
``tmp_path`` — the vscode_init module writes via ``os.replace`` on the
same filesystem so it's already safe, but we don't need to trust that
for this suite.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from coderouter import cli
from coderouter.vscode_init import (
    DEFAULT_PORT,
    DEFAULT_TOKEN,
    exit_code_for,
    format_result,
    run_vscode_init,
)

_SKIP_ON_WINDOWS = pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX mode bits are not meaningful on Windows",
)


def _mode_of(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)

# ---------------------------------------------------------------------------
# run_vscode_init — happy paths
# ---------------------------------------------------------------------------


def _read_settings(target: Path) -> dict:
    return json.loads((target / ".vscode" / "settings.json").read_text())


def test_fresh_workspace_creates_settings_json_with_all_three_os_keys(
    tmp_path: Path,
) -> None:
    """No existing settings.json → all three OS env keys populated.

    Cross-OS coverage matters because a .vscode/ folder committed on a
    Mac must still work when opened on a Linux CI runner or a Windows
    laptop. Writing all three keys means no second scaffolder run.
    """
    result = run_vscode_init(tmp_path)
    assert exit_code_for(result) == 0

    settings = _read_settings(tmp_path)
    for os_key in (
        "terminal.integrated.env.osx",
        "terminal.integrated.env.linux",
        "terminal.integrated.env.windows",
    ):
        assert os_key in settings
        assert settings[os_key]["ANTHROPIC_BASE_URL"] == f"http://localhost:{DEFAULT_PORT}"
        assert settings[os_key]["ANTHROPIC_AUTH_TOKEN"] == DEFAULT_TOKEN


def test_fresh_workspace_reports_created_action(tmp_path: Path) -> None:
    """A new settings.json should be a 'created' outcome, not 'updated'."""
    result = run_vscode_init(tmp_path)
    settings_outcome = next(
        o for o in result.outcomes if o.path.name == "settings.json"
    )
    assert settings_outcome.action == "created"
    assert settings_outcome.reason == ""


def test_custom_port_flows_into_base_url(tmp_path: Path) -> None:
    """--port 4000 → ANTHROPIC_BASE_URL=http://localhost:4000.

    Necessary because ``coderouter serve`` alone defaults to 4000 while
    the docs teach 8088; users who don't override serve's default need
    a matching --port here.
    """
    run_vscode_init(tmp_path, port=4000)
    settings = _read_settings(tmp_path)
    assert (
        settings["terminal.integrated.env.osx"]["ANTHROPIC_BASE_URL"]
        == "http://localhost:4000"
    )


def test_profile_arg_adds_coderouter_mode(tmp_path: Path) -> None:
    """--profile foo injects CODEROUTER_MODE=foo alongside the base vars."""
    run_vscode_init(tmp_path, profile="local-first")
    settings = _read_settings(tmp_path)
    assert (
        settings["terminal.integrated.env.linux"]["CODEROUTER_MODE"]
        == "local-first"
    )


def test_no_profile_arg_omits_coderouter_mode(tmp_path: Path) -> None:
    """Absent --profile → no CODEROUTER_MODE key (don't leak stale values)."""
    run_vscode_init(tmp_path)
    settings = _read_settings(tmp_path)
    assert "CODEROUTER_MODE" not in settings["terminal.integrated.env.osx"]


# ---------------------------------------------------------------------------
# Merge semantics: preserve unrelated keys
# ---------------------------------------------------------------------------


def test_merge_preserves_unrelated_top_level_keys(tmp_path: Path) -> None:
    """Existing editor.fontSize etc. must survive our merge."""
    vscode_dir = tmp_path / ".vscode"
    vscode_dir.mkdir()
    (vscode_dir / "settings.json").write_text(
        json.dumps({"editor.fontSize": 14, "python.testing.pytestEnabled": True}),
        encoding="utf-8",
    )

    result = run_vscode_init(tmp_path)
    assert exit_code_for(result) == 0

    settings = _read_settings(tmp_path)
    assert settings["editor.fontSize"] == 14
    assert settings["python.testing.pytestEnabled"] is True
    # And our keys landed:
    assert "terminal.integrated.env.osx" in settings


def test_merge_preserves_unrelated_env_keys_inside_terminal_block(
    tmp_path: Path,
) -> None:
    """A user's PATH tweak inside terminal.integrated.env.osx must survive."""
    vscode_dir = tmp_path / ".vscode"
    vscode_dir.mkdir()
    (vscode_dir / "settings.json").write_text(
        json.dumps(
            {
                "terminal.integrated.env.osx": {
                    "PATH": "/opt/homebrew/bin:${env:PATH}",
                    "MY_OTHER_VAR": "keep_me",
                }
            }
        ),
        encoding="utf-8",
    )

    run_vscode_init(tmp_path)
    settings = _read_settings(tmp_path)
    osx = settings["terminal.integrated.env.osx"]
    # User's env vars unharmed:
    assert osx["PATH"] == "/opt/homebrew/bin:${env:PATH}"
    assert osx["MY_OTHER_VAR"] == "keep_me"
    # And ours added:
    assert osx["ANTHROPIC_BASE_URL"] == f"http://localhost:{DEFAULT_PORT}"


# ---------------------------------------------------------------------------
# Conflict detection & --force
# ---------------------------------------------------------------------------


def test_conflict_when_existing_base_url_differs(tmp_path: Path) -> None:
    """Existing ANTHROPIC_BASE_URL with a different value → conflict, no write.

    This is the whole point of the conflict-aware design: if a user
    already pointed at a different router / port and re-runs the
    scaffolder, we ask them to confirm rather than silently retargeting.
    """
    vscode_dir = tmp_path / ".vscode"
    vscode_dir.mkdir()
    original = {
        "terminal.integrated.env.osx": {"ANTHROPIC_BASE_URL": "http://elsewhere:9999"}
    }
    settings_path = vscode_dir / "settings.json"
    settings_path.write_text(json.dumps(original), encoding="utf-8")

    result = run_vscode_init(tmp_path)
    assert result.has_conflicts
    assert exit_code_for(result) == 2

    # File must be unchanged.
    assert json.loads(settings_path.read_text()) == original


def test_conflict_includes_dotted_path_in_reason(tmp_path: Path) -> None:
    """The reason string should point at the specific offending key."""
    vscode_dir = tmp_path / ".vscode"
    vscode_dir.mkdir()
    (vscode_dir / "settings.json").write_text(
        json.dumps(
            {"terminal.integrated.env.osx": {"ANTHROPIC_BASE_URL": "http://x:9999"}}
        ),
        encoding="utf-8",
    )
    result = run_vscode_init(tmp_path)
    settings_outcome = next(
        o for o in result.outcomes if o.path.name == "settings.json"
    )
    assert settings_outcome.action == "conflict"
    assert (
        "terminal.integrated.env.osx.ANTHROPIC_BASE_URL"
        in settings_outcome.reason
    )


def test_force_overwrites_conflicting_values(tmp_path: Path) -> None:
    """--force must actually write the new value."""
    vscode_dir = tmp_path / ".vscode"
    vscode_dir.mkdir()
    (vscode_dir / "settings.json").write_text(
        json.dumps(
            {"terminal.integrated.env.osx": {"ANTHROPIC_BASE_URL": "http://x:9999"}}
        ),
        encoding="utf-8",
    )

    result = run_vscode_init(tmp_path, force=True)
    assert exit_code_for(result) == 0

    settings = _read_settings(tmp_path)
    assert (
        settings["terminal.integrated.env.osx"]["ANTHROPIC_BASE_URL"]
        == f"http://localhost:{DEFAULT_PORT}"
    )


def test_same_value_is_unchanged_not_conflict(tmp_path: Path) -> None:
    """Idempotency: existing value == desired value → 'unchanged', exit 0.

    A second identical run of ``vscode-init`` should be a no-op, not a
    spurious conflict. This is what makes it safe to include in
    onboarding scripts.
    """
    run_vscode_init(tmp_path)
    result = run_vscode_init(tmp_path)  # second run
    settings_outcome = next(
        o for o in result.outcomes if o.path.name == "settings.json"
    )
    assert settings_outcome.action == "unchanged"
    assert exit_code_for(result) == 0


# ---------------------------------------------------------------------------
# --dry-run
# ---------------------------------------------------------------------------


def test_dry_run_does_not_write_anything(tmp_path: Path) -> None:
    """--dry-run must not create the .vscode/ folder or settings.json."""
    result = run_vscode_init(tmp_path, dry_run=True)
    assert not (tmp_path / ".vscode").exists()
    # Yet the outcome is still reported as if it would have been created:
    settings_outcome = next(
        o for o in result.outcomes if o.path.name == "settings.json"
    )
    assert settings_outcome.action == "created"
    assert settings_outcome.diff  # unified diff was computed


def test_dry_run_computes_same_diff_as_real_run(tmp_path: Path) -> None:
    """--dry-run's diff must equal the diff a real run would emit.

    This is the "byte-identical to write path minus os.replace"
    invariant. If they diverge, users can't trust the preview.
    """
    # Real run in one workspace.
    real_target = tmp_path / "real"
    real_target.mkdir()
    real_result = run_vscode_init(real_target)
    real_diff = next(
        o.diff for o in real_result.outcomes if o.path.name == "settings.json"
    )

    # Dry run in a fresh workspace with same inputs.
    dry_target = tmp_path / "dry"
    dry_target.mkdir()
    dry_result = run_vscode_init(dry_target, dry_run=True)
    dry_diff = next(
        o.diff for o in dry_result.outcomes if o.path.name == "settings.json"
    )

    # The path fragments in the diff header will differ (different
    # dirnames), but the fromfile / tofile lines are name-only, so the
    # payload is identical.
    assert real_diff == dry_diff


# ---------------------------------------------------------------------------
# .envrc
# ---------------------------------------------------------------------------


def test_with_envrc_creates_dotfile(tmp_path: Path) -> None:
    """--with-envrc emits a .envrc with the two exports."""
    result = run_vscode_init(tmp_path, with_envrc=True)
    assert exit_code_for(result) == 0

    envrc = (tmp_path / ".envrc").read_text()
    assert f'export ANTHROPIC_BASE_URL="http://localhost:{DEFAULT_PORT}"' in envrc
    assert f'export ANTHROPIC_AUTH_TOKEN="{DEFAULT_TOKEN}"' in envrc


def test_envrc_omitted_by_default(tmp_path: Path) -> None:
    """No --with-envrc → no .envrc file (don't surprise non-direnv users)."""
    run_vscode_init(tmp_path)
    assert not (tmp_path / ".envrc").exists()


def test_envrc_profile_adds_coderouter_mode_line(tmp_path: Path) -> None:
    """--profile threads through into .envrc as well as settings.json."""
    run_vscode_init(tmp_path, with_envrc=True, profile="cloud-fallback")
    envrc = (tmp_path / ".envrc").read_text()
    assert 'export CODEROUTER_MODE="cloud-fallback"' in envrc


def test_existing_envrc_conflict_without_force(tmp_path: Path) -> None:
    """Existing .envrc with different contents → conflict, no write."""
    envrc_path = tmp_path / ".envrc"
    envrc_path.write_text("# user's own thing\nexport FOO=bar\n", encoding="utf-8")

    result = run_vscode_init(tmp_path, with_envrc=True)
    assert result.has_conflicts
    envrc_outcome = next(o for o in result.outcomes if o.path.name == ".envrc")
    assert envrc_outcome.action == "conflict"
    # File untouched:
    assert envrc_path.read_text() == "# user's own thing\nexport FOO=bar\n"


def test_existing_envrc_force_adopts_without_destroying(tmp_path: Path) -> None:
    """v2.14.0 behaviour change: --force APPENDS instead of replacing.

    Before v2.14.0 this test asserted ``"FOO=bar" not in envrc`` — the
    whole-file replace that made H-11 possible. The managed block is now
    fenced, so ``--force`` means "adopt a file I did not write" and the
    operator's own lines survive it. A backup is still taken, and the
    generated exports still land, so nothing the flag was *for* is lost.
    """
    (tmp_path / ".envrc").write_text("export FOO=bar\n", encoding="utf-8")
    result = run_vscode_init(tmp_path, with_envrc=True, force=True)
    assert exit_code_for(result) == 0
    envrc = (tmp_path / ".envrc").read_text()
    assert "ANTHROPIC_BASE_URL" in envrc
    assert "export FOO=bar" in envrc
    # And the managed part is fenced, so the next run touches only that.
    assert "# BEGIN coderouter-managed" in envrc
    assert "# END coderouter-managed" in envrc


# ---------------------------------------------------------------------------
# H-11: --force must not destroy data, loosen permissions, or leave debris
#
# ``--force`` on .envrc is a whole-file replace, not a merge. Before
# v2.11 the previous contents were simply gone — including the
# ``source_env_if_exists .envrc.local`` line docs/guides/vscode.md tells
# users to add — and os.replace reset the file's mode to the umask
# default (0600 → 0644 for a file holding ANTHROPIC_AUTH_TOKEN).
# ---------------------------------------------------------------------------


def test_force_backs_up_existing_envrc(tmp_path: Path) -> None:
    """--force stashes the exact previous bytes in .envrc.bak."""
    original = (
        "# hand-written\n"
        'export ANTHROPIC_BASE_URL="http://localhost:9999"\n'
        "source_env_if_exists .envrc.local\n"
    )
    (tmp_path / ".envrc").write_text(original, encoding="utf-8")

    result = run_vscode_init(tmp_path, with_envrc=True, force=True)
    assert exit_code_for(result) == 0

    backup = tmp_path / ".envrc.bak"
    assert backup.exists()
    assert backup.read_text(encoding="utf-8") == original


def test_existing_envrc_force_preserves_original_in_backup(tmp_path: Path) -> None:
    """Companion to test_existing_envrc_force_overwrites.

    That test pins "the live file no longer has FOO=bar". This one pins
    the other half of the contract: FOO=bar is not *lost*, it moved to
    the backup.
    """
    (tmp_path / ".envrc").write_text("export FOO=bar\n", encoding="utf-8")
    run_vscode_init(tmp_path, with_envrc=True, force=True)

    assert (tmp_path / ".envrc.bak").read_text(encoding="utf-8") == "export FOO=bar\n"


def test_force_backup_not_created_in_dry_run(tmp_path: Path) -> None:
    """--dry-run --force must not touch the filesystem, backups included."""
    original = "export FOO=bar\n"
    (tmp_path / ".envrc").write_text(original, encoding="utf-8")

    result = run_vscode_init(tmp_path, with_envrc=True, force=True, dry_run=True)
    assert exit_code_for(result) == 0

    assert not (tmp_path / ".envrc.bak").exists()
    assert (tmp_path / ".envrc").read_text(encoding="utf-8") == original


def test_backup_path_reported_in_outcome(tmp_path: Path) -> None:
    """The user has to be told where the old contents went."""
    (tmp_path / ".envrc").write_text("export FOO=bar\n", encoding="utf-8")
    result = run_vscode_init(tmp_path, with_envrc=True, force=True)

    envrc_outcome = next(o for o in result.outcomes if o.path.name == ".envrc")
    assert ".envrc.bak" in envrc_outcome.reason
    # ...and it must survive into what the CLI actually prints.
    text = format_result(result, dry_run=False, port=DEFAULT_PORT)
    assert ".envrc.bak" in text


def test_conflict_message_warns_whole_file_is_replaced(tmp_path: Path) -> None:
    """"Re-run with --force to overwrite" undersold the blast radius."""
    (tmp_path / ".envrc").write_text(
        "source_env_if_exists .envrc.local\n", encoding="utf-8"
    )
    result = run_vscode_init(tmp_path, with_envrc=True)

    envrc_outcome = next(o for o in result.outcomes if o.path.name == ".envrc")
    assert envrc_outcome.action == "conflict"
    assert ".envrc.bak" in envrc_outcome.reason


@_SKIP_ON_WINDOWS
def test_envrc_mode_preserved_on_force_overwrite(tmp_path: Path) -> None:
    """os.replace swaps inodes — the 0600 must not decay to 0644."""
    envrc = tmp_path / ".envrc"
    envrc.write_text('export ANTHROPIC_AUTH_TOKEN="sk-REAL-SECRET"\n', encoding="utf-8")
    envrc.chmod(0o600)

    run_vscode_init(tmp_path, with_envrc=True, force=True)

    assert _mode_of(envrc) == 0o600
    # The backup holds the secret too, so it must be just as tight.
    assert _mode_of(tmp_path / ".envrc.bak") == 0o600


@_SKIP_ON_WINDOWS
def test_new_envrc_created_with_0600(tmp_path: Path) -> None:
    """A generated .envrc carries a token — 0600 even under a lax umask."""
    previous = os.umask(0o022)
    try:
        run_vscode_init(tmp_path, with_envrc=True)
    finally:
        os.umask(previous)

    assert _mode_of(tmp_path / ".envrc") == 0o600


@_SKIP_ON_WINDOWS
def test_settings_json_mode_preserved(tmp_path: Path) -> None:
    """_atomic_write is shared, so settings.json must keep its mode too."""
    vscode_dir = tmp_path / ".vscode"
    vscode_dir.mkdir()
    settings = vscode_dir / "settings.json"
    settings.write_text(json.dumps({"editor.fontSize": 14}), encoding="utf-8")
    settings.chmod(0o600)

    result = run_vscode_init(tmp_path)
    assert exit_code_for(result) == 0
    assert _mode_of(settings) == 0o600


def test_settings_json_backed_up_before_rewrite(tmp_path: Path) -> None:
    """A merge bug must not be the last word on a hand-tuned settings.json."""
    vscode_dir = tmp_path / ".vscode"
    vscode_dir.mkdir()
    original = json.dumps({"editor.fontSize": 14})
    (vscode_dir / "settings.json").write_text(original, encoding="utf-8")

    run_vscode_init(tmp_path)

    assert (vscode_dir / "settings.json.bak").read_text(encoding="utf-8") == original


def test_atomic_write_leaves_no_tmp_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed rename must not litter the workspace with tmp files."""
    from coderouter import vscode_init as mod

    def boom(src: object, dst: object) -> None:
        raise OSError("no space left on device")

    monkeypatch.setattr(mod.os, "replace", boom)

    result = run_vscode_init(tmp_path, with_envrc=True)
    # Both writes failed → reported, not crashed.
    assert exit_code_for(result) == 1

    leftovers = [p.name for p in tmp_path.rglob("*") if p.name.endswith(".tmp")]
    assert leftovers == []
    assert not (tmp_path / ".envrc").exists()


def test_atomic_write_does_not_follow_symlink_tmp(tmp_path: Path) -> None:
    """CWE-377: a pre-planted <name>.tmp symlink must not be written through.

    The old implementation wrote to a fully predictable
    ``<name>.tmp`` with ``Path.write_text``, which follows a symlink to
    wherever it points. Regression detector for the mkstemp rewrite.
    """
    victim = tmp_path / "victim.txt"
    victim.write_text("precious\n", encoding="utf-8")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / ".envrc.tmp").symlink_to(victim)

    run_vscode_init(workspace, with_envrc=True)

    assert victim.read_text(encoding="utf-8") == "precious\n"
    assert not (workspace / ".envrc").is_symlink()
    assert "ANTHROPIC_BASE_URL" in (workspace / ".envrc").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Malformed inputs
# ---------------------------------------------------------------------------


def test_unparseable_settings_json_reports_skip_not_crash(tmp_path: Path) -> None:
    """Garbage in settings.json → 'skipped' outcome, exit 1, file untouched."""
    vscode_dir = tmp_path / ".vscode"
    vscode_dir.mkdir()
    bad = "{ this is not valid json"
    (vscode_dir / "settings.json").write_text(bad, encoding="utf-8")

    result = run_vscode_init(tmp_path)
    assert result.has_skipped
    assert exit_code_for(result) == 1
    # Untouched:
    assert (vscode_dir / "settings.json").read_text() == bad


def test_settings_json_that_is_a_list_is_skipped(tmp_path: Path) -> None:
    """A JSON list at the top level is legal JSON but not a settings dict."""
    vscode_dir = tmp_path / ".vscode"
    vscode_dir.mkdir()
    (vscode_dir / "settings.json").write_text(json.dumps([1, 2, 3]), encoding="utf-8")

    result = run_vscode_init(tmp_path)
    assert result.has_skipped
    assert exit_code_for(result) == 1


def test_missing_target_directory_raises(tmp_path: Path) -> None:
    """A nonexistent target → FileNotFoundError, no partial writes."""
    with pytest.raises(FileNotFoundError):
        run_vscode_init(tmp_path / "does-not-exist")


# ---------------------------------------------------------------------------
# Empty existing settings.json (whitespace only)
# ---------------------------------------------------------------------------


def test_empty_settings_json_is_treated_as_empty_dict(tmp_path: Path) -> None:
    """An existing empty (or whitespace-only) settings.json should not crash.

    VSCode itself is happy with an empty file, and users sometimes
    ``touch`` it before configuring anything. json.loads("") would
    raise; we defend against that.
    """
    vscode_dir = tmp_path / ".vscode"
    vscode_dir.mkdir()
    (vscode_dir / "settings.json").write_text("   \n", encoding="utf-8")

    result = run_vscode_init(tmp_path)
    assert exit_code_for(result) == 0
    settings = _read_settings(tmp_path)
    assert "terminal.integrated.env.osx" in settings


# ---------------------------------------------------------------------------
# format_result / cheat sheet
# ---------------------------------------------------------------------------


def test_format_result_includes_cheat_sheet_with_port(tmp_path: Path) -> None:
    """The cheat sheet must show the right port so Cline/Continue copy-paste."""
    result = run_vscode_init(tmp_path, port=4000, dry_run=True)
    text = format_result(result, dry_run=True, port=4000)
    assert "http://localhost:4000/v1" in text
    assert "Cline / Roo Code" in text
    assert "Continue.dev" in text


def test_format_result_shows_dry_run_reminder(tmp_path: Path) -> None:
    """dry_run=True renders the '(dry-run — no files were written.)' hint."""
    result = run_vscode_init(tmp_path, dry_run=True)
    text = format_result(result, dry_run=True, port=DEFAULT_PORT)
    assert "dry-run" in text


def test_cheat_sheet_continue_snippet_is_valid_json(tmp_path: Path) -> None:
    """The Continue.dev snippet must be copy-paste-valid JSON.

    Regression pin: an earlier hand-formatted version leaked an extra
    ``}`` from f-string / plain-string brace-escaping confusion, so
    users pasting the block into ``~/.continue/config.json`` got a
    parse error. Building the snippet via :func:`json.dumps` closes
    that class of bug; this test refuses to let the code slide back
    into hand-formatted concatenation without a machine-checked JSON
    round-trip.
    """
    from coderouter.vscode_init import _render_continue_snippet

    raw = _render_continue_snippet(port=8088)
    # Strip the 4-space indentation each line carries for the cheat
    # sheet layout — the raw JSON must round-trip on its own.
    stripped = "\n".join(
        line[4:] if line.startswith("    ") else line for line in raw.splitlines()
    )
    parsed = json.loads(stripped)
    assert parsed["apiBase"] == "http://localhost:8088/v1"
    assert parsed["provider"] == "openai"
    assert parsed["apiKey"] == "dummy"


def test_cheat_sheet_continue_snippet_port_flows_through(tmp_path: Path) -> None:
    """A custom port must appear inside the Continue.dev snippet's apiBase."""
    from coderouter.vscode_init import _render_continue_snippet

    raw = _render_continue_snippet(port=4000)
    stripped = "\n".join(
        line[4:] if line.startswith("    ") else line for line in raw.splitlines()
    )
    parsed = json.loads(stripped)
    assert parsed["apiBase"] == "http://localhost:4000/v1"


# ---------------------------------------------------------------------------
# CLI wrapper — argparse plumbing only
# ---------------------------------------------------------------------------


def test_cli_vscode_init_creates_settings_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``coderouter vscode-init --target PATH`` writes settings.json."""
    rc = cli.main(["vscode-init", "--target", str(tmp_path)])
    assert rc == 0
    assert (tmp_path / ".vscode" / "settings.json").exists()


def test_cli_vscode_init_dry_run_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--dry-run through the CLI must still be a no-op."""
    rc = cli.main(["vscode-init", "--target", str(tmp_path), "--dry-run"])
    assert rc == 0
    assert not (tmp_path / ".vscode").exists()
    out = capsys.readouterr().out
    assert "dry-run" in out


def test_cli_vscode_init_missing_target_exits_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Missing --target directory → friendly stderr + exit 1."""
    missing = tmp_path / "nope"
    rc = cli.main(["vscode-init", "--target", str(missing)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "target directory does not exist" in err
    assert str(missing) in err


def test_cli_vscode_init_conflict_returns_two(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """CLI must propagate exit code 2 when a conflict is present."""
    vscode_dir = tmp_path / ".vscode"
    vscode_dir.mkdir()
    (vscode_dir / "settings.json").write_text(
        json.dumps(
            {"terminal.integrated.env.osx": {"ANTHROPIC_BASE_URL": "http://x:9999"}}
        ),
        encoding="utf-8",
    )
    rc = cli.main(["vscode-init", "--target", str(tmp_path)])
    assert rc == 2


def test_cli_vscode_init_force_returns_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--force through the CLI resolves the conflict → exit 0."""
    vscode_dir = tmp_path / ".vscode"
    vscode_dir.mkdir()
    (vscode_dir / "settings.json").write_text(
        json.dumps(
            {"terminal.integrated.env.osx": {"ANTHROPIC_BASE_URL": "http://x:9999"}}
        ),
        encoding="utf-8",
    )
    rc = cli.main(["vscode-init", "--target", str(tmp_path), "--force"])
    assert rc == 0


def test_cli_vscode_init_with_envrc_flag(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--with-envrc through the CLI produces the .envrc file."""
    rc = cli.main(
        ["vscode-init", "--target", str(tmp_path), "--with-envrc"]
    )
    assert rc == 0
    assert (tmp_path / ".envrc").exists()


def test_cli_vscode_init_port_flag_flows_through(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--port 4000 must reach settings.json's ANTHROPIC_BASE_URL."""
    rc = cli.main(
        ["vscode-init", "--target", str(tmp_path), "--port", "4000"]
    )
    assert rc == 0
    settings = json.loads(
        (tmp_path / ".vscode" / "settings.json").read_text()
    )
    assert (
        settings["terminal.integrated.env.osx"]["ANTHROPIC_BASE_URL"]
        == "http://localhost:4000"
    )


# ---------------------------------------------------------------------------
# v2.14.0 — the managed block. `--force` used to mean "replace the file";
# it now means "adopt a file I did not write". A fenced file needs no
# --force at all, because rewriting only the fence is not destructive.
# ---------------------------------------------------------------------------

_BEGIN = "# BEGIN coderouter-managed"
_END = "# END coderouter-managed"


def _envrc_outcome(result):
    """The .envrc outcome only.

    A port change also makes settings.json report a conflict (pre-existing
    behaviour, unrelated to the fence), so these tests must not assert over
    the whole outcome list.
    """
    return next(o for o in result.outcomes if o.path.name == ".envrc")



def test_generated_envrc_is_fenced(tmp_path: Path) -> None:
    run_vscode_init(tmp_path, with_envrc=True)
    text = (tmp_path / ".envrc").read_text()
    assert text.index(_BEGIN) < text.index("ANTHROPIC_BASE_URL") < text.index(_END)


def test_rerun_rewrites_only_the_block_and_needs_no_force(tmp_path: Path) -> None:
    """The whole point: a second run must not be a conflict."""
    run_vscode_init(tmp_path, with_envrc=True)
    envrc = tmp_path / ".envrc"
    envrc.write_text(
        "source_env_if_exists .envrc.local\n\n"
        + envrc.read_text()
        + "\nexport MY_OWN=1\n",
        encoding="utf-8",
    )

    result = run_vscode_init(tmp_path, with_envrc=True, port=9999)

    assert _envrc_outcome(result).action == "updated"
    text = envrc.read_text()
    assert "source_env_if_exists .envrc.local" in text
    assert "export MY_OWN=1" in text
    assert "9999" in text
    assert text.count(_BEGIN) == 1


def test_unfenced_file_is_refused_without_force(tmp_path: Path) -> None:
    """A file we did not write is the operator's, not ours."""
    original = "# hand-written\nsource_env_if_exists .envrc.local\n"
    (tmp_path / ".envrc").write_text(original, encoding="utf-8")

    result = run_vscode_init(tmp_path, with_envrc=True)

    assert _envrc_outcome(result).action == "conflict"
    assert (tmp_path / ".envrc").read_text() == original


def test_force_on_an_unfenced_file_keeps_every_original_line(tmp_path: Path) -> None:
    original = "# hand-written\nsource_env_if_exists .envrc.local\nexport TEAM=blue\n"
    (tmp_path / ".envrc").write_text(original, encoding="utf-8")

    run_vscode_init(tmp_path, with_envrc=True, force=True)

    text = (tmp_path / ".envrc").read_text()
    for line in original.strip().splitlines():
        assert line in text
    assert _BEGIN in text


def test_a_managed_var_exported_outside_the_block_is_refused(tmp_path: Path) -> None:
    """direnv applies exports in order — a silent duplicate is unusable."""
    run_vscode_init(tmp_path, with_envrc=True)
    envrc = tmp_path / ".envrc"
    envrc.write_text(
        envrc.read_text() + '\nexport ANTHROPIC_BASE_URL="http://elsewhere"\n',
        encoding="utf-8",
    )
    before = envrc.read_text()

    result = run_vscode_init(tmp_path, with_envrc=True, port=9999)

    outcome = _envrc_outcome(result)
    assert outcome.action == "conflict"
    assert "ANTHROPIC_BASE_URL" in outcome.reason
    assert envrc.read_text() == before


def test_a_commented_out_export_is_not_a_conflict(tmp_path: Path) -> None:
    run_vscode_init(tmp_path, with_envrc=True)
    envrc = tmp_path / ".envrc"
    envrc.write_text(
        envrc.read_text() + '\n# export ANTHROPIC_BASE_URL="http://old"\n',
        encoding="utf-8",
    )
    result = run_vscode_init(tmp_path, with_envrc=True, port=9999)
    assert _envrc_outcome(result).action != "conflict"


def test_a_pre_v2_14_generated_file_is_adopted_without_force(tmp_path: Path) -> None:
    """Upgrading must not demand --force to re-adopt our own old output."""
    from coderouter.vscode_init import _render_envrc

    legacy = _render_envrc(base_url="http://localhost:8088", profile=None)
    (tmp_path / ".envrc").write_text(legacy, encoding="utf-8")

    result = run_vscode_init(tmp_path, with_envrc=True, port=8088)

    assert _envrc_outcome(result).action != "conflict"
    text = (tmp_path / ".envrc").read_text()
    assert _BEGIN in text
    assert text.count("ANTHROPIC_BASE_URL") == 1


def test_a_half_written_fence_is_refused_not_guessed_at(tmp_path: Path) -> None:
    original = f"{_BEGIN}\nexport ANTHROPIC_BASE_URL=\"http://x\"\n"  # no END
    (tmp_path / ".envrc").write_text(original, encoding="utf-8")

    result = run_vscode_init(tmp_path, with_envrc=True)

    assert _envrc_outcome(result).action == "conflict"
    assert (tmp_path / ".envrc").read_text() == original


@_SKIP_ON_WINDOWS
def test_block_rewrite_keeps_the_0600_mode(tmp_path: Path) -> None:
    run_vscode_init(tmp_path, with_envrc=True)
    run_vscode_init(tmp_path, with_envrc=True, port=9999)
    assert _mode_of(tmp_path / ".envrc") == 0o600


# ---------------------------------------------------------------------------
# v2.16.0: Claude Code gateway env vars
# ---------------------------------------------------------------------------


def test_cli_writes_claude_code_env_by_default(tmp_path: Path) -> None:
    rc = cli.main(["vscode-init", "--target", str(tmp_path)])
    assert rc == 0
    env = _read_settings(tmp_path)["terminal.integrated.env.osx"]
    assert env["ENABLE_TOOL_SEARCH"] == "false"
    assert env["CLAUDE_CODE_AUTO_MODE_SERVER"] == "0"
    # Only written when the operator states the window.
    assert "CLAUDE_CODE_MAX_CONTEXT_TOKENS" not in env


def test_cli_max_context_tokens_flows_into_env(tmp_path: Path) -> None:
    rc = cli.main(["vscode-init", "--target", str(tmp_path), "--max-context-tokens", "32768"])
    assert rc == 0
    for os_key in ("osx", "linux", "windows"):
        env = _read_settings(tmp_path)[f"terminal.integrated.env.{os_key}"]
        assert env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] == "32768"


def test_cli_no_claude_code_env_writes_base_vars_only(tmp_path: Path) -> None:
    rc = cli.main(["vscode-init", "--target", str(tmp_path), "--no-claude-code-env"])
    assert rc == 0
    env = _read_settings(tmp_path)["terminal.integrated.env.osx"]
    assert set(env) == {"ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN"}


def test_cli_rejects_max_context_with_no_claude_code_env(tmp_path: Path) -> None:
    rc = cli.main(
        ["vscode-init", "--target", str(tmp_path), "--no-claude-code-env", "--max-context-tokens", "8192"]
    )
    assert rc == 2
    assert not (tmp_path / ".vscode").exists()


def test_cli_rejects_non_positive_max_context(tmp_path: Path) -> None:
    rc = cli.main(["vscode-init", "--target", str(tmp_path), "--max-context-tokens", "0"])
    assert rc == 2


def test_envrc_block_carries_claude_code_env(tmp_path: Path) -> None:
    rc = cli.main(
        ["vscode-init", "--target", str(tmp_path), "--with-envrc", "--max-context-tokens", "65536"]
    )
    assert rc == 0
    text = (tmp_path / ".envrc").read_text(encoding="utf-8")
    assert 'export ENABLE_TOOL_SEARCH="false"' in text
    assert 'export CLAUDE_CODE_AUTO_MODE_SERVER="0"' in text
    assert 'export CLAUDE_CODE_MAX_CONTEXT_TOKENS="65536"' in text


def test_envrc_outside_export_of_claude_code_var_conflicts_only_when_written(
    tmp_path: Path,
) -> None:
    envrc = tmp_path / ".envrc"
    # First run creates the fenced block; then the operator adds their own
    # ENABLE_TOOL_SEARCH outside it.
    assert cli.main(["vscode-init", "--target", str(tmp_path), "--with-envrc"]) == 0
    envrc.write_text(envrc.read_text(encoding="utf-8") + 'export ENABLE_TOOL_SEARCH="true"\n', encoding="utf-8")
    # Writing it again would shadow theirs → conflict (exit 2).
    assert cli.main(["vscode-init", "--target", str(tmp_path), "--with-envrc"]) == 2
    # Opting out of the Claude Code vars → their export is theirs to keep.
    assert (
        cli.main(["vscode-init", "--target", str(tmp_path), "--with-envrc", "--no-claude-code-env"])
        == 0
    )


def test_rerun_is_idempotent_with_claude_code_env(tmp_path: Path) -> None:
    args = ["vscode-init", "--target", str(tmp_path), "--with-envrc", "--max-context-tokens", "32768"]
    assert cli.main(args) == 0
    before = (_read_settings(tmp_path), (tmp_path / ".envrc").read_text(encoding="utf-8"))
    assert cli.main(args) == 0
    after = (_read_settings(tmp_path), (tmp_path / ".envrc").read_text(encoding="utf-8"))
    assert before == after
