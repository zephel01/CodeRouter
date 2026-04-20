"""Adapter-layer integration tests for v1.0-A ``output_filters``.

Covers both adapters and both paths (generate / stream) so a future
change to any of the four chokepoints is caught:

    - OpenAICompatAdapter.generate      — filter choices[*].message.content
    - OpenAICompatAdapter.stream        — filter per-chunk delta.content
    - AnthropicAdapter.generate_anthropic — filter text content blocks
    - AnthropicAdapter.stream_anthropic — filter text_delta events,
      flush tail at content_block_stop via synthetic delta event

Also asserts the one-shot ``output-filter-applied`` info log fires
exactly once per request/stream with the expected payload shape.
"""

from __future__ import annotations

import json
import logging

import pytest
from pytest_httpx import HTTPXMock

from coderouter.adapters.anthropic_native import AnthropicAdapter
from coderouter.adapters.base import ChatRequest, Message
from coderouter.adapters.openai_compat import OpenAICompatAdapter
from coderouter.config.schemas import Capabilities, ProviderConfig
from coderouter.translation.anthropic import AnthropicRequest

# ======================================================================
# OpenAI-compat adapter
# ======================================================================


def _oai_provider(filters: list[str] | None = None, *, passthrough: bool = False) -> ProviderConfig:
    return ProviderConfig(
        name="local-qwen",
        base_url="http://localhost:11434/v1",
        model="qwen2.5-coder:14b",
        api_key_env=None,
        output_filters=filters or [],
        capabilities=Capabilities(reasoning_passthrough=passthrough),
    )


def _oai_request() -> ChatRequest:
    return ChatRequest(messages=[Message(role="user", content="hi")])


def _sse(chunks: list[dict]) -> bytes:
    pieces = [f"data: {json.dumps(c)}\n\n" for c in chunks]
    pieces.append("data: [DONE]\n\n")
    return "".join(pieces).encode("utf-8")


def _sse_chunk(
    delta: dict,
    *,
    id: str = "chatcmpl-s",
    model: str = "qwen2.5-coder:14b",
    finish_reason: str | None = None,
) -> dict:
    choice: dict = {"index": 0, "delta": delta}
    if finish_reason is not None:
        choice["finish_reason"] = finish_reason
    return {
        "id": id,
        "object": "chat.completion.chunk",
        "created": 0,
        "model": model,
        "choices": [choice],
    }


@pytest.mark.asyncio
async def test_oai_generate_strips_thinking_from_content(
    httpx_mock: HTTPXMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Qwen-style inline ``<think>...</think>`` in content must be scrubbed."""
    httpx_mock.add_response(
        url="http://localhost:11434/v1/chat/completions",
        method="POST",
        json={
            "id": "x",
            "object": "chat.completion",
            "created": 0,
            "model": "qwen2.5-coder:14b",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "Sure! <think>let me reason</think>pong",
                    },
                    "finish_reason": "stop",
                }
            ],
        },
    )

    adapter = OpenAICompatAdapter(_oai_provider(["strip_thinking"]))
    with caplog.at_level(logging.INFO, logger="coderouter"):
        resp = await adapter.generate(_oai_request())

    assert resp.choices[0]["message"]["content"] == "Sure! pong"

    recs = [r for r in caplog.records if r.msg == "output-filter-applied"]
    assert len(recs) == 1
    assert recs[0].provider == "local-qwen"
    assert recs[0].filters == ["strip_thinking"]
    assert recs[0].streaming is False


@pytest.mark.asyncio
async def test_oai_generate_strips_stop_markers(
    httpx_mock: HTTPXMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    httpx_mock.add_response(
        url="http://localhost:11434/v1/chat/completions",
        method="POST",
        json={
            "id": "x",
            "object": "chat.completion",
            "created": 0,
            "model": "qwen2.5-coder:14b",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "answer<|im_end|>",
                    },
                    "finish_reason": "stop",
                }
            ],
        },
    )

    adapter = OpenAICompatAdapter(_oai_provider(["strip_stop_markers"]))
    with caplog.at_level(logging.INFO, logger="coderouter"):
        resp = await adapter.generate(_oai_request())

    assert resp.choices[0]["message"]["content"] == "answer"
    recs = [r for r in caplog.records if r.msg == "output-filter-applied"]
    assert len(recs) == 1
    assert recs[0].filters == ["strip_stop_markers"]


@pytest.mark.asyncio
async def test_oai_generate_no_filters_no_log(
    httpx_mock: HTTPXMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Provider with empty ``output_filters`` is unchanged & silent."""
    httpx_mock.add_response(
        url="http://localhost:11434/v1/chat/completions",
        method="POST",
        json={
            "id": "x",
            "object": "chat.completion",
            "created": 0,
            "model": "qwen2.5-coder:14b",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "<think>kept</think>body",
                    },
                    "finish_reason": "stop",
                }
            ],
        },
    )

    adapter = OpenAICompatAdapter(_oai_provider([]))
    with caplog.at_level(logging.INFO, logger="coderouter"):
        resp = await adapter.generate(_oai_request())

    # No filter configured → body flows verbatim.
    assert resp.choices[0]["message"]["content"] == "<think>kept</think>body"
    recs = [r for r in caplog.records if r.msg == "output-filter-applied"]
    assert recs == []


@pytest.mark.asyncio
async def test_oai_generate_no_trigger_no_log(
    httpx_mock: HTTPXMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Chain configured but never triggered → no log line."""
    httpx_mock.add_response(
        url="http://localhost:11434/v1/chat/completions",
        method="POST",
        json={
            "id": "x",
            "object": "chat.completion",
            "created": 0,
            "model": "qwen2.5-coder:14b",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "clean"},
                    "finish_reason": "stop",
                }
            ],
        },
    )

    adapter = OpenAICompatAdapter(_oai_provider(["strip_thinking"]))
    with caplog.at_level(logging.INFO, logger="coderouter"):
        resp = await adapter.generate(_oai_request())

    assert resp.choices[0]["message"]["content"] == "clean"
    recs = [r for r in caplog.records if r.msg == "output-filter-applied"]
    assert recs == []


@pytest.mark.asyncio
async def test_oai_stream_strips_thinking_across_chunks(
    httpx_mock: HTTPXMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``<think>`` split across two chunks → fully scrubbed, log fires once."""
    body = _sse(
        [
            _sse_chunk({"content": "hello <thi"}),
            _sse_chunk({"content": "nk>secret</think> world"}),
            _sse_chunk({"content": ""}, finish_reason="stop"),
        ]
    )
    httpx_mock.add_response(
        url="http://localhost:11434/v1/chat/completions",
        method="POST",
        content=body,
        headers={"content-type": "text/event-stream"},
    )

    adapter = OpenAICompatAdapter(_oai_provider(["strip_thinking"]))
    req = _oai_request()
    req.stream = True
    with caplog.at_level(logging.INFO, logger="coderouter"):
        chunks = [c async for c in adapter.stream(req)]

    # Concatenate all delta.content we observed.
    observed = "".join(
        (c.choices[0].get("delta", {}) or {}).get("content", "") or "" for c in chunks
    )
    assert observed == "hello  world"

    recs = [r for r in caplog.records if r.msg == "output-filter-applied"]
    assert len(recs) == 1
    assert recs[0].streaming is True
    assert recs[0].filters == ["strip_thinking"]


@pytest.mark.asyncio
async def test_oai_stream_strips_stop_marker_split_across_chunks(
    httpx_mock: HTTPXMock,
) -> None:
    body = _sse(
        [
            _sse_chunk({"content": "keep<|pyth"}),
            _sse_chunk({"content": "on_tag|>more"}),
            _sse_chunk({"content": ""}, finish_reason="stop"),
        ]
    )
    httpx_mock.add_response(
        url="http://localhost:11434/v1/chat/completions",
        method="POST",
        content=body,
        headers={"content-type": "text/event-stream"},
    )

    adapter = OpenAICompatAdapter(_oai_provider(["strip_stop_markers"]))
    req = _oai_request()
    req.stream = True
    chunks = [c async for c in adapter.stream(req)]

    observed = "".join(
        (c.choices[0].get("delta", {}) or {}).get("content", "") or "" for c in chunks
    )
    assert observed == "keepmore"


@pytest.mark.asyncio
async def test_oai_stream_flushes_safe_tail_at_done(
    httpx_mock: HTTPXMock,
) -> None:
    """Partial-looking suffix that turns out to NOT be a tag must be
    released at [DONE] via a synthetic flush chunk."""
    body = _sse(
        [
            # Ends with "<|" — could be the start of any marker. The
            # filter holds it back until eof confirms it's not a marker.
            _sse_chunk({"content": "answer<|"}),
            _sse_chunk({"content": ""}, finish_reason="stop"),
        ]
    )
    httpx_mock.add_response(
        url="http://localhost:11434/v1/chat/completions",
        method="POST",
        content=body,
        headers={"content-type": "text/event-stream"},
    )

    adapter = OpenAICompatAdapter(_oai_provider(["strip_stop_markers"]))
    req = _oai_request()
    req.stream = True
    chunks = [c async for c in adapter.stream(req)]

    observed = "".join(
        (c.choices[0].get("delta", {}) or {}).get("content", "") or "" for c in chunks
    )
    # The dangling `<|` is legitimate content at EOF — must be released.
    assert observed == "answer<|"


@pytest.mark.asyncio
async def test_oai_stream_empty_filters_does_not_flush(
    httpx_mock: HTTPXMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No filters configured → no state, no synthetic flush chunk."""
    body = _sse(
        [
            _sse_chunk({"content": "hello "}),
            _sse_chunk({"content": "<|partial"}),
            _sse_chunk({"content": ""}, finish_reason="stop"),
        ]
    )
    httpx_mock.add_response(
        url="http://localhost:11434/v1/chat/completions",
        method="POST",
        content=body,
        headers={"content-type": "text/event-stream"},
    )

    adapter = OpenAICompatAdapter(_oai_provider([]))
    req = _oai_request()
    req.stream = True
    with caplog.at_level(logging.INFO, logger="coderouter"):
        chunks = [c async for c in adapter.stream(req)]

    observed = "".join(
        (c.choices[0].get("delta", {}) or {}).get("content", "") or "" for c in chunks
    )
    assert observed == "hello <|partial"
    recs = [r for r in caplog.records if r.msg == "output-filter-applied"]
    assert recs == []


# ======================================================================
# Anthropic native adapter
# ======================================================================


def _anth_provider(filters: list[str] | None = None) -> ProviderConfig:
    return ProviderConfig(
        name="anthropic-claude",
        kind="anthropic",
        base_url="https://api.anthropic.com",
        model="claude-3-5-sonnet-latest",
        api_key_env=None,
        output_filters=filters or [],
    )


def _anth_request() -> AnthropicRequest:
    return AnthropicRequest(
        model="claude",
        max_tokens=128,
        messages=[{"role": "user", "content": "hi"}],
    )


@pytest.mark.asyncio
async def test_anth_generate_strips_thinking_in_text_block(
    httpx_mock: HTTPXMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``<think>...</think>`` embedded in an Anthropic text block."""
    httpx_mock.add_response(
        url="https://api.anthropic.com/v1/messages",
        method="POST",
        json={
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": "claude-3-5-sonnet-latest",
            "content": [
                {
                    "type": "text",
                    "text": "prefix <think>hidden</think> tail",
                }
            ],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
    )

    adapter = AnthropicAdapter(_anth_provider(["strip_thinking"]))
    with caplog.at_level(logging.INFO, logger="coderouter"):
        resp = await adapter.generate_anthropic(_anth_request())

    assert resp.content[0]["text"] == "prefix  tail"
    recs = [r for r in caplog.records if r.msg == "output-filter-applied"]
    assert len(recs) == 1
    assert recs[0].streaming is False


@pytest.mark.asyncio
async def test_anth_generate_does_not_touch_tool_use_block(
    httpx_mock: HTTPXMock,
) -> None:
    """Only ``type: text`` blocks get filtered; tool_use blocks pass through."""
    httpx_mock.add_response(
        url="https://api.anthropic.com/v1/messages",
        method="POST",
        json={
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": "claude-3-5-sonnet-latest",
            "content": [
                {"type": "text", "text": "a<think>x</think>b"},
                {
                    "type": "tool_use",
                    "id": "tu_1",
                    "name": "echo",
                    "input": {"message": "<think>kept</think>"},
                },
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
    )

    adapter = AnthropicAdapter(_anth_provider(["strip_thinking"]))
    resp = await adapter.generate_anthropic(_anth_request())
    assert resp.content[0]["text"] == "ab"
    # tool_use.input preserved verbatim — filter MUST NOT descend into it.
    assert resp.content[1]["input"] == {"message": "<think>kept</think>"}


@pytest.mark.asyncio
async def test_anth_stream_strips_thinking_across_text_deltas(
    httpx_mock: HTTPXMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Text deltas split across SSE events — ``<think>`` scrubbed end-to-end."""
    events = [
        ("message_start", {"type": "message_start"}),
        (
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "hi <thi"},
            },
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "nk>secret</think> end"},
            },
        ),
        (
            "content_block_stop",
            {"type": "content_block_stop", "index": 0},
        ),
        ("message_stop", {"type": "message_stop"}),
    ]
    sse = ""
    for evt_type, data in events:
        sse += f"event: {evt_type}\ndata: {json.dumps(data)}\n\n"

    httpx_mock.add_response(
        url="https://api.anthropic.com/v1/messages",
        method="POST",
        content=sse.encode("utf-8"),
        headers={"content-type": "text/event-stream"},
    )

    adapter = AnthropicAdapter(_anth_provider(["strip_thinking"]))
    with caplog.at_level(logging.INFO, logger="coderouter"):
        emitted = [e async for e in adapter.stream_anthropic(_anth_request())]

    # Collect all text_delta bytes we saw downstream.
    text_seen = ""
    for evt in emitted:
        if evt.type == "content_block_delta":
            delta = evt.data.get("delta") or {}
            if delta.get("type") == "text_delta":
                text_seen += delta.get("text", "") or ""
    assert text_seen == "hi  end"

    # Every content_block_delta event must come BEFORE its
    # content_block_stop — i.e. the flush tail is inserted before stop.
    stop_idx = next(i for i, e in enumerate(emitted) if e.type == "content_block_stop")
    for _i, e in enumerate(emitted[:stop_idx]):
        assert e.type != "content_block_stop"

    recs = [r for r in caplog.records if r.msg == "output-filter-applied"]
    assert len(recs) == 1
    assert recs[0].streaming is True


@pytest.mark.asyncio
async def test_anth_stream_no_filter_passes_through(
    httpx_mock: HTTPXMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Provider with empty chain → events pass verbatim, no log."""
    events = [
        (
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "<think>raw</think>"},
            },
        ),
        (
            "content_block_stop",
            {"type": "content_block_stop", "index": 0},
        ),
    ]
    sse = ""
    for evt_type, data in events:
        sse += f"event: {evt_type}\ndata: {json.dumps(data)}\n\n"

    httpx_mock.add_response(
        url="https://api.anthropic.com/v1/messages",
        method="POST",
        content=sse.encode("utf-8"),
        headers={"content-type": "text/event-stream"},
    )

    adapter = AnthropicAdapter(_anth_provider([]))
    with caplog.at_level(logging.INFO, logger="coderouter"):
        emitted = [e async for e in adapter.stream_anthropic(_anth_request())]

    text_seen = ""
    for evt in emitted:
        if evt.type == "content_block_delta":
            delta = evt.data.get("delta") or {}
            if delta.get("type") == "text_delta":
                text_seen += delta.get("text", "") or ""
    assert text_seen == "<think>raw</think>"
    recs = [r for r in caplog.records if r.msg == "output-filter-applied"]
    assert recs == []
