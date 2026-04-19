"""Anthropic Messages ⇄ internal ChatRequest/ChatResponse translation.

The internal format is OpenAI-shaped (see coderouter/adapters/base.py), so the
translation is effectively Anthropic ⇄ OpenAI Chat Completions.

Three entry points:

    to_chat_request()                    Anthropic request → internal ChatRequest
    to_anthropic_response()              internal ChatResponse → Anthropic response
    stream_chat_to_anthropic_events()    async iterator → Anthropic SSE events

v0.2 scope: text + image + tool_use + tool_result.
v0.3+: thinking blocks, cache_control, documents, citations — currently passed
through as opaque dicts (extra="allow" on the models).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

from coderouter.adapters.base import (
    ChatRequest,
    ChatResponse,
    Message,
    StreamChunk,
)
from coderouter.translation.anthropic import (
    AnthropicRequest,
    AnthropicResponse,
    AnthropicStreamEvent,
    AnthropicUsage,
)

# ============================================================
# Anthropic → internal (OpenAI-shaped)
# ============================================================


def _system_as_text(system: str | list[dict[str, Any]] | None) -> str | None:
    """Anthropic's `system` can be a string or a list of content blocks.

    OpenAI accepts only a string for the system role, so join text blocks.
    Unknown block types are skipped with their type logged in the joined text
    so we don't silently drop user intent.
    """
    if system is None:
        return None
    if isinstance(system, str):
        return system
    parts: list[str] = []
    for block in system:
        btype = block.get("type")
        if btype == "text":
            parts.append(str(block.get("text", "")))
        else:
            # Preserve presence of non-text blocks (e.g. cache_control markers)
            # so the absence doesn't silently degrade the prompt.
            parts.append(f"[non-text block: {btype}]")
    return "\n".join(p for p in parts if p)


def _tool_use_to_openai_tool_call(block: dict[str, Any]) -> dict[str, Any]:
    """Anthropic tool_use block → OpenAI tool_calls entry."""
    return {
        "id": block.get("id", ""),
        "type": "function",
        "function": {
            "name": block.get("name", ""),
            # OpenAI expects a JSON-encoded string for arguments.
            "arguments": json.dumps(block.get("input", {}), ensure_ascii=False),
        },
    }


def _tool_result_content_to_str(
    content: str | list[dict[str, Any]] | None,
) -> str:
    """Normalize Anthropic tool_result content to a flat string.

    OpenAI's `role: "tool"` message accepts string content only.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in content:
        if block.get("type") == "text":
            parts.append(str(block.get("text", "")))
        else:
            # Rare — images as tool results. Encode as a placeholder.
            parts.append(f"[non-text tool_result block: {block.get('type')}]")
    return "\n".join(parts)


def _convert_anthropic_message(
    msg_dict: dict[str, Any],
) -> list[dict[str, Any]]:
    """Convert one Anthropic message to one-or-more OpenAI messages.

    - Short-form string content → single {role, content} message.
    - List of content blocks → may split into multiple messages when the
      user side embeds tool_result blocks (OpenAI encodes those as role=tool).
    - Assistant text + tool_use blocks merge into a single assistant message
      with both `content` and `tool_calls` set.
    """
    role = msg_dict["role"]
    content = msg_dict["content"]

    if isinstance(content, str):
        return [{"role": role, "content": content}]

    # content is a list of blocks
    text_parts: list[str] = []
    image_parts: list[dict[str, Any]] = []  # OpenAI vision content parts
    tool_calls: list[dict[str, Any]] = []  # for assistant
    tool_result_messages: list[dict[str, Any]] = []  # for user

    for block in content:
        btype = block.get("type")
        if btype == "text":
            text_parts.append(str(block.get("text", "")))
        elif btype == "image":
            # Anthropic image block → OpenAI `image_url` content part.
            src = block.get("source", {})
            src_type = src.get("type")
            if src_type == "base64":
                url = (
                    f"data:{src.get('media_type', 'image/png')};"
                    f"base64,{src.get('data', '')}"
                )
            elif src_type == "url":
                url = src.get("url", "")
            else:
                url = ""
            image_parts.append({"type": "image_url", "image_url": {"url": url}})
        elif btype == "tool_use":
            tool_calls.append(_tool_use_to_openai_tool_call(block))
        elif btype == "tool_result":
            tool_result_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": block.get("tool_use_id", ""),
                    "content": _tool_result_content_to_str(block.get("content")),
                }
            )
        # Unknown block types (thinking, document, …) are skipped in v0.2.

    out: list[dict[str, Any]] = []

    # tool_result blocks emit their own role=tool messages FIRST (they're the
    # answer to a previous assistant tool_use, so they precede any new user
    # text that might accompany them).
    out.extend(tool_result_messages)

    joined_text = "\n".join(t for t in text_parts if t)

    if role == "assistant":
        assistant_msg: dict[str, Any] = {"role": "assistant"}
        # OpenAI allows content: null when only tool_calls are present.
        assistant_msg["content"] = joined_text if joined_text else None
        if tool_calls:
            assistant_msg["tool_calls"] = tool_calls
        # Only emit the assistant message if something meaningful remains.
        if assistant_msg["content"] is not None or tool_calls:
            out.append(assistant_msg)
    else:  # user
        if image_parts:
            # Multimodal: OpenAI wants a content list with text + image parts.
            mm_content: list[dict[str, Any]] = []
            if joined_text:
                mm_content.append({"type": "text", "text": joined_text})
            mm_content.extend(image_parts)
            if mm_content:
                out.append({"role": "user", "content": mm_content})
        elif joined_text:
            out.append({"role": "user", "content": joined_text})
        # If it was purely tool_result blocks, tool_result_messages already
        # captured that — no extra user message needed.

    return out


def _convert_anthropic_tools(
    tools: list[Any] | None,
) -> list[dict[str, Any]] | None:
    if not tools:
        return None
    out: list[dict[str, Any]] = []
    for tool in tools:
        # Support both AnthropicTool models and plain dicts.
        t = tool.model_dump() if hasattr(tool, "model_dump") else dict(tool)
        out.append(
            {
                "type": "function",
                "function": {
                    "name": t.get("name", ""),
                    "description": t.get("description", ""),
                    "parameters": t.get("input_schema", {}) or {},
                },
            }
        )
    return out


def _convert_anthropic_tool_choice(
    tc: dict[str, Any] | None,
) -> Any | None:
    """Anthropic tool_choice → OpenAI tool_choice.

    Anthropic:
        {"type": "auto"}
        {"type": "any"}            # force any tool
        {"type": "tool", "name": "foo"}
        {"type": "none"}           # v0.3+
    OpenAI:
        "auto" | "none" | "required" | {"type": "function", "function": {"name"}}
    """
    if tc is None:
        return None
    t = tc.get("type")
    if t == "auto":
        return "auto"
    if t == "any":
        return "required"
    if t == "none":
        return "none"
    if t == "tool":
        return {"type": "function", "function": {"name": tc.get("name", "")}}
    return None


def to_chat_request(req: AnthropicRequest) -> ChatRequest:
    """Anthropic Messages request → internal ChatRequest (OpenAI-shaped)."""
    messages: list[dict[str, Any]] = []

    sys_text = _system_as_text(req.system)
    if sys_text:
        messages.append({"role": "system", "content": sys_text})

    for msg in req.messages:
        messages.extend(_convert_anthropic_message(msg.model_dump(exclude_none=True)))

    # Convert to Message models so downstream adapters see a consistent type.
    msg_models = [Message.model_validate(m) for m in messages]

    chat_req = ChatRequest(
        messages=msg_models,
        stream=req.stream,
        temperature=req.temperature,
        max_tokens=req.max_tokens,
        top_p=req.top_p,
        # Anthropic's stop_sequences → OpenAI's stop
        stop=req.stop_sequences,
        tools=_convert_anthropic_tools(req.tools),
        tool_choice=_convert_anthropic_tool_choice(req.tool_choice),
    )
    # Propagate CodeRouter routing hint.
    chat_req.profile = req.profile
    return chat_req


# ============================================================
# Internal → Anthropic (non-stream response)
# ============================================================


_FINISH_REASON_MAP = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "function_call": "tool_use",  # legacy OpenAI
    "content_filter": "end_turn",
}


def _tool_call_to_tool_use_block(tool_call: dict[str, Any]) -> dict[str, Any]:
    """OpenAI tool_calls entry → Anthropic tool_use content block."""
    fn = tool_call.get("function", {}) or {}
    args_raw = fn.get("arguments", "") or ""
    if isinstance(args_raw, dict):
        args_parsed: dict[str, Any] = args_raw
    else:
        try:
            args_parsed = json.loads(args_raw) if args_raw else {}
        except json.JSONDecodeError:
            # v0.2: keep the raw string in a `_raw` field so v1.0 can repair.
            args_parsed = {"_raw": args_raw}
    return {
        "type": "tool_use",
        "id": tool_call.get("id", f"toolu_{uuid.uuid4().hex[:16]}"),
        "name": fn.get("name", ""),
        "input": args_parsed,
    }


def to_anthropic_response(resp: ChatResponse) -> AnthropicResponse:
    """Internal ChatResponse (OpenAI-shaped) → Anthropic response."""
    choices = resp.choices or []
    message: dict[str, Any] = {}
    finish_reason: str | None = None
    if choices:
        message = choices[0].get("message", {}) or {}
        finish_reason = choices[0].get("finish_reason")

    content_blocks: list[dict[str, Any]] = []

    text = message.get("content")
    if isinstance(text, str) and text:
        content_blocks.append({"type": "text", "text": text})
    elif isinstance(text, list):
        # Rare: multimodal assistant response. Flatten text parts.
        for part in text:
            if part.get("type") == "text":
                content_blocks.append(
                    {"type": "text", "text": part.get("text", "")}
                )

    for tc in message.get("tool_calls") or []:
        content_blocks.append(_tool_call_to_tool_use_block(tc))

    # Empty response guard: Anthropic requires at least one content block.
    if not content_blocks:
        content_blocks.append({"type": "text", "text": ""})

    usage_in = resp.usage or {}
    usage = AnthropicUsage(
        input_tokens=int(usage_in.get("prompt_tokens", 0) or 0),
        output_tokens=int(usage_in.get("completion_tokens", 0) or 0),
    )

    return AnthropicResponse(
        id=f"msg_{resp.id}" if resp.id and not resp.id.startswith("msg_") else (
            resp.id or f"msg_{uuid.uuid4().hex[:24]}"
        ),
        model=resp.model,
        content=content_blocks,
        stop_reason=_FINISH_REASON_MAP.get(finish_reason or "stop", "end_turn"),
        usage=usage,
        coderouter_provider=resp.coderouter_provider,
    )


# ============================================================
# Stream translation (OpenAI chunks → Anthropic SSE events)
# ============================================================


class _StreamState:
    """Bookkeeping for the stateful stream translator.

    Anthropic's wire protocol requires open/close markers per content block,
    and block indices are contiguous (0, 1, 2, …). Text chunks and tool_call
    chunks from OpenAI must be re-segmented into these blocks.
    """

    def __init__(self) -> None:
        self.started: bool = False
        self.finished: bool = False
        self.current_block_index: int = -1
        self.current_block_type: str | None = None  # "text" | "tool_use"
        # openai tool_call index (from delta.tool_calls[i].index) →
        # anthropic content block index we allocated for it
        self.tool_call_block_map: dict[int, int] = {}
        self.message_id: str = f"msg_{uuid.uuid4().hex[:24]}"
        self.model: str = "unknown"
        self.output_tokens: int = 0


def _event(type_: str, data: dict[str, Any]) -> AnthropicStreamEvent:
    return AnthropicStreamEvent(type=type_, data={"type": type_, **data})


def _start_event(model: str, message_id: str) -> AnthropicStreamEvent:
    return _event(
        "message_start",
        {
            "message": {
                "id": message_id,
                "type": "message",
                "role": "assistant",
                "content": [],
                "model": model,
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            }
        },
    )


def _close_current_block(state: _StreamState) -> list[AnthropicStreamEvent]:
    if state.current_block_index < 0:
        return []
    evt = _event(
        "content_block_stop",
        {"index": state.current_block_index},
    )
    state.current_block_type = None
    return [evt]


def _open_text_block(state: _StreamState) -> list[AnthropicStreamEvent]:
    state.current_block_index += 1
    state.current_block_type = "text"
    return [
        _event(
            "content_block_start",
            {
                "index": state.current_block_index,
                "content_block": {"type": "text", "text": ""},
            },
        )
    ]


def _open_tool_use_block(
    state: _StreamState,
    openai_tc_index: int,
    tool_id: str,
    tool_name: str,
) -> list[AnthropicStreamEvent]:
    state.current_block_index += 1
    state.current_block_type = "tool_use"
    state.tool_call_block_map[openai_tc_index] = state.current_block_index
    return [
        _event(
            "content_block_start",
            {
                "index": state.current_block_index,
                "content_block": {
                    "type": "tool_use",
                    "id": tool_id or f"toolu_{uuid.uuid4().hex[:16]}",
                    "name": tool_name,
                    "input": {},
                },
            },
        )
    ]


def _handle_delta(
    state: _StreamState, delta: dict[str, Any]
) -> list[AnthropicStreamEvent]:
    """Translate one OpenAI delta dict into zero-or-more Anthropic events."""
    out: list[AnthropicStreamEvent] = []

    # Text content
    text = delta.get("content")
    if isinstance(text, str) and text:
        if state.current_block_type != "text":
            out.extend(_close_current_block(state))
            out.extend(_open_text_block(state))
        out.append(
            _event(
                "content_block_delta",
                {
                    "index": state.current_block_index,
                    "delta": {"type": "text_delta", "text": text},
                },
            )
        )

    # Tool calls
    for tc in delta.get("tool_calls") or []:
        tc_index = tc.get("index", 0)
        fn = tc.get("function", {}) or {}
        args_fragment = fn.get("arguments", "") or ""

        if tc_index not in state.tool_call_block_map:
            # First time we see this tool_call — close any prior block and open a new tool_use block.
            out.extend(_close_current_block(state))
            out.extend(
                _open_tool_use_block(
                    state,
                    openai_tc_index=tc_index,
                    tool_id=tc.get("id", ""),
                    tool_name=fn.get("name", ""),
                )
            )
        block_idx = state.tool_call_block_map[tc_index]
        if args_fragment:
            out.append(
                _event(
                    "content_block_delta",
                    {
                        "index": block_idx,
                        "delta": {
                            "type": "input_json_delta",
                            "partial_json": args_fragment,
                        },
                    },
                )
            )

    return out


async def stream_chat_to_anthropic_events(
    chunks: AsyncIterator[StreamChunk],
) -> AsyncIterator[AnthropicStreamEvent]:
    """Translate an internal StreamChunk async iterator into Anthropic events.

    Wire protocol emitted:
        message_start
        [content_block_start, (content_block_delta)*, content_block_stop]+
        message_delta (with stop_reason)
        message_stop

    This function is stateful across chunks — do NOT use more than once on
    the same instance.
    """
    state = _StreamState()
    stop_reason_openai: str | None = None

    async for chunk in chunks:
        if not state.started:
            state.started = True
            state.message_id = chunk.id if chunk.id else state.message_id
            if chunk.model:
                state.model = chunk.model
            yield _start_event(state.model, state.message_id)

        for choice in chunk.choices or []:
            delta = choice.get("delta", {}) or {}
            for evt in _handle_delta(state, delta):
                yield evt
            if choice.get("finish_reason"):
                stop_reason_openai = choice["finish_reason"]

        # Some providers put usage on the last chunk.
        usage = getattr(chunk, "usage", None)
        if isinstance(usage, dict) and usage.get("completion_tokens"):
            state.output_tokens = int(usage["completion_tokens"])

    # Terminator sequence
    for evt in _close_current_block(state):
        yield evt

    stop_reason = _FINISH_REASON_MAP.get(stop_reason_openai or "stop", "end_turn")
    yield _event(
        "message_delta",
        {
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": {"output_tokens": state.output_tokens},
        },
    )
    yield _event("message_stop", {})
