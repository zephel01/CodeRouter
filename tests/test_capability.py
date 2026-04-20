"""Unit tests for the v0.5-A capability gate (coderouter/routing/capability.py).

Pure-function tests — no engine, no HTTP. The fallback-integration tests
for the same feature live in tests/test_fallback_thinking.py.
"""

from __future__ import annotations

import pytest

from coderouter.config.schemas import Capabilities, ProviderConfig
from coderouter.routing.capability import (
    anthropic_request_requires_thinking,
    provider_supports_thinking,
    strip_thinking,
)
from coderouter.translation.anthropic import (
    AnthropicMessage,
    AnthropicRequest,
)

# ----------------------------------------------------------------------
# provider_supports_thinking
# ----------------------------------------------------------------------


def _provider(
    *,
    kind: str = "anthropic",
    model: str = "claude-sonnet-4-6",
    thinking: bool = False,
) -> ProviderConfig:
    return ProviderConfig(
        name="t",
        kind=kind,  # type: ignore[arg-type]
        base_url=(
            "https://api.anthropic.com"
            if kind == "anthropic"
            else "https://openrouter.ai/api/v1"
        ),
        model=model,
        capabilities=Capabilities(thinking=thinking),
    )


@pytest.mark.parametrize(
    "model",
    [
        "claude-sonnet-4-6",
        "claude-sonnet-4-6-20260101",  # future dated suffix
        "claude-sonnet-4-7",            # forward-compat
        "claude-opus-4-1",
        "claude-opus-4-6-20260201",
        "claude-haiku-4-5",
        "claude-haiku-4-6",
    ],
)
def test_heuristic_accepts_known_capable_families(model: str) -> None:
    """Anthropic kind + capable model family → True without explicit flag."""
    assert provider_supports_thinking(_provider(kind="anthropic", model=model))


@pytest.mark.parametrize(
    "model",
    [
        "claude-sonnet-4-5-20250929",     # the v0.4-D footnote model
        "claude-sonnet-4-5",
        "claude-sonnet-3-7",
        "claude-opus-3-5-20241022",
        "claude-haiku-3-5",
        "gpt-4o",                          # unrelated model slug
        "",                                # blank
    ],
)
def test_heuristic_rejects_known_incapable_families(model: str) -> None:
    """Anthropic kind but model not in the capable list → False."""
    assert not provider_supports_thinking(_provider(kind="anthropic", model=model))


def test_heuristic_rejects_openai_compat_even_with_capable_model_name() -> None:
    """openai_compat providers don't accept `thinking` regardless of the
    model slug — OpenRouter's Claude alias goes through OpenAI-shape
    translation and has no wire equivalent."""
    p = _provider(
        kind="openai_compat",
        model="anthropic/claude-sonnet-4-6",  # OpenRouter-style slug
    )
    assert not provider_supports_thinking(p)


def test_explicit_yaml_true_wins_over_heuristic() -> None:
    """User sets thinking: true on an unexpected model → honored. This is
    the escape hatch for new Anthropic families before we update the
    heuristic table."""
    p = _provider(
        kind="anthropic",
        model="claude-sonnet-5-0",  # hypothetical future, not in pattern
        thinking=True,
    )
    assert provider_supports_thinking(p)


def test_explicit_yaml_true_wins_on_openai_compat_too() -> None:
    """Explicit opt-in is honored even on openai_compat — some future
    upstream might wrap thinking into its own OpenAI-shape extension."""
    p = _provider(
        kind="openai_compat",
        model="some-future-model",
        thinking=True,
    )
    assert provider_supports_thinking(p)


# ----------------------------------------------------------------------
# anthropic_request_requires_thinking
# ----------------------------------------------------------------------


def _request(**extra: object) -> AnthropicRequest:
    """Build an AnthropicRequest with arbitrary extras (extra='allow')."""
    return AnthropicRequest.model_validate(
        {
            "max_tokens": 64,
            "messages": [{"role": "user", "content": "hi"}],
            **extra,
        }
    )


def test_requires_thinking_true_for_enabled_block() -> None:
    req = _request(thinking={"type": "enabled"})
    assert anthropic_request_requires_thinking(req)


def test_requires_thinking_true_for_enabled_with_budget() -> None:
    req = _request(thinking={"type": "enabled", "budget_tokens": 4096})
    assert anthropic_request_requires_thinking(req)


def test_requires_thinking_false_for_disabled_block() -> None:
    req = _request(thinking={"type": "disabled"})
    assert not anthropic_request_requires_thinking(req)


def test_requires_thinking_false_for_missing_field() -> None:
    req = _request()
    assert not anthropic_request_requires_thinking(req)


def test_requires_thinking_false_for_non_dict_value() -> None:
    """Defensive: if a client sends `thinking: true` or similar, don't
    crash. Only {type: enabled} is a real trigger."""
    for value in [True, "enabled", 1, []]:
        req = _request(thinking=value)
        assert not anthropic_request_requires_thinking(req)


# ----------------------------------------------------------------------
# strip_thinking
# ----------------------------------------------------------------------


def test_strip_thinking_removes_field() -> None:
    req = _request(thinking={"type": "enabled", "budget_tokens": 4096})
    stripped = strip_thinking(req)

    assert "thinking" not in (stripped.model_extra or {})
    # Original untouched (mutation-free contract).
    assert "thinking" in (req.model_extra or {})


def test_strip_thinking_preserves_other_data() -> None:
    req = _request(
        thinking={"type": "enabled"},
        temperature=0.5,
    )
    req.profile = "claude-code-direct"
    req.anthropic_beta = "context-management-2025-06-27"

    stripped = strip_thinking(req)

    assert stripped.temperature == 0.5
    assert stripped.max_tokens == 64
    assert stripped.messages[0].content == "hi"
    # exclude=True fields survive the roundtrip.
    assert stripped.profile == "claude-code-direct"
    assert stripped.anthropic_beta == "context-management-2025-06-27"


def test_strip_thinking_is_noop_when_absent() -> None:
    """No thinking field → return an equivalent copy (mutation-free
    contract is maintained but nothing to strip)."""
    req = _request(temperature=0.7)
    stripped = strip_thinking(req)

    assert stripped is not req  # fresh instance
    assert stripped.temperature == 0.7
    assert "thinking" not in (stripped.model_extra or {})


def test_strip_thinking_does_not_affect_other_extras() -> None:
    """`cache_control` and similar Anthropic-specific extras outside of
    `thinking` must survive. v0.5-A only gates thinking; cache_control
    handling is v0.5-B territory."""
    req = _request(
        thinking={"type": "enabled"},
        # Simulated extra — cache_control lives on content blocks in real
        # requests, but any top-level key covers the preservation test.
        custom_extra={"keep": "me"},
    )
    stripped = strip_thinking(req)

    assert "thinking" not in (stripped.model_extra or {})
    assert stripped.model_extra is not None
    assert stripped.model_extra.get("custom_extra") == {"keep": "me"}


def test_strip_thinking_wire_body_is_clean() -> None:
    """Final check: after strip, model_dump() (what the adapter sends on
    the wire) does NOT contain `thinking`."""
    req = _request(thinking={"type": "enabled", "budget_tokens": 4096})
    stripped = strip_thinking(req)

    body = stripped.model_dump(exclude_none=True)
    assert "thinking" not in body
