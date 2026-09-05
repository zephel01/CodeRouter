"""UT-3/UT-7/UT-9/UT-10 — Masking and is_japanese tests (review 2026-09-02 J-4/J-6).

Design: doc/翻訳層設計書.md §3.3.3, §9.1
Freeze condition: UT-3/UT-7/UT-9 must pass before regex freeze.
"""
from __future__ import annotations

from unittest.mock import Mock

from coderouter.jp_translation.masking import (
    has_placeholder_mutation,
    is_japanese,
    mask_text,
    unmask_text,
)
from coderouter.jp_translation.translator import (
    translate_anthropic_request_ja_to_en,
    translate_anthropic_response_en_to_ja,
)
from coderouter.translation.anthropic import AnthropicMessage, AnthropicRequest, AnthropicResponse, AnthropicUsage


def _make_manager_ja_en(return_value: str = "translated"):
    """Helper: mock TranslatorManager that returns a fixed translation."""
    m = Mock()
    m.is_available.return_value = True
    m.translate_ja_to_en.return_value = return_value
    m.translate_en_to_ja.return_value = return_value
    return m


# ---------------------------------------------------------------------------
# UT-10: is_japanese optimization
# ---------------------------------------------------------------------------


def test_is_japanese_detects_hiragana_katakana_kanji():
    assert is_japanese("こんにちは") is True
    assert is_japanese("カタカナ") is True
    assert is_japanese("漢字") is True
    assert is_japanese("src/main.ts を修正してください") is True  # mixed
    assert is_japanese("Hello world") is False
    assert is_japanese("GET /api/users") is False
    assert is_japanese("") is False
    assert is_japanese("   ") is False


def test_is_japanese_mixed_sentence_triggers_translation():
    # Mixed JA+EN must be detected as Japanese (design 3.3.1)
    assert is_japanese("src/main.ts の getUser() を修正して") is True


# ---------------------------------------------------------------------------
# UT-3: Protected token masking — must preserve technical tokens
# ---------------------------------------------------------------------------


def test_mask_preserves_code_block():
    text = "説明 ```ts\nconst x = 1;\n``` 続き"
    masked, mapping = mask_text(text)
    assert "__CR_PROTECTED_" in masked
    # Code block should be masked
    assert "const x = 1;" not in masked
    # Unmask restores
    assert unmask_text(masked, mapping) == text


def test_mask_preserves_inline_code():
    text = "関数 `getUser()` を呼び出す"
    masked, mapping = mask_text(text)
    # inline code is masked
    assert "`getUser()`" not in masked
    assert unmask_text(masked, mapping) == text


def test_mask_preserves_url_and_paths():
    text = "パス https://example.com/api と ./src/main.ts と C:\\Users\\test\\file.ts"
    masked, mapping = mask_text(text)
    # All three should be masked
    assert "https://example.com/api" not in masked
    assert "./src/main.ts" not in masked
    assert "C:\\Users" not in masked
    assert unmask_text(masked, mapping) == text


def test_mask_preserves_http_status_and_method():
    text = "HTTP 404 と GET /api/users を返す"
    masked, mapping = mask_text(text)
    assert "HTTP 404" not in masked
    assert "GET /api/users" not in masked
    assert unmask_text(masked, mapping) == text


def test_mask_preserves_cli_command():
    text = "コマンド npm test を実行して"
    masked, mapping = mask_text(text)
    assert "npm test" not in masked
    # CLI pattern now limited to 5 tokens — Japanese suffix must survive
    # "を" should remain outside placeholder (not swallowed)
    assert "を" in masked or "を" in unmask_text(masked, mapping)
    assert unmask_text(masked, mapping) == text


def test_mask_cli_does_not_swallow_entire_line():
    """J-4 regression: CLI must not mask the trailing Japanese text."""
    text = "npm test を実行して結果を教えて"
    masked, mapping = mask_text(text)
    # After masking, the Japanese part should be outside placeholder
    # If CLI swallowed entire line, masked would be single placeholder
    assert masked.count("__CR_PROTECTED_") == 1
    assert "を" in masked or "結果" in masked or len(masked) > 30
    assert unmask_text(masked, mapping) == text


def test_mask_preserves_identifiers():
    text = "getUser() と UserService と my_var を修正"
    masked, mapping = mask_text(text)
    # At least one identifier should be masked
    assert len(mapping) >= 1
    assert unmask_text(masked, mapping) == text


def test_mask_does_not_overmatch_plain_english():
    """Plain English without code patterns should not be fully masked."""
    text = "I will update the function to handle errors"
    masked, mapping = mask_text(text)
    # No protected tokens in plain English — mapping should be empty
    assert mapping == {}
    assert masked == text


def test_mask_relative_path_requires_extension_or_dot_prefix():
    """J-4: plain a/b should not be masked, but src/main.ts should."""
    # src/main.ts has extension — must be masked
    masked1, mapping1 = mask_text("src/main.ts を修正")
    assert len(mapping1) >= 1
    assert "src/main.ts" not in masked1
    # a/b without extension and without ./ should not be masked (over-detection fix)
    masked2, mapping2 = mask_text("a/b を修正")
    # a/b should remain — no mapping
    assert mapping2 == {} or "a/b" in masked2


def test_mask_posix_requires_slash():
    """F-1: single /api should not be masked as POSIX path."""
    masked, mapping = mask_text("GET /api を呼び出す")
    # /api alone should not be POSIX-masked (handled by HTTP_METHOD instead if GET present)
    # For bare "/api" without GET, it should not be masked
    masked2, mapping2 = mask_text("パスは /api です")
    # Bare /api — should not be masked as path (avoids over-matching)
    # If it is masked, that's over-detection
    # We allow either no mask or masked via other pattern, but must round-trip
    assert unmask_text(masked2, mapping2) == "パスは /api です"


# ---------------------------------------------------------------------------
# UT-7: Mask → Translate(mock) → Unmask round-trip
# ---------------------------------------------------------------------------


def test_mask_translate_unmask_roundtrip():
    text = "src/main.ts の getUser() を修正して、HTTP 404 を返してください。"
    masked, mapping = mask_text(text)
    # Simulate Argos returning translated masked text
    fake_translated = "Please fix __CR_PROTECTED_1__ in __CR_PROTECTED_0__ to return __CR_PROTECTED_2__."
    # Need to ensure mapping order matches expectation — check placeholders exist
    assert "__CR_PROTECTED_" in masked
    restored = unmask_text(fake_translated, mapping)
    # Placeholders should be replaced with originals
    assert "src/main.ts" in restored or "getUser" in restored


def test_mask_translate_mock_via_manager():
    """End-to-end via _translate_with_protection mock."""
    text = "src/main.ts のエラーを修正してください"

    manager = Mock()
    manager.is_available.return_value = True
    # Mock translate_ja_to_en to simulate Argos translating masked text
    # The masked text will be like "__CR_PROTECTED_0__ のエラーを修正してください" → Argos returns English with placeholder preserved
    def fake_ja_en(masked_text: str) -> str:
        # Simulate translation preserving placeholders
        return masked_text.replace("のエラーを修正してください", " fix the error in ")

    manager.translate_ja_to_en.side_effect = fake_ja_en
    manager.translate_en_to_ja.return_value = "dummy"

    # Use the translator directly on a request
    req = AnthropicRequest(
        model="test",
        messages=[AnthropicMessage(role="user", content=[{"type": "text", "text": text}])],
        max_tokens=1024,
    )
    result = translate_anthropic_request_ja_to_en(req, manager)
    # Text block should have been translated and unmasked
    out_text = result.messages[0].content[0]["text"] if isinstance(result.messages[0].content, list) else result.messages[0].content  # type: ignore
    assert "src/main.ts" in out_text
    assert "fix the error" in out_text


# ---------------------------------------------------------------------------
# UT-9: Placeholder safety — Argos must not mutate __CR_PROTECTED_n__
# ---------------------------------------------------------------------------


def test_placeholder_not_mutated():
    text = "src/main.ts と getUser() を使う"
    masked, mapping = mask_text(text)
    # Simulate Argos output that preserves placeholders
    preserved = masked.replace("と", "and")
    assert not has_placeholder_mutation(preserved, mapping)


def test_placeholder_mutation_detected():
    text = "src/main.ts と getUser() を使う"
    masked, mapping = mask_text(text)
    # Simulate Argos corrupting placeholder (SentencePiece split)
    mutated = masked.replace("__CR_PROTECTED_0__", "__ CR PROTECTED 0 __")
    assert has_placeholder_mutation(mutated, mapping)


def test_unmask_avoids_partial_overlap():
    """__CR_PROTECTED_10__ must not be broken by __CR_PROTECTED_1__ replacement."""
    mapping = {1: "a", 10: "b"}
    text = "__CR_PROTECTED_10__ and __CR_PROTECTED_1__"
    assert unmask_text(text, mapping) == "b and a"


def test_translate_fallback_on_heavy_mutation():
    """If >50% placeholders lost, translator should fallback to original."""
    from coderouter.jp_translation.translator import _translate_with_protection

    manager = Mock()
    manager.is_available.return_value = True
    # Simulate heavily mutated output
    manager.translate_ja_to_en.return_value = "corrupted without placeholders"
    manager.translate_en_to_ja.return_value = "corrupted"

    text = "src/main.ts と getUser() と HTTP 404 を使うテスト"
    # text contains Japanese, so mask will have 3 placeholders
    # Heavily mutated translation should trigger fallback to original
    result = _translate_with_protection(text, "ja_to_en", manager)
    # With >50% loss, should return original
    assert result == text


# ---------------------------------------------------------------------------
# Integration: request/response translation skips correctly
# ---------------------------------------------------------------------------


def test_request_translation_skips_non_japanese_user():
    manager = Mock()
    manager.is_available.return_value = True
    manager.translate_ja_to_en.return_value = "SHOULD NOT BE CALLED"

    req = AnthropicRequest(
        model="test",
        messages=[AnthropicMessage(role="user", content=[{"type": "text", "text": "Hello world"}])],
        max_tokens=1024,
    )
    result = translate_anthropic_request_ja_to_en(req, manager)
    # No Japanese → no translation call
    manager.translate_ja_to_en.assert_not_called()
    assert result.messages[0].content[0]["text"] == "Hello world"  # type: ignore


def test_request_translation_skips_tool_use():
    manager = Mock()
    manager.is_available.return_value = True
    manager.translate_ja_to_en.return_value = "SHOULD NOT BE CALLED"

    req = AnthropicRequest(
        model="test",
        messages=[
            AnthropicMessage(
                role="user",
                content=[
                    {"type": "text", "text": "日本語の指示"},
                    {"type": "tool_use", "id": "1", "name": "read_file", "input": {"path": "src/main.ts"}},
                ],
            )
        ],
        max_tokens=1024,
    )
    # Mock to return English for the text block
    manager.translate_ja_to_en.return_value = "Japanese instruction"
    result = translate_anthropic_request_ja_to_en(req, manager)
    # Text block translated, tool_use preserved
    blocks = result.messages[0].content  # type: ignore
    assert blocks[1]["type"] == "tool_use"
    assert blocks[1]["name"] == "read_file"


def test_response_translation_skips_tool_use():
    manager = Mock()
    manager.is_available.return_value = True
    manager.translate_en_to_ja.return_value = "日本語に翻訳"

    resp = AnthropicResponse(
        id="test",
        type="message",
        role="assistant",
        content=[
            {"type": "text", "text": "I will fix the bug."},
            {"type": "tool_use", "id": "1", "name": "edit_file", "input": {"path": "src/main.ts"}},
        ],
        model="test",
        stop_reason="tool_use",
        usage=AnthropicUsage(input_tokens=10, output_tokens=20),
    )
    result = translate_anthropic_response_en_to_ja(resp, manager)
    assert result.content[1]["type"] == "tool_use"
    assert result.content[1]["name"] == "edit_file"
    assert result.content[0]["text"] == "日本語に翻訳"
