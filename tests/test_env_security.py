"""Tests for ``coderouter.env_security`` (v1.6.3).

Three independent checks (permissions / .gitignore / git-tracking)
plus the existence pre-check, all driven from a single
``check_env_security(path)`` entry point. Tests use real ``git`` via
``subprocess`` rather than mocking — git plumbing behavior is part of
the contract we depend on, and mocking it would lose the value of the
test (we'd be re-implementing git's behavior in the test fixtures).

Skipped if ``git`` isn't on PATH (handled at module level).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from coderouter.env_security import (
    EnvSecurityVerdict,
    check_env_security,
    exit_code_for_env_security,
    format_env_security_report,
)

# git is required for 2 of the 4 checks. Tests that depend on it use
# this skip marker; the perms-only and existence-only tests still run
# without git.
_HAS_GIT = shutil.which("git") is not None
needs_git = pytest.mark.skipif(not _HAS_GIT, reason="git not on PATH")


# ---------------------------------------------------------------------------
# Existence
# ---------------------------------------------------------------------------


def test_nonexistent_file_skips_all(tmp_path: Path) -> None:
    """No file → existence SKIP, downstream checks all SKIP, exit 0."""
    target = tmp_path / "missing.env"
    report = check_env_security(target)
    names = [c.name for c in report.checks]
    verdicts = [c.verdict for c in report.checks]
    assert names == ["existence", "permissions", "gitignore", "git-tracking"]
    assert all(v == EnvSecurityVerdict.SKIP for v in verdicts)
    assert exit_code_for_env_security(report) == 0


def test_directory_at_path_is_error(tmp_path: Path) -> None:
    """Path is a directory, not a file → ERROR on existence."""
    d = tmp_path / "not_a_file"
    d.mkdir()
    report = check_env_security(d)
    assert report.checks[0].name == "existence"
    assert report.checks[0].verdict == EnvSecurityVerdict.ERROR
    assert exit_code_for_env_security(report) == 1


# ---------------------------------------------------------------------------
# Permissions (POSIX-only; Windows SKIPs the check)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(os.name == "nt", reason="POSIX file mode")
def test_owner_only_perms_pass(tmp_path: Path) -> None:
    p = tmp_path / ".env"
    p.write_text("FOO=bar\n")
    p.chmod(0o600)
    report = check_env_security(p)
    perms = next(c for c in report.checks if c.name == "permissions")
    assert perms.verdict == EnvSecurityVerdict.OK
    assert "0o600" in perms.detail


@pytest.mark.skipif(os.name == "nt", reason="POSIX file mode")
def test_group_readable_perms_warn(tmp_path: Path) -> None:
    p = tmp_path / ".env"
    p.write_text("FOO=bar\n")
    p.chmod(0o640)
    report = check_env_security(p)
    perms = next(c for c in report.checks if c.name == "permissions")
    assert perms.verdict == EnvSecurityVerdict.WARN
    assert "chmod 0600" in perms.fix
    assert exit_code_for_env_security(report) == 2


@pytest.mark.skipif(os.name == "nt", reason="POSIX file mode")
def test_world_readable_perms_warn(tmp_path: Path) -> None:
    p = tmp_path / ".env"
    p.write_text("FOO=bar\n")
    p.chmod(0o644)
    report = check_env_security(p)
    perms = next(c for c in report.checks if c.name == "permissions")
    assert perms.verdict == EnvSecurityVerdict.WARN


# ---------------------------------------------------------------------------
# .gitignore + git-tracking
# ---------------------------------------------------------------------------


@needs_git
def test_outside_git_repo_skips_git_checks(tmp_path: Path) -> None:
    """No surrounding git repo → both git checks SKIP."""
    p = tmp_path / ".env"
    p.write_text("FOO=bar\n")
    p.chmod(0o600)
    report = check_env_security(p)
    gi = next(c for c in report.checks if c.name == "gitignore")
    tr = next(c for c in report.checks if c.name == "git-tracking")
    assert gi.verdict == EnvSecurityVerdict.SKIP
    assert tr.verdict == EnvSecurityVerdict.SKIP
    assert "not inside a git repository" in gi.detail


@needs_git
def test_in_repo_no_gitignore_warns(tmp_path: Path) -> None:
    """`.env` exists in a git repo, no .gitignore covers it → WARN."""
    _git_init(tmp_path)
    p = tmp_path / ".env"
    p.write_text("FOO=bar\n")
    p.chmod(0o600)
    report = check_env_security(p)
    gi = next(c for c in report.checks if c.name == "gitignore")
    tr = next(c for c in report.checks if c.name == "git-tracking")
    assert gi.verdict == EnvSecurityVerdict.WARN
    assert tr.verdict == EnvSecurityVerdict.OK  # not yet tracked
    assert ".gitignore" in gi.fix
    assert exit_code_for_env_security(report) == 2


@needs_git
def test_in_repo_with_gitignore_passes(tmp_path: Path) -> None:
    """`.env` is in .gitignore and not tracked → both checks OK."""
    _git_init(tmp_path)
    (tmp_path / ".gitignore").write_text(".env\n")
    p = tmp_path / ".env"
    p.write_text("FOO=bar\n")
    p.chmod(0o600)
    report = check_env_security(p)
    gi = next(c for c in report.checks if c.name == "gitignore")
    tr = next(c for c in report.checks if c.name == "git-tracking")
    assert gi.verdict == EnvSecurityVerdict.OK
    assert tr.verdict == EnvSecurityVerdict.OK
    assert exit_code_for_env_security(report) == 0


@needs_git
def test_tracked_file_is_error(tmp_path: Path) -> None:
    """`.env` is tracked by git → ERROR (real leak risk)."""
    _git_init(tmp_path)
    p = tmp_path / ".env"
    p.write_text("SECRET=leak\n")
    p.chmod(0o600)
    # Track the file (git rejects -f if .gitignore matches; here there's
    # no .gitignore yet so plain `git add .env` works).
    subprocess.run(["git", "-C", str(tmp_path), "add", ".env"], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-q", "-m", "leak"], check=True
    )
    report = check_env_security(p)
    tr = next(c for c in report.checks if c.name == "git-tracking")
    assert tr.verdict == EnvSecurityVerdict.ERROR
    assert "rm --cached" in tr.fix
    assert exit_code_for_env_security(report) == 1


# ---------------------------------------------------------------------------
# Format / report
# ---------------------------------------------------------------------------


def test_format_report_renders_each_check(tmp_path: Path) -> None:
    p = tmp_path / ".env"
    p.write_text("FOO=bar\n")
    p.chmod(0o644)
    report = check_env_security(p)
    rendered = format_env_security_report(report)
    assert "env-security:" in rendered
    assert "permissions" in rendered
    assert "gitignore" in rendered
    assert "git-tracking" in rendered
    assert "Exit:" in rendered


def test_format_report_summary_picks_worst_verdict(tmp_path: Path) -> None:
    """When checks span OK / WARN / ERROR, summary reflects ERROR."""
    p = tmp_path / ".env"
    p.write_text("FOO=bar\n")
    # WARN-only path (no git repo, but world-readable)
    p.chmod(0o644)
    rendered = format_env_security_report(check_env_security(p))
    assert "WARN" in rendered
    assert "ERROR" not in rendered.split("Summary:", 1)[1]


# ---------------------------------------------------------------------------
# git_executable override (for environments where git is in unusual paths)
# ---------------------------------------------------------------------------


def test_explicit_git_executable_used(tmp_path: Path) -> None:
    """Verify the git_executable kwarg overrides shutil.which lookup."""
    p = tmp_path / ".env"
    p.write_text("FOO=bar\n")
    p.chmod(0o600)
    # Pass a deliberately invalid path — git checks should SKIP gracefully.
    report = check_env_security(p, git_executable="/nonexistent/git")
    gi = next(c for c in report.checks if c.name == "gitignore")
    tr = next(c for c in report.checks if c.name == "git-tracking")
    # _find_repo_root catches OSError and returns None → "not inside repo"
    assert gi.verdict == EnvSecurityVerdict.SKIP
    assert tr.verdict == EnvSecurityVerdict.SKIP


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _git_init(repo: Path) -> None:
    """Create an empty git repo at ``repo`` with deterministic identity."""
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "test@test"], check=True
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "test"], check=True
    )
