"""Translation execution logic with protection.

Design: doc/翻訳層設計書.md §3.3, §7
- content block level separation (text vs tool_use/tool_result/image)
- system prompt is never translated
- is_japanese optimization
- Mask → Translate → Unmask with mutation guard + fallback
"""
from __future__ import annotations

from typing import Any

from coderouter.logging import get_logger
from coderouter.translation.anthropic import AnthropicRequest, AnthropicResponse

from .manager import TranslatorManager
from .masking import has_placeholder_mutation, is_japanese, mask_text, unmask_text

logger = get_logger(__name__)


def _translate_with_protection(
    text: str,
    direction: str,
    manager: TranslatorManager,
) -> str:
    """Mask → translate → unmask with fallback on mutation."""
    if not text or not text.strip():
        return text

    masked, mapping = mask_text(text)

    # If masked text has no Japanese (JA→EN) we already checked outside,
    # but keep for EN→JA always translate.
    try:
        if direction == "ja_to_en":
            translated_masked = manager.translate_ja_to_en(masked)
        else:
            translated_masked = manager.translate_en_to_ja(masked)
    except Exception as exc:
        logger.warning("translation-failed", extra={"direction": direction, "error": str(exc)})
        return text

    # Guard: if placeholder was mutated (SentencePiece split etc.), fallback to original
    if mapping and has_placeholder_mutation(translated_masked, mapping):
        logger.warning(
            "translation-placeholder-mutated",
            extra={"direction": direction, "expected": len(mapping)},
        )
        # Try to still unmask what survived, but if critical, return original text?
        # We attempt unmask; if result still contains placeholder prefix fragments, return original masked translation unmasked partially?
        # Safer to return original text (transparent fallback) — but we try unmask first.
        # If many placeholders lost, original is safer.
        # Heuristic: if >50% placeholders lost, fallback to original
        from .masking import _PLACEHOLDER_RE

        found = len(_PLACEHOLDER_RE.findall(translated_masked))
        # Use <= 0.5 (not <) so that losing exactly half the placeholders
        # also triggers the safe fallback (e.g. 1 lost out of 2 = 50% loss).
        if found <= len(mapping) * 0.5:
            return text

    return unmask_text(translated_masked, mapping)


def translate_anthropic_request_ja_to_en(
    req: AnthropicRequest,
    manager: TranslatorManager,
) -> AnthropicRequest:
    """Translate user text blocks JA→EN. System/tool_use/tool_result are skipped.

    This is a synchronous function; caller must asyncio.to_thread if in async context.
    Returns a new AnthropicRequest (mutated copy via model_copy).
    """
    if not manager.is_available():
        return req

    # Work on a deep copy via model_copy
    # AnthropicRequest.messages is list[AnthropicMessage], content is str | list[dict]
    new_messages = []
    for msg in req.messages:
        role = msg.role
        content = msg.content
        if isinstance(content, str):
            # Short-form string content: only translate if user role and Japanese
            if role == "user" and is_japanese(content):
                new_content = _translate_with_protection(content, "ja_to_en", manager)
                new_messages.append(msg.model_copy(update={"content": new_content}))
            else:
                new_messages.append(msg)
            continue

        # content is list[dict]
        if not isinstance(content, list):
            new_messages.append(msg)
            continue

        new_blocks: list[dict[str, Any]] = []
        for block in content:
            # J-7: Anthropic content may be dict or Pydantic ContentBlock object
            if isinstance(block, dict):
                btype = block.get("type")
                btext = str(block.get("text", ""))
            else:
                btype = getattr(block, "type", None)
                btext = str(getattr(block, "text", "") or "")
            if btype == "text":
                # is_japanese optimization (design 3.3.1)
                if role == "user" and btext and is_japanese(btext):
                    new_text = _translate_with_protection(btext, "ja_to_en", manager)
                    if isinstance(block, dict):
                        new_block = dict(block)
                        new_block["text"] = new_text
                    else:
                        # Pydantic object: copy with updated text
                        try:
                            new_block = block.model_copy(update={"text": new_text})  # type: ignore[attr-defined]
                        except Exception:
                            new_block = block  # fail-open: keep original on copy error
                    new_blocks.append(new_block)  # type: ignore[arg-type]
                else:
                    new_blocks.append(block)  # type: ignore[arg-type]
            elif btype in ("tool_use", "tool_result", "image"):
                # Fully skipped (byte-perfect)
                new_blocks.append(block)  # type: ignore[arg-type]
            else:
                # Unknown block (thinking etc.) — skip translation conservatively
                logger.debug("skip unknown block", extra={"btype": str(btype)})
                new_blocks.append(block)  # type: ignore[arg-type]
        new_messages.append(msg.model_copy(update={"content": new_blocks}))

    # system field: NEVER translate (design 3.3.2 #1)
    # Even if system is list[ContentBlock] with type text, we skip.
    return req.model_copy(update={"messages": new_messages})


def translate_anthropic_response_en_to_ja(
    resp: AnthropicResponse,
    manager: TranslatorManager,
) -> AnthropicResponse:
    """Translate assistant text blocks EN→JA after Repair.

    Skips tool_use, image. Assumes Repair already structured tool_use.
    Skips blocks that are already Japanese (is_japanese guard) to avoid
    double-translation quality loss (e.g. mixed code-comment responses).
    Synchronous; caller must to_thread if needed.
    """
    if not manager.is_available():
        return resp

    new_content: list[dict[str, Any]] = []
    for block in resp.content:
        # J-7: AnthropicResponse.content may be dict or Pydantic object
        if isinstance(block, dict):
            btype = block.get("type")
            btext = str(block.get("text", ""))
        else:
            btype = getattr(block, "type", None)
            btext = str(getattr(block, "text", "") or "")
        if btype == "text":
            if btext and btext.strip():
                # is_japanese guard: skip EN→JA if text is already Japanese-heavy
                # (avoids double-translation quality loss, e.g. code comments + explanation)
                if is_japanese(btext):
                    new_content.append(block)  # type: ignore[arg-type]
                    continue
                new_text = _translate_with_protection(btext, "en_to_ja", manager)
                if isinstance(block, dict):
                    new_block = dict(block)
                    new_block["text"] = new_text
                else:
                    try:
                        new_block = block.model_copy(update={"text": new_text})  # type: ignore[attr-defined]
                    except Exception:
                        new_block = block
                new_content.append(new_block)  # type: ignore[arg-type]
            else:
                new_content.append(block)  # type: ignore[arg-type]
        elif btype in ("tool_use",):
            # Skip — tool_use structure is protected
            new_content.append(block)  # type: ignore[arg-type]
        else:
            new_content.append(block)  # type: ignore[arg-type]

    return resp.model_copy(update={"content": new_content})


# Backwards-compat aliases for design doc pseudocode names
translate_request = translate_anthropic_request_ja_to_en
translate_response = translate_anthropic_response_en_to_ja
