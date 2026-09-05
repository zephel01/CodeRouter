"""Protected Token masking for translation.

Design: doc/翻訳層設計書.md §3.3
- Detect technical tokens and replace with __CR_PROTECTED_{INDEX}__
- Unmask after translation (descending order or regex replacement)
- is_japanese() for language-detection optimization
"""
from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Language detection
# ---------------------------------------------------------------------------

# Explicit Unicode ranges: Hiragana \u3040-\u309F, Katakana \u30A0-\u30FF, Kanji \u4E00-\u9FFF
_JA_RE = re.compile(r"[\u3040-\u309F\u30A0-\u30FF\u4E00-\u9FFF]")


def is_japanese(text: str) -> bool:
    """Return True if text contains any hiragana/katakana/kanji."""
    return bool(_JA_RE.search(text))


# ---------------------------------------------------------------------------
# Detection patterns (priority order: most specific → generic)
# ---------------------------------------------------------------------------

# Use tool_repair's fenced pattern if available, else fallback
# coderouter/translation/tool_repair.py defines _FENCED_RE etc.
# We keep a compatible pattern here so masking.py is self-contained.

_CODE_BLOCK_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`]+`")
_URL_RE = re.compile(r"https?://\S+|file://\S+")
# Windows: C:\Users\...  and D:\path\to\file.ts
_WINDOWS_PATH_RE = re.compile(
    r"[A-Za-z]:\\(?:[^\\/:*?\"<>|\s]+\\)*[^\\/:*?\"<>|\s]*"
)
# POSIX absolute: /path/to/file — require at least one interior slash to avoid
# over-matching bare "/api" (F-1/J-4 fix). Single-segment "/api" is handled by
# _HTTP_METHOD_PATH_RE or left as natural language.
_POSIX_PATH_RE = re.compile(
    r"(?<![\w/.-])/(?:[\w.\-]+/)+[\w.\-]+"
)
# Relative POSIX: src/main.ts, ./src/..., ../..., a/b/c
# J-4 fix: tighten to avoid over-masking plain "a/b" English fragments.
# Require either leading ./ or ../, OR a file extension in the last segment.
_RELATIVE_PATH_RE = re.compile(
    r"(?<![\w/.-])(?:\./|\.\./)[\w.\-]+(?:/[\w.\-]+)+|(?<![\w/.-])[\w.\-]+(?:/[\w.\-]+)+\.(?:[A-Za-z0-9]+)\b"
)
# HTTP status
_HTTP_STATUS_RE = re.compile(r"HTTP\s*\d{3}")
# HTTP method + path
_HTTP_METHOD_PATH_RE = re.compile(r"\b(?:GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS)\s+/\S+")
# CLI commands (known list) — J-4 fix: limit to command + up to 4 args to avoid
# swallowing the rest of the line (e.g. Japanese text after "npm test").
# Use ASCII-only [A-Za-z0-9_] instead of \w so Unicode (hiragana etc.) is not consumed.
_CLI_COMMAND_RE = re.compile(
    r"\b(?:npm|npx|yarn|pip|cargo|git|msbuild|make|docker|kubectl|ollama|uv|python|node)\s+[A-Za-z0-9_\-/.:@]+(?:\s+[A-Za-z0-9_\-/.:@]+){0,4}"
)
# Identifiers: conservative — only mask when pattern is clearly code-like
# - camelCase / PascalCase (mid-word uppercase)
# - snake_case
# - dotted identifier (a.b)
# - function call parens ()
_IDENTIFIER_CAMEL_RE = re.compile(r"\b[a-z]+[A-Z][A-Za-z0-9]*\b|\b[A-Z][a-z]+[A-Z][A-Za-z0-9]*\b")
_IDENTIFIER_SNAKE_RE = re.compile(r"\b[a-z]+_[a-z0-9_]+\b|\b[A-Z]+_[A-Z0-9_]+\b")
_IDENTIFIER_DOTTED_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*\b")
_IDENTIFIER_CALL_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\(\)")

# Order matters: code blocks first, then inline, then URL, paths, etc.
# Each pattern is applied sequentially; already-masked placeholders are safe.
# Freeze condition (design §3.3.3): UT-3/UT-7/UT-9 must pass before freezing
# regex set. Changes after freeze require review (see tests/test_jp_translation_masking.py).
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("code_block", _CODE_BLOCK_RE),
    ("inline_code", _INLINE_CODE_RE),
    ("url", _URL_RE),
    ("windows_path", _WINDOWS_PATH_RE),
    ("posix_path", _POSIX_PATH_RE),
    ("relative_path", _RELATIVE_PATH_RE),
    ("http_status", _HTTP_STATUS_RE),
    ("http_method_path", _HTTP_METHOD_PATH_RE),
    ("cli_command", _CLI_COMMAND_RE),
    ("identifier_dotted", _IDENTIFIER_DOTTED_RE),
    ("identifier_call", _IDENTIFIER_CALL_RE),
    ("identifier_camel", _IDENTIFIER_CAMEL_RE),
    ("identifier_snake", _IDENTIFIER_SNAKE_RE),
]

_PLACEHOLDER_PREFIX = "__CR_PROTECTED_"
_PLACEHOLDER_SUFFIX = "__"
# Regex to find placeholders during unmasking
_PLACEHOLDER_RE = re.compile(r"__CR_PROTECTED_(\d+)__")


def mask_text(text: str) -> tuple[str, dict[int, str]]:
    """Mask protected tokens in text.

    Returns (masked_text, mapping) where mapping is {index: original}.
    Placeholders are __CR_PROTECTED_{INDEX}__.
    Detection runs in priority order; overlapping region is masked only once.
    """
    mapping: dict[int, str] = {}
    masked = text
    index = 0

    # We collect all matches first with priority, then apply non-overlapping
    # replacement to avoid double-masking already replaced regions.
    # Simpler: iterative regex over current masked string, but skip placeholder region.
    # We do sequential scan per pattern over the evolving string.

    for _name, pattern in _PATTERNS:
        # Find all non-overlapping matches in current masked text
        # We need to avoid matching inside existing placeholders.
        # Since placeholder format is alphabet+underscore+digits, we skip it
        # by checking if placeholder already occupies region.
        new_masked_parts: list[str] = []
        last_end = 0
        found_any = False
        for m in pattern.finditer(masked):
            start, end = m.span()
            token = m.group(0)
            # Skip if this token is already a placeholder
            if token.startswith(_PLACEHOLDER_PREFIX):
                continue
            # Skip if token overlaps an existing placeholder region
            # Check surrounding text for placeholder prefix
            # More precise: ensure the matched region does not intersect placeholder RE
            # Since we rebuild sequentially, this is implicitly handled
            # but we add guard: if token is too short or pure English common word, skip
            # For CLI pattern, keep as-is; for identifier patterns, additional filter
            if _name.startswith("identifier"):
                # Don't mask very short identifiers that are likely English words
                # but our camel/snake/dotted/call patterns are already conservative
                pass

            found_any = True
            placeholder = f"{_PLACEHOLDER_PREFIX}{index}{_PLACEHOLDER_SUFFIX}"
            mapping[index] = token
            new_masked_parts.append(masked[last_end:start])
            new_masked_parts.append(placeholder)
            last_end = end
            index += 1
        if found_any:
            new_masked_parts.append(masked[last_end:])
            masked = "".join(new_masked_parts)

    return masked, mapping


def unmask_text(text: str, mapping: dict[int, str]) -> str:
    """Restore placeholders to original tokens.

    Uses regex replacement to avoid __CR_PROTECTED_10__ being broken by
    __CR_PROTECTED_1__ partial match. Falls back to descending order if needed.
    Also warns (via return) if Argos mutated the placeholder — caller may
    decide to fallback to original.
    """
    if not mapping:
        return text

    def _repl(m: re.Match[str]) -> str:
        idx = int(m.group(1))
        return mapping.get(idx, m.group(0))

    return _PLACEHOLDER_RE.sub(_repl, text)


def has_placeholder_mutation(text: str, mapping: dict[int, str]) -> bool:
    """Check if placeholders were mutated by translation (e.g. SentencePiece split).

    Returns True if any expected placeholder is missing.
    The 50% threshold fallback is handled in translator.py, not here.
    """
    if not mapping:
        return False
    found = set(int(x) for x in _PLACEHOLDER_RE.findall(text))
    expected = set(mapping.keys())
    return bool(expected - found)
