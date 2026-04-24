"""Tests for ``coderouter.config.env_file`` (v1.6.3).

The parser is intentionally narrower than ``python-dotenv`` (no
variable expansion, no command substitution, no multi-line values),
so the tests focus on:

* Structural parsing: bare values, double-quoted, single-quoted,
  inline comments, blank lines, the ``export`` prefix.
* The deliberate footguns we surface: invalid keys, missing ``=``,
  unterminated quotes, content after a closing quote.
* The override-vs-default-fill semantics of :func:`load_env_file`.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from coderouter.config.env_file import (
    EnvFileError,
    load_env_file,
    load_env_files,
    parse_env_file,
)

# ---------------------------------------------------------------------------
# parse_env_file
# ---------------------------------------------------------------------------


def _write(tmp_path: Path, content: str) -> Path:
    p = tmp_path / ".env"
    p.write_text(content, encoding="utf-8")
    return p


def test_parse_bare_value(tmp_path: Path) -> None:
    p = _write(tmp_path, "FOO=bar\n")
    assert parse_env_file(p) == {"FOO": "bar"}


def test_parse_export_prefix_is_optional(tmp_path: Path) -> None:
    p = _write(tmp_path, "export FOO=bar\nBAZ=qux\nexport\tWITH_TAB=1\n")
    assert parse_env_file(p) == {"FOO": "bar", "BAZ": "qux", "WITH_TAB": "1"}


def test_parse_double_quoted_value(tmp_path: Path) -> None:
    p = _write(tmp_path, 'FOO="hello world"\n')
    assert parse_env_file(p) == {"FOO": "hello world"}


def test_parse_double_quoted_escapes(tmp_path: Path) -> None:
    p = _write(tmp_path, r'FOO="line1\nline2\t\"quoted\""' + "\n")
    assert parse_env_file(p) == {"FOO": 'line1\nline2\t"quoted"'}


def test_parse_double_quoted_unknown_escape_passes_through(tmp_path: Path) -> None:
    # POSIX weak quoting: unknown escape leaves the backslash literal.
    p = _write(tmp_path, r'FOO="\xhello"' + "\n")
    assert parse_env_file(p) == {"FOO": r"\xhello"}


def test_parse_single_quoted_is_literal(tmp_path: Path) -> None:
    # Single quotes never process escapes — `\n` stays as `\n`.
    p = _write(tmp_path, r"FOO='literal $VAR \n'" + "\n")
    assert parse_env_file(p) == {"FOO": r"literal $VAR \n"}


def test_parse_inline_comment_on_bare_value(tmp_path: Path) -> None:
    p = _write(tmp_path, "FOO=bar  # this is a comment\n")
    assert parse_env_file(p) == {"FOO": "bar"}


def test_parse_hash_inside_bare_value_is_kept(tmp_path: Path) -> None:
    # No whitespace before `#` → it's part of the value (matches shell).
    p = _write(tmp_path, "FOO=bar#baz\n")
    assert parse_env_file(p) == {"FOO": "bar#baz"}


def test_parse_quoted_value_keeps_hash(tmp_path: Path) -> None:
    # `#` inside quotes is verbatim, no comment stripping.
    p = _write(tmp_path, 'FOO="bar # not a comment"\n')
    assert parse_env_file(p) == {"FOO": "bar # not a comment"}


def test_parse_blank_lines_and_pure_comments(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        "\n"
        "# top-level comment\n"
        "FOO=bar\n"
        "\n"
        "  # indented comment\n"
        "BAZ=qux\n"
        "\n",
    )
    assert parse_env_file(p) == {"FOO": "bar", "BAZ": "qux"}


def test_parse_empty_value(tmp_path: Path) -> None:
    p = _write(tmp_path, "FOO=\nBAR=\n")
    assert parse_env_file(p) == {"FOO": "", "BAR": ""}


def test_parse_value_can_contain_equals_signs(tmp_path: Path) -> None:
    p = _write(tmp_path, "FOO=a=b=c\n")
    assert parse_env_file(p) == {"FOO": "a=b=c"}


def test_parse_quoted_value_inline_comment_after_close(tmp_path: Path) -> None:
    p = _write(tmp_path, 'FOO="hello"  # tail\n')
    assert parse_env_file(p) == {"FOO": "hello"}


def test_parse_missing_equals_raises(tmp_path: Path) -> None:
    p = _write(tmp_path, "BARE_LINE\n")
    with pytest.raises(EnvFileError, match="missing `=` separator"):
        parse_env_file(p)


def test_parse_invalid_key_starts_with_digit(tmp_path: Path) -> None:
    p = _write(tmp_path, "1FOO=bar\n")
    with pytest.raises(EnvFileError, match="invalid key"):
        parse_env_file(p)


def test_parse_invalid_key_with_hyphen(tmp_path: Path) -> None:
    p = _write(tmp_path, "FOO-BAR=baz\n")
    with pytest.raises(EnvFileError, match="invalid key"):
        parse_env_file(p)


def test_parse_unterminated_double_quote(tmp_path: Path) -> None:
    p = _write(tmp_path, 'FOO="no closing\n')
    with pytest.raises(EnvFileError, match="unterminated double-quoted value"):
        parse_env_file(p)


def test_parse_unterminated_single_quote(tmp_path: Path) -> None:
    p = _write(tmp_path, "FOO='no closing\n")
    with pytest.raises(EnvFileError, match="unterminated single-quoted value"):
        parse_env_file(p)


def test_parse_garbage_after_closing_quote(tmp_path: Path) -> None:
    p = _write(tmp_path, 'FOO="hello" garbage\n')
    with pytest.raises(EnvFileError, match="unexpected content after closing quote"):
        parse_env_file(p)


def test_parse_export_with_nothing_after(tmp_path: Path) -> None:
    p = _write(tmp_path, "export\n")
    # 'export\n' with no body — the line strip leaves `export` alone,
    # which doesn't start with `export ` so it falls through to the
    # missing-= check.
    with pytest.raises(EnvFileError, match="missing `=` separator"):
        parse_env_file(p)


def test_parse_export_with_trailing_whitespace_only(tmp_path: Path) -> None:
    # `export   ` (trailing whitespace only) becomes just `export` after
    # the leading-line strip, so it falls through to the missing-`=`
    # path rather than a special "export with nothing" message. The
    # diagnostic is still actionable so we accept the more general one.
    p = _write(tmp_path, "export  \n")
    with pytest.raises(EnvFileError, match="missing `=` separator"):
        parse_env_file(p)


def test_parse_file_not_found_raises_filenotfound(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        parse_env_file(tmp_path / "nonexistent.env")


def test_parse_error_message_includes_path_and_lineno(tmp_path: Path) -> None:
    p = _write(tmp_path, "GOOD=ok\nBAD-KEY=val\n")
    with pytest.raises(EnvFileError) as exc_info:
        parse_env_file(p)
    msg = str(exc_info.value)
    assert str(p) in msg
    assert ":2:" in msg


# ---------------------------------------------------------------------------
# load_env_file (override semantics)
# ---------------------------------------------------------------------------


def test_load_default_does_not_overwrite_existing(tmp_path: Path) -> None:
    p = _write(tmp_path, "FOO=fromfile\nBAR=newkey\n")
    env: dict[str, str] = {"FOO": "preset"}
    applied = load_env_file(p, environ=env)
    assert env == {"FOO": "preset", "BAR": "newkey"}
    assert applied == ["BAR"]


def test_load_with_override_overwrites_existing(tmp_path: Path) -> None:
    p = _write(tmp_path, "FOO=fromfile\nBAR=newkey\n")
    env: dict[str, str] = {"FOO": "preset"}
    applied = load_env_file(p, override=True, environ=env)
    assert env == {"FOO": "fromfile", "BAR": "newkey"}
    assert sorted(applied) == ["BAR", "FOO"]


def test_load_returns_empty_when_all_already_set(tmp_path: Path) -> None:
    p = _write(tmp_path, "FOO=fromfile\nBAR=fromfile\n")
    env: dict[str, str] = {"FOO": "x", "BAR": "y"}
    applied = load_env_file(p, environ=env)
    assert applied == []
    assert env == {"FOO": "x", "BAR": "y"}


def test_load_uses_real_os_environ_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = _write(tmp_path, "CODEROUTER_TEST_KEY=hello\n")
    monkeypatch.delenv("CODEROUTER_TEST_KEY", raising=False)
    applied = load_env_file(p)
    assert applied == ["CODEROUTER_TEST_KEY"]
    assert os.environ["CODEROUTER_TEST_KEY"] == "hello"


# ---------------------------------------------------------------------------
# load_env_files (multi-file layering)
# ---------------------------------------------------------------------------


def test_load_files_layer_left_to_right_default_fill_only(tmp_path: Path) -> None:
    """Default semantics: each file fills in keys not already set.

    File A defines FOO and BAR. File B defines BAR and BAZ. With
    override=False, BAR from B is ignored (A's value wins because A
    is processed first). BAZ from B is added.
    """
    a = tmp_path / "a.env"
    a.write_text("FOO=a_foo\nBAR=a_bar\n", encoding="utf-8")
    b = tmp_path / "b.env"
    b.write_text("BAR=b_bar\nBAZ=b_baz\n", encoding="utf-8")
    env: dict[str, str] = {}
    results = load_env_files([a, b], environ=env)
    assert env == {"FOO": "a_foo", "BAR": "a_bar", "BAZ": "b_baz"}
    assert results == [(str(a), ["FOO", "BAR"]), (str(b), ["BAZ"])]


def test_load_files_layer_left_to_right_with_override(tmp_path: Path) -> None:
    """override=True: later files DO replace earlier values.

    Same setup as the previous test, but override flips the semantics
    so B's BAR wins.
    """
    a = tmp_path / "a.env"
    a.write_text("FOO=a_foo\nBAR=a_bar\n", encoding="utf-8")
    b = tmp_path / "b.env"
    b.write_text("BAR=b_bar\nBAZ=b_baz\n", encoding="utf-8")
    env: dict[str, str] = {}
    load_env_files([a, b], override=True, environ=env)
    assert env == {"FOO": "a_foo", "BAR": "b_bar", "BAZ": "b_baz"}
