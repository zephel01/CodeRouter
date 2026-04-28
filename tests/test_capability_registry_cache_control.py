"""Unit tests for v1.9-B `cache_control` registry capability.

Validates that the registry resolves the new ``cache_control`` flag
correctly under the same first-match-per-flag semantics as the existing
flags, and that ``provider_supports_cache_control`` consults it before
falling back to the kind-based heuristic.
"""

from __future__ import annotations

from coderouter.config.capability_registry import (
    CapabilityRegistry,
    CapabilityRule,
    RegistryCapabilities,
)
from coderouter.config.schemas import Capabilities, ProviderConfig
from coderouter.routing.capability import provider_supports_cache_control


def _anthropic_provider(model: str = "claude-opus-4-8") -> ProviderConfig:
    return ProviderConfig(
        name="anth",
        kind="anthropic",
        base_url="https://api.anthropic.com",
        model=model,
        api_key_env="ANTHROPIC_API_KEY",
    )


def _openai_provider(
    model: str = "qwen-coder", *, prompt_cache: bool = False
) -> ProviderConfig:
    return ProviderConfig(
        name="oa",
        kind="openai_compat",
        base_url="http://localhost:11434/v1",
        model=model,
        capabilities=Capabilities(prompt_cache=prompt_cache),
    )


def _registry(rules: list[CapabilityRule]) -> CapabilityRegistry:
    return CapabilityRegistry.from_rule_lists(user=[], bundled=rules)


# ---------------------------------------------------------------------------
# Registry resolution
# ---------------------------------------------------------------------------


def test_registry_lookup_returns_cache_control_true_when_declared() -> None:
    """A bundled rule that declares ``cache_control: true`` resolves to True."""
    reg = _registry(
        [
            CapabilityRule(
                match="claude-opus-4-*",
                kind="anthropic",
                capabilities=RegistryCapabilities(cache_control=True),
            )
        ]
    )
    resolved = reg.lookup(kind="anthropic", model="claude-opus-4-8")
    assert resolved.cache_control is True


def test_registry_lookup_returns_cache_control_false_when_declared_false() -> None:
    """An explicit ``cache_control: false`` resolves to False (hard opt-out)."""
    reg = _registry(
        [
            CapabilityRule(
                match="qwen-broken",
                kind="anthropic",
                capabilities=RegistryCapabilities(cache_control=False),
            )
        ]
    )
    resolved = reg.lookup(kind="anthropic", model="qwen-broken")
    assert resolved.cache_control is False


def test_registry_lookup_returns_none_when_undeclared() -> None:
    """No matching rule → ``cache_control`` stays at its ``None`` default."""
    reg = _registry(
        [
            CapabilityRule(
                match="other-model",
                kind="anthropic",
                capabilities=RegistryCapabilities(cache_control=True),
            )
        ]
    )
    resolved = reg.lookup(kind="anthropic", model="claude-opus-4-8")
    assert resolved.cache_control is None


def test_registry_first_match_locks_cache_control_per_flag() -> None:
    """First-match-per-flag: an earlier specific rule wins over a broader later rule."""
    reg = _registry(
        [
            CapabilityRule(
                match="claude-opus-4-broken",
                kind="anthropic",
                capabilities=RegistryCapabilities(cache_control=False),
            ),
            CapabilityRule(
                match="claude-opus-4-*",
                kind="anthropic",
                capabilities=RegistryCapabilities(cache_control=True),
            ),
        ]
    )
    # Specific rule wins for the broken model
    assert (
        reg.lookup(kind="anthropic", model="claude-opus-4-broken").cache_control
        is False
    )
    # Broader rule wins for everyone else
    assert (
        reg.lookup(kind="anthropic", model="claude-opus-4-7").cache_control is True
    )


# ---------------------------------------------------------------------------
# provider_supports_cache_control gate
# ---------------------------------------------------------------------------


def test_gate_explicit_prompt_cache_wins() -> None:
    """``providers.yaml capabilities.prompt_cache: true`` overrides everything."""
    reg = _registry([])
    provider = _openai_provider(prompt_cache=True)
    assert provider_supports_cache_control(provider, registry=reg) is True


def test_gate_registry_true_overrides_kind_default() -> None:
    """Registry ``cache_control: true`` lets an openai_compat provider through.

    This is the v1.9-B opt-in path for any future ``openai_compat`` upstream
    that extends its wire format to preserve cache markers (LM Studio's
    Anthropic-compat path is the first real example, but we use a hypothetical
    here to keep the test isolated from kind-anthropic logic).
    """
    reg = _registry(
        [
            CapabilityRule(
                match="qwen3.6-*",
                kind="openai_compat",
                capabilities=RegistryCapabilities(cache_control=True),
            )
        ]
    )
    provider = _openai_provider(model="qwen3.6-35b-a3b")
    assert provider_supports_cache_control(provider, registry=reg) is True


def test_gate_registry_false_overrides_kind_default() -> None:
    """Registry ``cache_control: false`` hard-disables an anthropic-kind provider.

    Useful when an upstream regresses and the operator wants the
    capability-degraded log to fire while they investigate.
    """
    reg = _registry(
        [
            CapabilityRule(
                match="claude-opus-4-broken",
                kind="anthropic",
                capabilities=RegistryCapabilities(cache_control=False),
            )
        ]
    )
    provider = _anthropic_provider(model="claude-opus-4-broken")
    assert provider_supports_cache_control(provider, registry=reg) is False


def test_gate_undeclared_anthropic_still_true_via_kind_fallback() -> None:
    """Pre-v1.9-B behavior preserved: undeclared anthropic-kind → True.

    The kind-based fallback is what existing v0.5-B installations rely
    on; v1.9-B only ADDS positive/negative declarations as a registry
    layer above this fallback.
    """
    reg = _registry([])
    provider = _anthropic_provider()
    assert provider_supports_cache_control(provider, registry=reg) is True


def test_gate_undeclared_openai_compat_still_false_via_kind_fallback() -> None:
    """Pre-v1.9-B behavior preserved: undeclared openai_compat → False."""
    reg = _registry([])
    provider = _openai_provider()
    assert provider_supports_cache_control(provider, registry=reg) is False


# ---------------------------------------------------------------------------
# Bundled YAML wiring (smoke test against the shipped file)
# ---------------------------------------------------------------------------


def test_bundled_yaml_declares_cache_control_for_claude_families() -> None:
    """The shipped registry must declare ``cache_control: true`` for the
    Claude 4 family (sonnet / opus / haiku)."""
    reg = CapabilityRegistry.load_default()
    for model in (
        "claude-opus-4-8",
        "claude-sonnet-4-7",
        "claude-haiku-4-1",
    ):
        resolved = reg.lookup(kind="anthropic", model=model)
        assert resolved.cache_control is True, (
            f"expected cache_control=true for {model} in bundled registry"
        )


def test_bundled_yaml_declares_cache_control_for_qwen3_families() -> None:
    """LM Studio /v1/messages serves Qwen3.5/3.6 with Anthropic-shaped
    cache fields — the bundled registry must declare cache_control=true."""
    reg = CapabilityRegistry.load_default()
    for model in ("qwen3.5-9b", "qwen3.6-35b-a3b"):
        resolved = reg.lookup(kind="anthropic", model=model)
        assert resolved.cache_control is True, (
            f"expected cache_control=true for {model} in bundled registry"
        )


def test_bundled_yaml_leaves_openai_compat_models_undeclared() -> None:
    """openai_compat upstreams have no wire equivalent for cache_control;
    the bundled registry must NOT declare it for them (so the
    capability-degraded gate fires the translation-lossy log line)."""
    reg = CapabilityRegistry.load_default()
    # qwen2.5-coder is a typical openai_compat target with no cache support.
    resolved = reg.lookup(kind="openai_compat", model="qwen2.5-coder:7b")
    assert resolved.cache_control is None, (
        "openai_compat default leaks cache_control declaration — "
        "the gate would falsely treat the marker as preserved"
    )
