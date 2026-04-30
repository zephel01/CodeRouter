"""v1.9-E phase 2 (L5): backend-health monitor tests.

Three test groups:

- **Monitor (pure)**: state-machine transitions and recovery.
- **Engine integration (action dispatch)**: ``warn`` / ``demote`` /
  ``off`` paths through ``_resolve_chain``.
- **Engine integration (chain reorder)**: UNHEALTHY providers go to
  the back; uniformly-UNHEALTHY chains stay in original order.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

import pytest

from coderouter.adapters.anthropic_native import AnthropicAdapter
from coderouter.adapters.base import AdapterError, BaseAdapter, ProviderCallOverrides
from coderouter.config.schemas import (
    CodeRouterConfig,
    FallbackChain,
    ProviderConfig,
)
from coderouter.guards.backend_health import BackendHealthMonitor
from coderouter.routing import FallbackEngine
from coderouter.routing.fallback import NoProvidersAvailableError
from coderouter.translation.anthropic import (
    AnthropicMessage,
    AnthropicRequest,
    AnthropicResponse,
    AnthropicStreamEvent,
    AnthropicUsage,
)

# ----------------------------------------------------------------------
# Group 1: Monitor (pure)
# ----------------------------------------------------------------------


def test_monitor_initial_state_is_healthy() -> None:
    """A never-observed provider reports HEALTHY by default."""
    mon = BackendHealthMonitor()
    assert mon.state_for("anywhere") == "HEALTHY"
    assert mon.is_unhealthy("anywhere") is False


def test_monitor_consecutive_failures_walk_through_states() -> None:
    """threshold consecutive failures → DEGRADED; 2x threshold → UNHEALTHY."""
    mon = BackendHealthMonitor()
    threshold = 3

    # First two failures: still HEALTHY (counter ticks under the
    # threshold), no transition fired.
    for _ in range(2):
        t = mon.record_attempt("p", success=False, threshold=threshold)
        assert t is None
    assert mon.state_for("p") == "HEALTHY"

    # Third failure: HEALTHY → DEGRADED.
    t = mon.record_attempt("p", success=False, threshold=threshold)
    assert t is not None
    assert t.old_state == "HEALTHY"
    assert t.new_state == "DEGRADED"
    assert t.consecutive_failures == 3

    # Failures 4 and 5: still DEGRADED (no double-emit).
    for _ in range(2):
        t = mon.record_attempt("p", success=False, threshold=threshold)
        assert t is None
    assert mon.state_for("p") == "DEGRADED"

    # Sixth failure (= 2 * threshold): DEGRADED → UNHEALTHY.
    t = mon.record_attempt("p", success=False, threshold=threshold)
    assert t is not None
    assert t.old_state == "DEGRADED"
    assert t.new_state == "UNHEALTHY"
    assert t.consecutive_failures == 6
    assert mon.is_unhealthy("p") is True

    # Seventh failure: still UNHEALTHY, no transition.
    t = mon.record_attempt("p", success=False, threshold=threshold)
    assert t is None


def test_monitor_single_success_resets_to_healthy() -> None:
    """A success snaps an UNHEALTHY provider straight back to HEALTHY."""
    mon = BackendHealthMonitor()
    threshold = 2
    for _ in range(4):
        mon.record_attempt("p", success=False, threshold=threshold)
    assert mon.state_for("p") == "UNHEALTHY"

    t = mon.record_attempt("p", success=True, threshold=threshold)
    assert t is not None
    assert t.old_state == "UNHEALTHY"
    assert t.new_state == "HEALTHY"
    assert t.consecutive_failures == 0
    assert mon.is_unhealthy("p") is False

    # Subsequent successes: stable HEALTHY, no log spam.
    t = mon.record_attempt("p", success=True, threshold=threshold)
    assert t is None


# ----------------------------------------------------------------------
# Group 2: Engine integration — action dispatch
# ----------------------------------------------------------------------


class _AlwaysFailAdapter(AnthropicAdapter):
    """Test double: every call raises a non-OOM AdapterError.

    Distinct from ``_OOMAnthropicAdapter`` in test_memory_pressure —
    we want failures that DO trip backend-health but DO NOT trip the
    L2 memory-pressure detector. A plain "500 internal server error"
    body covers that.
    """

    async def healthcheck(self) -> bool:
        return True

    async def generate_anthropic(
        self,
        request: AnthropicRequest,
        *,
        overrides: ProviderCallOverrides | None = None,
    ) -> AnthropicResponse:
        raise AdapterError(
            "500 internal server error",
            provider=self.name,
            status_code=500,
            retryable=True,
        )

    async def stream_anthropic(  # pragma: no cover — not exercised here
        self,
        request: AnthropicRequest,
        *,
        overrides: ProviderCallOverrides | None = None,
    ) -> AsyncIterator[AnthropicStreamEvent]:
        if False:
            yield


class _HealthyAdapter(AnthropicAdapter):
    """Test double: returns a trivial successful response."""

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


def _config(
    chain: list[str],
    *,
    action: str,
    threshold: int = 2,
) -> CodeRouterConfig:
    return CodeRouterConfig(
        allow_paid=False,
        default_profile="default",
        providers=[_provider(name) for name in chain],
        profiles=[
            FallbackChain(
                name="default",
                providers=chain,
                backend_health_action=action,  # type: ignore[arg-type]
                backend_health_threshold=threshold,
            )
        ],
    )


def _engine(
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
async def test_action_warn_logs_state_changes_but_does_not_demote(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``action=warn``: state-changed log fires, chain order is
    unchanged, no ``demote-unhealthy-provider`` log."""
    config = _config(["primary", "fallback"], action="warn", threshold=2)
    primary = _AlwaysFailAdapter(config.provider_by_name("primary"))
    fallback = _HealthyAdapter(config.provider_by_name("fallback"))
    engine = _engine(config, {"primary": primary, "fallback": fallback})

    with caplog.at_level(logging.INFO, logger="coderouter"):
        # Drive enough failures on primary to reach UNHEALTHY (2 *
        # threshold = 4). Each request lets primary fail and fallback
        # serve.
        for _ in range(4):
            await engine.generate_anthropic(_request())

    assert engine._backend_health.is_unhealthy("primary") is True

    # State-changed events fired (HEALTHY→DEGRADED + DEGRADED→UNHEALTHY +
    # repeated HEALTHY recoveries on the fallback don't trigger
    # because fallback was always HEALTHY to begin with).
    transitions = [
        r for r in caplog.records if r.msg == "backend-health-changed"
    ]
    states = [(r.old_state, r.new_state) for r in transitions]  # type: ignore[attr-defined]
    assert ("HEALTHY", "DEGRADED") in states
    assert ("DEGRADED", "UNHEALTHY") in states

    # No demote log: warn is log-only.
    assert [
        r for r in caplog.records if r.msg == "demote-unhealthy-provider"
    ] == []


@pytest.mark.asyncio
async def test_action_demote_moves_unhealthy_to_chain_end(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``action=demote``: an UNHEALTHY provider is moved to the back of
    the chain on the next ``_resolve_chain`` and the engine attempts
    the previously-secondary provider FIRST."""
    config = _config(["primary", "fallback"], action="demote", threshold=2)
    primary = _AlwaysFailAdapter(config.provider_by_name("primary"))
    fallback = _HealthyAdapter(config.provider_by_name("fallback"))
    engine = _engine(config, {"primary": primary, "fallback": fallback})

    # Drive primary to UNHEALTHY (4 failures with threshold=2).
    for _ in range(4):
        await engine.generate_anthropic(_request())
    assert engine._backend_health.is_unhealthy("primary") is True

    # Next request: chain resolver demotes primary to back; fallback
    # is attempted first. Verify via the try-provider trail.
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="coderouter"):
        await engine.generate_anthropic(_request())

    # First try-provider should be ``fallback``, not ``primary``.
    try_records = [r for r in caplog.records if r.msg == "try-provider"]
    assert try_records, "expected at least one try-provider event"
    assert try_records[0].provider == "fallback"
    # And the demote-log fired for primary.
    demote_records = [
        r for r in caplog.records if r.msg == "demote-unhealthy-provider"
    ]
    assert len(demote_records) == 1
    assert demote_records[0].provider == "primary"


@pytest.mark.asyncio
async def test_action_off_disables_monitoring(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``action=off``: zero state-change observation, no demote log,
    monitor stays empty."""
    config = _config(["primary", "fallback"], action="off", threshold=2)
    primary = _AlwaysFailAdapter(config.provider_by_name("primary"))
    fallback = _HealthyAdapter(config.provider_by_name("fallback"))
    engine = _engine(config, {"primary": primary, "fallback": fallback})

    for _ in range(4):
        await engine.generate_anthropic(_request())

    # Monitor never recorded anything → state remains HEALTHY (default).
    assert engine._backend_health.state_for("primary") == "HEALTHY"
    assert [
        r for r in caplog.records if r.msg == "backend-health-changed"
    ] == []
    assert [
        r for r in caplog.records if r.msg == "demote-unhealthy-provider"
    ] == []


@pytest.mark.asyncio
async def test_recovery_emits_unhealthy_to_healthy_transition(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A success on a provider that was UNHEALTHY → ``HEALTHY``
    transition + log."""
    config = _config(
        ["primary", "fallback"], action="warn", threshold=2
    )
    # Use a "recovering" double: fails N times then succeeds.
    fail_budget = [4]

    class _RecoveringAdapter(AnthropicAdapter):
        async def healthcheck(self) -> bool:
            return True

        async def generate_anthropic(
            self,
            request: AnthropicRequest,
            *,
            overrides: ProviderCallOverrides | None = None,
        ) -> AnthropicResponse:
            if fail_budget[0] > 0:
                fail_budget[0] -= 1
                raise AdapterError(
                    "500 internal server error",
                    provider=self.name,
                    status_code=500,
                    retryable=True,
                )
            return AnthropicResponse(
                id="msg_recover",
                model=self.config.model,
                content=[{"type": "text", "text": "ok"}],
                stop_reason="end_turn",
                usage=AnthropicUsage(input_tokens=1, output_tokens=1),
                coderouter_provider=self.name,
            )

    primary = _RecoveringAdapter(config.provider_by_name("primary"))
    fallback = _HealthyAdapter(config.provider_by_name("fallback"))
    engine = _engine(config, {"primary": primary, "fallback": fallback})

    # 4 failing requests drive primary to UNHEALTHY.
    for _ in range(4):
        await engine.generate_anthropic(_request())
    assert engine._backend_health.is_unhealthy("primary") is True

    # 5th request: primary recovers.
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="coderouter"):
        resp = await engine.generate_anthropic(_request())

    assert resp.coderouter_provider == "primary"
    transitions = [
        r for r in caplog.records if r.msg == "backend-health-changed"
    ]
    assert any(
        r.old_state == "UNHEALTHY" and r.new_state == "HEALTHY"  # type: ignore[attr-defined]
        for r in transitions
    )
    assert engine._backend_health.is_unhealthy("primary") is False


@pytest.mark.asyncio
async def test_demote_no_op_when_chain_uniformly_unhealthy(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When EVERY provider is UNHEALTHY, demote leaves the chain in
    original order — moving "the unhealthy ones to the back" of an
    all-unhealthy list is a no-op, and emitting a demote log per
    provider would be log spam without operator value."""
    config = _config(["primary", "fallback"], action="demote", threshold=2)
    primary = _AlwaysFailAdapter(config.provider_by_name("primary"))
    fallback = _AlwaysFailAdapter(config.provider_by_name("fallback"))
    engine = _engine(config, {"primary": primary, "fallback": fallback})

    # Pre-mark both as UNHEALTHY directly via the monitor.
    for _ in range(4):
        engine._backend_health.record_attempt(
            "primary", success=False, threshold=2
        )
        engine._backend_health.record_attempt(
            "fallback", success=False, threshold=2
        )
    assert engine._backend_health.is_unhealthy("primary") is True
    assert engine._backend_health.is_unhealthy("fallback") is True

    caplog.clear()
    with (
        caplog.at_level(logging.INFO, logger="coderouter"),
        pytest.raises(NoProvidersAvailableError),
    ):
        await engine.generate_anthropic(_request())

    # No demote log because reorder would be a no-op.
    assert [
        r for r in caplog.records if r.msg == "demote-unhealthy-provider"
    ] == []
    # And the engine still attempted both (best-effort, not skip).
    try_records = [r for r in caplog.records if r.msg == "try-provider"]
    attempted = [r.provider for r in try_records]  # type: ignore[attr-defined]
    assert "primary" in attempted
    assert "fallback" in attempted
