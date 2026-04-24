"""`.env` file loader (v1.6.3).

Purpose
-------
Provide a standard-library-only parser for `.env`-style files so that
``coderouter serve --env-file PATH`` can act as a thin gateway between
CodeRouter and any tool that emits a `.env` (1Password CLI, sops,
direnv, plain hand-edited files, etc.).

We deliberately do NOT pull in ``python-dotenv``: the runtime-deps
freeze policy (5 packages, see plan.md §5.4) is part of the project's
audit story. The parser below covers the cases that 1Password / sops /
manual editing actually emit; the spec is intentionally narrower than
``python-dotenv`` (no variable expansion, no command substitution, no
multi-line values).

File format
-----------
Each non-empty, non-comment line must match::

    [export ]KEY=value

Where:

* ``KEY`` is ``[A-Za-z_][A-Za-z0-9_]*`` (POSIX identifier).
* ``value`` is one of:
    - bare:        ``value`` (no whitespace, no quotes)
    - double-quoted: ``"value with spaces"`` — backslash escapes
      ``\\n`` ``\\t`` ``\\"`` ``\\\\`` are interpreted.
    - single-quoted: ``'literal value'`` — no escape processing,
      contents are taken verbatim.
* Inline comments (` # comment`) are stripped from BARE values only.
  Quoted values keep ``#`` verbatim.
* Lines starting with ``#`` (after optional whitespace) are skipped.
* Blank lines are skipped.
* The ``export`` prefix is optional and discarded — supports `.env`
  files that double as shell sources (``source .env``).

Loading semantics
-----------------
:func:`load_env_file` returns a ``dict[str, str]`` and (by default)
copies entries into ``os.environ`` ONLY for keys that are not already
set. This is the v1.6.3 default — the file is treated as
"defaults / setup" rather than authoritative override, so an operator
who deliberately exports an override at the shell wins. Pass
``override=True`` to flip the precedence (useful for tests).

The parser raises :class:`EnvFileError` on malformed lines (not on
unknown keys — those just become entries in the returned dict). The
caller is expected to surface the error to the user; the CLI does so
with a friendly stderr message and exit 1.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from pathlib import Path

__all__ = [
    "EnvFileError",
    "load_env_file",
    "parse_env_file",
]


# POSIX identifier — matches sh / bash / zsh shell variable name rules.
# This is intentionally strict: keys with hyphens or starting with a
# digit are rejected so we surface the typo before exporting them
# into a place that can't actually consume them.
_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Recognized double-quote escape sequences. Anything else after a
# backslash inside double quotes is left as `\<char>` (matches POSIX
# "weak quote" behavior of the shell — surprising-free for users who
# copy paste shell snippets).
_DQ_ESCAPES = {
    "n": "\n",
    "t": "\t",
    "r": "\r",
    '"': '"',
    "\\": "\\",
    "$": "$",  # so ``"\$VAR"`` survives as ``$VAR`` literally
    "`": "`",
}


class EnvFileError(ValueError):
    """Raised when a `.env`-style file cannot be parsed.

    The exception message contains the file path and 1-based line
    number so the user can jump straight to the offending row. Caught
    by the CLI to emit a friendly error and exit 1.
    """


def parse_env_file(path: str | os.PathLike[str]) -> dict[str, str]:
    """Parse a `.env` file at ``path`` into a ``dict[str, str]``.

    Does NOT mutate ``os.environ`` — that is :func:`load_env_file`'s
    job. Pure parser, useful for tests and for callers that want to
    diff against the current environment before applying.

    Raises:
        FileNotFoundError: ``path`` does not exist.
        EnvFileError: malformed line (with file path + 1-based line
            number in the message).
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"env file not found: {p}")

    parsed: dict[str, str] = {}
    text = p.read_text(encoding="utf-8")
    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        # Skip blank lines and pure comment lines.
        if not line or line.startswith("#"):
            continue

        # Strip optional ``export `` prefix. After ``.strip()`` above,
        # ``line == "export"`` (no trailing whitespace) is impossible to
        # land in this branch — that case falls through to the
        # missing-``=`` check below, which surfaces a sufficient error
        # to the user. We only need to peel ``export`` when followed
        # by content.
        if line.startswith("export ") or line.startswith("export\t"):
            line = line[len("export") :].lstrip()

        # Split on the FIRST `=`. Subsequent `=` are part of the value.
        if "=" not in line:
            raise EnvFileError(f"{p}:{lineno}: missing `=` separator: {raw_line!r}")
        key_raw, value_raw = line.split("=", 1)
        key = key_raw.strip()

        if not _KEY_RE.match(key):
            raise EnvFileError(
                f"{p}:{lineno}: invalid key {key!r} "
                f"(must match {_KEY_RE.pattern})"
            )

        try:
            value = _parse_value(value_raw)
        except EnvFileError as exc:
            # Re-attach file:lineno context.
            raise EnvFileError(f"{p}:{lineno}: {exc}") from None

        parsed[key] = value

    return parsed


def load_env_file(
    path: str | os.PathLike[str],
    *,
    override: bool = False,
    environ: dict[str, str] | None = None,
) -> list[str]:
    """Parse ``path`` and copy entries into ``environ`` (default: ``os.environ``).

    Args:
        path: Path to the `.env`-style file.
        override: If ``False`` (default), only set keys that aren't
            already in ``environ`` — file values are best-effort
            defaults, the shell environment wins. If ``True``, file
            values overwrite existing entries.
        environ: Target mapping; defaults to ``os.environ``. Tests can
            pass a plain ``dict`` to avoid mutating the real env.

    Returns:
        List of key names that were actually applied (i.e. either
        newly set or overwritten). Useful for the CLI to log
        "loaded N variables from <path>".

    Raises:
        FileNotFoundError, EnvFileError: see :func:`parse_env_file`.
    """
    target = environ if environ is not None else os.environ
    applied: list[str] = []
    for key, value in parse_env_file(path).items():
        if not override and key in target:
            continue
        target[key] = value
        applied.append(key)
    return applied


def load_env_files(
    paths: Iterable[str | os.PathLike[str]],
    *,
    override: bool = False,
    environ: dict[str, str] | None = None,
) -> list[tuple[str, list[str]]]:
    """Apply :func:`load_env_file` to multiple paths in order.

    Useful for layering: ``[~/.coderouter/.env, ./.env]`` lets a user
    keep cross-project defaults globally and override per-project at
    the cwd. Files are processed left-to-right, so later files override
    earlier ones (when ``override=True``) or fill in gaps (default).

    Returns a list of ``(path, applied_keys)`` tuples in load order so
    the caller can log a per-file summary.
    """
    out: list[tuple[str, list[str]]] = []
    for p in paths:
        out.append((str(p), load_env_file(p, override=override, environ=environ)))
    return out


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _parse_value(raw: str) -> str:
    """Parse the right-hand side of `KEY=value`.

    Strips leading whitespace (so ``KEY = value`` is tolerated, though
    style-discouraged), detects quoting, applies the appropriate
    escape rules.
    """
    s = raw.lstrip()

    if not s:
        return ""

    if s[0] == '"':
        # Double-quoted: process escapes, terminate at unescaped ".
        return _parse_double_quoted(s)
    if s[0] == "'":
        # Single-quoted: literal until next single quote.
        return _parse_single_quoted(s)

    # Bare value: strip inline comment and trailing whitespace.
    # We use a tiny state machine rather than a regex so we don't
    # accidentally match `#` inside a hash-bang-looking value
    # (e.g. ``KEY=#1`` — though questionable, this preserves it).
    # The rule: a bare value's inline comment requires whitespace
    # before `#`, so ``KEY=foo#bar`` keeps `foo#bar`.
    out_chars: list[str] = []
    prev_was_space = False
    for ch in s:
        if ch == "#" and prev_was_space:
            break
        out_chars.append(ch)
        prev_was_space = ch in (" ", "\t")
    return "".join(out_chars).rstrip()


def _parse_double_quoted(s: str) -> str:
    """Parse a string starting at the opening ``"``.

    Recognized escapes: ``\\n``, ``\\t``, ``\\r``, ``\\"``, ``\\\\``,
    ``\\$``, ``\\```. Unknown escapes pass through as-is (POSIX weak
    quoting compatible).
    """
    assert s[0] == '"'
    out: list[str] = []
    i = 1
    while i < len(s):
        ch = s[i]
        if ch == "\\" and i + 1 < len(s):
            nxt = s[i + 1]
            out.append(_DQ_ESCAPES.get(nxt, "\\" + nxt))
            i += 2
            continue
        if ch == '"':
            # End of quoted value. Anything after (other than whitespace
            # and an inline comment) is a syntax error.
            tail = s[i + 1 :].lstrip()
            if tail and not tail.startswith("#"):
                raise EnvFileError(
                    f"unexpected content after closing quote: {tail!r}"
                )
            return "".join(out)
        out.append(ch)
        i += 1
    raise EnvFileError("unterminated double-quoted value")


def _parse_single_quoted(s: str) -> str:
    """Parse a string starting at the opening ``'``.

    Single quotes are literal — no escapes, the value is everything
    up to the next ``'``.
    """
    assert s[0] == "'"
    end = s.find("'", 1)
    if end == -1:
        raise EnvFileError("unterminated single-quoted value")
    tail = s[end + 1 :].lstrip()
    if tail and not tail.startswith("#"):
        raise EnvFileError(f"unexpected content after closing quote: {tail!r}")
    return s[1:end]
