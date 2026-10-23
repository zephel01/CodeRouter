"""v2.10-A: ``coderouter vscode-init`` — one-shot VSCode workspace scaffolder.

Writes ``.vscode/settings.json`` with ``terminal.integrated.env.*`` so a
Claude Code session launched from VSCode's integrated terminal
auto-points at CodeRouter. Optionally emits a direnv ``.envrc`` in the
same run.

The design contract:

* stdlib only — no new runtime deps (keeps the strict-5 rule in
  ``pyproject.toml`` intact).
* Never overwrite unrelated keys in an existing ``settings.json``. Only
  ``terminal.integrated.env.osx`` / ``.linux`` / ``.windows`` are
  touched, and inside them only the CodeRouter-managed env vars.
* When an existing target value differs from the desired one, refuse
  without ``--force`` and print a unified diff so the operator decides.
* ``--dry-run`` is byte-identical to the write path minus the actual
  ``os.replace`` at the end.
* All writes are atomic (``tempfile.mkstemp`` + ``fsync`` +
  ``os.replace``) so a partial write cannot corrupt an existing
  ``settings.json``. This matters more here than in most CodeRouter
  modules because the file we're editing is workspace-critical: a
  broken JSON blob would silently break every VSCode terminal env
  until a human debugged it.
* Nothing an operator wrote is destroyed without a copy: before any
  existing file is rewritten, its previous bytes (and mode) land in
  ``<name>.bak``, which ``coderouter rollback`` can swap back.
* Since v2.14.0, ``.envrc`` is edited through a marker-delimited block
  (``# BEGIN coderouter-managed`` … ``# END coderouter-managed``): only
  that block is rewritten and every other line survives byte-for-byte.
  A re-run over an already-fenced file therefore needs no ``--force``;
  ``--force`` now means "adopt a file I did not write". Before v2.14.0
  this was a whole-file replace, which is how the lost
  ``source_env_if_exists .envrc.local`` line (H-11) happened — see the
  "marker-delimited managed block in .envrc" section below for the full
  rationale and the conflict rules.
* A freshly generated ``.envrc`` is created 0600 — it carries
  ``ANTHROPIC_AUTH_TOKEN`` and sits next to the operator's real
  secrets. An existing file's mode is preserved across rewrites
  instead of being reset to the umask default by ``os.replace``.

Files touched (opt-in per flag):

* ``{target}/.vscode/settings.json`` — always
* ``{target}/.envrc``                 — ``--with-envrc``

Non-Claude-Code extensions (Cline / Roo / Kilo / Continue) are handled
by the cheat sheet printed to stdout at end of run and the fuller
``docs/guides/vscode.md`` — this CLI deliberately does NOT reach into
those extensions' settings, because each ships its own config schema
that changes with its own release cadence.

CLI plumbing lives in :mod:`coderouter.cli` (``_run_vscode_init``); this
module is import-clean so tests can exercise ``run_vscode_init``
directly without argparse in the way.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from difflib import unified_diff
from pathlib import Path

# v2.10-A: The README, quickstart, and every backend guide teach 8088 as
# the CodeRouter port. ``coderouter serve`` still defaults to 4000 (a
# pre-README default that predates the docs unification); vscode-init
# sides with the docs so that the settings.json it writes matches every
# tutorial the user is likely to be reading. Operators who deviate can
# pass ``--port`` explicitly.
DEFAULT_PORT = 8088

# Bogus token — CodeRouter's chat ingress does not validate ANTHROPIC_AUTH_TOKEN
# (real API keys are managed on the ``providers.yaml`` side). Using a
# fixed sentinel here means we never accidentally embed the operator's
# real token into a workspace file. (Not annotated with `noqa: S105`
# because this project doesn't enable that ruff rule — RUF100 would
# then flag the unused directive.)
DEFAULT_TOKEN = "dummy"

# v2.11 (H-11): a generated .envrc carries ANTHROPIC_AUTH_TOKEN, and
# users routinely add their real key next to it (or point
# ``source_env_if_exists .envrc.local`` at one). ``env_security``
# already WARNs on any ``.env`` whose mode has 0o077 bits set and tells
# the operator to ``chmod 0600``; a file we generate ourselves should
# not be born failing that check. Applies to newly created files only —
# an existing file keeps whatever mode its owner chose.
_ENVRC_MODE = 0o600

# VSCode reads terminal env from a separate key per host OS. Writing all
# three means the same ``.vscode/`` folder works whether the developer
# opens it on macOS, Linux, or WSL/Windows without a second scaffolder
# run. The keys are stable across VSCode versions (documented public
# API since 1.x).
_TERMINAL_ENV_KEYS: tuple[str, ...] = (
    "terminal.integrated.env.osx",
    "terminal.integrated.env.linux",
    "terminal.integrated.env.windows",
)

# Env vars this scaffolder owns inside the terminal-env blocks. Anything
# NOT in this set that already exists under one of the terminal-env
# keys is preserved verbatim on merge (e.g. the user's PATH tweak).
_MANAGED_ENV_KEYS: tuple[str, ...] = (
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_AUTH_TOKEN",
    "CODEROUTER_MODE",
)

# v2.16.0: Claude Code settings for a gateway that is not api.anthropic.com.
# Written by default (``--no-claude-code-env`` opts out):
#
# * ``ENABLE_TOOL_SEARCH=false`` — 2.1.304 turned tool search on behind a
#   custom ANTHROPIC_BASE_URL. Only api.anthropic.com runs the search; the
#   gateway would answer 400 on every session's first request and Claude
#   Code would retry without it. Saying so up front skips that round trip.
# * ``CLAUDE_CODE_AUTO_MODE_SERVER=0`` — on gateways auto mode defaults to
#   the server-side permission classifier, which a local backend does not
#   provide; ``0`` keeps the classifier client-side (its requests then go
#   through CodeRouter like any other request).
# * ``CLAUDE_CODE_MAX_CONTEXT_TOKENS`` — only when ``--max-context-tokens``
#   is given. Since 2.1.285 Claude Code assumes a 1M window behind a
#   gateway for Opus 4.7+ / Sonnet 5+ / Fable ids, so auto-compaction would
#   wait far past what a local model holds.
CLAUDE_CODE_ENV_KEYS: tuple[str, ...] = (
    "ENABLE_TOOL_SEARCH",
    "CLAUDE_CODE_AUTO_MODE_SERVER",
    "CLAUDE_CODE_MAX_CONTEXT_TOKENS",
)


def claude_code_env(*, max_context_tokens: int | None = None) -> dict[str, str]:
    """v2.16.0: the Claude Code env vars ``vscode-init`` writes by default."""
    env = {"ENABLE_TOOL_SEARCH": "false", "CLAUDE_CODE_AUTO_MODE_SERVER": "0"}
    if max_context_tokens is not None:
        if max_context_tokens <= 0:
            raise ValueError("max_context_tokens must be positive")
        env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] = str(max_context_tokens)
    return env


@dataclass(frozen=True)
class FileOutcome:
    """The outcome of touching (or planning to touch) one file.

    ``action`` values:

    * ``created``   — file didn't exist, we wrote it
    * ``updated``   — file existed, we changed it
    * ``unchanged`` — file already had the desired content
    * ``conflict``  — existing content differs and ``--force`` was
                      not set; the file was NOT written
    * ``skipped``   — hard error prevented processing (unparseable
                      JSON, wrong type, permission denied); NOT written
    """

    path: Path
    action: str
    diff: str = ""
    reason: str = ""


@dataclass(frozen=True)
class VSCodeInitResult:
    outcomes: list[FileOutcome]

    @property
    def has_conflicts(self) -> bool:
        return any(o.action == "conflict" for o in self.outcomes)

    @property
    def has_skipped(self) -> bool:
        return any(o.action == "skipped" for o in self.outcomes)


def run_vscode_init(
    target: Path,
    *,
    port: int = DEFAULT_PORT,
    profile: str | None = None,
    with_envrc: bool = False,
    dry_run: bool = False,
    force: bool = False,
    extra_env: dict[str, str] | None = None,
) -> VSCodeInitResult:
    """Scaffold VSCode workspace settings for CodeRouter.

    Parameters
    ----------
    target:
        Workspace root — the folder that VSCode opens. ``.vscode/`` and
        (if ``with_envrc``) ``.envrc`` are placed under this directory.
        Must already exist as a directory; we do not create the
        workspace root itself.
    port:
        CodeRouter port for ``ANTHROPIC_BASE_URL``. Defaults to 8088
        (docs-standard); override to match a non-default ``serve``.
    profile:
        Optional ``CODEROUTER_MODE`` value to preload. When set, the
        VSCode terminal gets ``CODEROUTER_MODE=<profile>`` too.
    with_envrc:
        When true, also write ``.envrc`` (direnv). Idempotent and
        conflict-aware just like ``settings.json``.
    dry_run:
        When true, compute all the outcomes and diffs but skip the
        actual ``os.replace``. Guarantees byte-parity with the write
        path — see the module docstring for the invariant.
    force:
        When true, existing conflicting values are overwritten. Without
        this flag, a conflict is reported with a diff and the file is
        left untouched.
    extra_env:
        v2.16.0: additional variables to write next to the base ones
        (the CLI passes :func:`claude_code_env`). Same conflict rules.

    Raises
    ------
    FileNotFoundError:
        The target directory does not exist. All other issues (bad
        JSON, permission denied, existing conflicts) are captured as
        ``FileOutcome`` entries with the appropriate action.
    """
    target = Path(target).expanduser().resolve()
    if not target.is_dir():
        raise FileNotFoundError(f"target directory does not exist: {target}")

    base_url = f"http://localhost:{port}"
    outcomes: list[FileOutcome] = []

    # 1. .vscode/settings.json (always).
    outcomes.append(
        _apply_settings_json(
            target / ".vscode" / "settings.json",
            base_url=base_url,
            profile=profile,
            force=force,
            dry_run=dry_run,
            extra_env=extra_env,
        )
    )

    # 2. .envrc (opt-in via --with-envrc).
    if with_envrc:
        outcomes.append(
            _apply_envrc(
                target / ".envrc",
                base_url=base_url,
                profile=profile,
                force=force,
                dry_run=dry_run,
                extra_env=extra_env,
            )
        )

    return VSCodeInitResult(outcomes=outcomes)


# ---------------------------------------------------------------------------
# settings.json
# ---------------------------------------------------------------------------


def _apply_settings_json(
    path: Path,
    *,
    base_url: str,
    profile: str | None,
    force: bool,
    dry_run: bool,
    extra_env: dict[str, str] | None = None,
) -> FileOutcome:
    desired_env: dict[str, str] = {
        "ANTHROPIC_BASE_URL": base_url,
        "ANTHROPIC_AUTH_TOKEN": DEFAULT_TOKEN,
    }
    if profile:
        desired_env["CODEROUTER_MODE"] = profile
    if extra_env:
        desired_env.update(extra_env)

    old_text = ""
    if path.exists():
        try:
            old_text = path.read_text(encoding="utf-8")
        except OSError as exc:
            return FileOutcome(
                path=path,
                action="skipped",
                reason=f"cannot read existing file: {exc}",
            )
        try:
            existing = json.loads(old_text) if old_text.strip() else {}
        except json.JSONDecodeError as exc:
            return FileOutcome(
                path=path,
                action="skipped",
                reason=(
                    "existing settings.json is not valid JSON "
                    f"({exc.msg} at line {exc.lineno}). Fix or move it "
                    "aside and re-run."
                ),
            )
        if not isinstance(existing, dict):
            return FileOutcome(
                path=path,
                action="skipped",
                reason=(
                    "existing settings.json is not a JSON object "
                    f"(got {type(existing).__name__}). Fix or move it "
                    "aside and re-run."
                ),
            )
    else:
        existing = {}

    merged, conflicts = _merge_terminal_env(existing, desired_env, force=force)

    if conflicts and not force:
        # Report the diff between existing and what --force WOULD write
        # so the operator can decide whether the change is desired.
        force_merged, _ = _merge_terminal_env(existing, desired_env, force=True)
        diff = _json_diff(existing, force_merged, path)
        return FileOutcome(
            path=path,
            action="conflict",
            diff=diff,
            reason=(
                "existing settings.json has different value(s) for "
                f"{', '.join(sorted(conflicts))}. Re-run with --force to "
                "overwrite those keys (unrelated keys are preserved; the "
                f"previous file is saved to {_backup_path(path).name})."
            ),
        )

    new_text = _dump_json(merged)
    if new_text == old_text:
        return FileOutcome(path=path, action="unchanged")

    diff = _text_diff(old_text, new_text, path)
    reason = ""
    if not dry_run:
        # H-11: same deal as .envrc. The merge preserves unrelated keys
        # by design, but a bug in _merge_terminal_env would rewrite a
        # hand-tuned settings.json with no way back — so keep the old
        # bytes. Unlike .envrc this is not gated on --force: an additive
        # merge rewrites the file without --force too, and that is
        # exactly the path a merge bug would take.
        if old_text:
            backup = _make_backup(path)
            reason = (
                f"previous contents backed up to {backup}"
                if backup is not None
                else (
                    f"could not write {_backup_path(path).name} — "
                    "previous contents were NOT preserved"
                )
            )
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(path, new_text)
        except OSError as exc:
            return FileOutcome(
                path=path,
                action="skipped",
                reason=f"write failed: {exc}",
                diff=diff,
            )
    elif old_text:
        reason = f"would back up previous contents to {_backup_path(path)}"

    return FileOutcome(
        path=path,
        action="updated" if old_text else "created",
        diff=diff,
        reason=reason,
    )


def _merge_terminal_env(
    existing: dict,
    desired_env: dict[str, str],
    *,
    force: bool,
) -> tuple[dict, set[str]]:
    """Merge our env vars into a settings.json dict.

    Preserves unrelated top-level keys (``editor.fontSize`` etc.) AND
    unrelated env keys inside the three ``terminal.integrated.env.*``
    blocks. When ``force`` is false, an existing conflicting value for
    one of our managed keys is left in place and its dotted path
    (e.g. ``terminal.integrated.env.osx.ANTHROPIC_BASE_URL``) is added
    to the returned conflict set.

    Returns
    -------
    merged:
        The merged dict, safe to serialize with :func:`_dump_json`.
    conflicts:
        Set of dotted paths whose existing value differs from the
        desired one and were NOT overwritten (because ``force=False``).
    """
    # Deep copy via json round-trip — the input dict may share nested
    # references with the caller and we must not mutate it.
    merged = json.loads(json.dumps(existing))
    conflicts: set[str] = set()

    for os_key in _TERMINAL_ENV_KEYS:
        sub = merged.get(os_key)
        if not isinstance(sub, dict):
            # Overwrite any non-dict value at this key — VSCode would
            # ignore it anyway. Preserve nothing since the previous
            # value was already broken.
            sub = {}

        for env_key, env_val in desired_env.items():
            if env_key in sub and sub[env_key] != env_val and not force:
                conflicts.add(f"{os_key}.{env_key}")
                # Leave sub[env_key] unchanged.
                continue
            sub[env_key] = env_val

        merged[os_key] = sub

    return merged, conflicts


# ---------------------------------------------------------------------------
# .envrc
# ---------------------------------------------------------------------------


def _apply_envrc(
    path: Path,
    *,
    base_url: str,
    profile: str | None,
    force: bool,
    dry_run: bool,
    extra_env: dict[str, str] | None = None,
) -> FileOutcome:
    """Write (or refresh) the managed block in ``.envrc``.

    Three cases, and only the third can refuse:

    * **No file** — create it with the fenced block.
    * **Fenced file** — replace the block, keep everything outside it
      byte-for-byte. No ``--force`` needed; this is not destructive.
    * **Unfenced file we did not write** — refuse, unless ``--force``,
      which now *appends* the block rather than replacing the file. The
      operator's lines survive either way.
    """
    block = _render_envrc_block(base_url=base_url, profile=profile, extra_env=extra_env)

    old_text = ""
    if path.exists():
        try:
            old_text = path.read_text(encoding="utf-8")
        except OSError as exc:
            return FileOutcome(
                path=path,
                action="skipped",
                reason=f"cannot read existing file: {exc}",
            )

    if not old_text:
        new_text = block + "\n"
    else:
        split = _split_managed_block(old_text)
        if split is None and _looks_like_our_legacy_output(
            old_text, base_url=base_url, profile=profile
        ):
            # Our own pre-v2.14.0 output: adopt it into the fence silently.
            split = ("", "")
        if split is None:
            outside = ""  # --force adopts the file wholesale; see below
            if not force:
                would = old_text.rstrip("\n") + "\n\n" + block + "\n"
                return FileOutcome(
                    path=path,
                    action="conflict",
                    diff=_text_diff(old_text, would, path),
                    reason=(
                        ".envrc exists and carries no "
                        f"`{_ENVRC_BEGIN}` fence, so CodeRouter did not write it. "
                        "Re-run with --force to APPEND the managed block "
                        "(your existing lines are kept — unlike before "
                        "v2.14.0, --force no longer replaces the whole file); "
                        f"the previous contents are still saved to "
                        f"{_backup_path(path).name}."
                    ),
                )
            # --force on an unfenced file means "adopt it, mine wins". The
            # block is APPENDED, not substituted for the file: direnv applies
            # exports in order, so a duplicate the operator already had is
            # shadowed rather than deleted, and their other lines survive. The
            # conflict scan is skipped here on purpose — refusing would leave
            # --force with nothing it could actually do.
            new_text = old_text.rstrip("\n") + "\n\n" + block + "\n"
        else:
            before, after = split
            outside = before + after
            new_text = (
                before.rstrip("\n") + ("\n\n" if before.strip() else "")
            ) + block + ("\n\n" + after.lstrip("\n") if after.strip() else "\n")

        conflicts = _unmanaged_conflicts(outside, extra_keys=tuple(extra_env or ()))
        if conflicts:
            # direnv applies exports in order, so a duplicate outside the
            # fence would silently win or lose depending on placement.
            # Refusing is the only answer that cannot surprise anyone.
            return FileOutcome(
                path=path,
                action="conflict",
                diff=_text_diff(old_text, new_text, path),
                reason=(
                    "these variables are already exported outside the managed "
                    f"block: {', '.join(conflicts)}. CodeRouter will not write "
                    "a second export for a value you own — direnv would apply "
                    "whichever comes last. Remove your line, or move it inside "
                    f"`{_ENVRC_BEGIN}` … `{_ENVRC_END}`."
                ),
            )

    if new_text == old_text:
        return FileOutcome(path=path, action="unchanged")

    diff = _text_diff(old_text, new_text, path)

    reason = ""
    if not dry_run:
        # H-11: keep the old bytes (mode included) even though the write is
        # now surgical. A bug in the fence logic would take exactly this
        # path. Inside the dry_run guard on purpose — --dry-run must not
        # touch the filesystem at all.
        if old_text:
            backup = _make_backup(path)
            reason = (
                f"previous contents backed up to {backup}"
                if backup is not None
                else (
                    f"could not write {_backup_path(path).name} — "
                    "previous contents were NOT preserved"
                )
            )
        try:
            _atomic_write(path, new_text, mode=_ENVRC_MODE)
        except OSError as exc:
            return FileOutcome(
                path=path,
                action="skipped",
                reason=f"write failed: {exc}",
                diff=diff,
            )
    elif old_text:
        reason = f"would back up previous contents to {_backup_path(path)}"

    return FileOutcome(
        path=path,
        action="updated" if old_text else "created",
        diff=diff,
        reason=reason,
    )


# ---------------------------------------------------------------------------
# v2.14.0 — marker-delimited managed block in .envrc
# ---------------------------------------------------------------------------
#
# Until now, ``--force`` on ``.envrc`` was a whole-file replace: it wrote the
# generated contents over whatever was there, dropping any line the operator
# had added. That is how H-11 happened, and "we back it up first" only helps
# somebody who knows to look for the backup.
#
# The fix is the one codex-router uses on ``~/.codex/config.toml``: fence the
# part we own between markers, rewrite only that, and leave everything else
# byte-for-byte. Once the fence exists, a re-run is no longer destructive, so
# it needs no ``--force`` at all — ``--force`` shrinks back to its honest
# meaning of "yes, adopt a file I did not write".

_ENVRC_BEGIN = "# BEGIN coderouter-managed"
_ENVRC_END = "# END coderouter-managed"

# The variables the managed block owns. A value for one of these sitting
# OUTSIDE the block is a conflict we refuse rather than silently shadow —
# direnv applies exports in order, so whichever came last would win and the
# operator would have no idea which one is live.
_MANAGED_ENVRC_VARS = ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "CODEROUTER_MODE")


def _split_managed_block(text: str) -> tuple[str, str] | None:
    """Split ``text`` around the managed block.

    Returns ``(before, after)`` with the block (and its markers) removed,
    or ``None`` when the file carries no fence. A file with a begin marker
    and no end marker also returns ``None`` — a half-written fence is not
    something to guess at, and the caller turns that into a refusal.
    """
    start = text.find(_ENVRC_BEGIN)
    if start == -1:
        return None
    end = text.find(_ENVRC_END, start)
    if end == -1:
        return None
    return text[:start], text[end + len(_ENVRC_END) :].lstrip("\n")


def _render_envrc_block(
    *, base_url: str, profile: str | None, extra_env: dict[str, str] | None = None
) -> str:
    """The fenced block CodeRouter owns, markers included."""
    lines = [
        _ENVRC_BEGIN,
        "# Managed by `coderouter vscode-init`. Edits inside this block are",
        "# overwritten on the next run; put your own lines outside it.",
        f'export ANTHROPIC_BASE_URL="{base_url}"',
        f'export ANTHROPIC_AUTH_TOKEN="{DEFAULT_TOKEN}"',
    ]
    if profile:
        lines.append(f'export CODEROUTER_MODE="{profile}"')
    for key, value in (extra_env or {}).items():
        lines.append(f'export {key}="{value}"')
    lines.append(_ENVRC_END)
    return "\n".join(lines)


def _unmanaged_conflicts(outside_text: str, *, extra_keys: tuple[str, ...] = ()) -> list[str]:
    """Managed variables the operator exports outside our block.

    v2.16.0: ``extra_keys`` (the Claude Code variables this run writes) are
    checked too — but only when this run writes them, so an operator who
    passed ``--no-claude-code-env`` can keep their own exports.
    """
    found: list[str] = []
    for line in outside_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        for var in (*_MANAGED_ENVRC_VARS, *extra_keys):
            if stripped.startswith((f"export {var}=", f"{var}=")) and var not in found:
                found.append(var)
    return found


def _looks_like_our_legacy_output(text: str, *, base_url: str, profile: str | None) -> bool:
    """True when the file is byte-identical to a pre-v2.14.0 generation.

    An operator upgrading has an unfenced ``.envrc`` that we wrote — asking
    them to pass ``--force`` to adopt our own previous output would be
    rude, and appending a second block would double the exports.
    """
    return text == _render_envrc(base_url=base_url, profile=profile)


def _render_envrc(*, base_url: str, profile: str | None) -> str:
    """Build the .envrc contents. Kept split for test targeting."""
    lines = [
        "# Auto-generated by `coderouter vscode-init`. Safe to edit.",
        "# direnv will export these when you `cd` into this directory.",
        "# Run `direnv allow` once after generation.",
        "",
        f'export ANTHROPIC_BASE_URL="{base_url}"',
        f'export ANTHROPIC_AUTH_TOKEN="{DEFAULT_TOKEN}"',
    ]
    if profile:
        lines.append(f'export CODEROUTER_MODE="{profile}"')
    lines.append("")  # trailing newline
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------


def format_result(result: VSCodeInitResult, *, dry_run: bool, port: int) -> str:
    """Human-readable rendering of the outcomes for CLI stdout.

    The output has three sections:

    1. Per-file outcome lines (with unified diffs where relevant).
    2. A cheat sheet for the extensions this scaffolder does NOT wire
       (Cline / Roo / Kilo / Continue) — the user copy-pastes.
    3. A trailing hint about the fuller ``docs/guides/vscode.md``.

    ``dry_run`` toggles the "(dry-run — nothing written)" reminder.
    """
    lines: list[str] = []
    marker_for = {
        "created": "  new ",
        "updated": " diff ",
        "unchanged": " nop  ",
        "conflict": "CONFLICT",
        "skipped": " SKIP ",
    }
    for o in result.outcomes:
        marker = marker_for.get(o.action, f" {o.action} ")
        lines.append(f"[{marker}] {o.path}")
        if o.reason:
            lines.append(f"          → {o.reason}")
        if o.diff and o.action in ("created", "updated", "conflict"):
            lines.append("")
            lines.extend(o.diff.rstrip("\n").splitlines())
            lines.append("")

    if dry_run:
        lines.append("")
        lines.append("(dry-run — no files were written. Re-run without --dry-run to apply.)")

    lines.append("")
    lines.append(_render_cheat_sheet(port))
    return "\n".join(lines)


def _render_cheat_sheet(port: int) -> str:
    """Emit the copy-paste snippets shown after every ``vscode-init`` run.

    The Continue.dev snippet is built with :func:`json.dumps` rather
    than hand-formatted string concatenation to guarantee the output
    is syntactically valid JSON. An earlier hand-formatted version
    accidentally leaked an extra ``}`` from mismatched f-string /
    plain-string brace escaping — the JSON round-trip closes that
    class of bug and the test in ``tests/test_vscode_init.py`` pins
    the invariant.
    """
    continue_snippet = _render_continue_snippet(port)
    return (
        "─── Other extensions (copy/paste) ─────────────────────────────\n"
        "\n"
        "  Cline / Roo Code / Kilo Code:\n"
        "    API Provider: OpenAI Compatible\n"
        f"    Base URL:     http://localhost:{port}/v1\n"
        "    API Key:      dummy   (any non-empty value)\n"
        "    Model ID:     anything (CodeRouter routes via default_profile)\n"
        "\n"
        "  Continue.dev — add to the models array in ~/.continue/config.json:\n"
        f"{continue_snippet}\n"
        "\n"
        "Full guide: docs/guides/vscode.md in the CodeRouter repo.\n"
    )


def _render_continue_snippet(port: int) -> str:
    """Return one indented pretty-printed JSON block for Continue.dev.

    Split out from :func:`_render_cheat_sheet` so tests can
    ``json.loads`` the exact bytes users copy-paste and verify the
    snippet is valid.
    """
    model_entry: dict[str, str] = {
        "title": "CodeRouter",
        "provider": "openai",
        "model": "any-id",
        "apiBase": f"http://localhost:{port}/v1",
        "apiKey": "dummy",
    }
    pretty = json.dumps(model_entry, indent=2, ensure_ascii=False)
    # Indent every line by 4 spaces so it aligns under the "─── Other
    # extensions" header exactly like the Cline block above.
    return "\n".join("    " + line for line in pretty.splitlines())


def exit_code_for(result: VSCodeInitResult) -> int:
    """Map the outcome set to a shell exit code.

    * 0 — every file is clean or a no-op.
    * 2 — at least one conflict (needs ``--force``); nothing was
          written for the conflicting file(s), but other files may
          have been.
    * 1 — a hard error prevented processing (unparseable JSON, write
          failure). Treated as more severe than a conflict.
    """
    if result.has_skipped:
        return 1
    if result.has_conflicts:
        return 2
    return 0


# ---------------------------------------------------------------------------
# Low-level I/O helpers
# ---------------------------------------------------------------------------


def _dump_json(obj: object) -> str:
    """Deterministic pretty JSON for settings.json.

    ``indent=2`` matches VSCode's own formatter default, ``ensure_ascii=False``
    keeps CJK env values (e.g. a profile name) readable in the file.
    A trailing newline is appended so POSIX tools don't complain.
    """
    return json.dumps(obj, indent=2, ensure_ascii=False) + "\n"


def _default_file_mode() -> int:
    """Return the mode a plain ``open(..., "w")`` would produce.

    ``tempfile.mkstemp`` deliberately creates its file 0600. That is the
    right default for a secret-bearing ``.envrc``, but it would silently
    tighten ``settings.json`` (previously umask-derived, typically
    0644). Reading the umask back keeps the pre-existing behaviour for
    files that have no explicit mode requirement.
    """
    current = os.umask(0)
    os.umask(current)
    return 0o666 & ~current


def _atomic_write(path: Path, text: str, *, mode: int | None = None) -> None:
    """Write ``text`` to ``path`` atomically (tmp + ``os.replace``).

    A partial write cannot corrupt the target file: the tmp file lives
    on the same filesystem (same parent directory) so ``os.replace`` is
    an atomic inode swap on POSIX. On Windows ``os.replace`` is also
    atomic since Python 3.3.

    v2.11 hardening (H-11). The tmp file used to be the predictable
    ``<name>.tmp`` next to the target, written with ``Path.write_text``:

    * a pre-planted symlink at that exact path was *followed*, so the
      write landed on the link's target (CWE-377);
    * two concurrent ``vscode-init`` runs on the same workspace
      clobbered each other's tmp file;
    * ``os.replace`` swaps inodes, so the target's mode was reset to the
      umask default — a 0600 ``.envrc`` holding ``ANTHROPIC_AUTH_TOKEN``
      silently became 0644;
    * without ``fsync`` a crash right after the rename could leave a
      zero-length file.

    So: ``tempfile.mkstemp`` (random name, ``O_EXCL``, no symlink
    following), ``fsync`` before the rename, mode restored from the
    file being replaced (or from ``mode`` for a fresh file), and the tmp
    file unlinked in ``finally`` so a failed write leaves no debris.

    Parameters
    ----------
    mode:
        POSIX mode for a *newly created* file. Ignored when ``path``
        already exists — an existing file's own mode wins, so we never
        loosen (or tighten) what the operator chose. ``None`` means
        "whatever the umask would have given us".
    """
    existing_mode: int | None = None
    try:
        existing_mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        existing_mode = None

    target_mode = existing_mode if existing_mode is not None else mode
    if target_mode is None:
        target_mode = _default_file_mode()

    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        # Windows has no POSIX mode bits worth honouring; os.chmod there
        # only toggles the read-only flag, which would be a surprising
        # side effect of "preserve the mode".
        if os.name != "nt":
            os.chmod(tmp, target_mode)
        os.replace(tmp, path)
    finally:
        # Best effort: a leftover tmp file is noise, not corruption, and
        # cleaning it up must never mask the original exception.
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)


def _backup_path(path: Path) -> Path:
    """Return the ``.bak`` sibling used to preserve ``path``'s old bytes."""
    return path.with_name(path.name + ".bak")


def _make_backup(path: Path) -> Path | None:
    """Copy ``path`` to ``<name>.bak``, preserving mode and mtime.

    Returns the backup path, or ``None`` when the copy failed (the
    caller then proceeds without a backup rather than refusing to write
    — but the failure is not silent, see the ``reason`` plumbing).

    The destination is unlinked first: ``shutil.copy2`` opens the
    destination for writing and would happily follow a symlink planted
    at ``<name>.bak``. ``Path.unlink`` removes the link itself, so the
    subsequent copy always creates a fresh regular file.
    """
    backup = _backup_path(path)
    try:
        backup.unlink(missing_ok=True)
        shutil.copy2(path, backup)
    except OSError:
        return None
    return backup


def _text_diff(old: str, new: str, path: Path) -> str:
    """Unified diff between two text buffers, labelled by file name."""
    return "".join(
        unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"a/{path.name}",
            tofile=f"b/{path.name}",
            n=3,
        )
    )


def _json_diff(old: dict, new: dict, path: Path) -> str:
    """Diff between two JSON dicts, formatted as ``_dump_json`` would."""
    old_text = _dump_json(old) if old else ""
    new_text = _dump_json(new)
    return _text_diff(old_text, new_text, path)
