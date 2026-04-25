"""Unit tests for v0.7-A declarative model-capabilities.yaml registry.

Focus: the registry module itself (schema, glob matching, layered
lookup, first-match-per-flag semantics) plus the thin integration with
``provider_supports_thinking`` that swaps the old regex heuristic for a
registry lookup.

The pre-v0.7-A regression surface is covered by tests/test_capability.py
(those tests exercise the same gate function without knowing the
registry exists). This file adds the new behavior — bundled YAML shape,
user-file layering, broader flag set (tools / max_context_tokens / etc.).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from coderouter.config.capability_registry import (
    CapabilityRegistry,
    CapabilityRegistryFile,
    CapabilityRule,
    RegistryCapabilities,
    ResolvedCapabilities,
)
from coderouter.config.schemas import Capabilities, ProviderConfig
from coderouter.routing import capability as capability_module
from coderouter.routing.capability import (
    get_default_registry,
    provider_supports_thinking,
    reset_default_registry,
)

# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _write_yaml(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


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
            "https://api.anthropic.com" if kind == "anthropic" else "https://openrouter.ai/api/v1"
        ),
        model=model,
        capabilities=Capabilities(thinking=thinking),
    )


# ======================================================================
# Schema validation
# ======================================================================


def test_registry_file_default_version_and_empty_rules_ok() -> None:
    """An empty YAML is a valid registry file — no rules, no-op lookup."""
    file = CapabilityRegistryFile.model_validate({})
    assert file.version == 1
    assert file.rules == []


def test_registry_file_rejects_unknown_top_level_field() -> None:
    """extra='forbid' catches typos in the top-level schema at load."""
    with pytest.raises(ValidationError):
        CapabilityRegistryFile.model_validate(
            {"version": 1, "rules": [], "ruls": []}  # typo
        )


def test_registry_file_rejects_unknown_rule_field() -> None:
    """Same fast-fail for a rule-level typo."""
    with pytest.raises(ValidationError):
        CapabilityRegistryFile.model_validate(
            {
                "version": 1,
                "rules": [
                    {
                        "match": "claude-*",
                        "kind": "anthropic",
                        "capabilities": {"thinking": True},
                        "kid": "anthropic",  # typo
                    }
                ],
            }
        )


def test_registry_file_rejects_unknown_capability_flag() -> None:
    """RegistryCapabilities is extra='forbid' — unknown flags fail at load.

    This matters because a silently-ignored typo (e.g. `thinkng: true`)
    would let the gate strip the block despite the user's intent. Fail
    loud at load time instead."""
    with pytest.raises(ValidationError):
        CapabilityRegistryFile.model_validate(
            {
                "version": 1,
                "rules": [
                    {
                        "match": "claude-*",
                        "capabilities": {"thinkng": True},  # typo
                    }
                ],
            }
        )


def test_registry_file_rejects_version_mismatch() -> None:
    """Literal[1] pins version to 1 — future formats force an explicit bump."""
    with pytest.raises(ValidationError):
        CapabilityRegistryFile.model_validate({"version": 2, "rules": []})


def test_registry_file_rejects_empty_match_string() -> None:
    """A blank glob matches everything on fnmatch, which is not what a
    misconfigured YAML author meant. Pydantic min_length=1 catches it."""
    with pytest.raises(ValidationError):
        CapabilityRegistryFile.model_validate(
            {"version": 1, "rules": [{"match": "", "capabilities": {}}]}
        )


def test_registry_rule_kind_default_is_any() -> None:
    """Omitted ``kind`` means the rule applies regardless of adapter kind."""
    rule = CapabilityRule.model_validate({"match": "anything-*", "capabilities": {"tools": True}})
    assert rule.kind == "any"


def test_registry_rule_kind_matches_accepts_any_provider_kind() -> None:
    """``kind: any`` admits every adapter kind."""
    rule = CapabilityRule(match="x", kind="any")
    assert rule.kind_matches("anthropic")
    assert rule.kind_matches("openai_compat")


def test_registry_rule_kind_matches_exact() -> None:
    """A non-any kind filter is a strict equality check."""
    rule = CapabilityRule(match="x", kind="anthropic")
    assert rule.kind_matches("anthropic")
    assert not rule.kind_matches("openai_compat")


# ======================================================================
# Glob matching
# ======================================================================


@pytest.mark.parametrize(
    "pattern,model,expected",
    [
        ("claude-opus-4-*", "claude-opus-4-1", True),
        ("claude-opus-4-*", "claude-opus-4-6-20260101", True),
        ("claude-opus-4-*", "claude-opus-3-5", False),
        ("claude-sonnet-4-6*", "claude-sonnet-4-6", True),
        ("claude-sonnet-4-6*", "claude-sonnet-4-6-20260101", True),
        ("claude-sonnet-4-6*", "claude-sonnet-4-5", False),
        ("qwen3-coder:*", "qwen3-coder:7b", True),
        ("qwen3-coder:*", "qwen3-coder:480b-a35b", True),
        ("qwen3-coder:*", "qwen2.5-coder:7b", False),
        # case sensitivity — fnmatchcase is case-sensitive
        ("claude-*", "CLAUDE-something", False),
    ],
)
def test_rule_glob_matches(pattern: str, model: str, expected: bool) -> None:
    rule = CapabilityRule(match=pattern)
    assert rule.glob_matches(model) is expected


# ======================================================================
# Registry.lookup — first-match-per-flag, layered
# ======================================================================


def test_lookup_returns_all_none_when_no_rules() -> None:
    reg = CapabilityRegistry([])
    result = reg.lookup(kind="anthropic", model="claude-sonnet-4-6")
    assert result == ResolvedCapabilities()


def test_lookup_returns_flag_from_matching_rule() -> None:
    rule = CapabilityRule(
        match="claude-sonnet-4-6*",
        kind="anthropic",
        capabilities=RegistryCapabilities(thinking=True),
    )
    reg = CapabilityRegistry([rule])
    assert reg.lookup(kind="anthropic", model="claude-sonnet-4-6").thinking is True


def test_lookup_respects_kind_filter() -> None:
    """A rule scoped to anthropic must not fire for openai_compat."""
    rule = CapabilityRule(
        match="*",
        kind="anthropic",
        capabilities=RegistryCapabilities(thinking=True),
    )
    reg = CapabilityRegistry([rule])
    assert reg.lookup(kind="anthropic", model="whatever").thinking is True
    assert reg.lookup(kind="openai_compat", model="whatever").thinking is None


def test_lookup_first_match_wins_for_flag() -> None:
    """Two rules both matching the same model — earlier one sets the flag."""
    reg = CapabilityRegistry(
        [
            CapabilityRule(
                match="claude-*",
                kind="anthropic",
                capabilities=RegistryCapabilities(thinking=True),
            ),
            CapabilityRule(
                match="claude-sonnet-4-6*",
                kind="anthropic",
                capabilities=RegistryCapabilities(thinking=False),
            ),
        ]
    )
    # First rule wins despite the second being more specific. YAML author
    # controls priority by ordering.
    assert reg.lookup(kind="anthropic", model="claude-sonnet-4-6").thinking is True


def test_lookup_per_flag_independence() -> None:
    """A rule that only declares `thinking` does NOT block a later rule
    from supplying `tools`. Flags resolve independently."""
    reg = CapabilityRegistry(
        [
            # Rule A: matches everything, sets only thinking.
            CapabilityRule(
                match="*",
                kind="anthropic",
                capabilities=RegistryCapabilities(thinking=True),
            ),
            # Rule B: same match, sets only tools. Should apply because
            # rule A didn't touch tools.
            CapabilityRule(
                match="*",
                kind="anthropic",
                capabilities=RegistryCapabilities(tools=True),
            ),
        ]
    )
    result = reg.lookup(kind="anthropic", model="claude-sonnet-4-6")
    assert result.thinking is True
    assert result.tools is True


def test_lookup_user_rules_override_bundled() -> None:
    """from_rule_lists places user rules first → user decisions win."""
    bundled = [
        CapabilityRule(
            match="claude-sonnet-4-6*",
            kind="anthropic",
            capabilities=RegistryCapabilities(thinking=True),
        )
    ]
    user = [
        CapabilityRule(
            match="claude-sonnet-4-6*",
            kind="anthropic",
            capabilities=RegistryCapabilities(thinking=False),
        )
    ]
    reg = CapabilityRegistry.from_rule_lists(user=user, bundled=bundled)
    assert reg.lookup(kind="anthropic", model="claude-sonnet-4-6").thinking is False


def test_lookup_unmatched_flags_are_none() -> None:
    """If no rule declares a given flag, lookup returns None for it."""
    reg = CapabilityRegistry(
        [
            CapabilityRule(
                match="claude-*",
                kind="anthropic",
                capabilities=RegistryCapabilities(thinking=True),
            )
        ]
    )
    result = reg.lookup(kind="anthropic", model="claude-sonnet-4-6")
    assert result.thinking is True
    assert result.tools is None
    assert result.reasoning_passthrough is None
    assert result.max_context_tokens is None
    assert result.claude_code_suitability is None


# ======================================================================
# v1.7-B: claude_code_suitability — schema + lookup
# ======================================================================


def test_registry_capabilities_accepts_claude_code_suitability_degraded() -> None:
    """Literal["ok", "degraded"] | None — schema validation for the v1.7-B flag."""
    caps = RegistryCapabilities.model_validate({"claude_code_suitability": "degraded"})
    assert caps.claude_code_suitability == "degraded"


def test_registry_capabilities_rejects_unknown_suitability_value() -> None:
    """Literal narrows the value space — typos fail at load."""
    with pytest.raises(ValidationError):
        RegistryCapabilities.model_validate({"claude_code_suitability": "broken"})


def test_lookup_returns_claude_code_suitability() -> None:
    """A rule declaring suitability surfaces it in ResolvedCapabilities."""
    reg = CapabilityRegistry(
        [
            CapabilityRule(
                match="meta/llama-3.3-70b*",
                kind="openai_compat",
                capabilities=RegistryCapabilities(claude_code_suitability="degraded"),
            )
        ]
    )
    result = reg.lookup(kind="openai_compat", model="meta/llama-3.3-70b-instruct")
    assert result.claude_code_suitability == "degraded"


def test_lookup_user_rule_can_flip_suitability_to_ok() -> None:
    """User rules win — opt-out path for operators with tuned Llama deployments."""
    bundled = [
        CapabilityRule(
            match="*llama-3.3-70b*",
            kind="openai_compat",
            capabilities=RegistryCapabilities(claude_code_suitability="degraded"),
        )
    ]
    user = [
        CapabilityRule(
            match="meta/llama-3.3-70b-instruct",
            kind="openai_compat",
            capabilities=RegistryCapabilities(claude_code_suitability="ok"),
        )
    ]
    reg = CapabilityRegistry.from_rule_lists(user=user, bundled=bundled)
    result = reg.lookup(kind="openai_compat", model="meta/llama-3.3-70b-instruct")
    assert result.claude_code_suitability == "ok"


def test_bundled_yaml_declares_llama_3_3_70b_degraded() -> None:
    """Bundled rules cover the common slug variants for Llama-3.3-70B.

    Verified glob coverage: NIM (``meta/llama-3.3-70b-instruct``),
    OpenRouter (``meta-llama/llama-3.3-70b-instruct``), and bare local
    server form (``Llama-3.3-70B-Instruct``). Case-sensitivity is
    handled by declaring lower-case and capitalized variants.
    """
    reg = CapabilityRegistry.load_default()
    for model in [
        "meta/llama-3.3-70b-instruct",
        "meta-llama/llama-3.3-70b-instruct",
        "Llama-3.3-70B-Instruct",
    ]:
        result = reg.lookup(kind="openai_compat", model=model)
        assert result.claude_code_suitability == "degraded", (
            f"bundled YAML should declare claude_code_suitability=degraded for {model}"
        )


def test_bundled_yaml_does_not_flag_other_llama_families_as_degraded() -> None:
    """Llama-3.1 / Llama-4 / different-size Llama-3.3 / Kimi must NOT
    inherit the Llama-3.3-70B *degraded* flag — the glob is
    intentionally narrow.

    Note: Qwen3-Coder family IS positively flagged (``ok``) by the
    v1.7-B registry update, so ``is None`` is the wrong assertion for
    Qwen — we explicitly check ``!= 'degraded'`` instead.
    """
    reg = CapabilityRegistry.load_default()
    for model in [
        "meta/llama-3.1-70b-instruct",
        "meta/llama-3.3-8b-instruct",  # different size
        "moonshotai/kimi-k2-instruct",
    ]:
        result = reg.lookup(kind="openai_compat", model=model)
        assert result.claude_code_suitability is None, (
            f"bundled YAML must not flag {model} as degraded "
            f"(got {result.claude_code_suitability!r})"
        )

    # Qwen3-Coder family is positively flagged as ``ok`` (v1.7-B);
    # verify it specifically resolves to ``ok``, not ``degraded``.
    qwen_result = reg.lookup(
        kind="openai_compat", model="qwen/qwen3-coder-480b-a35b-instruct"
    )
    assert qwen_result.claude_code_suitability == "ok", (
        f"Qwen3-Coder family should be flagged as 'ok' (v1.7-B), "
        f"got {qwen_result.claude_code_suitability!r}"
    )


def test_lookup_any_kind_rule_matches_both_adapter_kinds() -> None:
    """A rule with kind='any' applies regardless of the adapter kind."""
    reg = CapabilityRegistry(
        [
            CapabilityRule(
                match="ghost-*",
                kind="any",
                capabilities=RegistryCapabilities(tools=True),
            )
        ]
    )
    assert reg.lookup(kind="anthropic", model="ghost-1").tools is True
    assert reg.lookup(kind="openai_compat", model="ghost-2").tools is True


# ======================================================================
# Bundled YAML — load_default
# ======================================================================


def test_bundled_yaml_loads_and_encodes_v0_5a_heuristic() -> None:
    """The bundled default registry covers every family the v0.5-A
    regex covered — ensures the v0.7-A replacement is behaviorally
    equivalent for the thinking gate."""
    reg = CapabilityRegistry.load_default()

    capable_models = [
        ("claude-opus-4-1", True),
        ("claude-opus-4-6-20260201", True),
        ("claude-sonnet-4-6", True),
        ("claude-sonnet-4-6-20260101", True),
        ("claude-sonnet-4-7", True),
        ("claude-haiku-4-5", True),
        ("claude-haiku-4-6", True),
    ]
    for model, expected in capable_models:
        assert reg.lookup(kind="anthropic", model=model).thinking is expected, (
            f"bundled YAML should declare thinking for {model}"
        )


def test_bundled_yaml_rejects_pre_4_6_sonnet() -> None:
    """claude-sonnet-4-5-* was the v0.4-D footnote model that 400'd —
    the bundled YAML must NOT declare thinking for it."""
    reg = CapabilityRegistry.load_default()
    for model in ["claude-sonnet-4-5", "claude-sonnet-4-5-20250929"]:
        assert reg.lookup(kind="anthropic", model=model).thinking is None, (
            f"bundled YAML must not declare thinking for {model}"
        )


def test_bundled_yaml_does_not_declare_openai_compat_thinking() -> None:
    """openai_compat queries must never get thinking=True from the bundled
    YAML — translation would drop the block anyway."""
    reg = CapabilityRegistry.load_default()
    result = reg.lookup(kind="openai_compat", model="anthropic/claude-sonnet-4-6")
    assert result.thinking is None


# ======================================================================
# User override integration — load_from_paths / reset_default_registry
# ======================================================================


def test_load_from_paths_reads_both_files(tmp_path: Path) -> None:
    bundled = _write_yaml(
        tmp_path / "bundled.yaml",
        """
version: 1
rules:
  - match: "claude-sonnet-4-6*"
    kind: anthropic
    capabilities:
      thinking: true
""",
    )
    user = _write_yaml(
        tmp_path / "user.yaml",
        """
version: 1
rules:
  - match: "claude-sonnet-4-6*"
    kind: anthropic
    capabilities:
      thinking: false
""",
    )
    reg = CapabilityRegistry.load_from_paths(bundled_path=bundled, user_path=user)
    # User rule is evaluated first → overrides bundled
    assert reg.lookup(kind="anthropic", model="claude-sonnet-4-6").thinking is False


def test_load_from_paths_missing_user_file_is_no_error(tmp_path: Path) -> None:
    """An absent user YAML is a normal config — no error, bundled applies."""
    bundled = _write_yaml(
        tmp_path / "bundled.yaml",
        """
version: 1
rules:
  - match: "claude-sonnet-4-6*"
    kind: anthropic
    capabilities:
      thinking: true
""",
    )
    missing = tmp_path / "no-such.yaml"
    reg = CapabilityRegistry.load_from_paths(bundled_path=bundled, user_path=missing)
    assert reg.lookup(kind="anthropic", model="claude-sonnet-4-6").thinking is True


def test_load_from_paths_propagates_schema_error(tmp_path: Path) -> None:
    """A malformed user YAML fails fast — no silent fallback to bundled-only."""
    bundled = _write_yaml(
        tmp_path / "bundled.yaml",
        "version: 1\nrules: []\n",
    )
    user = _write_yaml(
        tmp_path / "user.yaml",
        "version: 1\nrules:\n  - match: x\n    kapabilities: {}\n",  # typo
    )
    with pytest.raises(ValidationError):
        CapabilityRegistry.load_from_paths(bundled_path=bundled, user_path=user)


# ======================================================================
# provider_supports_thinking — behavior against registry
# ======================================================================


def test_gate_uses_injected_registry() -> None:
    """Passing ``registry=`` bypasses the module-level default — key
    dependency-injection point for tests and for future engine wiring."""
    custom = CapabilityRegistry(
        [
            CapabilityRule(
                match="secret-model",
                kind="openai_compat",
                capabilities=RegistryCapabilities(thinking=True),
            )
        ]
    )
    p = _provider(kind="openai_compat", model="secret-model")
    assert provider_supports_thinking(p, registry=custom)


def test_gate_explicit_yaml_still_wins_over_registry() -> None:
    """providers.yaml `capabilities.thinking: true` is the highest
    precedence — registry lookup is only consulted when the explicit
    flag is unset."""
    empty = CapabilityRegistry([])
    p = _provider(kind="anthropic", model="claude-sonnet-5-0", thinking=True)
    assert provider_supports_thinking(p, registry=empty)


def test_gate_defaults_to_false_when_registry_has_no_opinion() -> None:
    """Registry returns None for an unknown model → gate returns False."""
    empty = CapabilityRegistry([])
    p = _provider(kind="anthropic", model="claude-sonnet-4-6")
    assert not provider_supports_thinking(p, registry=empty)


def test_reset_default_registry_forces_reload(tmp_path: Path) -> None:
    """After reset, the next lookup re-loads the default registry from
    disk. Verifies the test-hook works so downstream tests can stage a
    user YAML if they need to."""
    # Prime the cache.
    first = get_default_registry()
    assert first is not None

    reset_default_registry()

    # After reset we get a freshly loaded instance (may or may not be
    # the same object — in practice it will be a new one).
    second = get_default_registry()
    assert second is not None
    # Behavior is identical because the disk is unchanged.
    assert (
        second.lookup(kind="anthropic", model="claude-sonnet-4-6").thinking
        == first.lookup(kind="anthropic", model="claude-sonnet-4-6").thinking
    )


def test_module_level_default_matches_load_default() -> None:
    """get_default_registry() and load_default() produce equivalent
    behavior — the former just memoizes the latter."""
    reset_default_registry()
    try:
        default = get_default_registry()
        fresh = CapabilityRegistry.load_default()
        assert (
            default.lookup(kind="anthropic", model="claude-opus-4-1").thinking
            == fresh.lookup(kind="anthropic", model="claude-opus-4-1").thinking
        )
    finally:
        # Leave the cache in a predictable state for later tests.
        reset_default_registry()


def test_capability_module_reexports_registry_types() -> None:
    """Public API: ``CapabilityRegistry`` and ``ResolvedCapabilities``
    are importable from ``coderouter.routing.capability`` so
    adapters / engine don't need to reach into the config subpackage."""
    assert capability_module.CapabilityRegistry is CapabilityRegistry
    assert capability_module.ResolvedCapabilities is ResolvedCapabilities
