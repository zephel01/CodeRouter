"""Unit tests for the v0.4-A reverse translation layer.

Covers the "OpenAI → Anthropic" direction, complementing the forward
direction tested in ``test_translation_anthropic.py``:

    to_anthropic_request                ChatRequest → AnthropicRequest
    to_chat_response                    AnthropicResponse → ChatResponse
    stream_anthropic_to_chat_chunks     AnthropicStreamEvent* → StreamChunk*

These helpers back the AnthropicAdapter.generate / .stream contract when
the ingress is /v1/chat/completions but the provider is kind:anthropic
(design symmetry with to_chat_request / to_anthropic_response).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

from coderouter.adapters.base import (
    AdapterError,
    ChatRequest,
    Message,
)
from coderouter.translation import (
    AnthropicResponse,
    AnthropicStreamEvent,
    AnthropicUsage,
    stream_anthropic_to_chat_chunks,
    to_anthropic_request,
    to_chat_response,
)

# ============================================================
# to_anthropic_request
# ============================================================


def test_to_anthropic_request_simple_text() -> None:
    req = ChatRequest(messages=[Message(role="user", content="hi")])
    anth = to_anthropic_request(req)
    assert anth.max_tokens == 4096  # default applied
    assert len(anth.messages) == 1
    assert anth.messages[0].role == "user"
    assert anth.messages[0].content == "hi"
    assert anth.system is None
    assert anth.stream is False


def test_to_anthropic_request_system_lifted_to_top_level() -> None:
    req = ChatRequest(
        messages=[
            Message(role="system", content="you are terse"),
            Message(role="user", content="hi"),
        ]
    )
    anth = to_anthropic_request(req)
    assert anth.system == "you are terse"
    # System message is NOT represented as a message turn — only the user turn.
    roles = [m.role for m in anth.messages]
    assert roles == ["user"]


def test_to_anthropic_request_multiple_system_messages_joined() -> None:
    """OpenAI allows repeated system messages; Anthropic takes a single string."""
    req = ChatRequest(
        messages=[
            Message(role="system", content="be terse"),
            Message(role="system", content="answer in Japanese"),
            Message(role="user", content="hi"),
        ]
    )
    anth = to_anthropic_request(req)
    assert "be terse" in anth.system
    assert "answer in Japanese" in anth.system


def test_to_anthropic_request_system_list_content_flattened() -> None:
    """System content as a list of text parts (OpenAI multimodal-style) flattens."""
    req = ChatRequest(
        messages=[
            Message(
                role="system",
                content=[
                    {"type": "text", "text": "be terse"},
                    {"type": "text", "text": "only JSON"},
                ],
            ),
            Message(role="user", content="hi"),
        ]
    )
    anth = to_anthropic_request(req)
    assert "be terse" in anth.system
    assert "only JSON" in anth.system


def test_to_anthropic_request_assistant_tool_calls_become_tool_use_blocks() -> None:
    req = ChatRequest(
        messages=[
            Message(role="user", content="weather?"),
            Message(
                role="assistant",
                content="checking",
                tool_calls=[
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "get_weather",
                            "arguments": '{"city": "Tokyo"}',
                        },
                    }
                ],
            ),
        ]
    )
    anth = to_anthropic_request(req)
    asst = anth.messages[1]
    assert asst.role == "assistant"
    assert isinstance(asst.content, list)
    types = [b.get("type") for b in asst.content]
    assert types == ["text", "tool_use"]
    tool_use = asst.content[1]
    assert tool_use["name"] == "get_weather"
    assert tool_use["id"] == "call_1"
    assert tool_use["input"] == {"city": "Tokyo"}


def test_to_anthropic_request_consecutive_tool_messages_batch_into_one_user_turn() -> None:
    """Anthropic's canonical shape: multiple tool_result blocks ride on a single user turn."""
    req = ChatRequest(
        messages=[
            Message(role="user", content="parallel tools"),
            Message(
                role="assistant",
                content=None,
                tool_calls=[
                    {
                        "id": "call_a",
                        "type": "function",
                        "function": {"name": "a", "arguments": "{}"},
                    },
                    {
                        "id": "call_b",
                        "type": "function",
                        "function": {"name": "b", "arguments": "{}"},
                    },
                ],
            ),
            Message(role="tool", tool_call_id="call_a", content="A-ok"),
            Message(role="tool", tool_call_id="call_b", content="B-ok"),
            Message(role="user", content="great, continue"),
        ]
    )
    anth = to_anthropic_request(req)
    roles = [m.role for m in anth.messages]
    # user / assistant(tool_use*2) / user(tool_result*2) / user(continue)
    assert roles == ["user", "assistant", "user", "user"]
    tool_result_turn = anth.messages[2].content
    assert isinstance(tool_result_turn, list)
    assert len(tool_result_turn) == 2
    assert tool_result_turn[0]["tool_use_id"] == "call_a"
    assert tool_result_turn[0]["content"] == "A-ok"
    assert tool_result_turn[1]["tool_use_id"] == "call_b"


def test_to_anthropic_request_tool_followed_by_user_flushes_correctly() -> None:
    """Tool message followed by a user message emits the tool_result turn
    first, then the user turn — preserving conversation order."""
    req = ChatRequest(
        messages=[
            Message(
                role="assistant",
                content=None,
                tool_calls=[
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "x", "arguments": "{}"},
                    }
                ],
            ),
            Message(role="tool", tool_call_id="call_1", content="result"),
            Message(role="user", content="ok"),
        ]
    )
    anth = to_anthropic_request(req)
    assert [m.role for m in anth.messages] == ["assistant", "user", "user"]
    assert anth.messages[1].content[0]["type"] == "tool_result"
    assert anth.messages[2].content == "ok"


def test_to_anthropic_request_image_data_uri_becomes_base64_source() -> None:
    req = ChatRequest(
        messages=[
            Message(
                role="user",
                content=[
                    {"type": "text", "text": "describe this"},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "data:image/jpeg;base64,AAAA",
                        },
                    },
                ],
            )
        ]
    )
    anth = to_anthropic_request(req)
    content = anth.messages[0].content
    assert isinstance(content, list)
    # text + image blocks preserved
    assert content[0] == {"type": "text", "text": "describe this"}
    img = content[1]
    assert img["type"] == "image"
    assert img["source"] == {
        "type": "base64",
        "media_type": "image/jpeg",
        "data": "AAAA",
    }


def test_to_anthropic_request_image_remote_url_becomes_url_source() -> None:
    req = ChatRequest(
        messages=[
            Message(
                role="user",
                content=[
                    {
                        "type": "image_url",
                        "image_url": {"url": "https://example.com/x.png"},
                    }
                ],
            )
        ]
    )
    anth = to_anthropic_request(req)
    img = anth.messages[0].content[0]
    assert img["source"] == {
        "type": "url",
        "url": "https://example.com/x.png",
    }


def test_to_anthropic_request_tools_translated_to_input_schema_shape() -> None:
    req = ChatRequest(
        messages=[Message(role="user", content="hi")],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "Get current weather",
                    "parameters": {
                        "type": "object",
                        "properties": {"city": {"type": "string"}},
                    },
                },
            }
        ],
    )
    anth = to_anthropic_request(req)
    assert anth.tools is not None
    t = anth.tools[0]
    assert t.name == "get_weather"
    assert t.description == "Get current weather"
    # OpenAI `parameters` → Anthropic `input_schema`.
    assert t.input_schema["type"] == "object"
    assert "city" in t.input_schema["properties"]


@pytest.mark.parametrize(
    "openai_tc,expected_anth",
    [
        ("auto", {"type": "auto"}),
        ("none", {"type": "none"}),
        ("required", {"type": "any"}),
        (
            {"type": "function", "function": {"name": "foo"}},
            {"type": "tool", "name": "foo"},
        ),
    ],
)
def test_to_anthropic_request_tool_choice_map(
    openai_tc: Any, expected_anth: dict[str, Any]
) -> None:
    req = ChatRequest(
        messages=[Message(role="user", content="hi")],
        tool_choice=openai_tc,
    )
    anth = to_anthropic_request(req)
    assert anth.tool_choice == expected_anth


def test_to_anthropic_request_max_tokens_passes_through() -> None:
    req = ChatRequest(
        messages=[Message(role="user", content="hi")],
        max_tokens=256,
    )
    anth = to_anthropic_request(req)
    assert anth.max_tokens == 256


def test_to_anthropic_request_non_json_tool_arguments_preserved_as_raw() -> None:
    """Malformed JSON in tool_calls.arguments is kept in {_raw: ...} so the
    upstream model sees something rather than silently losing the data."""
    req = ChatRequest(
        messages=[
            Message(
                role="assistant",
                content=None,
                tool_calls=[
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "x",
                            "arguments": '{"broken",',  # not valid JSON
                        },
                    }
                ],
            )
        ]
    )
    anth = to_anthropic_request(req)
    tool_use = anth.messages[0].content[0]
    assert tool_use["input"] == {"_raw": '{"broken",'}


def test_to_anthropic_request_empty_user_content_skipped() -> None:
    """An empty user content collapse shouldn't produce a zero-block turn
    (Anthropic rejects those)."""
    req = ChatRequest(
        messages=[
            Message(role="user", content=""),
            Message(role="user", content="hi"),
        ]
    )
    anth = to_anthropic_request(req)
    # The empty turn is dropped; only the real "hi" turn survives.
    assert [m.content for m in anth.messages] == ["hi"]


def test_to_anthropic_request_assistant_no_content_no_tool_calls_emits_empty_text() -> None:
    """Edge case: an assistant message with neither content nor tool_calls
    still has to produce a syntactically valid Anthropic turn."""
    req = ChatRequest(
        messages=[
            Message(role="user", content="hi"),
            Message(role="assistant", content=None),
        ]
    )
    anth = to_anthropic_request(req)
    asst = anth.messages[1]
    assert asst.role == "assistant"
    assert asst.content == [{"type": "text", "text": ""}]


def test_to_anthropic_request_preserves_stream_and_profile_and_stop() -> None:
    req = ChatRequest(
        messages=[Message(role="user", content="hi")],
        stream=True,
        stop=["<END>"],
    )
    req.profile = "coding"
    anth = to_anthropic_request(req)
    assert anth.stream is True
    assert anth.stop_sequences == ["<END>"]
    assert anth.profile == "coding"


# ============================================================
# to_chat_response
# ============================================================


def test_to_chat_response_text_only() -> None:
    resp = AnthropicResponse(
        id="msg_1",
        model="claude-x",
        content=[{"type": "text", "text": "hello world"}],
        stop_reason="end_turn",
        usage=AnthropicUsage(input_tokens=3, output_tokens=2),
        coderouter_provider="anthropic-direct",
    )
    chat = to_chat_response(resp)
    assert chat.id == "msg_1"
    assert chat.model == "claude-x"
    assert chat.choices[0]["message"]["content"] == "hello world"
    assert chat.choices[0]["message"].get("tool_calls") is None
    assert chat.choices[0]["finish_reason"] == "stop"
    assert chat.usage["prompt_tokens"] == 3
    assert chat.usage["completion_tokens"] == 2
    assert chat.usage["total_tokens"] == 5
    assert chat.coderouter_provider == "anthropic-direct"


def test_to_chat_response_tool_use_becomes_tool_calls() -> None:
    resp = AnthropicResponse(
        id="msg_2",
        model="claude-x",
        content=[
            {
                "type": "tool_use",
                "id": "toolu_1",
                "name": "get_weather",
                "input": {"city": "NYC"},
            }
        ],
        stop_reason="tool_use",
        usage=AnthropicUsage(input_tokens=10, output_tokens=5),
    )
    chat = to_chat_response(resp)
    msg = chat.choices[0]["message"]
    # With no text blocks, content is null.
    assert msg["content"] is None
    tc = msg["tool_calls"][0]
    assert tc["id"] == "toolu_1"
    assert tc["type"] == "function"
    assert tc["function"]["name"] == "get_weather"
    assert json.loads(tc["function"]["arguments"]) == {"city": "NYC"}
    assert chat.choices[0]["finish_reason"] == "tool_calls"


def test_to_chat_response_mixed_text_and_tool_use() -> None:
    resp = AnthropicResponse(
        id="msg_3",
        model="claude-x",
        content=[
            {"type": "text", "text": "Let me check."},
            {
                "type": "tool_use",
                "id": "toolu_1",
                "name": "search",
                "input": {"q": "x"},
            },
        ],
        stop_reason="tool_use",
        usage=AnthropicUsage(input_tokens=1, output_tokens=1),
    )
    chat = to_chat_response(resp)
    msg = chat.choices[0]["message"]
    assert msg["content"] == "Let me check."
    assert len(msg["tool_calls"]) == 1


def test_to_chat_response_multiple_text_blocks_concatenated() -> None:
    """Anthropic may split text around tool_use; OpenAI has a single content field."""
    resp = AnthropicResponse(
        id="msg_4",
        model="claude-x",
        content=[
            {"type": "text", "text": "part1 "},
            {"type": "text", "text": "part2"},
        ],
        stop_reason="end_turn",
        usage=AnthropicUsage(input_tokens=1, output_tokens=1),
    )
    chat = to_chat_response(resp)
    assert chat.choices[0]["message"]["content"] == "part1 part2"


@pytest.mark.parametrize(
    "anth_reason,openai_reason",
    [
        ("end_turn", "stop"),
        ("max_tokens", "length"),
        ("tool_use", "tool_calls"),
        ("stop_sequence", "stop"),
    ],
)
def test_to_chat_response_stop_reason_map(
    anth_reason: str, openai_reason: str
) -> None:
    resp = AnthropicResponse(
        id="msg_sr",
        model="x",
        content=[{"type": "text", "text": "x"}],
        stop_reason=anth_reason,
        usage=AnthropicUsage(),
    )
    chat = to_chat_response(resp)
    assert chat.choices[0]["finish_reason"] == openai_reason


# ============================================================
# stream_anthropic_to_chat_chunks
# ============================================================


async def _as_stream(events: list[AnthropicStreamEvent]) -> AsyncIterator[AnthropicStreamEvent]:
    for e in events:
        yield e


def _evt(type_: str, data: dict[str, Any]) -> AnthropicStreamEvent:
    return AnthropicStreamEvent(type=type_, data={"type": type_, **data})


@pytest.mark.asyncio
async def test_stream_reverse_text_only() -> None:
    events = [
        _evt(
            "message_start",
            {
                "message": {
                    "id": "msg_xyz",
                    "type": "message",
                    "role": "assistant",
                    "model": "claude-s",
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 7, "output_tokens": 0},
                }
            },
        ),
        _evt(
            "content_block_start",
            {"index": 0, "content_block": {"type": "text", "text": ""}},
        ),
        _evt(
            "content_block_delta",
            {"index": 0, "delta": {"type": "text_delta", "text": "hello "}},
        ),
        _evt(
            "content_block_delta",
            {"index": 0, "delta": {"type": "text_delta", "text": "world"}},
        ),
        _evt("content_block_stop", {"index": 0}),
        _evt(
            "message_delta",
            {
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": 4},
            },
        ),
        _evt("message_stop", {}),
    ]
    chunks = [
        c
        async for c in stream_anthropic_to_chat_chunks(
            _as_stream(events), provider_name="test"
        )
    ]
    # First chunk: role=assistant.
    assert chunks[0].choices[0]["delta"].get("role") == "assistant"
    # Content chunks carry the deltas verbatim.
    content_deltas = [
        c.choices[0]["delta"].get("content")
        for c in chunks
        if c.choices and c.choices[0].get("delta", {}).get("content")
    ]
    assert content_deltas == ["hello ", "world"]
    # Finish chunk: end_turn → stop.
    finish = [
        c for c in chunks if c.choices and c.choices[0].get("finish_reason")
    ]
    assert finish[-1].choices[0]["finish_reason"] == "stop"
    # Trailing usage chunk (no choices).
    assert chunks[-1].choices == []
    assert chunks[-1].usage["prompt_tokens"] == 7
    assert chunks[-1].usage["completion_tokens"] == 4
    assert chunks[-1].usage["total_tokens"] == 11
    # Stream id propagated from message_start.
    assert chunks[0].id == "msg_xyz"


@pytest.mark.asyncio
async def test_stream_reverse_tool_use_emits_tool_calls_deltas() -> None:
    events = [
        _evt(
            "message_start",
            {
                "message": {
                    "id": "msg_tu",
                    "type": "message",
                    "role": "assistant",
                    "model": "claude-s",
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 3, "output_tokens": 0},
                }
            },
        ),
        _evt(
            "content_block_start",
            {
                "index": 0,
                "content_block": {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "search",
                    "input": {},
                },
            },
        ),
        _evt(
            "content_block_delta",
            {
                "index": 0,
                "delta": {
                    "type": "input_json_delta",
                    "partial_json": '{"q":',
                },
            },
        ),
        _evt(
            "content_block_delta",
            {
                "index": 0,
                "delta": {
                    "type": "input_json_delta",
                    "partial_json": '"x"}',
                },
            },
        ),
        _evt("content_block_stop", {"index": 0}),
        _evt(
            "message_delta",
            {
                "delta": {"stop_reason": "tool_use", "stop_sequence": None},
                "usage": {"output_tokens": 7},
            },
        ),
        _evt("message_stop", {}),
    ]
    chunks = [
        c
        async for c in stream_anthropic_to_chat_chunks(
            _as_stream(events), provider_name="test"
        )
    ]
    # Find tool_calls-bearing chunks.
    tc_chunks = [
        c
        for c in chunks
        if c.choices and c.choices[0].get("delta", {}).get("tool_calls")
    ]
    # At least: one for block_start (name + empty args), plus one per partial_json.
    assert len(tc_chunks) >= 3
    start_delta = tc_chunks[0].choices[0]["delta"]["tool_calls"][0]
    assert start_delta["index"] == 0
    assert start_delta["id"] == "toolu_1"
    assert start_delta["function"]["name"] == "search"
    # Concatenated partial_json reconstructs the original input.
    fragments = [
        c.choices[0]["delta"]["tool_calls"][0]["function"].get("arguments", "")
        for c in tc_chunks
    ]
    assert "".join(fragments) == '{"q":"x"}'
    # Finish chunk: tool_use → tool_calls.
    finish = [
        c for c in chunks if c.choices and c.choices[0].get("finish_reason")
    ]
    assert finish[-1].choices[0]["finish_reason"] == "tool_calls"


@pytest.mark.asyncio
async def test_stream_reverse_multiple_tool_use_blocks_get_distinct_indices() -> None:
    """Parallel tool calls: Anthropic block indices 0,1 → OpenAI tool_calls[].index 0,1."""
    events = [
        _evt(
            "message_start",
            {
                "message": {
                    "id": "m",
                    "type": "message",
                    "role": "assistant",
                    "model": "claude",
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 1, "output_tokens": 0},
                }
            },
        ),
        _evt(
            "content_block_start",
            {
                "index": 0,
                "content_block": {
                    "type": "tool_use",
                    "id": "toolu_a",
                    "name": "a",
                    "input": {},
                },
            },
        ),
        _evt(
            "content_block_delta",
            {
                "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": "{}"},
            },
        ),
        _evt("content_block_stop", {"index": 0}),
        _evt(
            "content_block_start",
            {
                "index": 1,
                "content_block": {
                    "type": "tool_use",
                    "id": "toolu_b",
                    "name": "b",
                    "input": {},
                },
            },
        ),
        _evt(
            "content_block_delta",
            {
                "index": 1,
                "delta": {"type": "input_json_delta", "partial_json": "{}"},
            },
        ),
        _evt("content_block_stop", {"index": 1}),
        _evt(
            "message_delta",
            {
                "delta": {"stop_reason": "tool_use", "stop_sequence": None},
                "usage": {"output_tokens": 1},
            },
        ),
        _evt("message_stop", {}),
    ]
    chunks = [
        c
        async for c in stream_anthropic_to_chat_chunks(
            _as_stream(events), provider_name="test"
        )
    ]
    tc_chunks = [
        c
        for c in chunks
        if c.choices and c.choices[0].get("delta", {}).get("tool_calls")
    ]
    # First tool_use block → tool_calls[0].index=0 with name=a.
    # Second → tool_calls[0].index=1 with name=b.
    names_by_index: dict[int, str] = {}
    for c in tc_chunks:
        for tc in c.choices[0]["delta"]["tool_calls"]:
            name = tc.get("function", {}).get("name")
            if name:
                names_by_index[tc["index"]] = name
    assert names_by_index == {0: "a", 1: "b"}


@pytest.mark.asyncio
async def test_stream_reverse_event_error_raises_non_retryable() -> None:
    events = [
        _evt(
            "message_start",
            {
                "message": {
                    "id": "m",
                    "type": "message",
                    "role": "assistant",
                    "model": "claude",
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                }
            },
        ),
        _evt(
            "error",
            {
                "error": {
                    "type": "overloaded_error",
                    "message": "service overloaded",
                }
            },
        ),
    ]
    produced: list = []
    with pytest.raises(AdapterError) as info:
        async for chunk in stream_anthropic_to_chat_chunks(
            _as_stream(events), provider_name="anthropic-native"
        ):
            produced.append(chunk)
    # Initial role chunk was already delivered.
    assert produced
    assert info.value.retryable is False
    assert info.value.provider == "anthropic-native"
