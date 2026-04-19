"""Fallback engine tests — uses fake adapters, no httpx network calls."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from typing import Any

import pytest

from coderouter.adapters.base import (
    AdapterError,
    BaseAdapter,
    ChatRequest,
    ChatResponse,
    Message,
    StreamChunk,
)
from coderouter.config.schemas import CodeRouterConfig
from coderouter.routing import (
    FallbackEngine,
    MidStreamError,
    NoProvidersAvailableError,
)


class FakeAdapter(BaseAdapter):
    """Programmable adapter for tests."""

    def __init__(
        self,
        config,
        *,
        fail_with: AdapterError | None = None,
        text: str = "ok",
        chunks: list[str] | None = None,
        fail_after_chunks: int | None = None,
        midstream_error: AdapterError | None = None,
    ) -> None:
        super().__init__(config)
        self.fail_with = fail_with
        self.text = text
        self.chunks = chunks or [text]
        self.call_count = 0
        # When set, stream() yields `fail_after_chunks` chunks then raises
        # `midstream_error` (defaults to a generic retryable AdapterError).
        self.fail_after_chunks = fail_after_chunks
        self.midstream_error = midstream_error

    async def healthcheck(self) -> bool:
        return self.fail_with is None

    async def generate(self, request: ChatRequest) -> ChatResponse:
        self.call_count += 1
        if self.fail_with:
            raise self.fail_with
        return ChatResponse(
            id=f"fake-{self.name}-{self.call_count}",
            created=int(time.time()),
            model=self.config.model,
            choices=[
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": self.text},
                    "finish_reason": "stop",
                }
            ],
            coderouter_provider=self.name,
        )

    async def stream(self, request: ChatRequest) -> AsyncIterator[StreamChunk]:
        self.call_count += 1
        if self.fail_with:
            raise self.fail_with
        for i, piece in enumerate(self.chunks):
            if (
                self.fail_after_chunks is not None
                and i >= self.fail_after_chunks
            ):
                raise self.midstream_error or AdapterError(
                    "midstream failure",
                    provider=self.name,
                    retryable=True,
                )
            yield StreamChunk(
                id=f"fake-{self.name}-stream",
                created=int(time.time()),
                model=self.config.model,
                choices=[{"index": 0, "delta": {"content": piece}}],
            )


def _engine_with(
    config: CodeRouterConfig, fakes: dict[str, FakeAdapter]
) -> FallbackEngine:
    engine = FallbackEngine(config)
    engine._adapters = fakes  # type: ignore[assignment]  # tests poke internals
    return engine


def _request(profile: str | None = None) -> ChatRequest:
    return ChatRequest(messages=[Message(role="user", content="hi")], profile=profile)


@pytest.mark.asyncio
async def test_first_provider_wins(basic_config: CodeRouterConfig) -> None:
    fakes = {
        "local": FakeAdapter(basic_config.provider_by_name("local"), text="from local"),
        "free-cloud": FakeAdapter(basic_config.provider_by_name("free-cloud")),
        "paid-cloud": FakeAdapter(basic_config.provider_by_name("paid-cloud")),
    }
    engine = _engine_with(basic_config, fakes)
    resp = await engine.generate(_request())
    assert resp.coderouter_provider == "local"
    assert fakes["local"].call_count == 1
    assert fakes["free-cloud"].call_count == 0


@pytest.mark.asyncio
async def test_fallback_skips_failed_provider(
    basic_config: CodeRouterConfig,
) -> None:
    fakes = {
        "local": FakeAdapter(
            basic_config.provider_by_name("local"),
            fail_with=AdapterError("down", provider="local", retryable=True),
        ),
        "free-cloud": FakeAdapter(
            basic_config.provider_by_name("free-cloud"), text="from free"
        ),
        "paid-cloud": FakeAdapter(basic_config.provider_by_name("paid-cloud")),
    }
    engine = _engine_with(basic_config, fakes)
    resp = await engine.generate(_request())
    assert resp.coderouter_provider == "free-cloud"
    assert fakes["paid-cloud"].call_count == 0


@pytest.mark.asyncio
async def test_paid_blocked_when_allow_paid_false(
    basic_config: CodeRouterConfig,
) -> None:
    # Make local + free fail so only paid would be left
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
    assert basic_config.allow_paid is False
    engine = _engine_with(basic_config, fakes)

    with pytest.raises(NoProvidersAvailableError):
        await engine.generate(_request())
    # paid was filtered out, never tried
    assert fakes["paid-cloud"].call_count == 0


@pytest.mark.asyncio
async def test_paid_used_when_allow_paid_true(
    basic_config: CodeRouterConfig,
) -> None:
    basic_config.allow_paid = True
    fakes = {
        "local": FakeAdapter(
            basic_config.provider_by_name("local"),
            fail_with=AdapterError("down", provider="local", retryable=True),
        ),
        "free-cloud": FakeAdapter(
            basic_config.provider_by_name("free-cloud"),
            fail_with=AdapterError("rate", provider="free-cloud", retryable=True),
        ),
        "paid-cloud": FakeAdapter(
            basic_config.provider_by_name("paid-cloud"), text="from paid"
        ),
    }
    engine = _engine_with(basic_config, fakes)
    resp = await engine.generate(_request())
    assert resp.coderouter_provider == "paid-cloud"


@pytest.mark.asyncio
async def test_non_retryable_error_aborts_chain(
    basic_config: CodeRouterConfig,
) -> None:
    fakes = {
        "local": FakeAdapter(
            basic_config.provider_by_name("local"),
            fail_with=AdapterError(
                "bad request", provider="local", status_code=400, retryable=False
            ),
        ),
        "free-cloud": FakeAdapter(basic_config.provider_by_name("free-cloud")),
        "paid-cloud": FakeAdapter(basic_config.provider_by_name("paid-cloud")),
    }
    engine = _engine_with(basic_config, fakes)
    with pytest.raises(NoProvidersAvailableError):
        await engine.generate(_request())
    # stopped at first non-retryable failure
    assert fakes["free-cloud"].call_count == 0


@pytest.mark.asyncio
async def test_streaming_first_provider_wins(
    basic_config: CodeRouterConfig,
) -> None:
    fakes = {
        "local": FakeAdapter(
            basic_config.provider_by_name("local"), chunks=["he", "llo"]
        ),
        "free-cloud": FakeAdapter(basic_config.provider_by_name("free-cloud")),
        "paid-cloud": FakeAdapter(basic_config.provider_by_name("paid-cloud")),
    }
    engine = _engine_with(basic_config, fakes)
    chunks: list[Any] = []
    req = _request()
    req.stream = True
    async for c in engine.stream(req):
        chunks.append(c)
    assert len(chunks) == 2
    assert fakes["free-cloud"].call_count == 0


@pytest.mark.asyncio
async def test_streaming_falls_back_when_first_errors_immediately(
    basic_config: CodeRouterConfig,
) -> None:
    fakes = {
        "local": FakeAdapter(
            basic_config.provider_by_name("local"),
            fail_with=AdapterError("down", provider="local", retryable=True),
        ),
        "free-cloud": FakeAdapter(
            basic_config.provider_by_name("free-cloud"), chunks=["hi"]
        ),
        "paid-cloud": FakeAdapter(basic_config.provider_by_name("paid-cloud")),
    }
    engine = _engine_with(basic_config, fakes)
    req = _request()
    req.stream = True
    chunks = [c async for c in engine.stream(req)]
    assert len(chunks) == 1
    assert fakes["paid-cloud"].call_count == 0


# ----------------------------------------------------------------------
# v0.3-B: Mid-stream fallback guard
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_streaming_midstream_failure_raises_midstream_error(
    basic_config: CodeRouterConfig,
) -> None:
    """If the active provider fails AFTER emitting chunks, the engine must
    NOT fall back — it raises MidStreamError so the caller can surface a
    terminal error to the client instead of silently switching providers
    (which would corrupt the partial stream the client has already seen).
    """
    fakes = {
        "local": FakeAdapter(
            basic_config.provider_by_name("local"),
            chunks=["hel", "lo", "_never_"],
            fail_after_chunks=2,
            midstream_error=AdapterError(
                "connection reset", provider="local", retryable=True
            ),
        ),
        "free-cloud": FakeAdapter(
            basic_config.provider_by_name("free-cloud"), chunks=["shouldnt-see"]
        ),
        "paid-cloud": FakeAdapter(basic_config.provider_by_name("paid-cloud")),
    }
    engine = _engine_with(basic_config, fakes)
    req = _request()
    req.stream = True

    received: list[Any] = []
    with pytest.raises(MidStreamError) as exc_info:
        async for c in engine.stream(req):
            received.append(c)

    # The client saw exactly the chunks that were emitted before the failure.
    assert len(received) == 2
    # The error carries the failing provider name + original AdapterError.
    assert exc_info.value.provider == "local"
    assert isinstance(exc_info.value.original, AdapterError)
    # Fallback did NOT happen — next provider was never called.
    assert fakes["free-cloud"].call_count == 0
    assert fakes["paid-cloud"].call_count == 0


@pytest.mark.asyncio
async def test_streaming_initial_error_still_falls_back(
    basic_config: CodeRouterConfig,
) -> None:
    """Sanity check: an error raised BEFORE the first chunk is emitted is
    still handled by the normal fallback path (it is not a mid-stream error).
    """
    fakes = {
        "local": FakeAdapter(
            basic_config.provider_by_name("local"),
            chunks=[],
            fail_after_chunks=0,  # fails before yielding anything
            midstream_error=AdapterError(
                "immediate failure", provider="local", retryable=True
            ),
        ),
        "free-cloud": FakeAdapter(
            basic_config.provider_by_name("free-cloud"), chunks=["ok"]
        ),
        "paid-cloud": FakeAdapter(basic_config.provider_by_name("paid-cloud")),
    }
    engine = _engine_with(basic_config, fakes)
    req = _request()
    req.stream = True
    chunks = [c async for c in engine.stream(req)]
    # Fell back cleanly to free-cloud.
    assert len(chunks) == 1
    assert fakes["free-cloud"].call_count == 1
    assert fakes["paid-cloud"].call_count == 0
