"""jp_translation package — Japanese ↔ English translation layer.

Public API:
- TranslatorManager
- translate_anthropic_request_ja_to_en
- translate_anthropic_response_en_to_ja
- masking utilities (is_japanese, mask_text, unmask_text)

Design: doc/翻訳層設計書.md
This package is separate from coderouter.translation (Anthropic⇔OpenAI wire).
"""
from .manager import TranslatorManager
from .masking import is_japanese, mask_text, unmask_text
from .translator import (
    translate_anthropic_request_ja_to_en,
    translate_anthropic_response_en_to_ja,
)

__all__ = [
    "TranslatorManager",
    "is_japanese",
    "mask_text",
    "translate_anthropic_request_ja_to_en",
    "translate_anthropic_response_en_to_ja",
    "unmask_text",
]
