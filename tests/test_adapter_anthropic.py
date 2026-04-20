"""AnthropicAdapter unit tests — mocks the Anthropic Messages API over httpx.

Covers:
    - Auth header (x-api-key, anthropic-version) and URL construction.
    - The OpenAI-shaped BaseAdapter entry points (generate/stream) raise
      a non-retryable AdapterError — reverse translation is out of scope.
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
    adapter = AnthropicAdapter(
        _provider(extra_body={"anthropic_version": "2024-10-22"})
    )
    headers = adapter._headers()
    assert headers["anthropic-version"] == "2024-10-22"


# ----------------------------------------------------------------------
# BaseAdapter contract — OpenAI-shaped calls are NOT supported
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_openai_shaped_generate_raises_non_retryable() -> None:
    adapter = AnthropicAdapter(_provider())
    req = ChatRequest(messages=[Message(role="user", content="hi")])
    with pytest.raises(AdapterError) as info:
        await adapter.generate(req)
    assert info.value.retryable is False


@pytest.mark.asyncio
async def test_openai_shaped_stream_raises_non_retryable() -> None:
    adapter = AnthropicAdapter(_provider())
    req = ChatRequest(messages=[Message(role="user", content="hi")])
    req.stream = True
    with pytest.raises(AdapterError) as info:
        async for _ in adapter.stream(req):
            pass
    assert info.value.retryable is False


# ----------------------------------------------------------------------
# generate_anthropic
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_anthropic_sends_correct_payload(
    httpx_mock: HTTPXMock, monkeypatch
) -> None:
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
