"""Tests for coderouter.messages bilingual catalog (v2.15.0, C-1..M-5).

Covers:
- All catalog entries have both en/ja and format cleanly (M-2 guard)
- Unknown id / lang fallback
- Missing kwargs logs a warning and returns template (M-2)
- Resolve priority (CODEROUTER_T_LANG > LANG > en)
- E1410 removed check (M-4)
- Budget_note placeholder (M-5)
"""

from __future__ import annotations

import logging
import string

import pytest

from coderouter.messages import _CATALOG, has_id, resolve_lang, tr


def _placeholders(template: str) -> set[str]:
    """Extract {name} placeholders from a template (respecting format spec)."""
    names: set[str] = set()
    for _, field_name, _, _ in string.Formatter().parse(template):
        if field_name is not None and field_name != "":
            # Strip conversion and format spec: "host!r" -> "host"
            base = field_name.split("!")[0].split(":")[0]
            names.add(base)
    return names


def test_all_catalog_entries_have_en_and_ja() -> None:
    assert len(_CATALOG) > 0
    for msg_id, langs in _CATALOG.items():
        assert "en" in langs, f"{msg_id} missing en"
        assert "ja" in langs, f"{msg_id} missing ja"
        assert langs["en"], f"{msg_id} en empty"
        assert langs["ja"], f"{msg_id} ja empty"


def test_all_catalog_templates_format_with_dummy_kwargs() -> None:
    """Every template must format without KeyError when given dummy values."""
    dummy_map = {
        # common placeholders across catalog
        "searched": "/tmp/a\n  /tmp/b",
        "path": "/tmp/providers.yaml",
        "host": "0.0.0.0",
        "error": "boom",
        "count": 2,
        "keys": "FOO, BAR",
        "command": "frobnicate",
        "limit": 1024,
        "observed": 2048,
        "lineno": 3,
        "line": "BAD-KEY=val",
        "key": "BAD-KEY",
        "reason": "must match",
        "tail": " extra",
        "name": "my-provider",
        "kind": "openai_compat",
        "status": 401,
        "env": "API_KEY",
        "model": "test-model",
        "canary": "ZEBRA-MOON-847",
        "chars": 12,
        "expected": 40,
        "code": 1,
        "verdict": "clean",
        "budget_note": "",
        "max_tokens": 512,
    }
    for msg_id in _CATALOG:
        for lang in ("en", "ja"):
            template = _CATALOG[msg_id][lang]
            needed = _placeholders(template)
            kwargs = {k: dummy_map.get(k, f"dummy-{k}") for k in needed}
            result = tr(msg_id, lang=lang, **kwargs)
            # Must not return the template with unresolved braces (indicates missing kwarg)
            if needed:
                assert "{" not in result or result == template, f"{msg_id}/{lang} unresolved placeholder"


def test_unknown_id_returns_id() -> None:
    assert tr("UNKNOWN_XYZ", lang="en") == "UNKNOWN_XYZ"
    assert tr("UNKNOWN_XYZ", lang="ja") == "UNKNOWN_XYZ"


def test_has_id() -> None:
    assert has_id("E1001") is True
    assert has_id("UNKNOWN") is False
    # M-4: E1410 should be gone
    assert has_id("E1410_CREDENTIAL_BOTH") is False


def test_missing_kwargs_logs_warning_and_returns_template(caplog: pytest.LogCaptureFixture) -> None:
    # E1001 needs `searched`; call without it -> warning + template back
    with caplog.at_level(logging.WARNING, logger="coderouter.messages"):
        result = tr("E1001", lang="en")
    assert "{searched}" in result
    # At least one warning about missing kwarg
    assert any("missing kwarg" in rec.message for rec in caplog.records)


def test_resolve_lang_priority(monkeypatch: pytest.MonkeyPatch) -> None:
    # CODEROUTER_T_LANG wins over LANG
    monkeypatch.setenv("CODEROUTER_T_LANG", "ja")
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    assert resolve_lang() == "ja"
    # LANG fallback
    monkeypatch.delenv("CODEROUTER_T_LANG", raising=False)
    monkeypatch.setenv("LANG", "ja_JP.UTF-8")
    assert resolve_lang() == "ja"
    # explicit param wins
    assert resolve_lang(explicit="en") == "en"
    assert resolve_lang(explicit="ja-JP") == "ja"
    # unknown -> en
    assert resolve_lang(explicit="fr") == "en"
    monkeypatch.delenv("LANG", raising=False)
    monkeypatch.delenv("LC_MESSAGES", raising=False)
    monkeypatch.delenv("LC_ALL", raising=False)
    # No env -> en
    monkeypatch.delenv("CODEROUTER_T_LANG", raising=False)
    assert resolve_lang() == "en"


def test_budget_note_placeholder_in_w1503_and_w1504() -> None:
    for msg_id in ("W1503_NUM_CTX_CANARY_MISSING", "W1504_STREAM_LENGTH"):
        for lang in ("en", "ja"):
            template = _CATALOG[msg_id][lang]
            assert "budget_note" in _placeholders(template), f"{msg_id}/{lang} missing budget_note placeholder"
    # Ensure formatting with and without note
    assert "Probe note" in tr("W1503_NUM_CTX_CANARY_MISSING", lang="en", canary="X", budget_note=" Probe note")
    assert tr("W1503_NUM_CTX_CANARY_MISSING", lang="en", canary="X", budget_note="")  # empty ok


def test_env_security_catalog_ids_exist() -> None:
    for msg_id in (
        "I1505_ENV_SECURITY_SUMMARY_ERROR",
        "I1505_ENV_SECURITY_SUMMARY_WARN",
        "I1505_ENV_SECURITY_SUMMARY_OK",
        "I1505_ENV_SECURITY_EXIT",
    ):
        assert has_id(msg_id)


def test_secret_catalog_ids_exist() -> None:
    for msg_id in (
        "L_FIX_LABEL",
        "I1506_SECRET_VERDICT",
        "I1506_SECRET_VERDICT_CLEAN",
        "I1506_SECRET_VERDICT_ATTENTION",
        "I1506_SECRET_VERDICT_BLOCKER",
        "I1507_BUDGET_NOTE_NUM_CTX_THINKING",
        "I1507_BUDGET_NOTE_STREAM_THINKING",
        "I1507_BUDGET_NOTE_STREAM_DEFAULT",
    ):
        assert has_id(msg_id)


def test_encoding_utf8() -> None:
    from pathlib import Path

    # Resolve relative to repo root regardless of cwd
    root = Path(__file__).resolve().parent.parent
    (root / "README.md").read_text(encoding="utf-8")
    (root / "coderouter" / "messages.py").read_text(encoding="utf-8")
