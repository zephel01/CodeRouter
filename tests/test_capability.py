"""Unit tests for the v0.5-A / v0.5-B capability gate.

Pure-function tests — no engine, no HTTP. The fallback-integration tests
for v0.5-A live in tests/test_fallback_thinking.py; v0.5-B integration
tests live in tests/test_fallback_cache_control.py.
"""

from __future__ import annotations

import pytest

from coderouter.config.schemas import Capabilities, ProviderConfig
from coderouter.routing.capability import (
    anthropic_request_has_cache_control,
    anthropic_request_requires_thinking,
    provider_supports_cache_control,
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


# ======================================================================
# v0.5-B: cache_control
# ======================================================================


# ----------------------------------------------------------------------
# provider_supports_cache_control
# ----------------------------------------------------------------------


def _cache_provider(
    *,
    kind: str = "anthropic",
    prompt_cache: bool = False,
) -> ProviderConfig:
    """Build a ProviderConfig for cache_control tests. Kept separate from
    the thinking helper because the relevant capability is `prompt_cache`
    (the YAML-level escape hatch), not `thinking`."""
    return ProviderConfig(
        name="t",
        kind=kind,  # type: ignore[arg-type]
        base_url=(
            "https://api.anthropic.com"
            if kind == "anthropic"
            else "https://openrouter.ai/api/v1"
        ),
        model="whatever",
        capabilities=Capabilities(prompt_cache=prompt_cache),
    )


def test_cache_control_supported_on_anthropic_kind_by_default() -> None:
    """Native Anthropic passthrough preserves cache_control end-to-end."""
    assert provider_supports_cache_control(_cache_provider(kind="anthropic"))


def test_cache_control_unsupported_on_openai_compat_kind_by_default() -> None:
    """OpenAI-shape translation drops cache_control — no wire equivalent."""
    assert not provider_supports_cache_control(
        _cache_provider(kind="openai_compat")
    )


def test_cache_control_explicit_prompt_cache_flag_promotes_openai_compat() -> None:
    """YAML escape hatch: `capabilities.prompt_cache: true` on an
    openai_compat provider tells the router the upstream extends the
    OpenAI wire to preserve the marker. Honor it verbatim."""
    assert provider_supports_cache_control(
        _cache_provider(kind="openai_compat", prompt_cache=True)
    )


def test_cache_control_explicit_prompt_cache_flag_redundant_on_anthropic() -> None:
    """anthropic kind is always supported; setting prompt_cache: true
    changes nothing but must not break."""
    assert provider_supports_cache_control(
        _cache_provider(kind="anthropic", prompt_cache=True)
    )


# ----------------------------------------------------------------------
# anthropic_request_has_cache_control
# ----------------------------------------------------------------------


def _req_with(**extra: object) -> AnthropicRequest:
    """Build a minimal AnthropicRequest with an override hook for the
    fields we're exercising (system / tools / messages)."""
    payload: dict[str, object] = {
        "max_tokens": 64,
        "messages": [{"role": "user", "content": "hi"}],
    }
    payload.update(extra)
    return AnthropicRequest.model_validate(payload)


def test_has_cache_control_false_for_plain_request() -> None:
    assert not anthropic_request_has_cache_control(_req_with())


def test_has_cache_control_false_for_bare_string_system() -> None:
    """Shorthand `system: str` cannot carry cache_control markers."""
    req = _req_with(system="you are helpful")
    assert not anthropic_request_has_cache_control(req)


def test_has_cache_control_true_on_system_block() -> None:
    """Typical cache_control placement: a system block flagged ephemeral."""
    req = _req_with(
        system=[
            {
                "type": "text",
                "text": "big reusable prompt",
                "cache_control": {"type": "ephemeral"},
            }
        ]
    )
    assert anthropic_request_has_cache_control(req)


def test_has_cache_control_false_on_system_block_without_marker() -> None:
    req = _req_with(
        system=[{"type": "text", "text": "no marker here"}]
    )
    assert not anthropic_request_has_cache_control(req)


def test_has_cache_control_true_on_tool_definition() -> None:
    """cache_control on a tool definition — caches the schema block."""
    req = _req_with(
        tools=[
            {
                "name": "search",
                "description": "search the web",
                "input_schema": {"type": "object", "properties": {}},
                "cache_control": {"type": "ephemeral"},
            }
        ]
    )
    assert anthropic_request_has_cache_control(req)


def test_has_cache_control_true_on_message_content_block() -> None:
    """cache_control on a user content block — caches big pasted context."""
    req = _req_with(
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "long pasted doc",
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
            }
        ]
    )
    assert anthropic_request_has_cache_control(req)


def test_has_cache_control_false_when_content_is_string() -> None:
    """String-form content cannot carry cache_control — shorthand only."""
    req = _req_with(
        messages=[{"role": "user", "content": "no list here"}]
    )
    assert not anthropic_request_has_cache_control(req)


def test_has_cache_control_detects_marker_in_second_message() -> None:
    """A single cache_control anywhere in the request is enough; don't
    short-circuit on the first message."""
    req = _req_with(
        messages=[
            {"role": "user", "content": "first"},
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "plain"},
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "this one cached",
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
            },
        ]
    )
    assert anthropic_request_has_cache_control(req)


def test_has_cache_control_survives_mixed_block_types() -> None:
    """Markers can live on image or tool_result blocks too — the walk
    inspects every dict block regardless of its `type`."""
    req = _req_with(
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "look at this"},
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": "iVBOR...",
                        },
                        "cache_control": {"type": "ephemeral"},
                    },
                ],
            }
        ]
    )
    assert anthropic_request_has_cache_control(req)
