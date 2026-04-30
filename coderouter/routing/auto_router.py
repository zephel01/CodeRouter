"""v1.6-A: task-aware auto routing — request-body inspection → profile name.

Slots into the v0.6-D precedence chain below the mode header and above
``default_profile``::

    body.profile
      > X-CodeRouter-Profile
      > X-CodeRouter-Mode
      > auto_router  (fires only when default_profile == "auto")
      > default_profile

The classifier is **rule-based** — no ML, no external calls, no small-LLM
pre-pass. Each rule is a matcher + target profile; first match wins. If
no rule matches, ``default_rule_profile`` is used.

Design reference: ``docs/designs/v1.6-auto-router.md``.

Pydantic schemas (:class:`RuleMatcher`, :class:`AutoRouteRule`,
:class:`AutoRouterConfig`) live in ``coderouter.config.schemas`` to keep
the routing package free of circular imports with the config loader;
they are re-exported here for call-site ergonomics.

Public surface:

- :data:`BUNDLED_RULES` — the zero-config default ruleset (image →
  multi / dense-code → coding). Falls through to ``writing`` via
  :data:`BUNDLED_DEFAULT_RULE_PROFILE`.
- :data:`BUNDLED_REQUIRED_PROFILES` — the three profile names the
  bundled ruleset needs present in ``profiles[]`` (validated at load).
- :data:`RESERVED_PROFILE_NAME` — ``"auto"``. Not allowed as a
  user-defined profile name.
- :func:`classify` — the classifier entry point.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

from coderouter.config.schemas import AutoRouterConfig, AutoRouteRule, RuleMatcher

if TYPE_CHECKING:
    from coderouter.config.schemas import CodeRouterConfig

logger = logging.getLogger("coderouter.routing.auto_router")

RESERVED_PROFILE_NAME = "auto"
BUNDLED_DEFAULT_RULE_PROFILE = "writing"
BUNDLED_REQUIRED_PROFILES: tuple[str, ...] = ("multi", "coding", "writing")


# ---------------------------------------------------------------------------
# Bundled ruleset
# ---------------------------------------------------------------------------


BUNDLED_RULES: list[AutoRouteRule] = [
    AutoRouteRule(
        id="builtin:image-attachment",
        profile="multi",
        match=RuleMatcher(has_image=True),
    ),
    AutoRouteRule(
        id="builtin:code-fence-dense",
        profile="coding",
        match=RuleMatcher(code_fence_ratio_min=0.3),
    ),
]


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------


_FENCE_RE = re.compile(r"```[\s\S]*?```")


def _latest_user_message(body: dict[str, Any]) -> dict[str, Any] | None:
    """Return the most recent ``role: user`` message, or None."""
    messages = body.get("messages")
    if not isinstance(messages, list):
        return None
    for msg in reversed(messages):
        if isinstance(msg, dict) and msg.get("role") == "user":
            return msg
    return None


def _has_image(message: dict[str, Any]) -> bool:
    """True iff the message has any image content block.

    Handles both OpenAI format (``type: image_url``) and Anthropic format
    (``type: image``) plus the top-level ``input_image`` extension.
    """
    content = message.get("content")
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype in ("image_url", "image", "input_image"):
                return True
    return False


def _extract_text(message: dict[str, Any]) -> str:
    """Concatenate all text content from a message into one string.

    String content stays verbatim. List content (OpenAI / Anthropic
    multimodal format) contributes only the ``text`` of text-type blocks.
    """
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        pieces: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    pieces.append(text)
            elif "text" in block and isinstance(block["text"], str):
                pieces.append(block["text"])
        return "\n".join(pieces)
    return ""


def _code_fence_ratio(text: str) -> float:
    """Return the fraction of ``text`` that lies inside ``` ``` fences.

    0.0 if the text is empty or has no fences. Fenced regions include
    their opening and closing triple backticks so the math stays stable
    regardless of language hints (```` ```python ```` vs ```` ``` ````).
    """
    if not text:
        return 0.0
    fenced = sum(len(m.group(0)) for m in _FENCE_RE.finditer(text))
    return fenced / len(text)


def _match_rule(
    rule: AutoRouteRule,
    message: dict[str, Any] | None,
    text: str,
    model: str | None,
) -> bool:
    m = rule.match
    if m.has_image is True:
        return message is not None and _has_image(message)
    if m.code_fence_ratio_min is not None:
        return _code_fence_ratio(text) >= m.code_fence_ratio_min
    if m.content_contains is not None:
        return m.content_contains in text
    if m.content_regex is not None:
        return re.search(m.content_regex, text) is not None
    if m.model_pattern is not None:
        # [Unreleased]: per-model auto-routing (free-claude-code 由来).
        # ``re.fullmatch`` because model identifiers are structured
        # tokens — patterns describe the whole id with explicit
        # wildcards (``claude-3-5-haiku.*``) rather than substrings.
        # Pre-compiled at schema load so this path is regex-safe.
        if model is None:
            return False
        return re.fullmatch(m.model_pattern, model) is not None
    return False  # pragma: no cover — _exactly_one guards against this


def _extract_model(body: dict[str, Any]) -> str | None:
    """Pull the top-level ``model`` field if it's a non-empty string.

    Both Anthropic ``/v1/messages`` and OpenAI ``/v1/chat/completions``
    bodies carry ``model`` at the top level; the auto-router treats
    them uniformly. Bodies without a ``model`` field (rare — typically
    test harnesses) cannot match any ``model_pattern`` rule.
    """
    candidate = body.get("model")
    if isinstance(candidate, str) and candidate:
        return candidate
    return None


def classify(body: dict[str, Any], config: CodeRouterConfig) -> str:
    """Resolve an incoming request body to a profile name.

    Rule order (first match wins) is determined by:

    - ``config.auto_router.rules`` when ``auto_router`` is set and
      ``rules`` is non-empty.
    - :data:`BUNDLED_RULES` otherwise.

    Fallthrough (no rule matches, or ``disabled`` is True) goes to
    ``config.auto_router.default_rule_profile`` when configured, else
    :data:`BUNDLED_DEFAULT_RULE_PROFILE`.

    Emits one of two log events: ``auto-router-resolved`` on match, or
    ``auto-router-fallthrough`` on default-rule fall.
    """
    user_msg = _latest_user_message(body)
    text = _extract_text(user_msg) if user_msg is not None else ""
    model = _extract_model(body)

    auto_cfg = config.auto_router
    if auto_cfg is not None and auto_cfg.disabled:
        _emit_fallthrough(auto_cfg.default_rule_profile, text, model, disabled=True)
        return auto_cfg.default_rule_profile

    rules = auto_cfg.rules if (auto_cfg is not None and auto_cfg.rules) else BUNDLED_RULES
    default_profile = (
        auto_cfg.default_rule_profile
        if auto_cfg is not None
        else BUNDLED_DEFAULT_RULE_PROFILE
    )

    # ``model_pattern`` matchers can fire even without a user message
    # (e.g. an empty messages list with only a model field). Other
    # matchers still require ``user_msg`` to be present — they short
    # out via ``_match_rule``'s message-None handling.
    for rule in rules:
        if _match_rule(rule, user_msg, text, model):
            _emit_resolved(rule, user_msg, text, model)
            return rule.profile

    _emit_fallthrough(default_profile, text, model)
    return default_profile


def _emit_resolved(
    rule: AutoRouteRule,
    message: dict[str, Any] | None,
    text: str,
    model: str | None,
) -> None:
    logger.info(
        "auto-router-resolved",
        extra={
            "rule_id": rule.id,
            "resolved_profile": rule.profile,
            "signals": {
                "has_image": message is not None and _has_image(message),
                "code_fence_ratio": round(_code_fence_ratio(text), 3),
                "content_len": len(text),
                "model": model,
            },
        },
    )


def _emit_fallthrough(
    profile: str, text: str, model: str | None, disabled: bool = False
) -> None:
    logger.info(
        "auto-router-fallthrough",
        extra={
            "resolved_profile": profile,
            "signals": {
                "code_fence_ratio": round(_code_fence_ratio(text), 3),
                "content_len": len(text),
                "model": model,
                "disabled": disabled,
            },
        },
    )


__all__ = [
    "BUNDLED_DEFAULT_RULE_PROFILE",
    "BUNDLED_REQUIRED_PROFILES",
    "BUNDLED_RULES",
    "RESERVED_PROFILE_NAME",
    "AutoRouteRule",
    "AutoRouterConfig",
    "RuleMatcher",
    "classify",
]
