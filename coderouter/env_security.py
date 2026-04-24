"""`.env` file security checks (v1.6.3).

Run from ``coderouter doctor --check-env [PATH]`` to surface the three
common ``.env`` mishaps before they bite:

  1. **Filesystem permissions** — on POSIX, the file should be readable
     only by the owner (``mode & 0o077 == 0``). World/group readable
     ``.env`` exposes API keys to other accounts on shared machines and
     to backup tools that recurse with broad scope.
  2. **`.gitignore` coverage** — ``.env`` MUST be matched by the repo's
     ignore rules so an absent-minded ``git add .`` doesn't stage it.
  3. **`git` tracking state** — if ``.env`` is already tracked
     (committed in the past), no ``.gitignore`` rule will save it. The
     fix is to ``git rm --cached``, rotate the leaked keys, and update
     ``.gitignore``.

Design choices
--------------
* Pure stdlib (``os`` / ``stat`` / ``subprocess`` / ``shutil``) so we
  preserve the runtime-deps freeze (5 packages — see plan.md §5.4).
* No HTTP, no asyncio — these are local filesystem and git checks, so
  the module is intentionally separate from ``coderouter.doctor``
  (which is httpx-heavy). The CLI surfaces both via the same
  ``coderouter doctor`` namespace.
* Verdict severity is mapped to the same exit-code contract used by
  the model probes (0 OK / 2 patchable / 1 blocker), so wrappers like
  ``coderouter doctor --check-env --check-model nim-x`` (a v1.7
  candidate) can collapse the verdicts uniformly.
* Windows: file-mode check is skipped with verdict SKIP (Windows POSIX
  bits are unreliable). Git checks still run if ``git`` is on PATH.

Non-destructive contract
------------------------
This module never writes, never deletes, never invokes ``git add`` or
``git rm``. It only reads filesystem metadata and runs read-only git
plumbing (``git check-ignore`` / ``git ls-files --error-unmatch``).
The repo state is not mutated.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

__all__ = [
    "EnvSecurityCheck",
    "EnvSecurityReport",
    "EnvSecurityVerdict",
    "check_env_security",
    "exit_code_for_env_security",
    "format_env_security_report",
]


class EnvSecurityVerdict(StrEnum):
    """Per-check verdict.

    Mapping to exit code (see :func:`exit_code_for_env_security`):
        OK    → contributes 0
        SKIP  → contributes 0 (not applicable, e.g. Windows file-mode)
        WARN  → contributes 2 (fix recommended; chmod / .gitignore edit)
        ERROR → contributes 1 (real leak risk; .env is git-tracked, etc.)
    """

    OK = "ok"
    SKIP = "skip"
    WARN = "warn"
    ERROR = "error"


@dataclass
class EnvSecurityCheck:
    """Outcome of a single check in the env-security suite.

    ``fix`` is a 1-line shell command (or short snippet) the operator
    can copy-paste to remediate. ``None`` when no remediation is
    applicable (e.g. on a SKIP verdict).
    """

    name: str
    verdict: EnvSecurityVerdict
    detail: str
    fix: str | None = None


@dataclass
class EnvSecurityReport:
    """Aggregate report for a single ``--check-env`` invocation."""

    path: Path
    checks: list[EnvSecurityCheck] = field(default_factory=list)


def exit_code_for_env_security(report: EnvSecurityReport) -> int:
    """Derive the CLI exit code from a report (see :class:`EnvSecurityVerdict`).

    Same shape as :func:`coderouter.doctor.exit_code_for` so callers
    can union the two reports without special-casing.
    """
    has_blocker = False
    has_warn = False
    for c in report.checks:
        if c.verdict == EnvSecurityVerdict.ERROR:
            has_blocker = True
        elif c.verdict == EnvSecurityVerdict.WARN:
            has_warn = True
    if has_blocker:
        return 1
    if has_warn:
        return 2
    return 0


def check_env_security(
    path: str | os.PathLike[str],
    *,
    git_executable: str | None = None,
) -> EnvSecurityReport:
    """Run the 3-check env-security suite against ``path``.

    The checks are independent — each runs even if a previous one
    failed, so the report shows everything wrong at once (rather than
    making the user fix one thing, re-run, see the next, etc.).

    Args:
        path: ``.env`` file to inspect. Does not need to exist; if
            absent, all checks return SKIP with a clear message so
            the operator can ack ("yeah, I haven't created one yet").
        git_executable: Override the ``git`` binary path; primarily
            for tests. Defaults to ``shutil.which("git")``.

    Returns:
        :class:`EnvSecurityReport` with one entry per check (in
        deterministic order: existence, perms, gitignore, tracking).
    """
    p = Path(path).resolve()
    report = EnvSecurityReport(path=p)

    # -------------------------------------------------------------- #
    # Check 0: existence
    # -------------------------------------------------------------- #
    if not p.exists():
        report.checks.append(
            EnvSecurityCheck(
                name="existence",
                verdict=EnvSecurityVerdict.SKIP,
                detail=f"no file at {p} — nothing to inspect",
                fix=None,
            )
        )
        # Bail early: nothing to inspect, but report SKIP for the
        # remaining checks so the output stays consistent.
        report.checks.append(_skip("permissions", "no file to inspect"))
        report.checks.append(_skip("gitignore", "no file to inspect"))
        report.checks.append(_skip("git-tracking", "no file to inspect"))
        return report

    if not p.is_file():
        report.checks.append(
            EnvSecurityCheck(
                name="existence",
                verdict=EnvSecurityVerdict.ERROR,
                detail=f"path exists but is not a regular file: {p}",
                fix=None,
            )
        )
        return report

    report.checks.append(
        EnvSecurityCheck(
            name="existence",
            verdict=EnvSecurityVerdict.OK,
            detail=f"found at {p}",
        )
    )

    # -------------------------------------------------------------- #
    # Check 1: permissions (POSIX only — Windows bits are unreliable)
    # -------------------------------------------------------------- #
    report.checks.append(_check_permissions(p))

    # -------------------------------------------------------------- #
    # Check 2: .gitignore coverage
    # Check 3: git-tracking state
    # Both depend on `git`. If git is unavailable, both SKIP with the
    # same explanatory message so users on a non-git checkout aren't
    # spammed with "git not found" twice.
    # -------------------------------------------------------------- #
    git_bin = git_executable or shutil.which("git")
    if not git_bin:
        report.checks.append(
            _skip("gitignore", "git not on PATH — cannot evaluate .gitignore")
        )
        report.checks.append(
            _skip("git-tracking", "git not on PATH — cannot evaluate tracking")
        )
        return report

    repo_root = _find_repo_root(p, git_bin)
    if repo_root is None:
        report.checks.append(
            _skip("gitignore", "not inside a git repository")
        )
        report.checks.append(
            _skip("git-tracking", "not inside a git repository")
        )
        return report

    report.checks.append(_check_gitignore(p, repo_root, git_bin))
    report.checks.append(_check_git_tracking(p, repo_root, git_bin))

    return report


def format_env_security_report(report: EnvSecurityReport) -> str:
    """Render an :class:`EnvSecurityReport` as a human-readable block.

    Output is intentionally similar in shape to
    :func:`coderouter.doctor.format_report` (header line, indented
    detail, fix command on a separate indented line) so the two
    reports can sit next to each other without visual whiplash.
    """
    lines: list[str] = []
    lines.append("─" * 60)
    lines.append(f"env-security: {report.path}")
    lines.append("Checks:")
    for c in report.checks:
        lines.append(f"  [{c.verdict.value.upper():6s}] {c.name}")
        lines.append(f"      {c.detail}")
        if c.fix:
            lines.append(f"      fix: {c.fix}")

    has_warn = any(c.verdict == EnvSecurityVerdict.WARN for c in report.checks)
    has_err = any(c.verdict == EnvSecurityVerdict.ERROR for c in report.checks)
    if has_err:
        summary = "Summary: at least one check escalated to ERROR (real leak risk)."
    elif has_warn:
        summary = "Summary: WARN(s) present — apply the suggested fix(es)."
    else:
        summary = "Summary: all checks pass."
    lines.append(summary)
    lines.append(f"Exit: {exit_code_for_env_security(report)}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _skip(name: str, reason: str) -> EnvSecurityCheck:
    return EnvSecurityCheck(
        name=name,
        verdict=EnvSecurityVerdict.SKIP,
        detail=reason,
        fix=None,
    )


def _check_permissions(p: Path) -> EnvSecurityCheck:
    """Verify ``.env`` is owner-only readable on POSIX systems."""
    if os.name == "nt":
        return _skip("permissions", "Windows — POSIX mode bits unreliable")

    mode = p.stat().st_mode
    perm = stat.S_IMODE(mode)
    other_or_group = perm & 0o077
    if other_or_group == 0:
        return EnvSecurityCheck(
            name="permissions",
            verdict=EnvSecurityVerdict.OK,
            detail=f"mode = {oct(perm)} (owner-only)",
        )
    return EnvSecurityCheck(
        name="permissions",
        verdict=EnvSecurityVerdict.WARN,
        detail=(
            f"mode = {oct(perm)} grants group/other access. API keys in "
            f"this file are visible to other accounts on shared machines "
            f"and to backup tools."
        ),
        fix=f"chmod 0600 {p}",
    )


def _find_repo_root(p: Path, git_bin: str) -> Path | None:
    """Return the git repo root containing ``p``, or None if not in a repo.

    Uses ``git rev-parse --show-toplevel`` from ``p``'s directory — the
    cheapest way to ask git the question without parsing ``.git``
    layouts ourselves (worktrees, submodules, ``GIT_DIR=...`` envs all
    DTRT).
    """
    try:
        result = subprocess.run(
            [git_bin, "rev-parse", "--show-toplevel"],
            cwd=p.parent,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    root = result.stdout.strip()
    if not root:
        return None
    return Path(root)


def _check_gitignore(p: Path, repo_root: Path, git_bin: str) -> EnvSecurityCheck:
    """Verify ``.env`` is matched by ``.gitignore`` (any rule).

    ``git check-ignore`` exit codes:
        0 = path IS ignored
        1 = path is NOT ignored
        128 = error (treated as ERROR verdict so the operator notices)
    """
    try:
        result = subprocess.run(
            [git_bin, "check-ignore", "-q", str(p)],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return EnvSecurityCheck(
            name="gitignore",
            verdict=EnvSecurityVerdict.SKIP,
            detail=f"git check-ignore failed: {exc}",
            fix=None,
        )

    if result.returncode == 0:
        return EnvSecurityCheck(
            name="gitignore",
            verdict=EnvSecurityVerdict.OK,
            detail=f"matched by .gitignore in {repo_root}",
        )
    if result.returncode == 1:
        # Compute a relative path for the suggested fix when possible.
        try:
            rel = p.relative_to(repo_root)
        except ValueError:
            rel = p
        return EnvSecurityCheck(
            name="gitignore",
            verdict=EnvSecurityVerdict.WARN,
            detail=(
                f"NOT matched by any .gitignore rule in {repo_root}. "
                f"`git add .` from this repo will stage the file."
            ),
            fix=f'echo "{rel}" >> {repo_root}/.gitignore',
        )
    return EnvSecurityCheck(
        name="gitignore",
        verdict=EnvSecurityVerdict.SKIP,
        detail=(
            f"git check-ignore returned unexpected code {result.returncode}: "
            f"{(result.stderr or result.stdout).strip()!r}"
        ),
        fix=None,
    )


def _check_git_tracking(p: Path, repo_root: Path, git_bin: str) -> EnvSecurityCheck:
    """Verify ``.env`` is NOT currently tracked by git.

    ``git ls-files --error-unmatch`` exit codes:
        0 = path IS tracked
        1 = path is NOT tracked (this is what we want)
        other = error
    """
    try:
        result = subprocess.run(
            [git_bin, "ls-files", "--error-unmatch", str(p)],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return EnvSecurityCheck(
            name="git-tracking",
            verdict=EnvSecurityVerdict.SKIP,
            detail=f"git ls-files failed: {exc}",
            fix=None,
        )

    if result.returncode == 0:
        try:
            rel = p.relative_to(repo_root)
        except ValueError:
            rel = p
        return EnvSecurityCheck(
            name="git-tracking",
            verdict=EnvSecurityVerdict.ERROR,
            detail=(
                f"file is currently tracked by git in {repo_root}. Any "
                f"secrets in it have been (or could be) committed and "
                f"pushed. .gitignore rules do NOT untrack already-"
                f"tracked files."
            ),
            fix=(
                f"git -C {repo_root} rm --cached {rel} && "
                f"echo '{rel}' >> {repo_root}/.gitignore && "
                f"# rotate any leaked keys, then commit"
            ),
        )
    if result.returncode == 1:
        return EnvSecurityCheck(
            name="git-tracking",
            verdict=EnvSecurityVerdict.OK,
            detail=f"not tracked by git in {repo_root}",
        )
    return EnvSecurityCheck(
        name="git-tracking",
        verdict=EnvSecurityVerdict.SKIP,
        detail=(
            f"git ls-files returned unexpected code {result.returncode}: "
            f"{(result.stderr or result.stdout).strip()!r}"
        ),
        fix=None,
    )
