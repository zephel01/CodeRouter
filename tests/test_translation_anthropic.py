"""Unit tests for the Anthropic ⇄ internal translation layer.

Covers:
    - Request translation (system, multi-turn, tool_use + tool_result, images)
    - Response translation (text only, tool_use, stop_reason mapping)
    - Stream translation (text-only, tool_use, mixed, multi-tool-call)
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

from coderouter.adapters.base import ChatResponse, StreamChunk
from coderouter.translation import (
    AnthropicRequest,
    stream_chat_to_anthropic_events,
    to_anthropic_response,
    to_chat_request,
)


# ============================================================
# Request translation
# ============================================================


def test_simple_text_request() -> None:
    req = AnthropicRequest.model_validate(
        {
            "model": "claude-any",
            "max_tokens": 100,
            "messages": [{"role": "user", "content": "hi"}],
        }
    )
    chat = to_chat_request(req)
    assert chat.messages[0].role == "user"
    assert chat.messages[0].content == "hi"
    assert chat.max_tokens == 100
    assert chat.stream is False


def test_system_string_becomes_system_message() -> None:
    req = AnthropicRequest.model_validate(
        {
            "model": "claude",
            "max_tokens": 50,
            "system": "You are a helpful coder.",
            "messages": [{"role": "user", "content": "hi"}],
        }
    )
    chat = to_chat_request(req)
    assert chat.messages[0].role == "system"
    assert chat.messages[0].content == "You are a helpful coder."
    assert chat.messages[1].role == "user"


def test_system_block_list_joined_as_text() -> None:
    req = AnthropicRequest.model_validate(
        {
            "model": "claude",
            "max_tokens": 50,
            "system": [
                {"type": "text", "text": "You are a coder."},
                {"type": "text", "text": "Always answer in Japanese."},
            ],
            "messages": [{"role": "user", "content": "hi"}],
        }
    )
    chat = to_chat_request(req)
    assert chat.messages[0].role == "system"
    assert "You are a coder." in chat.messages[0].content
    assert "Always answer in Japanese." in chat.messages[0].content


def test_tool_use_and_tool_result_round_trip() -> None:
    """Multi-turn: user asks, assistant tool_uses, user tool_results, user asks."""
    req = AnthropicRequest.model_validate(
        {
            "model": "claude",
            "max_tokens": 100,
            "messages": [
                {"role": "user", "content": "what's the weather?"},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "Let me check."},
                        {
                            "type": "tool_use",
                            "id": "toolu_abc",
                            "name": "get_weather",
                            "input": {"location": "Tokyo"},
                        },
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_abc",
                            "content": "Sunny, 20C",
                        }
                    ],
                },
            ],
        }
    )
    chat = to_chat_request(req)

    # Expected OpenAI shape:
    # [0] user: "what's the weather?"
    # [1] assistant: content="Let me check.", tool_calls=[{id:toolu_abc,...}]
    # [2] tool: tool_call_id=toolu_abc, content="Sunny, 20C"
    assert [m.role for m in chat.messages] == ["user", "assistant", "tool"]
    assistant = chat.messages[1]
    assert assistant.content == "Let me check."
    assert assistant.tool_calls is not None
    assert assistant.tool_calls[0]["id"] == "toolu_abc"
    assert assistant.tool_calls[0]["function"]["name"] == "get_weather"
    assert json.loads(assistant.tool_calls[0]["function"]["arguments"]) == {
        "location": "Tokyo"
    }
    tool_msg = chat.messages[2]
    assert tool_msg.tool_call_id == "toolu_abc"
    assert tool_msg.content == "Sunny, 20C"


def test_tools_array_is_converted() -> None:
    req = AnthropicRequest.model_validate(
        {
            "model": "claude",
            "max_tokens": 50,
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [
                {
                    "name": "get_weather",
                    "description": "Lookup weather",
                    "input_schema": {
                        "type": "object",
                        "properties": {"location": {"type": "string"}},
                    },
                }
            ],
        }
    )
    chat = to_chat_request(req)
    assert chat.tools is not None
    assert chat.tools[0] == {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Lookup weather",
            "parameters": {
                "type": "object",
                "properties": {"location": {"type": "string"}},
            },
        },
    }


def test_tool_choice_mapping() -> None:
    for anth_tc, expected in [
        ({"type": "auto"}, "auto"),
        ({"type": "any"}, "required"),
        ({"type": "none"}, "none"),
        (
            {"type": "tool", "name": "foo"},
            {"type": "function", "function": {"name": "foo"}},
        ),
    ]:
        req = AnthropicRequest.model_validate(
            {
                "model": "claude",
                "max_tokens": 10,
                "messages": [{"role": "user", "content": "hi"}],
                "tool_choice": anth_tc,
            }
        )
        chat = to_chat_request(req)
        assert chat.tool_choice == expected, anth_tc


def test_stop_sequences_map_to_stop() -> None:
    req = AnthropicRequest.model_validate(
        {
            "model": "claude",
            "max_tokens": 10,
            "messages": [{"role": "user", "content": "hi"}],
            "stop_sequences": ["```", "END"],
        }
    )
    chat = to_chat_request(req)
    assert chat.stop == ["```", "END"]


def test_profile_is_propagated() -> None:
    req = AnthropicRequest.model_validate(
        {
            "model": "claude",
            "max_tokens": 10,
            "messages": [{"role": "user", "content": "hi"}],
            "profile": "fast",
        }
    )
    chat = to_chat_request(req)
    assert chat.profile == "fast"


# ============================================================
# Response translation
# ============================================================


def _make_chat_response(
    *,
    content: str | None = "Hello!",
    tool_calls: list[dict[str, Any]] | None = None,
    finish_reason: str = "stop",
    usage: dict[str, int] | None = None,
) -> ChatResponse:
    msg: dict[str, Any] = {"role": "assistant"}
    if content is not None:
        msg["content"] = content
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return ChatResponse(
        id="chatcmpl-1",
        object="chat.completion",
        created=0,
        model="some-model",
        choices=[
            {"index": 0, "message": msg, "finish_reason": finish_reason}
        ],
        usage=usage or {"prompt_tokens": 5, "completion_tokens": 3},
        coderouter_provider="provider-x",
    )


def test_text_response_becomes_text_content_block() -> None:
    resp = to_anthropic_response(_make_chat_response(content="Hi there"))
    assert resp.content == [{"type": "text", "text": "Hi there"}]
    assert resp.stop_reason == "end_turn"
    assert resp.usage.input_tokens == 5
    assert resp.usage.output_tokens == 3
    assert resp.coderouter_provider == "provider-x"
    assert resp.id.startswith("msg_")


def test_tool_call_becomes_tool_use_block() -> None:
    resp = to_anthropic_response(
        _make_chat_response(
            content=None,
            tool_calls=[
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "arguments": '{"location":"Tokyo"}',
                    },
                }
            ],
            finish_reason="tool_calls",
        )
    )
    assert resp.stop_reason == "tool_use"
    # Empty text from content=None should NOT appear as a text block —
    # only the tool_use block should be present.
    assert len(resp.content) == 1
    block = resp.content[0]
    assert block["type"] == "tool_use"
    assert block["id"] == "call_1"
    assert block["name"] == "get_weather"
    assert block["input"] == {"location": "Tokyo"}


def test_finish_reason_map() -> None:
    pairs = [
        ("stop", "end_turn"),
        ("length", "max_tokens"),
        ("tool_calls", "tool_use"),
        ("content_filter", "end_turn"),
    ]
    for openai_fr, anth_sr in pairs:
        resp = to_anthropic_response(
            _make_chat_response(content="x", finish_reason=openai_fr)
        )
        assert resp.stop_reason == anth_sr


def test_malformed_tool_call_json_preserved_in_raw() -> None:
    resp = to_anthropic_response(
        _make_chat_response(
            content=None,
            tool_calls=[
                {
                    "id": "call_broken",
                    "function": {"name": "foo", "arguments": "not valid json"},
                }
            ],
            finish_reason="tool_calls",
        )
    )
    block = next(b for b in resp.content if b["type"] == "tool_use")
    assert block["input"] == {"_raw": "not valid json"}


def test_empty_response_emits_empty_text_block() -> None:
    resp = to_anthropic_response(
        _make_chat_response(content="", tool_calls=None, finish_reason="stop")
    )
    assert resp.content == [{"type": "text", "text": ""}]


# ============================================================
# Stream translation
# ============================================================


async def _as_async(chunks: list[StreamChunk]) -> AsyncIterator[StreamChunk]:
    for c in chunks:
        yield c


def _chunk(**kw: Any) -> StreamChunk:
    base: dict[str, Any] = {
        "id": kw.pop("id", "chatcmpl-1"),
        "object": "chat.completion.chunk",
        "created": 0,
        "model": kw.pop("model", "m"),
        "choices": kw.pop("choices", [{"index": 0, "delta": {}}]),
    }
    base.update(kw)
    return StreamChunk(**base)


@pytest.mark.asyncio
async def test_stream_text_only_events_in_order() -> None:
    chunks = [
        _chunk(choices=[{"index": 0, "delta": {"role": "assistant"}}]),
        _chunk(choices=[{"index": 0, "delta": {"content": "Hello"}}]),
        _chunk(choices=[{"index": 0, "delta": {"content": " world"}}]),
        _chunk(
            choices=[{"index": 0, "delta": {}, "finish_reason": "stop"}],
        ),
    ]
    events = [
        ev async for ev in stream_chat_to_anthropic_events(_as_async(chunks))
    ]
    types = [e.type for e in events]
    assert types == [
        "message_start",
        "content_block_start",
        "content_block_delta",
        "content_block_delta",
        "content_block_stop",
        "message_delta",
        "message_stop",
    ]
    # deltas carry the text
    deltas = [e.data["delta"]["text"] for e in events if e.type == "content_block_delta"]
    assert deltas == ["Hello", " world"]
    # stop_reason mapped
    stop_evt = next(e for e in events if e.type == "message_delta")
    assert stop_evt.data["delta"]["stop_reason"] == "end_turn"


@pytest.mark.asyncio
async def test_stream_tool_use_events() -> None:
    chunks = [
        _chunk(choices=[{"index": 0, "delta": {"role": "assistant"}}]),
        _chunk(
            choices=[
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "get_weather", "arguments": ""},
                            }
                        ]
                    },
                }
            ]
        ),
        _chunk(
            choices=[
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "function": {"arguments": '{"loca'},
                            }
                        ]
                    },
                }
            ]
        ),
        _chunk(
            choices=[
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "function": {"arguments": 'tion":"Tokyo"}'},
                            }
                        ]
                    },
                }
            ]
        ),
        _chunk(
            choices=[{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
        ),
    ]
    events = [
        ev async for ev in stream_chat_to_anthropic_events(_as_async(chunks))
    ]
    types = [e.type for e in events]
    assert types == [
        "message_start",
        "content_block_start",
        "content_block_delta",
        "content_block_delta",
        "content_block_stop",
        "message_delta",
        "message_stop",
    ]
    start = next(e for e in events if e.type == "content_block_start")
    assert start.data["content_block"] == {
        "type": "tool_use",
        "id": "call_1",
        "name": "get_weather",
        "input": {},
    }
    frags = [
        e.data["delta"]["partial_json"]
        for e in events
        if e.type == "content_block_delta"
    ]
    assert frags == ['{"loca', 'tion":"Tokyo"}']
    stop_evt = next(e for e in events if e.type == "message_delta")
    assert stop_evt.data["delta"]["stop_reason"] == "tool_use"


@pytest.mark.asyncio
async def test_stream_text_then_tool_use_closes_text_first() -> None:
    """When text precedes a tool_call, the text block must stop before tool_use opens."""
    chunks = [
        _chunk(choices=[{"index": 0, "delta": {"content": "thinking..."}}]),
        _chunk(
            choices=[
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "c1",
                                "function": {"name": "f", "arguments": "{}"},
                            }
                        ]
                    },
                }
            ]
        ),
        _chunk(choices=[{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]),
    ]
    events = [
        ev async for ev in stream_chat_to_anthropic_events(_as_async(chunks))
    ]
    types = [e.type for e in events]
    # text block starts, deltas, stops — THEN tool_use block starts.
    assert types == [
        "message_start",
        "content_block_start",    # text idx 0
        "content_block_delta",    # "thinking..."
        "content_block_stop",     # close text
        "content_block_start",    # tool_use idx 1
        "content_block_delta",    # arguments "{}"
        "content_block_stop",     # close tool_use
        "message_delta",
        "message_stop",
    ]
    # Block indices are 0 then 1
    starts = [e for e in events if e.type == "content_block_start"]
    assert starts[0].data["index"] == 0
    assert starts[0].data["content_block"]["type"] == "text"
    assert starts[1].data["index"] == 1
    assert starts[1].data["content_block"]["type"] == "tool_use"


@pytest.mark.asyncio
async def test_stream_multiple_tool_calls_get_distinct_block_indices() -> None:
    chunks = [
        _chunk(
            choices=[
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "a",
                                "function": {"name": "f1", "arguments": "{}"},
                            }
                        ]
                    },
                }
            ]
        ),
        _chunk(
            choices=[
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 1,
                                "id": "b",
                                "function": {"name": "f2", "arguments": "{}"},
                            }
                        ]
                    },
                }
            ]
        ),
        _chunk(choices=[{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]),
    ]
    events = [
        ev async for ev in stream_chat_to_anthropic_events(_as_async(chunks))
    ]
    starts = [e for e in events if e.type == "content_block_start"]
    assert [s.data["index"] for s in starts] == [0, 1]
    assert starts[0].data["content_block"]["name"] == "f1"
    assert starts[1].data["content_block"]["name"] == "f2"
