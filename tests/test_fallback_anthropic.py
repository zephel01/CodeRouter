"""Engine-level tests for `FallbackEngine.generate_anthropic` /
`stream_anthropic` — the v0.3.x-1 Anthropic-shaped entry points.

These verify:
    - native passthrough for `kind: anthropic` providers,
    - translation round-trip for `kind: openai_compat` providers,
    - tool-call repair runs on non-streaming openai_compat responses,
    - v0.3-D downgrade happens at the engine for openai_compat + tools,
    - mixed chains (native → openai_compat fallback) work both directions,
    - mid-stream guard + NoProvidersAvailableError semantics are preserved.

No HTTP: the adapters are replaced with scripted fakes that implement
the minimal surface each engine path exercises.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator

import pytest

from coderouter.adapters.anthropic_native import AnthropicAdapter
from coderouter.adapters.base import (
    AdapterError,
    BaseAdapter,
    ChatRequest,
    ChatResponse,
    StreamChunk,
)
from coderouter.config.schemas import (
    CodeRouterConfig,
    FallbackChain,
    ProviderConfig,
)
from coderouter.routing import (
    FallbackEngine,
    MidStreamError,
    NoProvidersAvailableError,
)
from coderouter.translation import (
    AnthropicMessage,
    AnthropicRequest,
    AnthropicResponse,
    AnthropicStreamEvent,
    AnthropicTool,
    AnthropicUsage,
)

# ----------------------------------------------------------------------
# Fake adapters
# ----------------------------------------------------------------------


class FakeOpenAIAdapter(BaseAdapter):
    """Fake `kind: openai_compat` adapter that returns ChatResponses /
    StreamChunks we control — exercises the translation round-trip in
    the engine.
    """

    def __init__(
        self,
        config: ProviderConfig,
        *,
        text: str = "hello from openai",
        tool_as_text: str | None = None,
        fail_with: AdapterError | None = None,
        chunks: list[str] | None = None,
        fail_after_chunks: int | None = None,
        midstream_error: AdapterError | None = None,
    ) -> None:
        super().__init__(config)
        self.text = text
        self.tool_as_text = tool_as_text
        self.fail_with = fail_with
        self.chunks = chunks or [text]
        self.fail_after_chunks = fail_after_chunks
        self.midstream_error = midstream_error
        self.generate_calls: list[ChatRequest] = []
        self.stream_calls: list[ChatRequest] = []

    async def healthcheck(self) -> bool:
        return self.fail_with is None

    async def generate(self, request: ChatRequest) -> ChatResponse:
        self.generate_calls.append(request)
        if self.fail_with:
            raise self.fail_with
        content = self.tool_as_text if self.tool_as_text is not None else self.text
        return ChatResponse(
            id=f"chatcmpl-{self.name}",
            created=int(time.time()),
            model=self.config.model,
            choices=[
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            usage={"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
            coderouter_provider=self.name,
        )

    async def stream(self, request: ChatRequest) -> AsyncIterator[StreamChunk]:
        self.stream_calls.append(request)
        if self.fail_with:
            raise self.fail_with
        for i, piece in enumerate(self.chunks):
            if (
                self.fail_after_chunks is not None
                and i >= self.fail_after_chunks
            ):
                raise self.midstream_error or AdapterError(
                    "midstream failure", provider=self.name, retryable=True
                )
            yield StreamChunk(
                id=f"chatcmpl-{self.name}-stream",
                created=int(time.time()),
                model=self.config.model,
                choices=[
                    {"index": 0, "delta": {"content": piece}, "finish_reason": None}
                ],
            )
        # Terminal chunk so the translator emits finish_reason=stop.
        yield StreamChunk(
            id=f"chatcmpl-{self.name}-stream",
            created=int(time.time()),
            model=self.config.model,
            choices=[{"index": 0, "delta": {}, "finish_reason": "stop"}],
        )


class FakeAnthropicAdapter(AnthropicAdapter):
    """Fake `kind: anthropic` adapter.

    Subclasses AnthropicAdapter so `isinstance(adapter, AnthropicAdapter)`
    in the engine returns True (triggering the native passthrough path),
    but overrides the HTTP-calling methods so no network is touched.
    """

    def __init__(
        self,
        config: ProviderConfig,
        *,
        text: str = "hello from native anthropic",
        fail_with: AdapterError | None = None,
        events: list[AnthropicStreamEvent] | None = None,
        stream_fail_with: AdapterError | None = None,
        stream_fail_after: int | None = None,
    ) -> None:
        super().__init__(config)
        self.text = text
        self.fail_with = fail_with
        self._events = events
        self.stream_fail_with = stream_fail_with
        self.stream_fail_after = stream_fail_after
        self.generate_calls: list[AnthropicRequest] = []
        self.stream_calls: list[AnthropicRequest] = []

    async def healthcheck(self) -> bool:
        return self.fail_with is None

    async def generate_anthropic(
        self, request: AnthropicRequest
    ) -> AnthropicResponse:
        self.generate_calls.append(request)
        if self.fail_with:
            raise self.fail_with
        return AnthropicResponse(
            id="msg_native",
            model=self.config.model,
            content=[{"type": "text", "text": self.text}],
            stop_reason="end_turn",
            usage=AnthropicUsage(input_tokens=1, output_tokens=2),
            coderouter_provider=self.name,
        )

    async def stream_anthropic(
        self, request: AnthropicRequest
    ) -> AsyncIterator[AnthropicStreamEvent]:
        self.stream_calls.append(request)
        if self.fail_with:
            raise self.fail_with
        events = self._events or _default_native_events(self.text, self.config.model)
        for i, ev in enumerate(events):
            if (
                self.stream_fail_after is not None
                and i >= self.stream_fail_after
            ):
                raise self.stream_fail_with or AdapterError(
                    "midstream", provider=self.name, retryable=True
                )
            yield ev


def _default_native_events(text: str, model: str) -> list[AnthropicStreamEvent]:
    """A minimal compliant Anthropic stream for fake native adapters."""
    return [
        AnthropicStreamEvent(
            type="message_start",
            data={
                "type": "message_start",
                "message": {
                    "id": "msg_native",
                    "type": "message",
                    "role": "assistant",
                    "content": [],
                    "model": model,
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                },
            },
        ),
        AnthropicStreamEvent(
            type="content_block_start",
            data={
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
        ),
        AnthropicStreamEvent(
            type="content_block_delta",
            data={
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": text},
            },
        ),
        AnthropicStreamEvent(
            type="content_block_stop",
            data={"type": "content_block_stop", "index": 0},
        ),
        AnthropicStreamEvent(
            type="message_delta",
            data={
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": 2},
            },
        ),
        AnthropicStreamEvent(
            type="message_stop",
            data={"type": "message_stop"},
        ),
    ]


# ----------------------------------------------------------------------
# Config helpers
# ----------------------------------------------------------------------


def _mixed_config(
    first_kind: str = "anthropic",
    second_kind: str = "openai_compat",
) -> CodeRouterConfig:
    """Profile with two providers — kinds configurable for mix-and-match."""
    return CodeRouterConfig(
        allow_paid=False,
        default_profile="default",
        providers=[
            ProviderConfig(
                name="first",
                kind=first_kind,  # type: ignore[arg-type]
                base_url=(
                    "https://api.anthropic.com"
                    if first_kind == "anthropic"
                    else "http://localhost:11434/v1"
                ),
                model="first-model",
                api_key_env="ANTHROPIC_API_KEY" if first_kind == "anthropic" else None,
            ),
            ProviderConfig(
                name="second",
                kind=second_kind,  # type: ignore[arg-type]
                base_url=(
                    "https://api.anthropic.com"
                    if second_kind == "anthropic"
                    else "http://localhost:11434/v1"
                ),
                model="second-model",
                api_key_env="ANTHROPIC_API_KEY" if second_kind == "anthropic" else None,
            ),
        ],
        profiles=[FallbackChain(name="default", providers=["first", "second"])],
    )


def _engine_with_adapters(
    config: CodeRouterConfig, fakes: dict[str, BaseAdapter]
) -> FallbackEngine:
    engine = FallbackEngine.__new__(FallbackEngine)
    engine.config = config
    engine._adapters = fakes  # type: ignore[attr-defined]
    return engine


def _anth_req(
    *,
    stream: bool = False,
    tools: list[AnthropicTool] | None = None,
) -> AnthropicRequest:
    return AnthropicRequest(
        max_tokens=64,
        messages=[AnthropicMessage(role="user", content="hi")],
        stream=stream,
        tools=tools,
    )


# ----------------------------------------------------------------------
# generate_anthropic: native passthrough vs translation round-trip
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_native_passthrough() -> None:
    """kind: anthropic goes straight through generate_anthropic — no
    translation to ChatRequest and back."""
    config = _mixed_config(first_kind="anthropic", second_kind="openai_compat")
    native = FakeAnthropicAdapter(
        config.provider_by_name("first"), text="from native"
    )
    fallback = FakeOpenAIAdapter(config.provider_by_name("second"))
    engine = _engine_with_adapters(config, {"first": native, "second": fallback})

    resp = await engine.generate_anthropic(_anth_req())

    assert resp.coderouter_provider == "first"
    assert resp.content == [{"type": "text", "text": "from native"}]
    assert native.generate_calls  # called
    assert fallback.generate_calls == []  # second never tried


@pytest.mark.asyncio
async def test_generate_openai_compat_round_trip() -> None:
    """kind: openai_compat translates AnthropicRequest → ChatRequest and
    back. The text reaches the client through to_anthropic_response."""
    config = _mixed_config(first_kind="openai_compat", second_kind="openai_compat")
    first = FakeOpenAIAdapter(
        config.provider_by_name("first"), text="hello from oai"
    )
    second = FakeOpenAIAdapter(config.provider_by_name("second"))
    engine = _engine_with_adapters(config, {"first": first, "second": second})

    resp = await engine.generate_anthropic(_anth_req())

    # text made the round trip through translation
    assert resp.content[0]["type"] == "text"
    assert resp.content[0]["text"] == "hello from oai"
    # prompt_tokens/completion_tokens propagate
    assert resp.usage.input_tokens == 3
    assert resp.usage.output_tokens == 4


@pytest.mark.asyncio
async def test_generate_tool_call_repair_runs_on_round_trip() -> None:
    """qwen2.5-coder failure mode: assistant writes tool_call as prose.
    The engine must still surface a structured tool_use block."""
    config = _mixed_config(first_kind="openai_compat", second_kind="openai_compat")
    first = FakeOpenAIAdapter(
        config.provider_by_name("first"),
        tool_as_text=(
            "Let me look it up.\n"
            '{"name": "get_weather", "arguments": {"location": "Tokyo"}}'
        ),
    )
    second = FakeOpenAIAdapter(config.provider_by_name("second"))
    engine = _engine_with_adapters(config, {"first": first, "second": second})

    req = _anth_req(
        tools=[
            AnthropicTool(
                name="get_weather",
                description="Get weather",
                input_schema={
                    "type": "object",
                    "properties": {"location": {"type": "string"}},
                },
            )
        ]
    )
    resp = await engine.generate_anthropic(req)

    # Must have extracted the tool_use block from the text.
    tool_uses = [b for b in resp.content if b.get("type") == "tool_use"]
    assert len(tool_uses) == 1
    assert tool_uses[0]["name"] == "get_weather"
    assert tool_uses[0]["input"] == {"location": "Tokyo"}
    # And re-mapped finish_reason from "stop" → "tool_use".
    assert resp.stop_reason == "tool_use"


@pytest.mark.asyncio
async def test_generate_mixed_chain_falls_back_from_native_to_openai() -> None:
    """Native provider fails retryably → openai_compat second provider
    answers. Both paths are in the same profile."""
    config = _mixed_config(first_kind="anthropic", second_kind="openai_compat")
    native = FakeAnthropicAdapter(
        config.provider_by_name("first"),
        fail_with=AdapterError("down", provider="first", retryable=True),
    )
    fallback = FakeOpenAIAdapter(
        config.provider_by_name("second"), text="fallback answer"
    )
    engine = _engine_with_adapters(config, {"first": native, "second": fallback})

    resp = await engine.generate_anthropic(_anth_req())

    assert resp.content[0]["text"] == "fallback answer"
    assert native.generate_calls  # tried
    assert fallback.generate_calls  # fell through


@pytest.mark.asyncio
async def test_generate_mixed_chain_falls_back_from_openai_to_native() -> None:
    """openai_compat fails retryably → native picks up."""
    config = _mixed_config(first_kind="openai_compat", second_kind="anthropic")
    first = FakeOpenAIAdapter(
        config.provider_by_name("first"),
        fail_with=AdapterError("rate", provider="first", retryable=True),
    )
    native = FakeAnthropicAdapter(
        config.provider_by_name("second"), text="native saves the day"
    )
    engine = _engine_with_adapters(config, {"first": first, "second": native})

    resp = await engine.generate_anthropic(_anth_req())

    assert resp.content[0]["text"] == "native saves the day"
    assert first.generate_calls
    assert native.generate_calls


@pytest.mark.asyncio
async def test_generate_all_failed_raises_no_providers() -> None:
    config = _mixed_config(first_kind="anthropic", second_kind="openai_compat")
    native = FakeAnthropicAdapter(
        config.provider_by_name("first"),
        fail_with=AdapterError("down", provider="first", retryable=True),
    )
    fallback = FakeOpenAIAdapter(
        config.provider_by_name("second"),
        fail_with=AdapterError("down", provider="second", retryable=True),
    )
    engine = _engine_with_adapters(config, {"first": native, "second": fallback})

    with pytest.raises(NoProvidersAvailableError):
        await engine.generate_anthropic(_anth_req())


@pytest.mark.asyncio
async def test_generate_non_retryable_aborts_chain() -> None:
    config = _mixed_config(first_kind="anthropic", second_kind="openai_compat")
    native = FakeAnthropicAdapter(
        config.provider_by_name("first"),
        fail_with=AdapterError(
            "bad request", provider="first", status_code=400, retryable=False
        ),
    )
    fallback = FakeOpenAIAdapter(config.provider_by_name("second"))
    engine = _engine_with_adapters(config, {"first": native, "second": fallback})

    with pytest.raises(NoProvidersAvailableError):
        await engine.generate_anthropic(_anth_req())
    assert fallback.generate_calls == []  # aborted before fallback


# ----------------------------------------------------------------------
# stream_anthropic: native real streaming vs openai downgrade
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_native_is_real_streaming() -> None:
    """Native providers stream events one by one — no downgrade."""
    config = _mixed_config(first_kind="anthropic", second_kind="openai_compat")
    native = FakeAnthropicAdapter(
        config.provider_by_name("first"), text="native text"
    )
    fallback = FakeOpenAIAdapter(config.provider_by_name("second"))
    engine = _engine_with_adapters(config, {"first": native, "second": fallback})

    events = [ev async for ev in engine.stream_anthropic(_anth_req(stream=True))]

    types = [e.type for e in events]
    assert types[0] == "message_start"
    assert types[-1] == "message_stop"
    assert native.stream_calls  # real stream path
    # Fallback must not have been consulted.
    assert fallback.stream_calls == []
    assert fallback.generate_calls == []


@pytest.mark.asyncio
async def test_stream_openai_without_tools_uses_real_streaming() -> None:
    """openai_compat without tools → real streaming path (no downgrade)."""
    config = _mixed_config(first_kind="openai_compat", second_kind="openai_compat")
    first = FakeOpenAIAdapter(
        config.provider_by_name("first"), chunks=["hel", "lo"]
    )
    second = FakeOpenAIAdapter(config.provider_by_name("second"))
    engine = _engine_with_adapters(config, {"first": first, "second": second})

    events = [ev async for ev in engine.stream_anthropic(_anth_req(stream=True))]

    # The streaming path was taken — adapter.stream was called, not generate.
    assert first.stream_calls
    assert first.generate_calls == []

    types = [e.type for e in events]
    assert types[0] == "message_start"
    assert types[-1] == "message_stop"


@pytest.mark.asyncio
async def test_stream_openai_with_tools_downgrades_and_repairs() -> None:
    """v0.3-D: when tools declared + openai_compat provider, the engine
    downgrades to non-stream internally, runs tool-call repair, and
    synthesizes a compliant Anthropic event sequence. The adapter's
    stream() must NOT be called."""
    config = _mixed_config(first_kind="openai_compat", second_kind="openai_compat")
    first = FakeOpenAIAdapter(
        config.provider_by_name("first"),
        tool_as_text=(
            "Calling the tool.\n"
            '{"name": "get_weather", "arguments": {"location": "Tokyo"}}'
        ),
    )
    second = FakeOpenAIAdapter(config.provider_by_name("second"))
    engine = _engine_with_adapters(config, {"first": first, "second": second})

    req = _anth_req(
        stream=True,
        tools=[
            AnthropicTool(
                name="get_weather",
                description="Weather",
                input_schema={
                    "type": "object",
                    "properties": {"location": {"type": "string"}},
                },
            )
        ],
    )
    events = [ev async for ev in engine.stream_anthropic(req)]

    # Downgrade: generate was called, stream was not.
    assert first.generate_calls
    assert first.stream_calls == []

    types = [e.type for e in events]
    assert types[0] == "message_start"
    assert types[-1] == "message_stop"
    # The tool_use content_block must be present.
    tool_starts = [
        e
        for e in events
        if e.type == "content_block_start"
        and e.data.get("content_block", {}).get("type") == "tool_use"
    ]
    assert len(tool_starts) == 1
    assert tool_starts[0].data["content_block"]["name"] == "get_weather"
    # Final stop_reason reflects tool_use.
    md = next(e for e in events if e.type == "message_delta")
    assert md.data["delta"]["stop_reason"] == "tool_use"


@pytest.mark.asyncio
async def test_stream_native_with_tools_no_downgrade() -> None:
    """Native providers don't need the downgrade — Anthropic emits
    tool_use blocks natively. Confirms stream_anthropic is called, not
    generate_anthropic."""
    config = _mixed_config(first_kind="anthropic", second_kind="openai_compat")
    native = FakeAnthropicAdapter(config.provider_by_name("first"))
    fallback = FakeOpenAIAdapter(config.provider_by_name("second"))
    engine = _engine_with_adapters(config, {"first": native, "second": fallback})

    req = _anth_req(
        stream=True,
        tools=[
            AnthropicTool(
                name="get_weather",
                input_schema={"type": "object"},
            )
        ],
    )
    _ = [ev async for ev in engine.stream_anthropic(req)]

    assert native.stream_calls  # real native stream
    assert native.generate_calls == []  # NOT downgraded


@pytest.mark.asyncio
async def test_stream_midstream_raises_midstream_error() -> None:
    """Once the first event has been yielded, a later adapter error must
    raise MidStreamError — no silent fallback."""
    config = _mixed_config(first_kind="anthropic", second_kind="openai_compat")
    native = FakeAnthropicAdapter(
        config.provider_by_name("first"),
        stream_fail_after=2,
        stream_fail_with=AdapterError(
            "connection reset", provider="first", retryable=True
        ),
    )
    fallback = FakeOpenAIAdapter(config.provider_by_name("second"))
    engine = _engine_with_adapters(config, {"first": native, "second": fallback})

    received: list[AnthropicStreamEvent] = []
    with pytest.raises(MidStreamError) as exc_info:
        async for ev in engine.stream_anthropic(_anth_req(stream=True)):
            received.append(ev)

    assert exc_info.value.provider == "first"
    # Some events shipped before failure.
    assert len(received) >= 1
    # Fallback was NOT consulted.
    assert fallback.stream_calls == []
    assert fallback.generate_calls == []


@pytest.mark.asyncio
async def test_stream_initial_error_still_falls_back() -> None:
    """A failure BEFORE any event is yielded is not a mid-stream error —
    the engine should fall through to the next provider normally."""
    config = _mixed_config(first_kind="anthropic", second_kind="openai_compat")
    native = FakeAnthropicAdapter(
        config.provider_by_name("first"),
        fail_with=AdapterError("down", provider="first", retryable=True),
    )
    fallback = FakeOpenAIAdapter(
        config.provider_by_name("second"), chunks=["ok"]
    )
    engine = _engine_with_adapters(config, {"first": native, "second": fallback})

    events = [ev async for ev in engine.stream_anthropic(_anth_req(stream=True))]

    assert events  # got something
    assert fallback.stream_calls  # fell through
