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
    AnthropicResponse,
    AnthropicUsage,
    stream_chat_to_anthropic_events,
    synthesize_anthropic_stream_from_response,
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
    assert json.loads(assistant.tool_calls[0]["function"]["arguments"]) == {"location": "Tokyo"}
    tool_msg = chat.messages[2]
    assert tool_msg.tool_call_id == "toolu_abc"
    assert tool_msg.content == "Sunny, 20C"


def test_assistant_message_with_only_tool_use_has_null_content() -> None:
    """Regression for v0.3-E crash:

    When an assistant turn in the Anthropic request carries ONLY a tool_use
    block (no text), translation must produce an OpenAI assistant message
    with `content: null` and a populated `tool_calls`. Previously the
    internal `Message` model rejected None — which blew up the route with
    pydantic ValidationError the moment Claude Code's multi-turn history
    included a tool_use-only assistant turn.
    """
    req = AnthropicRequest.model_validate(
        {
            "model": "claude",
            "max_tokens": 100,
            "messages": [
                {"role": "user", "content": "run pwd"},
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_xyz",
                            "name": "Bash",
                            "input": {"command": "pwd"},
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_xyz",
                            "content": "/home/user",
                        }
                    ],
                },
            ],
        }
    )
    chat = to_chat_request(req)
    # Sequence: user → assistant(tool_use) → tool(result)
    assert [m.role for m in chat.messages] == ["user", "assistant", "tool"]
    assistant = chat.messages[1]
    assert assistant.content is None
    assert assistant.tool_calls is not None
    assert assistant.tool_calls[0]["function"]["name"] == "Bash"

    # Serialization with exclude_none must strip the None content so upstream
    # sees `{"role": "assistant", "tool_calls": [...]}` — the OpenAI-compatible
    # shape every upstream accepts.
    dumped = assistant.model_dump(exclude_none=True)
    assert "content" not in dumped
    assert "tool_calls" in dumped


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
        choices=[{"index": 0, "message": msg, "finish_reason": finish_reason}],
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
        resp = to_anthropic_response(_make_chat_response(content="x", finish_reason=openai_fr))
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
# Tool-call repair (v0.3)
# ============================================================


def test_repair_extracts_bare_json_tool_call_from_text() -> None:
    """qwen2.5-coder failure mode: tool call written as JSON in message body."""
    chat_resp = _make_chat_response(
        content=(
            "Let me check the current working directory.\n"
            '{"name": "Bash", "arguments": {"command": "pwd"}}'
        ),
        tool_calls=None,
        finish_reason="stop",
    )
    resp = to_anthropic_response(chat_resp, allowed_tool_names=["Bash", "Read"])
    # Preamble survives as text, tool call is extracted as tool_use block.
    types = [b["type"] for b in resp.content]
    assert types == ["text", "tool_use"]
    assert resp.content[0]["text"] == "Let me check the current working directory."
    assert resp.content[1]["name"] == "Bash"
    assert resp.content[1]["input"] == {"command": "pwd"}
    # stop_reason should be remapped from "end_turn" → "tool_use".
    assert resp.stop_reason == "tool_use"


def test_repair_skipped_when_structured_tool_calls_already_present() -> None:
    """If the model did its job and populated tool_calls, don't double-extract."""
    chat_resp = _make_chat_response(
        content='Describing the call: {"name": "Bash", "arguments": {}}',
        tool_calls=[
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "Bash", "arguments": '{"command": "pwd"}'},
            }
        ],
        finish_reason="tool_calls",
    )
    resp = to_anthropic_response(chat_resp, allowed_tool_names=["Bash"])
    # Structured call wins. The text (including its embedded JSON) is
    # preserved as-is — we don't attempt repair on narration that coexists
    # with a real tool_calls entry.
    types = [b["type"] for b in resp.content]
    assert types == ["text", "tool_use"]
    assert '"name": "Bash"' in resp.content[0]["text"]
    assert resp.content[1]["input"] == {"command": "pwd"}


def test_repair_respects_allowlist() -> None:
    """A JSON object whose name isn't in the request's tool list stays as text."""
    chat_resp = _make_chat_response(
        content='{"name": "NukeEverything", "arguments": {}}',
        tool_calls=None,
        finish_reason="stop",
    )
    resp = to_anthropic_response(chat_resp, allowed_tool_names=["Bash", "Read"])
    # Not repaired — falls through to text content block unchanged.
    assert resp.content == [{"type": "text", "text": '{"name": "NukeEverything", "arguments": {}}'}]
    assert resp.stop_reason == "end_turn"


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
    events = [ev async for ev in stream_chat_to_anthropic_events(_as_async(chunks))]
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
    events = [ev async for ev in stream_chat_to_anthropic_events(_as_async(chunks))]
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
    frags = [e.data["delta"]["partial_json"] for e in events if e.type == "content_block_delta"]
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
    events = [ev async for ev in stream_chat_to_anthropic_events(_as_async(chunks))]
    types = [e.type for e in events]
    # text block starts, deltas, stops — THEN tool_use block starts.
    assert types == [
        "message_start",
        "content_block_start",  # text idx 0
        "content_block_delta",  # "thinking..."
        "content_block_stop",  # close text
        "content_block_start",  # tool_use idx 1
        "content_block_delta",  # arguments "{}"
        "content_block_stop",  # close tool_use
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
    events = [ev async for ev in stream_chat_to_anthropic_events(_as_async(chunks))]
    starts = [e for e in events if e.type == "content_block_start"]
    assert [s.data["index"] for s in starts] == [0, 1]
    assert starts[0].data["content_block"]["name"] == "f1"
    assert starts[1].data["content_block"]["name"] == "f2"


# ----------------------------------------------------------------------
# v0.3-C: Usage aggregation in streaming translation
# ----------------------------------------------------------------------


def _message_delta_usage(events: list[Any]) -> dict[str, Any]:
    md = next(e for e in events if e.type == "message_delta")
    return md.data["usage"]


@pytest.mark.asyncio
async def test_stream_usage_uses_upstream_completion_tokens_when_present() -> None:
    """If the provider sends a usage chunk (stream_options.include_usage), the
    translator must use it verbatim — it's authoritative and typically
    matches what the client will be billed for.
    """
    chunks = [
        _chunk(choices=[{"index": 0, "delta": {"content": "Hi"}}]),
        _chunk(choices=[{"index": 0, "delta": {}, "finish_reason": "stop"}]),
        # Terminal usage-only chunk (OpenAI include_usage pattern).
        _chunk(
            choices=[],
            usage={
                "prompt_tokens": 42,
                "completion_tokens": 7,
                "total_tokens": 49,
            },
        ),
    ]
    events = [ev async for ev in stream_chat_to_anthropic_events(_as_async(chunks))]
    usage = _message_delta_usage(events)
    assert usage["output_tokens"] == 7
    assert usage["input_tokens"] == 42


@pytest.mark.asyncio
async def test_stream_usage_estimates_when_upstream_silent() -> None:
    """Without an upstream usage chunk (e.g. Ollama ignoring include_usage),
    the translator must fall back to a char-based estimate so clients don't
    see 0 tokens for a clearly non-empty response.
    """
    # 4 + 6 + 7 = 17 chars → (17+3)//4 = 5 tokens
    chunks = [
        _chunk(choices=[{"index": 0, "delta": {"content": "Hell"}}]),
        _chunk(choices=[{"index": 0, "delta": {"content": "o, wo"}}]),
        _chunk(choices=[{"index": 0, "delta": {"content": "rld!!!"}}]),
        _chunk(choices=[{"index": 0, "delta": {}, "finish_reason": "stop"}]),
    ]
    # Chars = 4 + 5 + 6 = 15  -> (15 + 3) // 4 = 4
    events = [ev async for ev in stream_chat_to_anthropic_events(_as_async(chunks))]
    usage = _message_delta_usage(events)
    assert usage["output_tokens"] == 4
    # input_tokens is only reported when upstream provides it.
    assert "input_tokens" not in usage


@pytest.mark.asyncio
async def test_stream_usage_estimate_counts_tool_call_arguments() -> None:
    """Tool-call JSON arguments are generated output too, so they must be
    rolled into the estimator. Otherwise tool-heavy responses under-report.
    """
    chunks = [
        _chunk(
            choices=[
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "c1",
                                "function": {
                                    "name": "search",
                                    "arguments": '{"q":"hello world"}',
                                },
                            }
                        ]
                    },
                }
            ]
        ),
        _chunk(choices=[{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]),
    ]
    events = [ev async for ev in stream_chat_to_anthropic_events(_as_async(chunks))]
    usage = _message_delta_usage(events)
    # name "search" (6) + arguments '{"q":"hello world"}' (19) = 25 chars
    # → (25 + 3) // 4 = 7
    assert usage["output_tokens"] == 7


@pytest.mark.asyncio
async def test_stream_usage_empty_response_reports_zero() -> None:
    """No content chunks at all → output_tokens must be exactly 0 (not 1).
    Guards against the `max(1, …)` fallback being too aggressive.
    """
    chunks = [
        _chunk(choices=[{"index": 0, "delta": {"role": "assistant"}}]),
        _chunk(choices=[{"index": 0, "delta": {}, "finish_reason": "stop"}]),
    ]
    events = [ev async for ev in stream_chat_to_anthropic_events(_as_async(chunks))]
    usage = _message_delta_usage(events)
    assert usage["output_tokens"] == 0


@pytest.mark.asyncio
async def test_stream_usage_upstream_wins_over_estimate() -> None:
    """Even when the translator has emitted many chars, a trailing upstream
    usage value MUST override the estimate — the provider's own count is
    always more accurate than our heuristic.
    """
    chunks = [
        _chunk(
            choices=[
                {
                    "index": 0,
                    "delta": {
                        "content": "x" * 400  # estimate would be ~100 tokens
                    },
                }
            ]
        ),
        _chunk(choices=[{"index": 0, "delta": {}, "finish_reason": "stop"}]),
        _chunk(
            choices=[],
            usage={"prompt_tokens": 10, "completion_tokens": 3},
        ),
    ]
    events = [ev async for ev in stream_chat_to_anthropic_events(_as_async(chunks))]
    usage = _message_delta_usage(events)
    assert usage["output_tokens"] == 3
    assert usage["input_tokens"] == 10


# ----------------------------------------------------------------------
# v0.3-D: Synthesize Anthropic stream from finalized response
# ----------------------------------------------------------------------


def _mk_anth_response(
    content: list[dict[str, Any]],
    *,
    stop_reason: str = "end_turn",
    input_tokens: int = 5,
    output_tokens: int = 8,
) -> AnthropicResponse:
    return AnthropicResponse(
        id="msg_test",
        model="qwen-coder",
        content=content,
        stop_reason=stop_reason,
        usage=AnthropicUsage(input_tokens=input_tokens, output_tokens=output_tokens),
        coderouter_provider="local",
    )


@pytest.mark.asyncio
async def test_synthesize_stream_text_only_response() -> None:
    """A text-only response must emit the full Anthropic event sequence:
    message_start, content_block_start, one text_delta, content_block_stop,
    message_delta (with stop_reason + usage), message_stop.
    """
    resp = _mk_anth_response(
        [{"type": "text", "text": "Hello, world!"}],
        stop_reason="end_turn",
        input_tokens=4,
        output_tokens=4,
    )
    events = [e async for e in synthesize_anthropic_stream_from_response(resp)]
    types = [e.type for e in events]
    assert types == [
        "message_start",
        "content_block_start",
        "content_block_delta",
        "content_block_stop",
        "message_delta",
        "message_stop",
    ]
    # message_start carries initial usage with input_tokens + output_tokens=0.
    start = events[0]
    assert start.data["message"]["usage"] == {
        "input_tokens": 4,
        "output_tokens": 0,
    }
    # The single delta has the full text.
    delta = events[2]
    assert delta.data["delta"] == {"type": "text_delta", "text": "Hello, world!"}
    # Final message_delta carries stop_reason and output_tokens.
    md = events[4]
    assert md.data["delta"]["stop_reason"] == "end_turn"
    assert md.data["usage"]["output_tokens"] == 4


@pytest.mark.asyncio
async def test_synthesize_stream_tool_use_response() -> None:
    """Tool-use blocks must surface as content_block_start(tool_use) +
    input_json_delta carrying the JSON-serialized input.
    """
    resp = _mk_anth_response(
        [
            {
                "type": "tool_use",
                "id": "toolu_123",
                "name": "get_weather",
                "input": {"location": "Tokyo", "unit": "C"},
            }
        ],
        stop_reason="tool_use",
    )
    events = [e async for e in synthesize_anthropic_stream_from_response(resp)]
    # Exactly one tool_use block — so one start, one delta, one stop.
    starts = [e for e in events if e.type == "content_block_start"]
    assert len(starts) == 1
    assert starts[0].data["index"] == 0
    assert starts[0].data["content_block"] == {
        "type": "tool_use",
        "id": "toolu_123",
        "name": "get_weather",
        "input": {},
    }
    deltas = [e for e in events if e.type == "content_block_delta"]
    assert len(deltas) == 1
    assert deltas[0].data["delta"]["type"] == "input_json_delta"
    # The partial_json must decode back to the original input dict.
    import json as _json

    assert _json.loads(deltas[0].data["delta"]["partial_json"]) == {
        "location": "Tokyo",
        "unit": "C",
    }
    md = next(e for e in events if e.type == "message_delta")
    assert md.data["delta"]["stop_reason"] == "tool_use"


@pytest.mark.asyncio
async def test_synthesize_stream_mixed_text_and_tool_use() -> None:
    """Text block followed by tool_use block — both must close cleanly
    with contiguous indices 0, 1.
    """
    resp = _mk_anth_response(
        [
            {"type": "text", "text": "Let me check."},
            {
                "type": "tool_use",
                "id": "toolu_a",
                "name": "search",
                "input": {"q": "x"},
            },
        ],
        stop_reason="tool_use",
    )
    events = [e async for e in synthesize_anthropic_stream_from_response(resp)]
    types = [e.type for e in events]
    assert types == [
        "message_start",
        "content_block_start",  # text idx 0
        "content_block_delta",
        "content_block_stop",
        "content_block_start",  # tool_use idx 1
        "content_block_delta",
        "content_block_stop",
        "message_delta",
        "message_stop",
    ]
    starts = [e for e in events if e.type == "content_block_start"]
    assert [s.data["index"] for s in starts] == [0, 1]
    assert starts[0].data["content_block"]["type"] == "text"
    assert starts[1].data["content_block"]["type"] == "tool_use"
