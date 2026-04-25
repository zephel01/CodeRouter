"""Unit tests for v1.7-B claude_code_suitability startup check.

Scope:
    - The :func:`check_claude_code_chain_suitability` walker on its own
      (does it correctly identify ``claude-code-*`` profiles, look up
      ``claude_code_suitability`` per provider, and emit one warn per
      affected profile?).
    - Profile-name prefix gate (does a non-``claude-code`` profile stay
      quiet even with a degraded provider?).
    - Opt-out via user override (does a registry rule with
      ``claude_code_suitability: ok`` suppress the warn?).
    - Log payload shape (matches
      :class:`ChainClaudeCodeSuitabilityDegradedPayload`).

The bundled YAML's actual Llama-3.3-70B coverage is exercised in
``tests/test_capability_registry.py`` (load_default-based assertions);
this file uses synthetic registries so the tests are independent of the
bundled YAML's contents.
"""

from __future__ import annotations

import logging

import pytest

from coderouter.config.capability_registry import (
    CapabilityRegistry,
    CapabilityRule,
    RegistryCapabilities,
)
from coderouter.config.schemas import (
    Capabilities,
    CodeRouterConfig,
    FallbackChain,
    ProviderConfig,
)
from coderouter.routing.capability import (
    CLAUDE_CODE_PROFILE_PREFIX,
    check_claude_code_chain_suitability,
)

# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _provider(name: str, model: str) -> ProviderConfig:
    """Build a minimal openai_compat provider for chain assembly."""
    return ProviderConfig(
        name=name,
        kind="openai_compat",
        base_url="https://example.invalid/v1",
        model=model,
        paid=False,
        capabilities=Capabilities(),
    )


def _config(*, providers: list[ProviderConfig], profiles: list[FallbackChain]) -> CodeRouterConfig:
    """Build a CodeRouterConfig from explicit providers + profiles.

    The first profile's name is used as ``default_profile`` so the
    config-level validators (``_check_default_profile_exists``) pass —
    we don't care which one is default for these tests, only that the
    walker correctly inspects every ``claude-code-*`` profile.
    """
    return CodeRouterConfig(
        allow_paid=False,
        default_profile=profiles[0].name,
        providers=providers,
        profiles=profiles,
    )


def _degraded_registry() -> CapabilityRegistry:
    """Synthetic registry that flags ``llama-3.3-70b*`` as degraded.

    Independent of the bundled YAML so tests don't depend on its rules
    surviving future edits.
    """
    return CapabilityRegistry(
        [
            CapabilityRule(
                match="*llama-3.3-70b*",
                kind="openai_compat",
                capabilities=RegistryCapabilities(claude_code_suitability="degraded"),
            )
        ]
    )


# ======================================================================
# Profile-name prefix gate
# ======================================================================


def test_constant_value_is_claude_code() -> None:
    """The prefix is documented at module level; lock the value so a
    silent rename of the constant doesn't quietly disable the gate."""
    assert CLAUDE_CODE_PROFILE_PREFIX == "claude-code"


def test_warns_when_claude_code_chain_has_degraded_provider(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The walker emits exactly one warn per claude-code-* profile that
    contains a registry-flagged degraded provider.

    Payload fields verified: profile name, parallel
    ``degraded_providers`` / ``degraded_models`` lists, and the canned
    hint string.
    """
    config = _config(
        providers=[
            _provider("nim-llama-3.3-70b", "meta/llama-3.3-70b-instruct"),
            _provider("nim-qwen3-coder", "qwen/qwen3-coder-480b-a35b-instruct"),
        ],
        profiles=[
            FallbackChain(
                name="claude-code-nim",
                providers=["nim-llama-3.3-70b", "nim-qwen3-coder"],
            ),
        ],
    )
    logger = logging.getLogger("test.suitability.warn")
    with caplog.at_level(logging.WARNING, logger=logger.name):
        flagged = check_claude_code_chain_suitability(
            config, logger=logger, registry=_degraded_registry()
        )

    assert flagged == {
        "claude-code-nim": [("nim-llama-3.3-70b", "meta/llama-3.3-70b-instruct")]
    }
    records = [
        r for r in caplog.records if r.message == "chain-claude-code-suitability-degraded"
    ]
    assert len(records) == 1
    record = records[0]
    assert record.profile == "claude-code-nim"  # type: ignore[attr-defined]
    assert record.degraded_providers == ["nim-llama-3.3-70b"]  # type: ignore[attr-defined]
    assert record.degraded_models == ["meta/llama-3.3-70b-instruct"]  # type: ignore[attr-defined]
    # Hint text intentionally stable so operators can grep it.
    assert "claude-code" not in record.hint  # hint is generic, not profile-name-bound  # type: ignore[attr-defined]
    assert "troubleshooting.md" in record.hint  # type: ignore[attr-defined]


def test_silent_when_non_claude_code_profile_has_degraded_provider(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A ``writing`` (or any non-``claude-code-*``) profile with a
    degraded provider must NOT emit a warn — Llama-3.3-70B is fine
    outside the agentic harness."""
    config = _config(
        providers=[_provider("nim-llama-3.3-70b", "meta/llama-3.3-70b-instruct")],
        profiles=[FallbackChain(name="writing", providers=["nim-llama-3.3-70b"])],
    )
    logger = logging.getLogger("test.suitability.silent.non_cc")
    with caplog.at_level(logging.WARNING, logger=logger.name):
        flagged = check_claude_code_chain_suitability(
            config, logger=logger, registry=_degraded_registry()
        )

    assert flagged == {}
    assert not any(
        r.message == "chain-claude-code-suitability-degraded" for r in caplog.records
    )


def test_silent_when_claude_code_chain_has_no_degraded_providers(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A ``claude-code-*`` profile composed entirely of unflagged
    providers stays quiet (the chain is healthy)."""
    config = _config(
        providers=[
            _provider("nim-qwen3-coder", "qwen/qwen3-coder-480b-a35b-instruct"),
            _provider("nim-kimi-k2", "moonshotai/kimi-k2-instruct"),
        ],
        profiles=[
            FallbackChain(
                name="claude-code-nim",
                providers=["nim-qwen3-coder", "nim-kimi-k2"],
            ),
        ],
    )
    logger = logging.getLogger("test.suitability.silent.clean")
    with caplog.at_level(logging.WARNING, logger=logger.name):
        flagged = check_claude_code_chain_suitability(
            config, logger=logger, registry=_degraded_registry()
        )

    assert flagged == {}


# ======================================================================
# Opt-out via user-side ``ok`` declaration
# ======================================================================


def test_user_rule_flipping_to_ok_suppresses_warn(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When ``~/.coderouter/model-capabilities.yaml`` declares
    ``claude_code_suitability: ok`` for the matching glob, the user
    rule beats the bundled ``degraded`` rule (user rules are evaluated
    first in the registry's first-match-per-flag walk)."""
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
    registry = CapabilityRegistry.from_rule_lists(user=user, bundled=bundled)
    config = _config(
        providers=[_provider("nim-llama-3.3-70b", "meta/llama-3.3-70b-instruct")],
        profiles=[
            FallbackChain(
                name="claude-code-nim",
                providers=["nim-llama-3.3-70b"],
            ),
        ],
    )
    logger = logging.getLogger("test.suitability.optout")
    with caplog.at_level(logging.WARNING, logger=logger.name):
        flagged = check_claude_code_chain_suitability(
            config, logger=logger, registry=registry
        )

    assert flagged == {}


# ======================================================================
# Multi-profile aggregation
# ======================================================================


def test_emits_one_warn_per_affected_profile(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Two ``claude-code-*`` profiles each containing a degraded provider
    produce two distinct warns (one per profile, not one per provider).
    Provider/model lists in each payload reflect chain order."""
    config = _config(
        providers=[
            _provider("nim-llama", "meta/llama-3.3-70b-instruct"),
            _provider("or-llama", "meta-llama/llama-3.3-70b-instruct"),
            _provider("nim-qwen", "qwen/qwen3-coder-480b-a35b-instruct"),
        ],
        profiles=[
            FallbackChain(
                name="claude-code-nim",
                providers=["nim-llama", "nim-qwen"],
            ),
            FallbackChain(
                name="claude-code-openrouter",
                providers=["or-llama", "nim-qwen"],
            ),
        ],
    )
    logger = logging.getLogger("test.suitability.multi")
    with caplog.at_level(logging.WARNING, logger=logger.name):
        flagged = check_claude_code_chain_suitability(
            config, logger=logger, registry=_degraded_registry()
        )

    assert set(flagged.keys()) == {"claude-code-nim", "claude-code-openrouter"}
    records = [
        r for r in caplog.records if r.message == "chain-claude-code-suitability-degraded"
    ]
    assert len(records) == 2
    profiles_in_records = sorted(r.profile for r in records)  # type: ignore[attr-defined]
    assert profiles_in_records == ["claude-code-nim", "claude-code-openrouter"]
