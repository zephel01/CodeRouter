"""v0.6-C: 宣言的 ALLOW_PAID gate — ``chain-paid-gate-blocked`` warn.

Motivation (plan.md §9.3 #3):
    v0.1 already filters ``paid: true`` providers from the chain when
    ``allow_paid=False``, and emits ``skip-paid-provider`` INFO per
    provider. But when the paid gate filters the ENTIRE chain to empty,
    the operator-visible surface was just ``NoProvidersAvailableError``
    — the gate was the root cause, but the log trail required grepping
    INFO lines and correlating them against the thrown exception. v0.6-C
    adds a single aggregate warn at chain granularity so the declarative
    rule ("this chain was rejected because every provider is paid and
    ALLOW_PAID=false") is visible in one grep.

Scope covered here
    - all-paid chain + ALLOW_PAID=false → warn fires (once), payload
      carries profile + blocked provider names + hint
    - mixed chain (one paid + one free), free fails retryably → warn
      does NOT fire (chain was still tried); normal provider-failed
      trail carries the diagnosis
    - ALLOW_PAID=true on all-paid chain → warn does NOT fire (paid
      providers pass the gate)
    - unknown-provider-only chain (paid gate never applies) → warn does
      NOT fire
    - each of the 4 engine entry points (generate / stream /
      generate_anthropic / stream_anthropic) emits the warn

The warn is separate from the raised ``NoProvidersAvailableError``
which keeps its existing shape — this is purely a log-lane addition
(parallel to v0.5.1 A-3 ``chain-uniform-auth-failure``).
"""

from __future__ import annotations

import logging

import pytest

from coderouter.adapters.base import AdapterError
from coderouter.config.schemas import CodeRouterConfig
from coderouter.routing import NoProvidersAvailableError
from coderouter.translation import AnthropicMessage, AnthropicRequest
from tests.test_fallback import FakeAdapter, _engine_with, _request


def _paid_gate_logs(
    caplog: pytest.LogCaptureFixture,
) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.msg == "chain-paid-gate-blocked"]


def _skip_paid_logs(
    caplog: pytest.LogCaptureFixture,
) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.msg == "skip-paid-provider"]


def _anthropic_request(profile: str | None = None) -> AnthropicRequest:
    return AnthropicRequest(
        max_tokens=8,
        messages=[AnthropicMessage(role="user", content="hi")],
        profile=profile,
    )


# ----------------------------------------------------------------------
# Primary case: chain is 100% paid, ALLOW_PAID=false
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_paid_chain_fires_warn(
    basic_config: CodeRouterConfig,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Every provider in the chain is paid-blocked → warn fires once."""
    # Rewrite the default profile to hold ONLY the paid provider
    basic_config.profiles[0].providers = ["paid-cloud"]
    assert basic_config.allow_paid is False
    fakes = {
        "local": FakeAdapter(basic_config.provider_by_name("local")),
        "free-cloud": FakeAdapter(basic_config.provider_by_name("free-cloud")),
        "paid-cloud": FakeAdapter(basic_config.provider_by_name("paid-cloud")),
    }
    engine = _engine_with(basic_config, fakes)

    with (
        caplog.at_level(logging.INFO, logger="coderouter"),
        pytest.raises(NoProvidersAvailableError),
    ):
        await engine.generate(_request())

    # paid-cloud was never dialed
    assert fakes["paid-cloud"].call_count == 0
    # skip-paid-provider INFO: one per blocked provider (preserved behavior)
    assert len(_skip_paid_logs(caplog)) == 1
    # chain-paid-gate-blocked WARN: one aggregate line (new v0.6-C behavior)
    paid_warns = _paid_gate_logs(caplog)
    assert len(paid_warns) == 1
    rec = paid_warns[0]
    assert rec.levelname == "WARNING"
    assert rec.profile == "default"
    assert rec.blocked_providers == ["paid-cloud"]
    assert "ALLOW_PAID" in rec.hint


@pytest.mark.asyncio
async def test_all_paid_chain_reports_every_blocked_provider(
    basic_config: CodeRouterConfig,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Multi-paid chain: ``blocked_providers`` lists them all, chain order."""
    # Add a second paid provider and build a chain of both
    basic_config.providers.append(
        type(basic_config.providers[0])(
            name="paid-cloud-2",
            base_url="https://api.example.com/v1",
            model="fancy-model",
            paid=True,
            capabilities=basic_config.providers[2].capabilities,
        )
    )
    basic_config.profiles[0].providers = ["paid-cloud", "paid-cloud-2"]
    fakes = {
        "local": FakeAdapter(basic_config.provider_by_name("local")),
        "free-cloud": FakeAdapter(basic_config.provider_by_name("free-cloud")),
        "paid-cloud": FakeAdapter(basic_config.provider_by_name("paid-cloud")),
        "paid-cloud-2": FakeAdapter(basic_config.provider_by_name("paid-cloud-2")),
    }
    engine = _engine_with(basic_config, fakes)

    with (
        caplog.at_level(logging.WARNING, logger="coderouter"),
        pytest.raises(NoProvidersAvailableError),
    ):
        await engine.generate(_request())

    paid_warns = _paid_gate_logs(caplog)
    assert len(paid_warns) == 1
    assert paid_warns[0].blocked_providers == ["paid-cloud", "paid-cloud-2"]


# ----------------------------------------------------------------------
# Negative cases — warn must NOT fire
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mixed_chain_with_surviving_free_does_not_warn(
    basic_config: CodeRouterConfig,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Free provider present → paid gate did not empty the chain.

    Even if the free provider goes on to fail (retryable), the paid gate
    warn stays silent because the chain was exercised — it's the normal
    ``provider-failed`` lane that carries the diagnosis.
    """
    # default chain is local → free-cloud → paid-cloud; local and free fail
    fakes = {
        "local": FakeAdapter(
            basic_config.provider_by_name("local"),
            fail_with=AdapterError("down", provider="local", retryable=True),
        ),
        "free-cloud": FakeAdapter(
            basic_config.provider_by_name("free-cloud"),
            fail_with=AdapterError("rate", provider="free-cloud", retryable=True),
        ),
        "paid-cloud": FakeAdapter(basic_config.provider_by_name("paid-cloud")),
    }
    engine = _engine_with(basic_config, fakes)

    with (
        caplog.at_level(logging.WARNING, logger="coderouter"),
        pytest.raises(NoProvidersAvailableError),
    ):
        await engine.generate(_request())

    # skip-paid-provider INFO still fires (paid-cloud was filtered)
    # but chain-paid-gate-blocked must NOT — the chain was tried.
    assert _paid_gate_logs(caplog) == []


@pytest.mark.asyncio
async def test_allow_paid_true_does_not_warn(
    basic_config: CodeRouterConfig,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """ALLOW_PAID=true → no provider is blocked, warn never considered."""
    basic_config.allow_paid = True
    basic_config.profiles[0].providers = ["paid-cloud"]
    fakes = {
        "local": FakeAdapter(basic_config.provider_by_name("local")),
        "free-cloud": FakeAdapter(basic_config.provider_by_name("free-cloud")),
        "paid-cloud": FakeAdapter(basic_config.provider_by_name("paid-cloud"), text="from paid"),
    }
    engine = _engine_with(basic_config, fakes)

    with caplog.at_level(logging.WARNING, logger="coderouter"):
        resp = await engine.generate(_request())

    assert resp.coderouter_provider == "paid-cloud"
    assert _paid_gate_logs(caplog) == []
    assert _skip_paid_logs(caplog) == []


@pytest.mark.asyncio
async def test_unknown_provider_only_chain_does_not_warn(
    basic_config: CodeRouterConfig,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Chain of only unknown providers → skip-unknown-provider fires, but
    paid-gate warn stays silent (no providers were paid-blocked; the
    emptiness has a different root cause).
    """
    basic_config.profiles[0].providers = ["nope", "also-nope"]
    fakes = {
        "local": FakeAdapter(basic_config.provider_by_name("local")),
        "free-cloud": FakeAdapter(basic_config.provider_by_name("free-cloud")),
        "paid-cloud": FakeAdapter(basic_config.provider_by_name("paid-cloud")),
    }
    engine = _engine_with(basic_config, fakes)

    with (
        caplog.at_level(logging.WARNING, logger="coderouter"),
        pytest.raises(NoProvidersAvailableError),
    ):
        await engine.generate(_request())

    assert _paid_gate_logs(caplog) == []


# ----------------------------------------------------------------------
# Coverage across all 4 engine entry points
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_streaming_path_also_fires_warn(
    basic_config: CodeRouterConfig,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``engine.stream()`` resolves the chain with the same helper as
    ``generate``; warn must surface there too.
    """
    basic_config.profiles[0].providers = ["paid-cloud"]
    fakes = {
        "local": FakeAdapter(basic_config.provider_by_name("local")),
        "free-cloud": FakeAdapter(basic_config.provider_by_name("free-cloud")),
        "paid-cloud": FakeAdapter(basic_config.provider_by_name("paid-cloud")),
    }
    engine = _engine_with(basic_config, fakes)

    req = _request()
    req.stream = True
    with (
        caplog.at_level(logging.WARNING, logger="coderouter"),
        pytest.raises(NoProvidersAvailableError),
    ):
        async for _ in engine.stream(req):
            pass

    paid_warns = _paid_gate_logs(caplog)
    assert len(paid_warns) == 1
    assert paid_warns[0].blocked_providers == ["paid-cloud"]


@pytest.mark.asyncio
async def test_generate_anthropic_path_also_fires_warn(
    basic_config: CodeRouterConfig,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Anthropic-shaped non-streaming entry point — warn should surface
    because ``_resolve_anthropic_chain`` delegates to ``_resolve_chain``.
    """
    basic_config.profiles[0].providers = ["paid-cloud"]
    fakes = {
        "local": FakeAdapter(basic_config.provider_by_name("local")),
        "free-cloud": FakeAdapter(basic_config.provider_by_name("free-cloud")),
        "paid-cloud": FakeAdapter(basic_config.provider_by_name("paid-cloud")),
    }
    engine = _engine_with(basic_config, fakes)

    with (
        caplog.at_level(logging.WARNING, logger="coderouter"),
        pytest.raises(NoProvidersAvailableError),
    ):
        await engine.generate_anthropic(_anthropic_request())

    assert len(_paid_gate_logs(caplog)) == 1


@pytest.mark.asyncio
async def test_stream_anthropic_path_also_fires_warn(
    basic_config: CodeRouterConfig,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Anthropic-shaped streaming entry point — same expectation."""
    basic_config.profiles[0].providers = ["paid-cloud"]
    fakes = {
        "local": FakeAdapter(basic_config.provider_by_name("local")),
        "free-cloud": FakeAdapter(basic_config.provider_by_name("free-cloud")),
        "paid-cloud": FakeAdapter(basic_config.provider_by_name("paid-cloud")),
    }
    engine = _engine_with(basic_config, fakes)

    with (
        caplog.at_level(logging.WARNING, logger="coderouter"),
        pytest.raises(NoProvidersAvailableError),
    ):
        async for _ in engine.stream_anthropic(_anthropic_request()):
            pass

    assert len(_paid_gate_logs(caplog)) == 1
