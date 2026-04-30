"""v1.9-E phase 2 (L2): memory-pressure guard tests.

Three test groups:

- **Detector (pure)**: ``is_memory_pressure_error`` matches OOM
  phrases, false on non-OOM errors. Stateless.
- **Guard (stateful)**: ``MemoryPressureGuard`` mark/cooldown/expiry
  semantics with ``now=`` injection for deterministic timing.
- **Engine integration**: ``memory_pressure_action`` dispatch
  (off / warn / skip), per-attempt detection, chain skip on
  pressured providers, ``chain-memory-pressure-blocked`` warn.
"""

from __future__ import annotations

import logging

import pytest

from coderouter.adapters.anthropic_native import AnthropicAdapter
from coderouter.adapters.base import AdapterError, BaseAdapter, ProviderCallOverrides
from coderouter.config.schemas import (
    CodeRouterConfig,
    FallbackChain,
    ProviderConfig,
)
from coderouter.guards.memory_pressure import (
    MemoryPressureGuard,
    is_memory_pressure_error,
)
from coderouter.routing import FallbackEngine
from coderouter.routing.fallback import NoProvidersAvailableError
from coderouter.translation.anthropic import (
    AnthropicMessage,
    AnthropicRequest,
    AnthropicResponse,
    AnthropicUsage,
)

# ----------------------------------------------------------------------
# Group 1: Detector (pure)
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        "500 from upstream: out of memory",
        "CUDA out of memory",
        "GPU error: insufficient memory for model",
        "Metal out of memory",
        "model requires more system memory than available",
        "failed to allocate buffer for tensor",
        "OOM killed by oom_killer",
        "ggml_cuda_host_malloc: failed to allocate",
    ],
)
def test_is_memory_pressure_error_matches_oom_patterns(message: str) -> None:
    """Each curated OOM phrase produces a positive detection.

    Phrases are case-insensitive and substring-anchored, so the
    surrounding wire-format wrappers ("500 from upstream:", body
    truncation suffixes, etc.) don't disrupt the match.
    """
    exc = AdapterError(message, provider="ollama-local", retryable=True)
    assert is_memory_pressure_error(exc) is True


@pytest.mark.parametrize(
    "message",
    [
        "401 unauthorized",
        "model not found: claude-fictional",
        "connection refused",
        "rate limited: try again in 60s",
        "invalid request: missing field 'messages'",
    ],
)
def test_is_memory_pressure_error_false_on_non_oom(message: str) -> None:
    """Non-OOM errors are NOT classified as memory pressure.

    Auth failures, missing models, network issues, rate limits —
    cooldown wouldn't help any of these, and a false positive would
    make the operator's chain quieter than it should be.
    """
    exc = AdapterError(message, provider="ollama-local", retryable=True)
    assert is_memory_pressure_error(exc) is False


# ----------------------------------------------------------------------
# Group 2: Guard (stateful)
# ----------------------------------------------------------------------


def test_guard_mark_pressured_blocks_until_cooldown_expires() -> None:
    """``mark_pressured`` populates a TTL entry; ``is_pressured`` reads it."""
    guard = MemoryPressureGuard()
    # t=100: mark with 60s cooldown → deadline = 160.
    guard.mark_pressured("ollama-local", cooldown_s=60.0, now=100.0)
    assert guard.is_pressured("ollama-local", now=100.0) is True
    assert guard.is_pressured("ollama-local", now=159.9) is True
    # Lazy expiry sweep — at the deadline the entry is dropped before
    # ``is_pressured`` answers, so the call returns False AND a
    # second read sees a fresh state.
    assert guard.is_pressured("ollama-local", now=160.0) is False
    assert guard.is_pressured("ollama-local", now=160.5) is False
    # An untracked provider always reports False — no KeyError.
    assert guard.is_pressured("never-marked", now=100.0) is False


def test_guard_remark_extends_cooldown_window() -> None:
    """Re-marking a pressured provider extends the deadline.

    Common case: backend keeps OOM-ing on every retry, so the engine
    re-marks each time. The deadline should walk forward, not stay
    pinned to the first detection.
    """
    guard = MemoryPressureGuard()
    guard.mark_pressured("ollama-local", cooldown_s=60.0, now=100.0)
    # Initial deadline 160. Re-mark at t=130 with 60s → new deadline 190.
    guard.mark_pressured("ollama-local", cooldown_s=60.0, now=130.0)
    assert guard.is_pressured("ollama-local", now=160.5) is True
    assert guard.is_pressured("ollama-local", now=189.9) is True
    assert guard.is_pressured("ollama-local", now=190.0) is False


# ----------------------------------------------------------------------
# Group 3: Engine integration
# ----------------------------------------------------------------------


class _OOMAnthropicAdapter(AnthropicAdapter):
    """Test double: raises an OOM-coded AdapterError on every call."""

    async def healthcheck(self) -> bool:
        return True

    async def generate_anthropic(
        self,
        request: AnthropicRequest,
        *,
        overrides: ProviderCallOverrides | None = None,
    ) -> AnthropicResponse:
        raise AdapterError(
            "500 from upstream: 'model requires more system memory'",
            provider=self.name,
            status_code=500,
            retryable=True,
        )


class _HealthyAnthropicAdapter(AnthropicAdapter):
    """Test double: returns a trivial successful AnthropicResponse."""

    async def healthcheck(self) -> bool:
        return True

    async def generate_anthropic(
        self,
        request: AnthropicRequest,
        *,
        overrides: ProviderCallOverrides | None = None,
    ) -> AnthropicResponse:
        return AnthropicResponse(
            id="msg_healthy",
            model=self.config.model,
            content=[{"type": "text", "text": "ok"}],
            stop_reason="end_turn",
            usage=AnthropicUsage(input_tokens=1, output_tokens=1),
            coderouter_provider=self.name,
        )


def _provider(name: str) -> ProviderConfig:
    return ProviderConfig(
        name=name,
        kind="anthropic",
        base_url="https://api.anthropic.com",
        model="claude-sonnet-4-6",
        api_key_env="ANTHROPIC_API_KEY",
    )


def _config_with_action(
    action: str,
    *,
    chain_providers: list[str],
    cooldown_s: int = 120,
) -> CodeRouterConfig:
    providers = [_provider(name) for name in chain_providers]
    return CodeRouterConfig(
        allow_paid=False,
        default_profile="default",
        providers=providers,
        profiles=[
            FallbackChain(
                name="default",
                providers=chain_providers,
                memory_pressure_action=action,  # type: ignore[arg-type]
                memory_pressure_cooldown_s=cooldown_s,
            )
        ],
    )


def _engine_with(
    config: CodeRouterConfig, adapters: dict[str, BaseAdapter]
) -> FallbackEngine:
    engine = FallbackEngine(config)
    engine._adapters = adapters  # type: ignore[assignment]
    return engine


def _request() -> AnthropicRequest:
    return AnthropicRequest(
        max_tokens=64,
        messages=[AnthropicMessage(role="user", content="hi")],
    )


@pytest.mark.asyncio
async def test_action_warn_logs_but_does_not_skip_or_mark(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``action=warn``: emit ``memory-pressure-detected`` info on
    OOM but do NOT mark the provider pressured. The chain still
    tries the same provider next time."""
    config = _config_with_action(
        "warn", chain_providers=["primary", "fallback"]
    )
    primary = _OOMAnthropicAdapter(config.provider_by_name("primary"))
    fallback = _HealthyAnthropicAdapter(config.provider_by_name("fallback"))
    engine = _engine_with(
        config, {"primary": primary, "fallback": fallback}
    )

    with caplog.at_level(logging.INFO, logger="coderouter"):
        resp = await engine.generate_anthropic(_request())

    assert resp.coderouter_provider == "fallback"

    detected = [
        r for r in caplog.records if r.msg == "memory-pressure-detected"
    ]
    assert len(detected) == 1
    rec = detected[0]
    assert rec.provider == "primary"
    assert rec.action == "warn"
    assert rec.cooldown_s == 120

    # No skip-memory-pressure: ``warn`` is log-only.
    assert [r for r in caplog.records if r.msg == "skip-memory-pressure"] == []
    # And the guard tracks no cooldown.
    assert engine._memory_pressure.is_pressured("primary") is False


@pytest.mark.asyncio
async def test_action_skip_marks_pressured_and_skips_next_request(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``action=skip``: OOM marks the provider pressured; the next
    request's chain resolver filters it out with
    ``skip-memory-pressure`` and falls through to the next entry."""
    config = _config_with_action(
        "skip", chain_providers=["primary", "fallback"]
    )
    primary = _OOMAnthropicAdapter(config.provider_by_name("primary"))
    fallback = _HealthyAnthropicAdapter(config.provider_by_name("fallback"))
    engine = _engine_with(
        config, {"primary": primary, "fallback": fallback}
    )

    # First request: primary OOMs, fallback serves. Provider gets
    # pressured.
    await engine.generate_anthropic(_request())
    assert engine._memory_pressure.is_pressured("primary") is True

    # Second request: chain resolver SKIPS primary, fallback serves
    # without primary even being attempted. Verify via log trail.
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="coderouter"):
        resp = await engine.generate_anthropic(_request())

    assert resp.coderouter_provider == "fallback"
    skip_records = [
        r for r in caplog.records if r.msg == "skip-memory-pressure"
    ]
    assert len(skip_records) == 1
    assert skip_records[0].provider == "primary"
    # ``primary`` should not have been attempted on the 2nd request —
    # no try-provider for primary means the OOM detector also
    # didn't fire a second time.
    primary_attempts = [
        r
        for r in caplog.records
        if r.msg == "try-provider" and r.provider == "primary"
    ]
    assert primary_attempts == []


@pytest.mark.asyncio
async def test_action_off_disables_detection_entirely(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``action=off``: no detection log, no marking. Backward-compat
    path for operators who don't want any L2 behavior."""
    config = _config_with_action(
        "off", chain_providers=["primary", "fallback"]
    )
    primary = _OOMAnthropicAdapter(config.provider_by_name("primary"))
    fallback = _HealthyAnthropicAdapter(config.provider_by_name("fallback"))
    engine = _engine_with(
        config, {"primary": primary, "fallback": fallback}
    )

    with caplog.at_level(logging.INFO, logger="coderouter"):
        await engine.generate_anthropic(_request())

    assert [
        r for r in caplog.records if r.msg == "memory-pressure-detected"
    ] == []
    assert engine._memory_pressure.is_pressured("primary") is False


@pytest.mark.asyncio
async def test_chain_memory_pressure_blocked_warn_on_full_chain_pressure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When every provider in the chain is pressured, the chain
    resolver emits ``chain-memory-pressure-blocked`` warn and the
    request raises ``NoProvidersAvailableError``."""
    config = _config_with_action(
        "skip", chain_providers=["primary", "fallback"]
    )
    primary = _OOMAnthropicAdapter(config.provider_by_name("primary"))
    fallback = _OOMAnthropicAdapter(config.provider_by_name("fallback"))
    engine = _engine_with(
        config, {"primary": primary, "fallback": fallback}
    )

    # Pre-mark both as pressured so the chain resolver filters them
    # both out without us having to drive two failing requests.
    engine._memory_pressure.mark_pressured("primary", 120.0)
    engine._memory_pressure.mark_pressured("fallback", 120.0)

    with (
        caplog.at_level(logging.INFO, logger="coderouter"),
        pytest.raises(NoProvidersAvailableError),
    ):
        await engine.generate_anthropic(_request())

    skip_records = [
        r for r in caplog.records if r.msg == "skip-memory-pressure"
    ]
    assert len(skip_records) == 2
    chain_records = [
        r for r in caplog.records if r.msg == "chain-memory-pressure-blocked"
    ]
    assert len(chain_records) == 1
    chain_rec = chain_records[0]
    assert chain_rec.profile == "default"
    assert chain_rec.blocked_providers == ["primary", "fallback"]
