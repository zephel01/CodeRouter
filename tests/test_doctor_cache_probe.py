"""Unit tests for v1.9-B `_probe_cache` doctor probe.

Round-trip verification of Anthropic prompt caching:
  1st call → cache_creation_input_tokens > 0
  2nd call → cache_read_input_tokens > 0  → OK

Probe gate is intentionally tight: only fires when the
``cache_control`` flag has an explicit positive declaration (registry
``cache_control: true`` OR per-provider ``capabilities.prompt_cache:
true``). Undeclared anthropic-kind providers SKIP — running 2 paid
HTTP calls against an unverified model would be wasteful.
"""

from __future__ import annotations

from typing import Any

import pytest
from pytest_httpx import HTTPXMock

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
from coderouter.doctor import ProbeVerdict, check_model

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _anthropic_provider(
    *,
    model: str = "claude-opus-4-8",
    api_key_env: str | None = None,
    prompt_cache: bool = False,
) -> ProviderConfig:
    return ProviderConfig(
        name="anth",
        kind="anthropic",
        base_url="https://api.anthropic.com",
        model=model,
        api_key_env=api_key_env,
        capabilities=Capabilities(prompt_cache=prompt_cache),
    )


def _openai_provider() -> ProviderConfig:
    return ProviderConfig(
        name="oa",
        kind="openai_compat",
        base_url="http://localhost:11434/v1",
        model="qwen-coder",
    )


def _config_for(providers: list[ProviderConfig]) -> CodeRouterConfig:
    return CodeRouterConfig(
        providers=providers,
        profiles=[FallbackChain(name="default", providers=[providers[0].name])],
    )


def _registry_with_cache_control_true() -> CapabilityRegistry:
    return CapabilityRegistry(
        [
            CapabilityRule(
                match="claude-opus-4-*",
                kind="anthropic",
                capabilities=RegistryCapabilities(cache_control=True),
            )
        ]
    )


def _registry_empty() -> CapabilityRegistry:
    return CapabilityRegistry([])


def _anthropic_ok_response(
    *,
    cache_read: int = 0,
    cache_creation: int = 0,
    input_tokens: int = 100,
    output_tokens: int = 4,
) -> dict[str, Any]:
    """Build an Anthropic response shape with the requested usage fields."""
    usage: dict[str, int] = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }
    if cache_read:
        usage["cache_read_input_tokens"] = cache_read
    if cache_creation:
        usage["cache_creation_input_tokens"] = cache_creation
    return {
        "id": "msg_probe",
        "type": "message",
        "role": "assistant",
        "model": "probe",
        "content": [{"type": "text", "text": "hi"}],
        "stop_reason": "end_turn",
        "usage": usage,
    }


def _add_basic_anthropic_mocks(httpx_mock: HTTPXMock) -> None:
    """Mock the auth + tool_calls + thinking probes (3 calls total).

    Each test that exercises the cache probe needs the predecessor
    probes to succeed first since the orchestrator runs them in
    order. None of these emit cache_read / cache_creation, so the
    cache observation logic isn't accidentally exercised by these
    mocks.
    """
    # auth + basic-chat
    httpx_mock.add_response(
        url="https://api.anthropic.com/v1/messages",
        method="POST",
        status_code=200,
        json=_anthropic_ok_response(),
    )
    # tool_calls probe
    httpx_mock.add_response(
        url="https://api.anthropic.com/v1/messages",
        method="POST",
        status_code=200,
        json=_anthropic_ok_response(),
    )
    # thinking probe
    httpx_mock.add_response(
        url="https://api.anthropic.com/v1/messages",
        method="POST",
        status_code=200,
        json=_anthropic_ok_response(),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cache_probe_skipped_for_openai_compat(
    httpx_mock: HTTPXMock,
) -> None:
    """openai_compat → SKIP regardless of registry / explicit flags.

    The OpenAI Chat Completions wire has no equivalent for cache_control,
    so the round-trip can't be measured — running the probe would just
    produce a confusing report.
    """
    provider = _openai_provider()
    # Predecessor mocks (auth, num_ctx, tool_calls, reasoning-leak, streaming).
    httpx_mock.add_response(
        url="http://localhost:11434/v1/chat/completions",
        method="POST",
        status_code=200,
        json={
            "id": "x",
            "object": "chat.completion",
            "model": "qwen-coder",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "PONG"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "total_tokens": 2,
            },
        },
        is_reusable=True,
    )
    # The streaming probe in v1.0-C uses a different endpoint shape; mark
    # the openai_compat handler reusable above so all probes pass.

    report = await check_model(
        _config_for([provider]),
        provider.name,
        registry=_registry_empty(),
    )
    cache = next(r for r in report.results if r.name == "cache")
    assert cache.verdict == ProbeVerdict.SKIP
    assert "Anthropic-shaped" in cache.detail or "kind: anthropic" in cache.detail


@pytest.mark.asyncio
async def test_cache_probe_skipped_when_no_explicit_declaration(
    httpx_mock: HTTPXMock,
) -> None:
    """Anthropic-kind without registry/explicit declaration → SKIP.

    The unified ``provider_supports_cache_control`` would say True via
    the kind fallback, but the doctor probe gate is tighter: it only
    runs against an explicit positive declaration to avoid spending 2
    paid calls on an unverified model.
    """
    provider = _anthropic_provider(api_key_env="CR_TEST_ANTH_KEY")
    _add_basic_anthropic_mocks(httpx_mock)

    report = await check_model(
        _config_for([provider]),
        provider.name,
        registry=_registry_empty(),
    )
    cache = next(r for r in report.results if r.name == "cache")
    assert cache.verdict == ProbeVerdict.SKIP
    assert "no explicit" in cache.detail.lower() or "opt-in" in cache.detail.lower()


@pytest.mark.asyncio
async def test_cache_probe_ok_when_round_trip_succeeds(
    httpx_mock: HTTPXMock,
) -> None:
    """1st call writes (creation>0), 2nd call hits (read>0) → OK.

    This is the happy path that confirms cache_control plumbing is
    intact end-to-end through CodeRouter to the upstream.
    """
    provider = _anthropic_provider(api_key_env="CR_TEST_ANTH_KEY")
    _add_basic_anthropic_mocks(httpx_mock)
    # Cache probe 1st call: cache_creation > 0
    httpx_mock.add_response(
        url="https://api.anthropic.com/v1/messages",
        method="POST",
        status_code=200,
        json=_anthropic_ok_response(cache_creation=1900),
    )
    # Cache probe 2nd call: cache_read > 0
    httpx_mock.add_response(
        url="https://api.anthropic.com/v1/messages",
        method="POST",
        status_code=200,
        json=_anthropic_ok_response(cache_read=1900),
    )

    report = await check_model(
        _config_for([provider]),
        provider.name,
        registry=_registry_with_cache_control_true(),
    )
    cache = next(r for r in report.results if r.name == "cache")
    assert cache.verdict == ProbeVerdict.OK
    assert "round-trip" in cache.detail
    assert "creation=1900" in cache.detail
    assert "read=1900" in cache.detail


@pytest.mark.asyncio
async def test_cache_probe_needs_tuning_when_2nd_call_does_not_hit(
    httpx_mock: HTTPXMock,
) -> None:
    """1st call writes but 2nd call doesn't read → NEEDS_TUNING.

    Indicates a TTL shorter than the test interval, or upstream-side
    cache key mismatch. The probe surfaces it as actionable rather
    than failing silently."""
    provider = _anthropic_provider(api_key_env="CR_TEST_ANTH_KEY")
    _add_basic_anthropic_mocks(httpx_mock)
    httpx_mock.add_response(
        url="https://api.anthropic.com/v1/messages",
        method="POST",
        status_code=200,
        json=_anthropic_ok_response(cache_creation=1900),
    )
    httpx_mock.add_response(
        url="https://api.anthropic.com/v1/messages",
        method="POST",
        status_code=200,
        json=_anthropic_ok_response(),  # no cache fields
    )

    report = await check_model(
        _config_for([provider]),
        provider.name,
        registry=_registry_with_cache_control_true(),
    )
    cache = next(r for r in report.results if r.name == "cache")
    assert cache.verdict == ProbeVerdict.NEEDS_TUNING
    assert "TTL" in cache.detail or "did not hit" in cache.detail


@pytest.mark.asyncio
async def test_cache_probe_needs_tuning_when_no_creation_observed(
    httpx_mock: HTTPXMock,
) -> None:
    """Neither call wrote cache → NEEDS_TUNING with diagnostic note.

    Suggests the upstream silently ignores cache_control despite
    advertising support. Common when a newer Anthropic-compat
    implementation regresses on the cache field.
    """
    provider = _anthropic_provider(api_key_env="CR_TEST_ANTH_KEY")
    _add_basic_anthropic_mocks(httpx_mock)
    httpx_mock.add_response(
        url="https://api.anthropic.com/v1/messages",
        method="POST",
        status_code=200,
        json=_anthropic_ok_response(),  # no cache fields
        is_reusable=True,
    )

    report = await check_model(
        _config_for([provider]),
        provider.name,
        registry=_registry_with_cache_control_true(),
    )
    cache = next(r for r in report.results if r.name == "cache")
    assert cache.verdict == ProbeVerdict.NEEDS_TUNING
    assert "no cache_creation" in cache.detail or "does not honor" in cache.detail.lower()


@pytest.mark.asyncio
async def test_cache_probe_explicit_prompt_cache_flag_enables_probe(
    httpx_mock: HTTPXMock,
) -> None:
    """``providers.yaml capabilities.prompt_cache: true`` is enough to fire
    the probe even without a registry rule."""
    provider = _anthropic_provider(
        api_key_env="CR_TEST_ANTH_KEY",
        prompt_cache=True,
    )
    _add_basic_anthropic_mocks(httpx_mock)
    httpx_mock.add_response(
        url="https://api.anthropic.com/v1/messages",
        method="POST",
        status_code=200,
        json=_anthropic_ok_response(cache_creation=1900),
    )
    httpx_mock.add_response(
        url="https://api.anthropic.com/v1/messages",
        method="POST",
        status_code=200,
        json=_anthropic_ok_response(cache_read=1900),
    )

    report = await check_model(
        _config_for([provider]),
        provider.name,
        registry=_registry_empty(),  # registry has no cache_control rule
    )
    cache = next(r for r in report.results if r.name == "cache")
    assert cache.verdict == ProbeVerdict.OK


@pytest.mark.asyncio
async def test_cache_probe_skipped_on_first_call_upstream_error(
    httpx_mock: HTTPXMock,
) -> None:
    """1st call returns 5xx → SKIP (transient; no second call attempted)."""
    provider = _anthropic_provider(api_key_env="CR_TEST_ANTH_KEY")
    _add_basic_anthropic_mocks(httpx_mock)
    httpx_mock.add_response(
        url="https://api.anthropic.com/v1/messages",
        method="POST",
        status_code=502,
        text="upstream gone",
    )

    report = await check_model(
        _config_for([provider]),
        provider.name,
        registry=_registry_with_cache_control_true(),
    )
    cache = next(r for r in report.results if r.name == "cache")
    assert cache.verdict == ProbeVerdict.SKIP
    assert "1st call" in cache.detail


@pytest.mark.asyncio
async def test_cache_probe_skipped_when_auth_fails(
    httpx_mock: HTTPXMock,
) -> None:
    """Auth probe failure → cache probe SKIPS like the others."""
    provider = _anthropic_provider(api_key_env="CR_TEST_BAD_KEY")
    httpx_mock.add_response(
        url="https://api.anthropic.com/v1/messages",
        method="POST",
        status_code=401,
        text="unauthorized",
    )

    report = await check_model(
        _config_for([provider]),
        provider.name,
        registry=_registry_with_cache_control_true(),
    )
    cache = next(r for r in report.results if r.name == "cache")
    assert cache.verdict == ProbeVerdict.SKIP
    assert "auth probe" in cache.detail.lower()
