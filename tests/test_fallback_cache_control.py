"""Engine-level tests for the v0.5-B cache_control observability gate.

Focus: FallbackEngine.generate_anthropic / stream_anthropic with
requests that carry ``cache_control`` markers. Unlike v0.5-A (thinking),
cache_control handling is observability-only:

  1. Chain order is NOT changed by cache_control — the user's ordering
     (a latency / cost decision) is always respected.
  2. A ``capability-degraded`` log with ``reason: "translation-lossy"``
     fires when handing a cache_control request to an openai_compat
     provider (the marker is silently lost during translation).
  3. No log fires for anthropic-kind providers (marker passes through).
  4. No log fires for plain requests without cache_control.
  5. Streaming path mirrors non-streaming behavior.

Reuses the FakeAnthropicAdapter / FakeOpenAIAdapter scaffolding from
tests/test_fallback_anthropic.py.
"""

from __future__ import annotations

import logging

import pytest

from coderouter.adapters.base import AdapterError, BaseAdapter
from coderouter.config.schemas import (
    Capabilities,
    CodeRouterConfig,
    FallbackChain,
    ProviderConfig,
)
from coderouter.routing import FallbackEngine
from coderouter.translation.anthropic import (
    AnthropicMessage,
    AnthropicRequest,
)
from tests.test_fallback_anthropic import (
    FakeAnthropicAdapter,
    FakeOpenAIAdapter,
)

# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _anthropic_provider(name: str) -> ProviderConfig:
    return ProviderConfig(
        name=name,
        kind="anthropic",
        base_url="https://api.anthropic.com",
        model="claude-sonnet-4-6",
        api_key_env="ANTHROPIC_API_KEY",
    )


def _openai_provider(name: str, *, prompt_cache: bool = False) -> ProviderConfig:
    return ProviderConfig(
        name=name,
        kind="openai_compat",
        base_url="http://localhost:11434/v1",
        model="qwen-coder",
        capabilities=Capabilities(prompt_cache=prompt_cache),
    )


def _config(providers: list[ProviderConfig], chain: list[str]) -> CodeRouterConfig:
    return CodeRouterConfig(
        allow_paid=False,
        default_profile="default",
        providers=providers,
        profiles=[FallbackChain(name="default", providers=chain)],
    )


def _engine(config: CodeRouterConfig, adapters: dict[str, BaseAdapter]) -> FallbackEngine:
    engine = FallbackEngine.__new__(FallbackEngine)
    engine.config = config
    engine._adapters = adapters  # type: ignore[attr-defined]
    return engine


def _cache_request() -> AnthropicRequest:
    """Request carrying a cache_control marker on the system block."""
    return AnthropicRequest.model_validate(
        {
            "max_tokens": 64,
            "system": [
                {
                    "type": "text",
                    "text": "long reusable system prompt",
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "messages": [{"role": "user", "content": "hi"}],
        }
    )


def _plain_request() -> AnthropicRequest:
    return AnthropicRequest(
        max_tokens=64,
        messages=[AnthropicMessage(role="user", content="hi")],
    )


def _degraded_logs(
    caplog: pytest.LogCaptureFixture, *, reason: str | None = None
) -> list[logging.LogRecord]:
    records = [r for r in caplog.records if r.msg == "capability-degraded"]
    if reason is not None:
        records = [r for r in records if getattr(r, "reason", None) == reason]
    return records


# ----------------------------------------------------------------------
# Non-streaming path
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cache_control_request_on_openai_compat_logs_translation_lossy(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """cache_control request hits an openai_compat provider → the
    marker is silently dropped during translation; the engine must emit
    a capability-degraded log tagged with reason=translation-lossy."""
    compat_cfg = _openai_provider("ollama")
    config = _config([compat_cfg], chain=["ollama"])
    compat = FakeOpenAIAdapter(compat_cfg, text="ok")
    engine = _engine(config, {"ollama": compat})

    with caplog.at_level(logging.INFO, logger="coderouter"):
        await engine.generate_anthropic(_cache_request())

    records = _degraded_logs(caplog, reason="translation-lossy")
    assert len(records) == 1
    assert records[0].provider == "ollama"
    assert records[0].dropped == ["cache_control"]


@pytest.mark.asyncio
async def test_cache_control_request_on_anthropic_kind_does_not_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Native anthropic passthrough preserves cache_control — no
    translation-lossy log should fire."""
    anth_cfg = _anthropic_provider("sonnet-4-6")
    config = _config([anth_cfg], chain=["sonnet-4-6"])
    anth = FakeAnthropicAdapter(anth_cfg, text="ok")
    engine = _engine(config, {"sonnet-4-6": anth})

    with caplog.at_level(logging.INFO, logger="coderouter"):
        await engine.generate_anthropic(_cache_request())

    assert _degraded_logs(caplog, reason="translation-lossy") == []


@pytest.mark.asyncio
async def test_plain_request_on_openai_compat_does_not_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Gate only fires for requests carrying cache_control. A plain
    request through an openai_compat provider must not produce a
    translation-lossy log."""
    compat_cfg = _openai_provider("ollama")
    config = _config([compat_cfg], chain=["ollama"])
    compat = FakeOpenAIAdapter(compat_cfg, text="ok")
    engine = _engine(config, {"ollama": compat})

    with caplog.at_level(logging.INFO, logger="coderouter"):
        await engine.generate_anthropic(_plain_request())

    assert _degraded_logs(caplog, reason="translation-lossy") == []


@pytest.mark.asyncio
async def test_cache_control_does_not_reorder_chain(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Key v0.5-B design decision: cache_control does NOT reorder the
    chain. User-declared ordering (latency / cost) outweighs cache-hit
    savings. The openai_compat provider listed first still goes first
    even when an anthropic provider (which would preserve the marker)
    is listed later."""
    compat_cfg = _openai_provider("ollama")
    anth_cfg = _anthropic_provider("sonnet-4-6")
    config = _config([compat_cfg, anth_cfg], chain=["ollama", "sonnet-4-6"])
    compat = FakeOpenAIAdapter(compat_cfg, text="compat first")
    anth = FakeAnthropicAdapter(anth_cfg, text="anthropic second")
    engine = _engine(config, {"ollama": compat, "sonnet-4-6": anth})

    with caplog.at_level(logging.INFO, logger="coderouter"):
        resp = await engine.generate_anthropic(_cache_request())

    # Ollama answered (order preserved), anthropic never consulted.
    assert resp.coderouter_provider == "ollama"
    assert anth.generate_calls == []
    # And the lossy log fired exactly once, on the first provider.
    records = _degraded_logs(caplog, reason="translation-lossy")
    assert len(records) == 1
    assert records[0].provider == "ollama"


@pytest.mark.asyncio
async def test_explicit_prompt_cache_flag_suppresses_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """YAML escape hatch: setting `capabilities.prompt_cache: true` on
    an openai_compat provider declares the upstream preserves the
    marker. The engine must treat it as capable and not log a drop."""
    compat_cfg = _openai_provider("future-compat", prompt_cache=True)
    config = _config([compat_cfg], chain=["future-compat"])
    compat = FakeOpenAIAdapter(compat_cfg, text="ok")
    engine = _engine(config, {"future-compat": compat})

    with caplog.at_level(logging.INFO, logger="coderouter"):
        await engine.generate_anthropic(_cache_request())

    assert _degraded_logs(caplog, reason="translation-lossy") == []


@pytest.mark.asyncio
async def test_log_fires_on_each_provider_in_fallback_chain(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When the first (openai_compat) provider fails and the chain
    falls through to a second openai_compat provider, the gate must
    emit a log for EACH lossy provider that gets tried (one per
    actual handoff)."""
    compat_a_cfg = _openai_provider("ollama-a")
    compat_b_cfg = _openai_provider("ollama-b")
    config = _config([compat_a_cfg, compat_b_cfg], chain=["ollama-a", "ollama-b"])
    compat_a = FakeOpenAIAdapter(
        compat_a_cfg,
        fail_with=AdapterError("boom", provider="ollama-a", retryable=True),
    )
    compat_b = FakeOpenAIAdapter(compat_b_cfg, text="ok")
    engine = _engine(config, {"ollama-a": compat_a, "ollama-b": compat_b})

    with caplog.at_level(logging.INFO, logger="coderouter"):
        await engine.generate_anthropic(_cache_request())

    records = _degraded_logs(caplog, reason="translation-lossy")
    providers = [r.provider for r in records]
    assert providers == ["ollama-a", "ollama-b"]


# ----------------------------------------------------------------------
# Streaming path
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_cache_control_on_openai_compat_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Streaming mirror: cache_control + openai_compat → log fires."""
    compat_cfg = _openai_provider("ollama")
    config = _config([compat_cfg], chain=["ollama"])
    compat = FakeOpenAIAdapter(compat_cfg, text="streamed")
    engine = _engine(config, {"ollama": compat})

    with caplog.at_level(logging.INFO, logger="coderouter"):
        events = [ev async for ev in engine.stream_anthropic(_cache_request())]

    assert events  # stream produced events
    records = _degraded_logs(caplog, reason="translation-lossy")
    assert len(records) == 1
    assert records[0].provider == "ollama"
    assert records[0].dropped == ["cache_control"]


@pytest.mark.asyncio
async def test_stream_cache_control_on_anthropic_does_not_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Streaming mirror: cache_control + anthropic kind → no log."""
    anth_cfg = _anthropic_provider("sonnet-4-6")
    config = _config([anth_cfg], chain=["sonnet-4-6"])
    anth = FakeAnthropicAdapter(anth_cfg, text="streamed")
    engine = _engine(config, {"sonnet-4-6": anth})

    with caplog.at_level(logging.INFO, logger="coderouter"):
        events = [ev async for ev in engine.stream_anthropic(_cache_request())]

    assert events
    assert _degraded_logs(caplog, reason="translation-lossy") == []
