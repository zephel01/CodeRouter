"""v2.17.0: ``ProviderConfig.ttft_timeout_s`` — time-to-first-token cut-off.

A streaming attempt that has not produced its first real output within the
provider's ``ttft_timeout_s`` is abandoned before any byte reaches the client,
and the chain moves on. Never applied to the last provider of a chain.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from coderouter.adapters.base import ProviderCallOverrides, TTFTTimeoutError
from coderouter.config.schemas import CodeRouterConfig, FallbackChain, ProviderConfig
from coderouter.routing import NoProvidersAvailableError
from coderouter.routing.fallback_trace import REASON_TTFT_TIMEOUT, classify_adapter_error
from coderouter.translation import AnthropicRequest, AnthropicStreamEvent
from tests.test_fallback_anthropic import (
    FakeAnthropicAdapter,
    _anth_req,
    _default_native_events,
    _engine_with_adapters,
)


class SlowAdapter(FakeAnthropicAdapter):
    """Sleeps before the first event, or between the preamble and content."""

    def __init__(
        self,
        config: ProviderConfig,
        *,
        delay_before_first: float = 0.0,
        delay_before_content: float = 0.0,
        text: str = "slow",
    ) -> None:
        super().__init__(config, text=text)
        self.delay_before_first = delay_before_first
        self.delay_before_content = delay_before_content
        self.closed = False

    async def stream_anthropic(
        self,
        request: AnthropicRequest,
        *,
        overrides: ProviderCallOverrides | None = None,
    ) -> AsyncIterator[AnthropicStreamEvent]:
        self.stream_calls.append(request)
        try:
            await asyncio.sleep(self.delay_before_first)
            events = _default_native_events(self.text, self.config.model)
            yield events[0]  # message_start
            yield events[1]  # empty content_block_start
            await asyncio.sleep(self.delay_before_content)
            for ev in events[2:]:
                yield ev
        finally:
            self.closed = True


def _config(*, ttft: float | None, empty_fallback: bool = False, n: int = 2) -> CodeRouterConfig:
    names = ["first", "second", "third"][:n]
    return CodeRouterConfig(
        allow_paid=False,
        default_profile="default",
        providers=[
            ProviderConfig(
                name=name,
                kind="anthropic",
                base_url="http://127.0.0.1:11434",
                model=f"{name}-model",
                ttft_timeout_s=ttft,
            )
            for name in names
        ],
        profiles=[
            FallbackChain(
                name="default",
                providers=names,
                empty_response_action="fallback" if empty_fallback else "off",
            )
        ],
    )


async def _collect(engine, request) -> list[AnthropicStreamEvent]:  # type: ignore[no-untyped-def]
    return [ev async for ev in engine.stream_anthropic(request)]


def _text(events: list[AnthropicStreamEvent]) -> str:
    return "".join(
        ev.data.get("delta", {}).get("text", "")
        for ev in events
        if ev.type == "content_block_delta"
    )


@pytest.mark.asyncio
async def test_slow_first_event_falls_back_to_next_provider() -> None:
    config = _config(ttft=0.05)
    slow = SlowAdapter(config.providers[0], delay_before_first=1.0, text="slow")
    fast = SlowAdapter(config.providers[1], text="fast")
    engine = _engine_with_adapters(config, {"first": slow, "second": fast})
    events = await _collect(engine, _anth_req(stream=True))
    assert _text(events) == "fast"
    assert slow.closed  # the abandoned upstream stream was unwound


@pytest.mark.asyncio
async def test_slow_content_after_preamble_falls_back_under_empty_fallback() -> None:
    config = _config(ttft=0.05, empty_fallback=True)
    slow = SlowAdapter(config.providers[0], delay_before_content=1.0, text="slow")
    fast = SlowAdapter(config.providers[1], text="fast")
    engine = _engine_with_adapters(config, {"first": slow, "second": fast})
    events = await _collect(engine, _anth_req(stream=True))
    assert _text(events) == "fast"
    # Only one message_start reached the client — the slow preamble was withheld.
    assert sum(1 for ev in events if ev.type == "message_start") == 1


@pytest.mark.asyncio
async def test_last_provider_is_never_cut_off() -> None:
    config = _config(ttft=0.05, n=1)
    slow = SlowAdapter(config.providers[0], delay_before_first=0.2, text="patience")
    engine = _engine_with_adapters(config, {"first": slow})
    events = await _collect(engine, _anth_req(stream=True))
    assert _text(events) == "patience"


@pytest.mark.asyncio
async def test_fast_provider_is_unaffected() -> None:
    config = _config(ttft=0.5)
    fast = SlowAdapter(config.providers[0], text="fast")
    other = SlowAdapter(config.providers[1], text="unused")
    engine = _engine_with_adapters(config, {"first": fast, "second": other})
    events = await _collect(engine, _anth_req(stream=True))
    assert _text(events) == "fast"
    assert other.stream_calls == []


@pytest.mark.asyncio
async def test_disabled_by_default() -> None:
    config = _config(ttft=None)
    slow = SlowAdapter(config.providers[0], delay_before_first=0.15, text="slow")
    other = SlowAdapter(config.providers[1], text="unused")
    engine = _engine_with_adapters(config, {"first": slow, "second": other})
    events = await _collect(engine, _anth_req(stream=True))
    assert _text(events) == "slow"


@pytest.mark.asyncio
async def test_all_slow_chain_ends_in_no_providers_with_ttft_reason() -> None:
    config = _config(ttft=0.05, n=2)
    a = SlowAdapter(config.providers[0], delay_before_first=1.0)
    # The last provider is exempt from the cut-off, so make it fail outright.
    from coderouter.adapters.base import AdapterError

    b = FakeAnthropicAdapter(
        config.providers[1], fail_with=AdapterError("boom", provider="second", status_code=503)
    )
    engine = _engine_with_adapters(config, {"first": a, "second": b})
    with pytest.raises(NoProvidersAvailableError) as info:
        await _collect(engine, _anth_req(stream=True))
    assert isinstance(info.value.errors[0], TTFTTimeoutError)


def test_ttft_error_classifies_as_ttft_timeout() -> None:
    assert classify_adapter_error(TTFTTimeoutError(provider="p", ttft_timeout_s=2.0)) == REASON_TTFT_TIMEOUT


def test_schema_rejects_non_positive_ttft() -> None:
    with pytest.raises(ValueError):
        ProviderConfig(name="x", base_url="http://127.0.0.1:1/v1", model="m", ttft_timeout_s=0)
