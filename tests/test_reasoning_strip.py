"""Unit + adapter tests for the v0.5-C ``reasoning`` field passive strip.

Context:
    Some OpenRouter free models (confirmed on ``openai/gpt-oss-120b:free``
    on 2026-04-20) return a non-standard ``reasoning`` field inside each
    choice's ``message`` (non-streaming) or ``delta`` (streaming). The
    field is not in the OpenAI Chat Completions spec. Strict downstream
    clients can reject the unknown key, so v0.5-C strips it at the
    adapter boundary and emits a one-shot structured log.

    ``capabilities.reasoning_passthrough: true`` opts out — use when
    CodeRouter is intentionally fronting a reasoning-aware downstream.

Layers covered:
    1. ``_strip_reasoning_field`` helper — pure-function strip.
    2. ``OpenAICompatAdapter.generate`` — strips on non-streaming.
    3. ``OpenAICompatAdapter.stream`` — strips on each chunk, logs once.
"""

from __future__ import annotations

import json
import logging

import pytest
from pytest_httpx import HTTPXMock

from coderouter.adapters.base import ChatRequest, Message
from coderouter.adapters.openai_compat import (
    OpenAICompatAdapter,
    _strip_reasoning_field,
)
from coderouter.config.schemas import Capabilities, ProviderConfig

# ======================================================================
# Unit tests — _strip_reasoning_field helper
# ======================================================================


def test_strip_helper_removes_reasoning_from_message() -> None:
    choices = [
        {
            "index": 0,
            "message": {
                "role": "assistant",
                "content": "final",
                "reasoning": "hidden chain of thought",
            },
            "finish_reason": "stop",
        }
    ]
    stripped = _strip_reasoning_field(choices, delta_key=False)

    assert stripped is True
    assert choices[0]["message"] == {"role": "assistant", "content": "final"}


def test_strip_helper_removes_reasoning_from_delta() -> None:
    choices = [
        {
            "index": 0,
            "delta": {"content": "tok", "reasoning": "..."},
        }
    ]
    stripped = _strip_reasoning_field(choices, delta_key=True)

    assert stripped is True
    assert choices[0]["delta"] == {"content": "tok"}


def test_strip_helper_is_noop_when_reasoning_absent() -> None:
    choices = [{"index": 0, "message": {"role": "assistant", "content": "clean"}}]
    stripped = _strip_reasoning_field(choices, delta_key=False)

    assert stripped is False
    assert choices[0]["message"] == {"role": "assistant", "content": "clean"}


def test_strip_helper_handles_empty_and_none() -> None:
    assert _strip_reasoning_field(None, delta_key=False) is False
    assert _strip_reasoning_field([], delta_key=False) is False


def test_strip_helper_wrong_key_does_not_strip() -> None:
    """If looking for delta but payload has message, no-op (and vice versa)."""
    choices = [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "x", "reasoning": "y"},
        }
    ]
    # Looking in `delta` but the field is in `message` — leave it alone.
    stripped = _strip_reasoning_field(choices, delta_key=True)
    assert stripped is False
    assert "reasoning" in choices[0]["message"]


def test_strip_helper_skips_non_dict_choice() -> None:
    """Defensive: a malformed stream chunk with a non-dict choice must not crash."""
    choices: list = [None, "not-a-dict", {"index": 0, "delta": {"reasoning": "x"}}]
    stripped = _strip_reasoning_field(choices, delta_key=True)
    assert stripped is True
    assert choices[2]["delta"] == {}


def test_strip_helper_handles_multiple_choices() -> None:
    """n>1 choices — strip every one that carries the field."""
    choices = [
        {"index": 0, "message": {"content": "a", "reasoning": "r0"}},
        {"index": 1, "message": {"content": "b"}},
        {"index": 2, "message": {"content": "c", "reasoning": "r2"}},
    ]
    stripped = _strip_reasoning_field(choices, delta_key=False)
    assert stripped is True
    assert "reasoning" not in choices[0]["message"]
    assert "reasoning" not in choices[1]["message"]
    assert "reasoning" not in choices[2]["message"]


def test_strip_helper_removes_reasoning_content_field() -> None:
    """v1.8.3: llama.cpp's ``reasoning_content`` is treated the same as
    ``reasoning`` — both are non-standard chain-of-thought fields with
    different vendor naming.

    Confirmed 2026-04-26 with Qwen3.6:35b-a3b on llama-server: the
    response shape is::

        {"message": {"role": "assistant", "content": "...",
                     "reasoning_content": "<thinking trace>"}}

    Strict OpenAI clients reject the unknown ``reasoning_content`` key
    just as they would reject ``reasoning``.
    """
    choices = [
        {
            "index": 0,
            "message": {
                "role": "assistant",
                "content": "Hello",
                "reasoning_content": "Here's a thinking process: ...",
            },
            "finish_reason": "stop",
        }
    ]
    stripped = _strip_reasoning_field(choices, delta_key=False)
    assert stripped is True
    assert choices[0]["message"] == {"role": "assistant", "content": "Hello"}


def test_strip_helper_removes_both_reasoning_and_reasoning_content() -> None:
    """When a single message carries both keys (defensive — unlikely in
    practice but possible if a proxy merges OpenRouter + llama.cpp
    upstreams), the strip removes both in one pass.
    """
    choices = [
        {
            "index": 0,
            "message": {
                "role": "assistant",
                "content": "answer",
                "reasoning": "ollama-style trace",
                "reasoning_content": "llama-cpp-style trace",
            },
        }
    ]
    stripped = _strip_reasoning_field(choices, delta_key=False)
    assert stripped is True
    assert choices[0]["message"] == {"role": "assistant", "content": "answer"}


def test_strip_helper_removes_reasoning_content_from_delta() -> None:
    """Stream chunks may carry ``reasoning_content`` in ``delta`` too —
    llama-server's streaming path emits it incrementally.
    """
    choices = [
        {
            "index": 0,
            "delta": {"content": "tok", "reasoning_content": "thinking..."},
        }
    ]
    stripped = _strip_reasoning_field(choices, delta_key=True)
    assert stripped is True
    assert choices[0]["delta"] == {"content": "tok"}


# ======================================================================
# Adapter tests — generate() (non-streaming)
# ======================================================================


def _provider(*, passthrough: bool = False) -> ProviderConfig:
    return ProviderConfig(
        name="openrouter-gpt-oss-free",
        base_url="https://openrouter.ai/api/v1",
        model="openai/gpt-oss-120b:free",
        api_key_env=None,
        capabilities=Capabilities(reasoning_passthrough=passthrough),
    )


def _request() -> ChatRequest:
    return ChatRequest(messages=[Message(role="user", content="hi")])


@pytest.mark.asyncio
async def test_generate_strips_reasoning_from_message(
    httpx_mock: HTTPXMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Real-world OpenRouter gpt-oss response — `reasoning` on message must
    be stripped before ChatResponse is built, and a one-shot log fires."""
    httpx_mock.add_response(
        url="https://openrouter.ai/api/v1/chat/completions",
        method="POST",
        json={
            "id": "chatcmpl-1",
            "object": "chat.completion",
            "created": 0,
            "model": "openai/gpt-oss-120b:free",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "pong",
                        "reasoning": "The user said hi so I say pong.",
                    },
                    "finish_reason": "stop",
                }
            ],
        },
    )

    adapter = OpenAICompatAdapter(_provider())
    with caplog.at_level(logging.INFO, logger="coderouter"):
        resp = await adapter.generate(_request())

    # Reasoning must not leak into the ChatResponse.
    assert "reasoning" not in resp.choices[0]["message"]
    assert resp.choices[0]["message"]["content"] == "pong"

    # Structured log fired exactly once, tagged non-standard-field.
    recs = [
        r
        for r in caplog.records
        if r.msg == "capability-degraded" and getattr(r, "reason", None) == "non-standard-field"
    ]
    assert len(recs) == 1
    assert recs[0].provider == "openrouter-gpt-oss-free"
    # v1.8.3: log dropped now lists both Ollama/OpenRouter (`reasoning`)
    # and llama.cpp (`reasoning_content`) since the same strip handles both.
    assert recs[0].dropped == ["reasoning", "reasoning_content"]


@pytest.mark.asyncio
async def test_generate_no_log_when_reasoning_absent(
    httpx_mock: HTTPXMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Clean response → no log. The gate must only fire on actual strips."""
    httpx_mock.add_response(
        url="https://openrouter.ai/api/v1/chat/completions",
        method="POST",
        json={
            "id": "x",
            "object": "chat.completion",
            "created": 0,
            "model": "openai/gpt-oss-120b:free",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "clean"},
                    "finish_reason": "stop",
                }
            ],
        },
    )

    adapter = OpenAICompatAdapter(_provider())
    with caplog.at_level(logging.INFO, logger="coderouter"):
        await adapter.generate(_request())

    recs = [
        r
        for r in caplog.records
        if r.msg == "capability-degraded" and getattr(r, "reason", None) == "non-standard-field"
    ]
    assert recs == []


@pytest.mark.asyncio
async def test_generate_passthrough_preserves_reasoning(
    httpx_mock: HTTPXMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Escape hatch: `capabilities.reasoning_passthrough: true` → keep the
    field and do not log."""
    httpx_mock.add_response(
        url="https://openrouter.ai/api/v1/chat/completions",
        method="POST",
        json={
            "id": "x",
            "object": "chat.completion",
            "created": 0,
            "model": "openai/gpt-oss-120b:free",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "pong",
                        "reasoning": "kept",
                    },
                    "finish_reason": "stop",
                }
            ],
        },
    )

    adapter = OpenAICompatAdapter(_provider(passthrough=True))
    with caplog.at_level(logging.INFO, logger="coderouter"):
        resp = await adapter.generate(_request())

    assert resp.choices[0]["message"].get("reasoning") == "kept"
    recs = [
        r
        for r in caplog.records
        if r.msg == "capability-degraded" and getattr(r, "reason", None) == "non-standard-field"
    ]
    assert recs == []


@pytest.mark.asyncio
async def test_generate_does_not_touch_content(httpx_mock: HTTPXMock) -> None:
    """Regression: `content` must never be altered by the strip."""
    httpx_mock.add_response(
        url="https://openrouter.ai/api/v1/chat/completions",
        method="POST",
        json={
            "id": "x",
            "object": "chat.completion",
            "created": 0,
            "model": "openai/gpt-oss-120b:free",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "the actual answer",
                        "reasoning": "drop this",
                    },
                    "finish_reason": "stop",
                }
            ],
        },
    )

    adapter = OpenAICompatAdapter(_provider())
    resp = await adapter.generate(_request())

    assert resp.choices[0]["message"]["content"] == "the actual answer"


# ======================================================================
# Adapter tests — stream() (streaming)
# ======================================================================


def _sse(chunks: list[dict]) -> bytes:
    """Serialize a list of chunk dicts as an SSE stream body."""
    pieces = [f"data: {json.dumps(c)}\n\n" for c in chunks]
    pieces.append("data: [DONE]\n\n")
    return "".join(pieces).encode("utf-8")


def _sse_chunk(
    delta: dict, *, id: str = "chatcmpl-s", model: str = "openai/gpt-oss-120b:free"
) -> dict:
    return {
        "id": id,
        "object": "chat.completion.chunk",
        "created": 0,
        "model": model,
        "choices": [{"index": 0, "delta": delta}],
    }


@pytest.mark.asyncio
async def test_stream_strips_reasoning_from_each_delta(
    httpx_mock: HTTPXMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Every chunk's `delta.reasoning` must be stripped before the chunk
    is yielded, but the log fires only once per stream."""
    body = _sse(
        [
            _sse_chunk({"content": "po", "reasoning": "part 1 of hidden track"}),
            _sse_chunk({"content": "ng", "reasoning": "part 2 of hidden track"}),
            _sse_chunk({"content": "", "finish_reason": "stop"}),
        ]
    )
    httpx_mock.add_response(
        url="https://openrouter.ai/api/v1/chat/completions",
        method="POST",
        content=body,
        headers={"content-type": "text/event-stream"},
    )

    adapter = OpenAICompatAdapter(_provider())
    req = _request()
    req.stream = True
    with caplog.at_level(logging.INFO, logger="coderouter"):
        chunks = [c async for c in adapter.stream(req)]

    # Every emitted chunk lacks `reasoning`.
    for chunk in chunks:
        for choice in chunk.choices:
            assert "reasoning" not in choice.get("delta", {})

    # Log fires exactly once even though two chunks carried the field.
    recs = [
        r
        for r in caplog.records
        if r.msg == "capability-degraded" and getattr(r, "reason", None) == "non-standard-field"
    ]
    assert len(recs) == 1
    assert recs[0].provider == "openrouter-gpt-oss-free"
    # v1.8.3: log dropped now lists both Ollama/OpenRouter (`reasoning`)
    # and llama.cpp (`reasoning_content`) since the same strip handles both.
    assert recs[0].dropped == ["reasoning", "reasoning_content"]


@pytest.mark.asyncio
async def test_stream_no_log_when_no_chunk_carries_reasoning(
    httpx_mock: HTTPXMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    body = _sse(
        [
            _sse_chunk({"content": "hello"}),
            _sse_chunk({"content": " world"}),
        ]
    )
    httpx_mock.add_response(
        url="https://openrouter.ai/api/v1/chat/completions",
        method="POST",
        content=body,
        headers={"content-type": "text/event-stream"},
    )

    adapter = OpenAICompatAdapter(_provider())
    req = _request()
    req.stream = True
    with caplog.at_level(logging.INFO, logger="coderouter"):
        _ = [c async for c in adapter.stream(req)]

    recs = [
        r
        for r in caplog.records
        if r.msg == "capability-degraded" and getattr(r, "reason", None) == "non-standard-field"
    ]
    assert recs == []


@pytest.mark.asyncio
async def test_stream_passthrough_preserves_reasoning(
    httpx_mock: HTTPXMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    body = _sse(
        [
            _sse_chunk({"content": "po", "reasoning": "kept"}),
        ]
    )
    httpx_mock.add_response(
        url="https://openrouter.ai/api/v1/chat/completions",
        method="POST",
        content=body,
        headers={"content-type": "text/event-stream"},
    )

    adapter = OpenAICompatAdapter(_provider(passthrough=True))
    req = _request()
    req.stream = True
    with caplog.at_level(logging.INFO, logger="coderouter"):
        chunks = [c async for c in adapter.stream(req)]

    # Reasoning kept verbatim.
    assert chunks[0].choices[0]["delta"].get("reasoning") == "kept"
    # No log.
    recs = [
        r
        for r in caplog.records
        if r.msg == "capability-degraded" and getattr(r, "reason", None) == "non-standard-field"
    ]
    assert recs == []


@pytest.mark.asyncio
async def test_stream_content_is_preserved(httpx_mock: HTTPXMock) -> None:
    """Regression: `delta.content` must flow through untouched even when
    `reasoning` is stripped from the same delta."""
    body = _sse(
        [
            _sse_chunk({"content": "A", "reasoning": "drop"}),
            _sse_chunk({"content": "B", "reasoning": "drop"}),
        ]
    )
    httpx_mock.add_response(
        url="https://openrouter.ai/api/v1/chat/completions",
        method="POST",
        content=body,
        headers={"content-type": "text/event-stream"},
    )

    adapter = OpenAICompatAdapter(_provider())
    req = _request()
    req.stream = True
    chunks = [c async for c in adapter.stream(req)]

    contents = [c.choices[0]["delta"].get("content") for c in chunks]
    assert contents == ["A", "B"]
