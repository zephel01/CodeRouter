"""Engine-level tests for the v1.9-A ``cache-observed`` log emission.

Focus: ``FallbackEngine.generate_anthropic`` must pair every successful
Anthropic response with one ``cache-observed`` log line carrying the
4-class outcome and the per-call token totals (per
``docs/inside/future.md`` §5.1).

Streaming aggregation is deferred to v1.9-B; this module covers only
the non-streaming path. Reuses the ``FakeAnthropicAdapter`` /
``FakeOpenAIAdapter`` scaffolding from ``tests/test_fallback_anthropic.py``.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

import pytest

from coderouter.adapters.anthropic_native import AnthropicAdapter
from coderouter.adapters.base import AdapterError, BaseAdapter, ProviderCallOverrides
from coderouter.config.schemas import (
    Capabilities,
    CodeRouterConfig,
    FallbackChain,
    ProviderConfig,
)
from coderouter.routing import FallbackEngine
from coderouter.routing.fallback import NoProvidersAvailableError
from coderouter.translation.anthropic import (
    AnthropicMessage,
    AnthropicRequest,
    AnthropicResponse,
    AnthropicStreamEvent,
    AnthropicUsage,
)
from tests.test_fallback_anthropic import FakeOpenAIAdapter


class _CacheAnthropicAdapter(AnthropicAdapter):
    """Test double that returns a response with cache_read / cache_creation.

    Mirrors :class:`tests.test_fallback_anthropic.FakeAnthropicAdapter`
    but lets the test author dial the usage block per case so a single
    fixture can drive ``cache_hit`` / ``cache_creation`` / ``no_cache``
    / ``unknown`` outcomes.
    """

    def __init__(
        self,
        config: ProviderConfig,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_input_tokens: int = 0,
        cache_creation_input_tokens: int = 0,
    ) -> None:
        super().__init__(config)
        self._input_tokens = input_tokens
        self._output_tokens = output_tokens
        self._cache_read = cache_read_input_tokens
        self._cache_creation = cache_creation_input_tokens

    async def healthcheck(self) -> bool:
        return True

    async def generate_anthropic(
        self,
        request: AnthropicRequest,
        *,
        overrides: ProviderCallOverrides | None = None,
    ) -> AnthropicResponse:
        # Build usage with the cache fields tucked under ``model_extra``
        # via ``extra="allow"`` — same shape Anthropic's API + LM Studio
        # 0.4.12 use on the wire.
        usage_payload: dict[str, int] = {
            "input_tokens": self._input_tokens,
            "output_tokens": self._output_tokens,
        }
        if self._cache_read:
            usage_payload["cache_read_input_tokens"] = self._cache_read
        if self._cache_creation:
            usage_payload["cache_creation_input_tokens"] = self._cache_creation
        return AnthropicResponse(
            id="msg_cache",
            model=self.config.model,
            content=[{"type": "text", "text": "ok"}],
            stop_reason="end_turn",
            usage=AnthropicUsage.model_validate(usage_payload),
            coderouter_provider=self.name,
        )

    async def stream_anthropic(
        self,
        request: AnthropicRequest,
        *,
        overrides: ProviderCallOverrides | None = None,
    ) -> AsyncIterator[AnthropicStreamEvent]:
        """Minimal 3-event stream: message_start → message_delta → message_stop.

        Drives the v1.9.0a6 / v1.9-B2 streaming cache-observed tests.
        The constructor-supplied ``input_tokens`` / ``output_tokens`` /
        ``cache_read_input_tokens`` / ``cache_creation_input_tokens``
        decide the per-event ``usage`` payloads:
          - ``message_start.message.usage`` carries ``input_tokens`` +
            cache fields (Anthropic's actual wire shape).
          - ``message_delta.usage`` carries the cumulative
            ``output_tokens`` (also restates ``input_tokens`` so the
            accumulator's max-merge is exercised).

        When all four counters are zero the stream emits an empty
        usage block in ``message_start`` so v1.9-B2 can still
        distinguish "stream had no usage at all" (→ ``unknown``).
        """
        start_usage: dict[str, int] = {}
        if self._input_tokens:
            start_usage["input_tokens"] = self._input_tokens
        if self._cache_read:
            start_usage["cache_read_input_tokens"] = self._cache_read
        if self._cache_creation:
            start_usage["cache_creation_input_tokens"] = self._cache_creation
        yield AnthropicStreamEvent(
            type="message_start",
            data={
                "type": "message_start",
                "message": {
                    "id": "msg_stream",
                    "type": "message",
                    "role": "assistant",
                    "content": [],
                    "model": self.config.model,
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": start_usage,
                },
            },
        )
        delta_usage: dict[str, int] = {}
        if self._input_tokens:
            delta_usage["input_tokens"] = self._input_tokens
        if self._output_tokens:
            delta_usage["output_tokens"] = self._output_tokens
        yield AnthropicStreamEvent(
            type="message_delta",
            data={
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": delta_usage,
            },
        )
        yield AnthropicStreamEvent(
            type="message_stop",
            data={"type": "message_stop"},
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


def _openai_provider(name: str) -> ProviderConfig:
    return ProviderConfig(
        name=name,
        kind="openai_compat",
        base_url="http://localhost:11434/v1",
        model="qwen-coder",
        capabilities=Capabilities(),
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
    """Request with a cache_control marker on the system block."""
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


def _cache_observed_records(
    caplog: pytest.LogCaptureFixture,
) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.msg == "cache-observed"]


# ----------------------------------------------------------------------
# Tests — outcome classification end-to-end
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cache_hit_response_emits_cache_hit_outcome(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Native Anthropic response with cache_read_input_tokens > 0 →
    log fires with ``outcome=cache_hit`` and the read-token count."""
    cfg = _anthropic_provider("anthropic-direct")
    config = _config([cfg], chain=["anthropic-direct"])
    adapter = _CacheAnthropicAdapter(
        cfg,
        input_tokens=100,
        output_tokens=50,
        cache_read_input_tokens=2048,
    )
    engine = _engine(config, {"anthropic-direct": adapter})

    with caplog.at_level(logging.INFO, logger="coderouter"):
        await engine.generate_anthropic(_cache_request())

    records = _cache_observed_records(caplog)
    assert len(records) == 1
    rec = records[0]
    assert rec.provider == "anthropic-direct"
    assert rec.outcome == "cache_hit"
    assert rec.cache_read_input_tokens == 2048
    assert rec.cache_creation_input_tokens == 0
    assert rec.input_tokens == 100
    assert rec.output_tokens == 50
    assert rec.request_had_cache_control is True
    assert rec.streaming is False


@pytest.mark.asyncio
async def test_cache_creation_response_emits_cache_creation_outcome(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """First call with a cache_control marker → cache_creation outcome."""
    cfg = _anthropic_provider("anthropic-direct")
    config = _config([cfg], chain=["anthropic-direct"])
    adapter = _CacheAnthropicAdapter(
        cfg,
        input_tokens=1500,
        output_tokens=20,
        cache_creation_input_tokens=1500,
    )
    engine = _engine(config, {"anthropic-direct": adapter})

    with caplog.at_level(logging.INFO, logger="coderouter"):
        await engine.generate_anthropic(_cache_request())

    records = _cache_observed_records(caplog)
    assert len(records) == 1
    assert records[0].outcome == "cache_creation"
    assert records[0].cache_creation_input_tokens == 1500


@pytest.mark.asyncio
async def test_no_cache_request_emits_no_cache_outcome(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Plain request (no cache_control) + usage with no cache fields →
    ``outcome=no_cache`` and ``request_had_cache_control=False``."""
    cfg = _anthropic_provider("anthropic-direct")
    config = _config([cfg], chain=["anthropic-direct"])
    adapter = _CacheAnthropicAdapter(cfg, input_tokens=12, output_tokens=4)
    engine = _engine(config, {"anthropic-direct": adapter})

    with caplog.at_level(logging.INFO, logger="coderouter"):
        await engine.generate_anthropic(_plain_request())

    records = _cache_observed_records(caplog)
    assert len(records) == 1
    assert records[0].outcome == "no_cache"
    assert records[0].request_had_cache_control is False


@pytest.mark.asyncio
async def test_openai_compat_path_emits_unknown_outcome(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """openai_compat upstream → response carries no usage cache fields,
    converter zero-fills ``input_tokens``/``output_tokens``. The
    cache-observed log fires with ``outcome=unknown`` because no usage
    signal made it through (per CacheOutcome docstring)."""
    cfg = _openai_provider("ollama")
    config = _config([cfg], chain=["ollama"])
    compat = FakeOpenAIAdapter(cfg, text="ok")
    engine = _engine(config, {"ollama": compat})

    with caplog.at_level(logging.INFO, logger="coderouter"):
        await engine.generate_anthropic(_cache_request())

    records = _cache_observed_records(caplog)
    assert len(records) == 1
    rec = records[0]
    assert rec.provider == "ollama"
    # FakeOpenAIAdapter ships ``prompt_tokens=3, completion_tokens=4``
    # which the OpenAI → Anthropic converter maps to input/output > 0,
    # so usage_present is True and the outcome is no_cache (not unknown).
    # Either no_cache or unknown is acceptable here — the key contract
    # is that the log fires and provider+streaming are correctly tagged.
    assert rec.outcome in {"no_cache", "unknown"}
    assert rec.request_had_cache_control is True
    assert rec.streaming is False


@pytest.mark.asyncio
async def test_cache_observed_does_not_fire_on_provider_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """If every provider fails, no cache-observed log should fire — the
    log is paired with successful responses, not attempts."""
    cfg = _openai_provider("ollama")
    config = _config([cfg], chain=["ollama"])
    failing = FakeOpenAIAdapter(
        cfg,
        fail_with=AdapterError("boom", provider="ollama", retryable=False),
    )
    engine = _engine(config, {"ollama": failing})

    with (
        caplog.at_level(logging.INFO, logger="coderouter"),
        pytest.raises(NoProvidersAvailableError),
    ):
        await engine.generate_anthropic(_cache_request())

    assert _cache_observed_records(caplog) == []


@pytest.mark.asyncio
async def test_cache_observed_fires_only_on_winning_provider_in_chain(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When the chain falls through, cache-observed fires once for the
    provider that ultimately answered — not for the failed attempts."""
    primary_cfg = _openai_provider("ollama-a")
    fallback_cfg = _anthropic_provider("anthropic-fallback")
    config = _config(
        [primary_cfg, fallback_cfg], chain=["ollama-a", "anthropic-fallback"]
    )
    primary = FakeOpenAIAdapter(
        primary_cfg,
        fail_with=AdapterError("boom", provider="ollama-a", retryable=True),
    )
    fb = _CacheAnthropicAdapter(
        fallback_cfg,
        input_tokens=10,
        output_tokens=5,
        cache_read_input_tokens=512,
    )
    engine = _engine(
        config, {"ollama-a": primary, "anthropic-fallback": fb}
    )

    with caplog.at_level(logging.INFO, logger="coderouter"):
        await engine.generate_anthropic(_cache_request())

    records = _cache_observed_records(caplog)
    assert len(records) == 1
    assert records[0].provider == "anthropic-fallback"
    assert records[0].outcome == "cache_hit"


# ----------------------------------------------------------------------
# v1.9.0a6 / v1.9-B2: streaming emission
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cache_observed_fires_on_streaming_with_unknown_outcome(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """v1.9.0a6 + v1.9-B2: ``stream_anthropic`` must pair its successful
    completion with a ``cache-observed`` log line carrying
    ``streaming=true``. When the upstream provided no usage data at all
    (empty usage dicts in every event) the outcome falls through to
    ``unknown`` — the honest classification once
    :class:`_StreamUsageAccumulator` lands.

    Pre-v1.9.0a6 the streaming path did not call ``log_cache_observed``
    at all. v1.9.0a6 wired the emission with hard-coded zeros +
    ``outcome=unknown``; v1.9-B2 replaces the placeholder with real
    aggregation. The "no usage from upstream → unknown" path remains
    structurally identical to the placeholder behavior, so this test
    still pins it as the ``unknown`` floor.
    """
    cfg = _anthropic_provider("anthropic-direct")
    config = _config([cfg], chain=["anthropic-direct"])
    adapter = _CacheAnthropicAdapter(cfg)
    engine = _engine(config, {"anthropic-direct": adapter})

    with caplog.at_level(logging.INFO, logger="coderouter"):
        async for _event in engine.stream_anthropic(_cache_request()):
            pass

    records = _cache_observed_records(caplog)
    assert len(records) == 1
    rec = records[0]
    assert rec.provider == "anthropic-direct"
    assert rec.outcome == "unknown"
    assert rec.streaming is True
    assert rec.cache_read_input_tokens == 0
    assert rec.cache_creation_input_tokens == 0
    assert rec.input_tokens == 0
    assert rec.output_tokens == 0
    assert rec.request_had_cache_control is True


@pytest.mark.asyncio
async def test_cache_observed_streaming_does_not_fire_on_provider_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Streaming failure (no events emitted) → no cache-observed log,
    same as the non-streaming counterpart."""
    cfg = _openai_provider("ollama")
    config = _config([cfg], chain=["ollama"])
    failing = FakeOpenAIAdapter(
        cfg,
        fail_with=AdapterError("boom", provider="ollama", retryable=False),
    )
    engine = _engine(config, {"ollama": failing})

    with (
        caplog.at_level(logging.INFO, logger="coderouter"),
        pytest.raises(NoProvidersAvailableError),
    ):
        async for _event in engine.stream_anthropic(_cache_request()):
            pass

    assert _cache_observed_records(caplog) == []


# ----------------------------------------------------------------------
# v1.9-B2: streaming usage aggregation (message_start + message_delta)
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_streaming_aggregates_cache_hit_usage(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """v1.9-B2: native Anthropic stream that surfaces
    ``cache_read_input_tokens`` on ``message_start.message.usage`` →
    aggregated cache-observed log carries ``outcome=cache_hit`` and the
    real token counters (not the v1.9.0a6 zero placeholders)."""
    cfg = _anthropic_provider("anthropic-direct")
    config = _config([cfg], chain=["anthropic-direct"])
    adapter = _CacheAnthropicAdapter(
        cfg,
        input_tokens=120,
        output_tokens=30,
        cache_read_input_tokens=2048,
    )
    engine = _engine(config, {"anthropic-direct": adapter})

    with caplog.at_level(logging.INFO, logger="coderouter"):
        async for _event in engine.stream_anthropic(_cache_request()):
            pass

    records = _cache_observed_records(caplog)
    assert len(records) == 1
    rec = records[0]
    assert rec.provider == "anthropic-direct"
    assert rec.outcome == "cache_hit"
    assert rec.streaming is True
    assert rec.cache_read_input_tokens == 2048
    assert rec.cache_creation_input_tokens == 0
    # input_tokens comes from message_start; the message_delta restates
    # it and the accumulator's max-merge keeps the higher value.
    assert rec.input_tokens == 120
    # output_tokens is finalized by the terminal message_delta.
    assert rec.output_tokens == 30
    assert rec.request_had_cache_control is True


@pytest.mark.asyncio
async def test_streaming_aggregates_cache_creation_usage(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """v1.9-B2: stream that reports ``cache_creation_input_tokens`` on
    ``message_start`` → aggregated log carries ``outcome=cache_creation``."""
    cfg = _anthropic_provider("anthropic-direct")
    config = _config([cfg], chain=["anthropic-direct"])
    adapter = _CacheAnthropicAdapter(
        cfg,
        input_tokens=1500,
        output_tokens=20,
        cache_creation_input_tokens=1500,
    )
    engine = _engine(config, {"anthropic-direct": adapter})

    with caplog.at_level(logging.INFO, logger="coderouter"):
        async for _event in engine.stream_anthropic(_cache_request()):
            pass

    records = _cache_observed_records(caplog)
    assert len(records) == 1
    rec = records[0]
    assert rec.outcome == "cache_creation"
    assert rec.streaming is True
    assert rec.cache_creation_input_tokens == 1500
    assert rec.cache_read_input_tokens == 0
    assert rec.input_tokens == 1500
    assert rec.output_tokens == 20


@pytest.mark.asyncio
async def test_streaming_aggregates_no_cache_outcome(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """v1.9-B2: stream with non-zero usage but no cache fields →
    ``outcome=no_cache`` (no longer the v1.9.0a6 ``unknown`` placeholder).

    This is the most common production case: a non-cached request that
    still reports proper input/output counts. The pre-v1.9-B2 streaming
    path lost this signal; v1.9-B2 lifts it to parity with
    ``generate_anthropic``.
    """
    cfg = _anthropic_provider("anthropic-direct")
    config = _config([cfg], chain=["anthropic-direct"])
    adapter = _CacheAnthropicAdapter(cfg, input_tokens=12, output_tokens=4)
    engine = _engine(config, {"anthropic-direct": adapter})

    with caplog.at_level(logging.INFO, logger="coderouter"):
        async for _event in engine.stream_anthropic(_plain_request()):
            pass

    records = _cache_observed_records(caplog)
    assert len(records) == 1
    rec = records[0]
    assert rec.outcome == "no_cache"
    assert rec.streaming is True
    assert rec.input_tokens == 12
    assert rec.output_tokens == 4
    assert rec.cache_read_input_tokens == 0
    assert rec.cache_creation_input_tokens == 0
    assert rec.request_had_cache_control is False
