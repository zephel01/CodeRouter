"""AnthropicAdapter unit tests — mocks the Anthropic Messages API over httpx.

Covers:
    - Auth header (x-api-key, anthropic-version) and URL construction.
    - The OpenAI-shaped BaseAdapter entry points (generate/stream) work via
      reverse translation in v0.4-A: ChatRequest → AnthropicRequest →
      native call → (AnthropicResponse / AnthropicStreamEvent) → ChatResponse
      / StreamChunk. Retryable semantics are preserved through the reverse
      path.
    - generate_anthropic: happy path, error mapping, JSON parse failure.
    - stream_anthropic: SSE parsing, error status on initial response,
      mid-stream-style upstream error is never silently swallowed.
"""

from __future__ import annotations

import json

import httpx
import pytest
from pytest_httpx import HTTPXMock

from coderouter.adapters.anthropic_native import AnthropicAdapter
from coderouter.adapters.base import AdapterError, ChatRequest, Message
from coderouter.config.schemas import ProviderConfig
from coderouter.translation import AnthropicMessage, AnthropicRequest


def _provider(
    *,
    base_url: str = "https://api.anthropic.com",
    api_key_env: str | None = None,
    model: str = "claude-sonnet-4-6",
    extra_body: dict | None = None,
) -> ProviderConfig:
    return ProviderConfig(
        name="anthropic-native",
        kind="anthropic",
        base_url=base_url,
        model=model,
        api_key_env=api_key_env,
        extra_body=extra_body or {},
    )


def _request(
    *,
    stream: bool = False,
    max_tokens: int = 64,
) -> AnthropicRequest:
    return AnthropicRequest(
        max_tokens=max_tokens,
        messages=[AnthropicMessage(role="user", content="hi")],
        stream=stream,
    )


# ----------------------------------------------------------------------
# URL + header shape
# ----------------------------------------------------------------------


def test_url_normalizes_trailing_v1() -> None:
    """base_url may end in /v1 or not — both resolve to /v1/messages."""
    for base in (
        "https://api.anthropic.com",
        "https://api.anthropic.com/",
        "https://api.anthropic.com/v1",
        "https://api.anthropic.com/v1/",
    ):
        adapter = AnthropicAdapter(_provider(base_url=base))
        assert adapter._url() == "https://api.anthropic.com/v1/messages"


def test_headers_use_x_api_key_not_authorization(monkeypatch) -> None:
    """Anthropic uses x-api-key. Must NOT set Authorization: Bearer."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    adapter = AnthropicAdapter(_provider(api_key_env="ANTHROPIC_API_KEY"))
    headers = adapter._headers()
    assert headers["x-api-key"] == "sk-ant-test"
    assert "Authorization" not in headers
    assert headers["anthropic-version"] == "2023-06-01"  # default


def test_anthropic_version_override_via_extra_body() -> None:
    """Users on a pinned minor version can set anthropic-version via extra_body."""
    adapter = AnthropicAdapter(_provider(extra_body={"anthropic_version": "2024-10-22"}))
    headers = adapter._headers()
    assert headers["anthropic-version"] == "2024-10-22"


def test_headers_omit_anthropic_beta_when_not_set() -> None:
    """No request, or request without anthropic_beta → no header (default)."""
    adapter = AnthropicAdapter(_provider())
    assert "anthropic-beta" not in adapter._headers()
    req = AnthropicRequest(
        max_tokens=8,
        messages=[AnthropicMessage(role="user", content="hi")],
    )
    assert "anthropic-beta" not in adapter._headers(req)


def test_headers_forward_anthropic_beta_when_set() -> None:
    """v0.4-D: request.anthropic_beta is forwarded verbatim as a header.

    Claude Code sends this for body fields gated behind a beta flag
    (e.g. context_management). Without forwarding, api.anthropic.com
    400s those fields as "Extra inputs are not permitted".
    """
    adapter = AnthropicAdapter(_provider())
    req = AnthropicRequest(
        max_tokens=8,
        messages=[AnthropicMessage(role="user", content="hi")],
    )
    req.anthropic_beta = "context-management-2025-06-27"
    assert adapter._headers(req)["anthropic-beta"] == "context-management-2025-06-27"


# ----------------------------------------------------------------------
# BaseAdapter contract — OpenAI-shaped calls work via v0.4-A reverse path
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_openai_shaped_generate_reverse_translates(
    httpx_mock: HTTPXMock, monkeypatch
) -> None:
    """OpenAI ChatRequest → Anthropic body (system lifted, tool_result batched)
    → Anthropic response (text + tool_use) → OpenAI ChatResponse.

    End-to-end check that the v0.4-A reverse path preserves the shape on
    both sides with realistic structure (system/user/assistant-tool_calls/
    tool/user and a tool_use-terminated response).
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    captured: dict = {}

    def _capture(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "msg_42",
                "type": "message",
                "role": "assistant",
                "model": "claude-sonnet-4-6",
                "content": [
                    {"type": "text", "text": "calling tool..."},
                    {
                        "type": "tool_use",
                        "id": "toolu_abc",
                        "name": "weather",
                        "input": {"city": "Tokyo"},
                    },
                ],
                "stop_reason": "tool_use",
                "usage": {"input_tokens": 42, "output_tokens": 9},
            },
        )

    httpx_mock.add_callback(
        _capture,
        url="https://api.anthropic.com/v1/messages",
        method="POST",
    )

    adapter = AnthropicAdapter(_provider(api_key_env="ANTHROPIC_API_KEY"))
    req = ChatRequest(
        model="gpt-4o-ignored",  # client-sent model is a routing placeholder
        messages=[
            Message(role="system", content="you are terse"),
            Message(role="user", content="weather in Tokyo?"),
            Message(
                role="assistant",
                content=None,
                tool_calls=[
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "weather",
                            "arguments": '{"city": "Tokyo"}',
                        },
                    }
                ],
            ),
            Message(role="tool", tool_call_id="call_1", content="sunny, 22C"),
            Message(role="user", content="ok and tomorrow?"),
        ],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "weather",
                    "description": "Get weather",
                    "parameters": {"type": "object"},
                },
            }
        ],
        tool_choice="auto",
    )

    resp = await adapter.generate(req)

    # ---- Outbound body assertions ---------------------------------------
    body = captured["body"]
    # Provider config's model always wins on the wire.
    assert body["model"] == "claude-sonnet-4-6"
    # System role lifted to top-level `system` field.
    assert body["system"] == "you are terse"
    assert body["stream"] is False
    # Anthropic requires max_tokens; default kicks in when ChatRequest omits it.
    assert body["max_tokens"] == 4096
    # Role sequence: user / assistant(tool_use) / user(tool_result) / user.
    roles = [m["role"] for m in body["messages"]]
    assert roles == ["user", "assistant", "user", "user"]
    asst_blocks = body["messages"][1]["content"]
    assert any(b.get("type") == "tool_use" for b in asst_blocks)
    # Tool result is batched as a user turn with tool_result blocks.
    tr_blocks = body["messages"][2]["content"]
    assert isinstance(tr_blocks, list)
    assert tr_blocks[0]["type"] == "tool_result"
    assert tr_blocks[0]["tool_use_id"] == "call_1"
    assert tr_blocks[0]["content"] == "sunny, 22C"
    # Tools carry Anthropic-shape (name / input_schema).
    assert body["tools"][0]["name"] == "weather"
    assert "input_schema" in body["tools"][0]
    assert body["tool_choice"] == {"type": "auto"}

    # ---- Response conversion assertions --------------------------------
    msg = resp.choices[0]["message"]
    assert msg["role"] == "assistant"
    assert msg["content"] == "calling tool..."
    assert msg["tool_calls"][0]["function"]["name"] == "weather"
    args = json.loads(msg["tool_calls"][0]["function"]["arguments"])
    assert args == {"city": "Tokyo"}
    # tool_use → finish_reason=tool_calls
    assert resp.choices[0]["finish_reason"] == "tool_calls"
    assert resp.coderouter_provider == "anthropic-native"
    assert resp.usage["prompt_tokens"] == 42
    assert resp.usage["completion_tokens"] == 9


@pytest.mark.asyncio
async def test_openai_shaped_generate_429_is_retryable(
    httpx_mock: HTTPXMock,
) -> None:
    """Retryable upstream statuses propagate through the reverse path unchanged."""
    httpx_mock.add_response(
        url="https://api.anthropic.com/v1/messages",
        method="POST",
        status_code=429,
        json={"type": "error", "error": {"type": "rate_limit_error"}},
    )
    adapter = AnthropicAdapter(_provider())
    req = ChatRequest(messages=[Message(role="user", content="hi")])
    with pytest.raises(AdapterError) as info:
        await adapter.generate(req)
    assert info.value.status_code == 429
    assert info.value.retryable is True


@pytest.mark.asyncio
async def test_openai_shaped_stream_reverse_translates(
    httpx_mock: HTTPXMock,
) -> None:
    """Anthropic SSE events → OpenAI StreamChunk sequence.

    Verifies: (1) initial role=assistant chunk, (2) content deltas carry
    text, (3) final finish chunk uses the reverse stop_reason map, (4)
    trailing usage chunk mirrors OpenAI's stream_options.include_usage.
    """
    httpx_mock.add_response(
        url="https://api.anthropic.com/v1/messages",
        method="POST",
        content=_SSE_TEXT_STREAM.encode("utf-8"),
        headers={"content-type": "text/event-stream"},
    )
    adapter = AnthropicAdapter(_provider())
    req = ChatRequest(
        messages=[Message(role="user", content="hi")],
        stream=True,
    )
    chunks = [c async for c in adapter.stream(req)]

    # 1st chunk: role=assistant (OpenAI convention).
    first_delta = chunks[0].choices[0]["delta"]
    assert first_delta.get("role") == "assistant"

    # At least one chunk carries the text "hello".
    text_chunks = [c for c in chunks if c.choices and c.choices[0].get("delta", {}).get("content")]
    assert any(c.choices[0]["delta"]["content"] == "hello" for c in text_chunks)

    # Finish chunk: end_turn → stop.
    finish_chunks = [c for c in chunks if c.choices and c.choices[0].get("finish_reason")]
    assert finish_chunks and finish_chunks[-1].choices[0]["finish_reason"] == "stop"

    # Trailing usage chunk (no choices).
    assert chunks[-1].choices == []
    assert chunks[-1].usage["prompt_tokens"] == 5
    assert chunks[-1].usage["completion_tokens"] == 3
    assert chunks[-1].usage["total_tokens"] == 8


@pytest.mark.asyncio
async def test_openai_shaped_stream_anthropic_error_event_is_non_retryable(
    httpx_mock: HTTPXMock,
) -> None:
    """Anthropic `event: error` mid-stream → AdapterError(retryable=False).

    The engine's v0.3-B mid-stream guard re-raises this as MidStreamError
    once at least one chunk has already been delivered. Here we assert the
    translator's half of that contract: retryable=False at the source.
    """
    body = (
        "event: message_start\n"
        'data: {"type":"message_start","message":{"id":"msg_x","type":"message",'
        '"role":"assistant","model":"claude","content":[],'
        '"stop_reason":null,"stop_sequence":null,'
        '"usage":{"input_tokens":0,"output_tokens":0}}}\n'
        "\n"
        "event: error\n"
        'data: {"type":"error","error":{"type":"overloaded_error",'
        '"message":"service overloaded"}}\n'
        "\n"
    )
    httpx_mock.add_response(
        url="https://api.anthropic.com/v1/messages",
        method="POST",
        content=body.encode("utf-8"),
        headers={"content-type": "text/event-stream"},
    )
    adapter = AnthropicAdapter(_provider())
    req = ChatRequest(
        messages=[Message(role="user", content="hi")],
        stream=True,
    )
    collected: list = []
    with pytest.raises(AdapterError) as info:
        async for chunk in adapter.stream(req):
            collected.append(chunk)
    # The first chunk (role=assistant from message_start) was already delivered
    # before the error event. This is the condition the engine needs to convert
    # the error into MidStreamError.
    assert collected, "expected at least one chunk before error"
    assert info.value.retryable is False


# ----------------------------------------------------------------------
# generate_anthropic
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_anthropic_sends_correct_payload(httpx_mock: HTTPXMock, monkeypatch) -> None:
    """Body must carry provider's model (client-sent model is ignored),
    stream=False, and the messages array verbatim."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    captured: dict = {}

    def _capture(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        captured["headers"] = dict(request.headers)
        return httpx.Response(
            200,
            json={
                "id": "msg_01",
                "type": "message",
                "role": "assistant",
                "model": "claude-sonnet-4-6",
                "content": [{"type": "text", "text": "hello"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 5, "output_tokens": 3},
            },
        )

    httpx_mock.add_callback(
        _capture,
        url="https://api.anthropic.com/v1/messages",
        method="POST",
    )

    adapter = AnthropicAdapter(_provider(api_key_env="ANTHROPIC_API_KEY"))
    # Client-sent model should be overridden by provider config.
    req = AnthropicRequest(
        model="claude-whatever",  # must be stripped in outbound body
        max_tokens=32,
        messages=[AnthropicMessage(role="user", content="hi")],
    )
    resp = await adapter.generate_anthropic(req)

    # Provider model wins.
    assert captured["body"]["model"] == "claude-sonnet-4-6"
    assert captured["body"]["stream"] is False
    assert captured["body"]["max_tokens"] == 32
    assert captured["body"]["messages"] == [{"role": "user", "content": "hi"}]
    # profile / model aren't leaked to upstream.
    assert "profile" not in captured["body"]
    # Auth + version headers are set.
    assert captured["headers"]["x-api-key"] == "sk-ant-test"
    assert captured["headers"]["anthropic-version"] == "2023-06-01"
    # Response is parsed and tagged with coderouter_provider.
    assert resp.coderouter_provider == "anthropic-native"
    assert resp.content[0]["text"] == "hello"
    assert resp.usage.input_tokens == 5
    assert resp.usage.output_tokens == 3


@pytest.mark.asyncio
async def test_generate_anthropic_forwards_anthropic_beta_header(
    httpx_mock: HTTPXMock,
) -> None:
    """v0.4-D: when request.anthropic_beta is set, the outbound request
    carries an `anthropic-beta` header AND the field is NOT serialized
    into the JSON body (it's a Field(exclude=True) stash).
    """
    captured: dict = {}

    def _capture(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        captured["headers"] = dict(request.headers)
        return httpx.Response(
            200,
            json={
                "id": "msg_beta",
                "type": "message",
                "role": "assistant",
                "model": "claude-sonnet-4-6",
                "content": [{"type": "text", "text": "ok"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )

    httpx_mock.add_callback(
        _capture,
        url="https://api.anthropic.com/v1/messages",
        method="POST",
    )

    adapter = AnthropicAdapter(_provider())
    req = _request()
    req.anthropic_beta = "context-management-2025-06-27"
    await adapter.generate_anthropic(req)

    assert captured["headers"]["anthropic-beta"] == "context-management-2025-06-27"
    # Critical: the beta flag is a header hop, NOT a body field. If it
    # leaked into the body, Anthropic would 400 with
    # "Extra inputs are not permitted" (ironically, the exact class of
    # bug this fix is for).
    assert "anthropic_beta" not in captured["body"]


@pytest.mark.asyncio
async def test_generate_anthropic_429_is_retryable(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="https://api.anthropic.com/v1/messages",
        method="POST",
        status_code=429,
        json={"type": "error", "error": {"type": "rate_limit_error"}},
    )
    adapter = AnthropicAdapter(_provider())
    with pytest.raises(AdapterError) as info:
        await adapter.generate_anthropic(_request())
    assert info.value.status_code == 429
    assert info.value.retryable is True


@pytest.mark.asyncio
async def test_generate_anthropic_400_is_not_retryable(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="https://api.anthropic.com/v1/messages",
        method="POST",
        status_code=400,
        json={"type": "error", "error": {"type": "invalid_request_error"}},
    )
    adapter = AnthropicAdapter(_provider())
    with pytest.raises(AdapterError) as info:
        await adapter.generate_anthropic(_request())
    assert info.value.status_code == 400
    assert info.value.retryable is False


@pytest.mark.asyncio
async def test_generate_anthropic_500_is_retryable(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="https://api.anthropic.com/v1/messages",
        method="POST",
        status_code=500,
        json={"type": "error", "error": {"type": "api_error"}},
    )
    adapter = AnthropicAdapter(_provider())
    with pytest.raises(AdapterError) as info:
        await adapter.generate_anthropic(_request())
    assert info.value.status_code == 500
    assert info.value.retryable is True


# ----------------------------------------------------------------------
# stream_anthropic
# ----------------------------------------------------------------------


_SSE_TEXT_STREAM = (
    "event: message_start\n"
    'data: {"type":"message_start","message":{"id":"msg_01","type":"message",'
    '"role":"assistant","model":"claude-sonnet-4-6","content":[],'
    '"stop_reason":null,"stop_sequence":null,'
    '"usage":{"input_tokens":5,"output_tokens":0}}}\n'
    "\n"
    "event: content_block_start\n"
    'data: {"type":"content_block_start","index":0,'
    '"content_block":{"type":"text","text":""}}\n'
    "\n"
    "event: content_block_delta\n"
    'data: {"type":"content_block_delta","index":0,'
    '"delta":{"type":"text_delta","text":"hello"}}\n'
    "\n"
    "event: content_block_stop\n"
    'data: {"type":"content_block_stop","index":0}\n'
    "\n"
    "event: message_delta\n"
    'data: {"type":"message_delta","delta":{"stop_reason":"end_turn",'
    '"stop_sequence":null},"usage":{"output_tokens":3}}\n'
    "\n"
    "event: message_stop\n"
    'data: {"type":"message_stop"}\n'
    "\n"
)


@pytest.mark.asyncio
async def test_stream_anthropic_parses_sse_into_events(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(
        url="https://api.anthropic.com/v1/messages",
        method="POST",
        content=_SSE_TEXT_STREAM.encode("utf-8"),
        headers={"content-type": "text/event-stream"},
    )
    adapter = AnthropicAdapter(_provider())
    events = [e async for e in adapter.stream_anthropic(_request(stream=True))]
    types = [e.type for e in events]
    assert types == [
        "message_start",
        "content_block_start",
        "content_block_delta",
        "content_block_stop",
        "message_delta",
        "message_stop",
    ]
    # Each event.data round-trips the upstream JSON.
    delta_ev = events[2]
    assert delta_ev.data["delta"]["text"] == "hello"


@pytest.mark.asyncio
async def test_stream_anthropic_forwards_anthropic_beta_header(
    httpx_mock: HTTPXMock,
) -> None:
    """v0.4-D: streaming path must also forward the anthropic-beta header.

    Mirror of the non-streaming test — the header is how beta body fields
    get unlocked, and streaming is the default path Claude Code uses.
    """
    captured: dict = {}

    def _capture(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        return httpx.Response(
            200,
            content=b"",
            headers={"content-type": "text/event-stream"},
        )

    httpx_mock.add_callback(
        _capture,
        url="https://api.anthropic.com/v1/messages",
        method="POST",
    )
    adapter = AnthropicAdapter(_provider())
    req = _request(stream=True)
    req.anthropic_beta = "context-management-2025-06-27"
    _ = [e async for e in adapter.stream_anthropic(req)]
    assert captured["headers"]["anthropic-beta"] == "context-management-2025-06-27"


@pytest.mark.asyncio
async def test_stream_anthropic_stream_payload_has_stream_true(
    httpx_mock: HTTPXMock,
) -> None:
    """Streaming requests must set stream=true in the outbound body."""
    captured: dict = {}

    def _capture(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            content=b"",
            headers={"content-type": "text/event-stream"},
        )

    httpx_mock.add_callback(
        _capture,
        url="https://api.anthropic.com/v1/messages",
        method="POST",
    )
    adapter = AnthropicAdapter(_provider())
    _ = [e async for e in adapter.stream_anthropic(_request(stream=True))]
    assert captured["body"]["stream"] is True


@pytest.mark.asyncio
async def test_stream_anthropic_initial_4xx_raises_adapter_error(
    httpx_mock: HTTPXMock,
) -> None:
    """A 4xx response before any SSE event must raise AdapterError with
    an appropriate retryability flag. The fallback engine then decides
    whether to try the next provider."""
    httpx_mock.add_response(
        url="https://api.anthropic.com/v1/messages",
        method="POST",
        status_code=429,
        json={"type": "error", "error": {"type": "rate_limit_error"}},
    )
    adapter = AnthropicAdapter(_provider())
    with pytest.raises(AdapterError) as info:
        async for _ in adapter.stream_anthropic(_request(stream=True)):
            pass
    assert info.value.status_code == 429
    assert info.value.retryable is True


@pytest.mark.asyncio
async def test_stream_anthropic_skips_comments_and_malformed_blocks(
    httpx_mock: HTTPXMock,
) -> None:
    """SSE heartbeat comments (': keepalive') and JSON-parse failures
    must not abort the stream — they're skipped silently."""
    body = (
        ": heartbeat\n"
        "\n"
        "event: message_start\n"
        'data: {"type":"message_start","message":{"id":"msg_x","type":"message",'
        '"role":"assistant","model":"claude","content":[],'
        '"stop_reason":null,"stop_sequence":null,'
        '"usage":{"input_tokens":0,"output_tokens":0}}}\n'
        "\n"
        "event: ping\n"
        "data: {not-json}\n"
        "\n"
        "event: message_stop\n"
        'data: {"type":"message_stop"}\n'
        "\n"
    )
    httpx_mock.add_response(
        url="https://api.anthropic.com/v1/messages",
        method="POST",
        content=body.encode("utf-8"),
        headers={"content-type": "text/event-stream"},
    )
    adapter = AnthropicAdapter(_provider())
    events = [e async for e in adapter.stream_anthropic(_request(stream=True))]
    types = [e.type for e in events]
    # Malformed ping block was skipped; message_start + message_stop survive.
    assert types == ["message_start", "message_stop"]
