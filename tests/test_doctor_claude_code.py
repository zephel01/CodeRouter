"""v2.16.0: ``coderouter doctor --check-claude-code``."""

from __future__ import annotations

import pytest

from coderouter import cli
from coderouter.doctor_claude_code import check, parse_version, run_check

_GOOD_ENV = {
    "ENABLE_TOOL_SEARCH": "false",
    "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "32768",
    "CLAUDE_CODE_AUTO_MODE_SERVER": "0",
}


def _warns(findings) -> list[str]:  # type: ignore[no-untyped-def]
    return [f.message for f in findings if f.level == "warn"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [("2.1.309 (Claude Code)", (2, 1, 309)), ("claude 2.1.30", (2, 1, 30)), ("garbage", None)],
)
def test_parse_version(text: str, expected: tuple[int, int, int] | None) -> None:
    assert parse_version(text) == expected


def test_clean_setup_has_no_warnings() -> None:
    assert _warns(check((2, 1, 309), _GOOD_ENV)) == []


def test_old_version_warns() -> None:
    assert any("2.1.301" in w for w in _warns(check((2, 1, 300), _GOOD_ENV)))


def test_2_1_304_warns_tool_search_regression() -> None:
    assert any("2.1.305" in w for w in _warns(check((2, 1, 304), _GOOD_ENV)))


@pytest.mark.parametrize("missing", list(_GOOD_ENV))
def test_each_missing_env_var_warns(missing: str) -> None:
    env = {k: v for k, v in _GOOD_ENV.items() if k != missing}
    warns = _warns(check((2, 1, 309), env))
    assert len(warns) == 1
    assert missing in warns[0]


def test_max_context_not_required_before_1m_default() -> None:
    env = {k: v for k, v in _GOOD_ENV.items() if k != "CLAUDE_CODE_MAX_CONTEXT_TOKENS"}
    # 2.1.284 predates the 1M-behind-a-gateway default (2.1.285).
    assert _warns(check((2, 1, 301), env)) != []
    assert all("CLAUDE_CODE_MAX_CONTEXT_TOKENS" not in w for w in _warns(check((2, 1, 284), env)))


def test_run_check_with_injected_runner_and_exit_codes() -> None:
    ok = run_check(env=_GOOD_ENV, run=lambda cmd: "2.1.309 (Claude Code)\n")
    assert ok.version == (2, 1, 309)
    assert ok.exit_code == 0
    bad = run_check(env={}, run=lambda cmd: "2.1.309\n")
    assert bad.exit_code == 2


def test_unparseable_version_is_info_not_failure() -> None:
    report = run_check(env=_GOOD_ENV, run=lambda cmd: "???")
    assert report.version is None
    assert report.exit_code == 0
    assert report.findings[0].level == "info"


def test_cli_flag_wires_through(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    import coderouter.doctor_claude_code as mod

    monkeypatch.setattr(mod, "_claude_version", lambda run=None: ((2, 1, 309), None))
    for k, v in _GOOD_ENV.items():
        monkeypatch.setenv(k, v)
    rc = cli.main(["doctor", "--check-claude-code"])
    assert rc == 0
    assert "Claude Code 2.1.309" in capsys.readouterr().out
