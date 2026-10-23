"""OpenAI Responses API ⇄ Anthropic Messages translation (v2.19.0).

The ``POST /v1/responses`` ingress exists for the OpenAI Codex CLI, which
since 2026-02 speaks only the Responses API. CodeRouter keeps Anthropic
Messages as its internal pivot, so a Responses request is translated to an
:class:`AnthropicRequest`, run through the fallback engine (tool-call
repair, guards, fallback all apply), and the engine's Anthropic events are
translated back into Responses SSE events.

The wire contract below was taken from openai/codex ``970111f``
(2026-10-23); see ``docs/start/quickstart.md`` Pattern B for the client
config. Key points:

- Codex sends no ``instructions`` field; the system prompt is the first
  ``input`` item, a ``developer`` message. Every ``developer`` / ``system``
  message becomes Anthropic ``system`` text, in order.
- Tool kinds: ``function`` (answer: ``function_call`` with a JSON-string
  ``arguments``), ``custom`` (freeform grammar tools such as
  ``apply_patch``; answer: ``custom_tool_call`` with a raw ``input``
  string — a ``function_call`` is rejected), and ``namespace`` (MCP; answer
  must carry ``namespace`` plus the inner ``name``). Hosted tools
  (``web_search``, …) are dropped: only OpenAI runs them.
- Codex acts on ``response.output_item.done``; a text delta needs an open
  item; ``response.completed`` must carry ``response.id``; usage detail
  objects must be complete or absent.
- In-stream failure is ``response.failed`` followed by end of stream, and
  ``context_length_exceeded`` there is the only context-overflow signal
  Codex recognizes.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from coderouter.translation.anthropic import AnthropicRequest, AnthropicStreamEvent

#: Default ``max_tokens`` when the Responses request has no
#: ``max_output_tokens`` (Codex never sends one).
DEFAULT_MAX_OUTPUT_TOKENS = 16384

_NAME_SAFE = re.compile(r"[^a-zA-Z0-9_-]")
_HOSTED_TOOL_TYPES = frozenset(
    {"web_search", "web_search_preview", "file_search", "image_generation", "code_interpreter",
     "computer_use_preview", "local_shell", "mcp", "tool_search"}
)


_MAX_TOOL_NAME = 64


def _safe_name(raw: str) -> str:
    name = _NAME_SAFE.sub("_", raw) or "tool"
    if len(name) <= _MAX_TOOL_NAME:
        return name
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
    return f"{name[: _MAX_TOOL_NAME - 9]}_{digest}"


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:24]}"


# ---------------------------------------------------------------------------
# Tool mapping
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolRef:
    """How an Anthropic tool name maps back to a Responses tool."""

    kind: str  # "function" | "custom"
    name: str
    namespace: str | None = None


@dataclass
class ToolMap:
    """Reversible per-request mapping between flattened Anthropic tool
    names and Responses tools (namespaces are flattened)."""

    by_flat: dict[str, ToolRef] = field(default_factory=dict)

    def flat_name(self, name: str, namespace: str | None) -> str:
        """Provider-safe name for ``(namespace, name)``; unique per request.

        Capped at 64 chars (OpenAI-compatible servers reject longer function
        names) with a short hash suffix so long ``mcp__server__tool`` names
        stay distinct.
        """
        base = _safe_name(f"{namespace}__{name}" if namespace else name)
        flat, i = base, 2
        while flat in self.by_flat and (
            self.by_flat[flat].name,
            self.by_flat[flat].namespace,
        ) != (name, namespace):
            flat = f"{base[:56]}_{i}"
            i += 1
        return flat

    def add(self, kind: str, name: str, namespace: str | None) -> str:
        flat = self.flat_name(name, namespace)
        self.by_flat[flat] = ToolRef(kind=kind, name=name, namespace=namespace)
        return flat

    def lookup(self, flat: str) -> ToolRef:
        return self.by_flat.get(flat, ToolRef(kind="function", name=flat))

    def flat_for(self, name: str, namespace: str | None) -> str:
        for flat, ref in self.by_flat.items():
            if ref.name == name and ref.namespace == namespace:
                return flat
        return _safe_name(f"{namespace}__{name}" if namespace else name)


def _custom_tool_schema(tool: dict[str, Any]) -> dict[str, Any]:
    fmt = tool.get("format") or {}
    hint = ""
    if isinstance(fmt, dict) and fmt.get("definition"):
        hint = (
            f" The text must follow this {fmt.get('syntax', 'grammar')} grammar:\n"
            f"{fmt['definition']}"
        )
    return {
        "type": "object",
        "properties": {
            "input": {
                "type": "string",
                "description": "The raw text input for this tool, passed verbatim." + hint,
            }
        },
        "required": ["input"],
    }


def _convert_tools(tools: list[Any] | None, tool_map: ToolMap) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        kind = tool.get("type")
        if kind == "namespace":
            ns = tool.get("name")
            for inner in tool.get("tools") or []:
                out.extend(_convert_one_tool(inner, tool_map, namespace=ns))
            continue
        out.extend(_convert_one_tool(tool, tool_map, namespace=None))
    return out


def _convert_one_tool(
    tool: dict[str, Any], tool_map: ToolMap, *, namespace: str | None
) -> list[dict[str, Any]]:
    kind = tool.get("type")
    name = tool.get("name")
    if not isinstance(name, str) or not name:
        return []
    if kind == "function":
        flat = tool_map.add("function", name, namespace)
        return [
            {
                "name": flat,
                "description": tool.get("description") or "",
                "input_schema": tool.get("parameters") or {"type": "object", "properties": {}},
            }
        ]
    if kind == "custom":
        flat = tool_map.add("custom", name, namespace)
        return [
            {
                "name": flat,
                "description": tool.get("description") or "",
                "input_schema": _custom_tool_schema(tool),
            }
        ]
    # Hosted / unknown tool types only run on OpenAI's side.
    return []


# ---------------------------------------------------------------------------
# Input → Anthropic messages
# ---------------------------------------------------------------------------


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for part in content or []:
        if isinstance(part, dict) and isinstance(part.get("text"), str):
            parts.append(part["text"])
    return "\n".join(parts)


def _image_block(part: dict[str, Any]) -> dict[str, Any] | None:
    url = part.get("image_url")
    if not isinstance(url, str) or not url:
        return None
    m = re.match(r"data:([^;]+);base64,(.*)", url, re.DOTALL)
    if m:
        return {"type": "image", "source": {"type": "base64", "media_type": m.group(1), "data": m.group(2)}}
    return {"type": "image", "source": {"type": "url", "url": url}}


def _user_blocks(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    blocks: list[dict[str, Any]] = []
    for part in content or []:
        if not isinstance(part, dict):
            continue
        ptype = part.get("type")
        if ptype in ("input_text", "output_text", "text") and part.get("text"):
            blocks.append({"type": "text", "text": part["text"]})
        elif ptype == "input_image":
            img = _image_block(part)
            if img is not None:
                blocks.append(img)
    return blocks


def _tool_output_content(output: Any) -> str | list[dict[str, Any]]:
    """``*_call_output.output`` → Anthropic ``tool_result`` content.

    Strings pass through. Arrays keep their text parts as text blocks and
    their images as image blocks (Codex's ``view_image`` and MCP image
    results), instead of dumping base64 into the prompt as text. Backends
    that cannot take images get a placeholder from the translation layer.
    """
    if isinstance(output, str):
        return output
    if not isinstance(output, list):
        return json.dumps(output, ensure_ascii=False)
    blocks: list[dict[str, Any]] = []
    for part in output:
        if not isinstance(part, dict):
            continue
        ptype = part.get("type")
        if ptype in ("input_text", "output_text", "text") and isinstance(part.get("text"), str):
            blocks.append({"type": "text", "text": part["text"]})
        elif ptype == "input_image":
            img = _image_block(part)
            if img is not None:
                blocks.append(img)
    return blocks or ""


def _append(messages: list[dict[str, Any]], role: str, blocks: list[dict[str, Any]]) -> None:
    if not blocks:
        return
    if messages and messages[-1]["role"] == role:
        messages[-1]["content"].extend(blocks)
    else:
        messages.append({"role": role, "content": list(blocks)})


def _repair_tool_pairing(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Make tool_use / tool_result pairing valid for Anthropic.

    - a tool_result whose tool_use is unknown is dropped;
    - a tool_use with no tool_result in the next user turn gets a
      synthetic ``"aborted"`` result (Codex does the same normalization,
      but not necessarily before every request).
    """
    seen_ids: set[str] = set()
    out: list[dict[str, Any]] = []
    for i, msg in enumerate(messages):
        if msg["role"] == "assistant":
            ids = [b["id"] for b in msg["content"] if b.get("type") == "tool_use"]
            seen_ids.update(ids)
            out.append(msg)
            nxt = messages[i + 1] if i + 1 < len(messages) else None
            answered = {
                b.get("tool_use_id")
                for b in (nxt["content"] if nxt and nxt["role"] == "user" else [])
                if b.get("type") == "tool_result"
            }
            missing = [tid for tid in ids if tid not in answered]
            if missing and nxt is not None:
                synth = [
                    {"type": "tool_result", "tool_use_id": tid, "content": "aborted"} for tid in missing
                ]
                if nxt["role"] == "user":
                    nxt["content"][:0] = synth
                else:
                    out.append({"role": "user", "content": synth})
            continue
        msg = {
            "role": msg["role"],
            "content": [
                b
                for b in msg["content"]
                if b.get("type") != "tool_result" or b.get("tool_use_id") in seen_ids
            ],
        }
        if msg["content"]:
            # tool_results must come first in a user turn.
            msg["content"].sort(key=lambda b: 0 if b.get("type") == "tool_result" else 1)
            if out and out[-1]["role"] == "user":
                out[-1]["content"].extend(msg["content"])
            else:
                out.append(msg)
    return out


def responses_to_anthropic(body: dict[str, Any]) -> tuple[AnthropicRequest, ToolMap]:
    """Translate a Responses request body into an :class:`AnthropicRequest`."""
    tool_map = ToolMap()
    tools = _convert_tools(body.get("tools"), tool_map)

    system_parts: list[str] = []
    instructions = body.get("instructions")
    if isinstance(instructions, str) and instructions.strip():
        system_parts.append(instructions)

    messages: list[dict[str, Any]] = []
    raw_input = body.get("input")
    items: list[Any] = (
        [{"type": "message", "role": "user", "content": raw_input}]
        if isinstance(raw_input, str)
        else list(raw_input or [])
    )
    for item in items:
        if not isinstance(item, dict):
            continue
        itype = item.get("type", "message" if "role" in item else None)
        if itype == "message":
            role = item.get("role")
            if role in ("developer", "system"):
                text = _text_of(item.get("content"))
                if text.strip():
                    system_parts.append(text)
            elif role == "assistant":
                text = _text_of(item.get("content"))
                if text:
                    _append(messages, "assistant", [{"type": "text", "text": text}])
            else:
                _append(messages, "user", _user_blocks(item.get("content")))
        elif itype in ("function_call", "custom_tool_call"):
            call_id = item.get("call_id")
            name = item.get("name")
            if not call_id or not name:
                continue
            ns = item.get("namespace")
            if itype == "function_call":
                try:
                    args = json.loads(item.get("arguments") or "{}")
                except (TypeError, ValueError):
                    args = {"_raw_arguments": item.get("arguments")}
                if not isinstance(args, dict):
                    args = {"value": args}
            else:
                args = {"input": item.get("input") or ""}
            _append(
                messages,
                "assistant",
                [{"type": "tool_use", "id": call_id, "name": tool_map.flat_for(name, ns), "input": args}],
            )
        elif itype in ("function_call_output", "custom_tool_call_output"):
            call_id = item.get("call_id")
            if not call_id:
                continue
            _append(
                messages,
                "user",
                [{"type": "tool_result", "tool_use_id": call_id, "content": _tool_output_content(item.get("output"))}],
            )
        # reasoning / hosted-tool / compaction items: not representable for a
        # non-OpenAI backend — dropped.

    messages = _repair_tool_pairing(messages)
    if not messages or messages[0]["role"] != "user":
        messages.insert(0, {"role": "user", "content": [{"type": "text", "text": "(continue)"}]})

    payload: dict[str, Any] = {
        "model": body.get("model"),
        "max_tokens": int(body.get("max_output_tokens") or DEFAULT_MAX_OUTPUT_TOKENS),
        "messages": messages,
        "stream": bool(body.get("stream", False)),
    }
    if system_parts:
        payload["system"] = "\n\n".join(system_parts)
    if tools:
        payload["tools"] = tools
    for key in ("temperature", "top_p"):
        if body.get(key) is not None:
            payload[key] = body[key]
    if isinstance(body.get("profile"), str):
        payload["profile"] = body["profile"]
    return AnthropicRequest.model_validate(payload), tool_map


# ---------------------------------------------------------------------------
# Anthropic output → Responses items
# ---------------------------------------------------------------------------


def _tool_item(block_id: str, flat_name: str, args: Any, tool_map: ToolMap) -> dict[str, Any]:
    ref = tool_map.lookup(flat_name)
    if ref.kind == "custom":
        raw: str | None = None
        if isinstance(args, dict):
            if isinstance(args.get("input"), str):
                raw = args["input"]
            else:
                # A repaired / local call may name the field differently
                # ({"patch": "..."}); a lone string value is the input.
                strings = [v for v in args.values() if isinstance(v, str)]
                if len(args) == 1 and len(strings) == 1:
                    raw = strings[0]
        elif isinstance(args, str):
            raw = args
        item: dict[str, Any] = {
            "type": "custom_tool_call",
            "id": _new_id("ctc"),
            "call_id": block_id,
            "name": ref.name,
            "input": raw if raw is not None else json.dumps(args, ensure_ascii=False),
            "status": "completed",
        }
    else:
        item = {
            "type": "function_call",
            "id": _new_id("fc"),
            "call_id": block_id,
            "name": ref.name,
            "arguments": json.dumps(args if args is not None else {}, ensure_ascii=False),
            "status": "completed",
        }
    if ref.namespace:
        item["namespace"] = ref.namespace
    return item


def _usage(
    input_tokens: int, output_tokens: int, cached: int, cache_write: int = 0
) -> dict[str, Any]:
    """OpenAI-style usage from Anthropic counts.

    Anthropic's ``input_tokens`` excludes cache reads and writes; OpenAI's
    includes them, and Codex uses ``total_tokens`` as context-window usage
    to decide when to compact. So the cache counts are added back in.
    """
    input_tokens = input_tokens + cached + cache_write
    details: dict[str, Any] = {"cached_tokens": cached}
    if cache_write:
        details["cache_write_tokens"] = cache_write
    return {
        "input_tokens": input_tokens,
        "input_tokens_details": details,
        "output_tokens": output_tokens,
        "output_tokens_details": {"reasoning_tokens": 0},
        "total_tokens": input_tokens + output_tokens,
    }


def _response_obj(
    resp_id: str, model: str, status: str, output: list[dict[str, Any]], usage: dict[str, Any] | None
) -> dict[str, Any]:
    obj: dict[str, Any] = {
        "id": resp_id,
        "object": "response",
        "created_at": 0,
        "status": status,
        "model": model,
        "output": output,
    }
    if usage is not None:
        obj["usage"] = usage
    return obj


def anthropic_response_to_responses(
    resp: dict[str, Any], tool_map: ToolMap, *, model: str
) -> dict[str, Any]:
    """Non-streaming: an Anthropic response dict → a Responses ``response``."""
    output: list[dict[str, Any]] = []
    for block in resp.get("content") or []:
        btype = block.get("type")
        if btype == "text" and block.get("text"):
            output.append(
                {
                    "type": "message",
                    "id": _new_id("msg"),
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": block["text"], "annotations": []}],
                }
            )
        elif btype == "tool_use":
            output.append(_tool_item(block.get("id") or _new_id("call"), block.get("name", ""), block.get("input"), tool_map))
    usage = resp.get("usage") or {}
    status = "incomplete" if resp.get("stop_reason") == "max_tokens" else "completed"
    obj = _response_obj(
        _new_id("resp"),
        model,
        status,
        output,
        _usage(
            int(usage.get("input_tokens") or 0),
            int(usage.get("output_tokens") or 0),
            int(usage.get("cache_read_input_tokens") or 0),
            int(usage.get("cache_creation_input_tokens") or 0),
        ),
    )
    if status == "incomplete":
        obj["incomplete_details"] = {"reason": "max_output_tokens"}
    return obj


class ResponsesStreamTranslator:
    """Anthropic stream events → Responses SSE events (stateful, one request)."""

    def __init__(self, tool_map: ToolMap, *, model: str) -> None:
        self.tool_map = tool_map
        self.model = model
        self.resp_id = _new_id("resp")
        self.output: list[dict[str, Any]] = []
        self._blocks: dict[int, dict[str, Any]] = {}
        self._seq = 0
        self._input_tokens = 0
        self._output_tokens = 0
        self._cached = 0
        self._cache_write = 0
        self._stop_reason: str | None = None
        self._stopped = False
        self.started = False

    def _ev(self, etype: str, **data: Any) -> dict[str, Any]:
        self._seq += 1
        return {"type": etype, "sequence_number": self._seq, **data}

    def start(self) -> list[dict[str, Any]]:
        self.started = True
        return [
            self._ev(
                "response.created",
                response=_response_obj(self.resp_id, self.model, "in_progress", [], None),
            )
        ]

    def feed(self, ev: AnthropicStreamEvent) -> list[dict[str, Any]]:
        data = ev.data or {}
        etype = ev.type
        out: list[dict[str, Any]] = []
        if etype == "message_start":
            usage = (data.get("message") or {}).get("usage") or {}
            self._input_tokens = int(usage.get("input_tokens") or 0)
            self._cached = int(usage.get("cache_read_input_tokens") or 0)
            self._cache_write = int(usage.get("cache_creation_input_tokens") or 0)
        elif etype == "content_block_start":
            idx = int(data.get("index", 0))
            cb = data.get("content_block") or {}
            ctype = cb.get("type")
            if ctype == "text":
                item = {
                    "type": "message",
                    "id": _new_id("msg"),
                    "role": "assistant",
                    "status": "in_progress",
                    "content": [],
                }
                self._blocks[idx] = {"kind": "text", "item": item, "text": "", "out_index": len(self.output)}
                self.output.append(item)
                out.append(self._ev("response.output_item.added", output_index=self._blocks[idx]["out_index"], item=item))
                out.append(
                    self._ev(
                        "response.content_part.added",
                        item_id=item["id"],
                        output_index=self._blocks[idx]["out_index"],
                        content_index=0,
                        part={"type": "output_text", "text": "", "annotations": []},
                    )
                )
                if cb.get("text"):
                    out.extend(self._text_delta(idx, cb["text"]))
            elif ctype == "tool_use":
                self._blocks[idx] = {
                    "kind": "tool",
                    "id": cb.get("id") or _new_id("call"),
                    "name": cb.get("name", ""),
                    "json": "",
                    "input": cb.get("input") if cb.get("input") else None,
                }
            else:
                self._blocks[idx] = {"kind": "skip"}
        elif etype == "content_block_delta":
            idx = int(data.get("index", 0))
            delta = data.get("delta") or {}
            block = self._blocks.get(idx)
            if block is None:
                return out
            if block["kind"] == "text" and delta.get("type") == "text_delta":
                out.extend(self._text_delta(idx, delta.get("text", "")))
            elif block["kind"] == "tool" and delta.get("type") == "input_json_delta":
                block["json"] += delta.get("partial_json", "")
        elif etype == "content_block_stop":
            idx = int(data.get("index", 0))
            block = self._blocks.pop(idx, None)
            if block is None:
                return out
            if block["kind"] == "text":
                item = block["item"]
                item["status"] = "completed"
                item["content"] = [{"type": "output_text", "text": block["text"], "annotations": []}]
                out.append(
                    self._ev(
                        "response.output_text.done",
                        item_id=item["id"],
                        output_index=block["out_index"],
                        content_index=0,
                        text=block["text"],
                    )
                )
                out.append(self._ev("response.output_item.done", output_index=block["out_index"], item=item))
            elif block["kind"] == "tool":
                if block["json"]:
                    try:
                        args = json.loads(block["json"])
                    except ValueError:
                        args = {"_raw_arguments": block["json"]}
                else:
                    args = block["input"] or {}
                item = _tool_item(block["id"], block["name"], args, self.tool_map)
                out_index = len(self.output)
                self.output.append(item)
                out.append(self._ev("response.output_item.added", output_index=out_index, item=item))
                out.append(self._ev("response.output_item.done", output_index=out_index, item=item))
        elif etype == "message_delta":
            usage = data.get("usage") or {}
            if usage.get("output_tokens") is not None:
                self._output_tokens = int(usage["output_tokens"])
            if usage.get("input_tokens"):
                self._input_tokens = int(usage["input_tokens"])
            stop = (data.get("delta") or {}).get("stop_reason")
            if stop:
                self._stop_reason = stop
        elif etype == "message_stop":
            self._stopped = True
        return out

    def _text_delta(self, idx: int, text: str) -> list[dict[str, Any]]:
        if not text:
            return []
        block = self._blocks[idx]
        block["text"] += text
        return [
            self._ev(
                "response.output_text.delta",
                item_id=block["item"]["id"],
                output_index=block["out_index"],
                content_index=0,
                delta=text,
            )
        ]

    def finish(self) -> list[dict[str, Any]]:
        # A stream that ended without ``message_stop``, or with a block
        # still open, was cut off. Reporting it as completed would make
        # Codex drop the partial item and end the turn as a success; a
        # ``response.failed`` (retryable ``server_error``) makes it retry.
        if not self._stopped or self._blocks:
            return self.failed(
                "server_error", "upstream stream ended before the message was complete"
            )
        usage = _usage(self._input_tokens, self._output_tokens, self._cached, self._cache_write)
        if self._stop_reason == "max_tokens":
            obj = _response_obj(self.resp_id, self.model, "incomplete", self.output, usage)
            obj["incomplete_details"] = {"reason": "max_output_tokens"}
            return [self._ev("response.incomplete", response=obj)]
        obj = _response_obj(self.resp_id, self.model, "completed", self.output, usage)
        return [self._ev("response.completed", response=obj)]

    def failed(self, code: str, message: str) -> list[dict[str, Any]]:
        obj = _response_obj(self.resp_id, self.model, "failed", self.output, None)
        obj["error"] = {"code": code, "message": message}
        return [self._ev("response.failed", response=obj)]


def format_sse(event: dict[str, Any]) -> str:
    return f"event: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
