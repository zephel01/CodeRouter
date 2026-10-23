"""Pydantic models for the Anthropic Messages API wire format.

Reference: https://docs.anthropic.com/en/api/messages

v0.2 scope: text, image, tool_use, tool_result content blocks + streaming.
Out of scope (v0.3+): thinking blocks, cache_control, documents, citations.
These remaining shapes are represented with extra="allow" so they pass
through unchanged if a client sends them.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

logger = logging.getLogger(__name__)

# ============================================================
# Content blocks
# ============================================================


class AnthropicTextBlock(BaseModel):
    """Anthropic ``text`` content block — the common plain-prose case."""

    model_config = ConfigDict(extra="allow")

    type: Literal["text"] = "text"
    text: str


class AnthropicImageBlock(BaseModel):
    """Anthropic image block.

    `source` shape varies by type:
        - base64:   {"type": "base64", "media_type": "image/png", "data": "<b64>"}
        - url:      {"type": "url", "url": "https://..."}
    """

    model_config = ConfigDict(extra="allow")

    type: Literal["image"] = "image"
    source: dict[str, Any]


class AnthropicToolUseBlock(BaseModel):
    """Emitted by assistant when the model decides to call a tool."""

    model_config = ConfigDict(extra="allow")

    type: Literal["tool_use"] = "tool_use"
    id: str
    name: str
    input: dict[str, Any] = Field(default_factory=dict)


class AnthropicToolResultBlock(BaseModel):
    """Sent by user/client after executing a tool call the assistant requested."""

    model_config = ConfigDict(extra="allow")

    type: Literal["tool_result"] = "tool_result"
    tool_use_id: str
    # Anthropic accepts str OR list of blocks (text/image) as content.
    content: str | list[dict[str, Any]] | None = None
    is_error: bool | None = None


# Discriminated-union style isn't strictly required here — we union-type at
# the parsing boundary (AnthropicMessage.content) and dispatch on `type`.
AnthropicContentBlock = (
    AnthropicTextBlock
    | AnthropicImageBlock
    | AnthropicToolUseBlock
    | AnthropicToolResultBlock
    # forward-compat for unknown block types (thinking, document, etc.)
    | dict[str, Any]
)


# ============================================================
# Messages + Tools
# ============================================================


class AnthropicMessage(BaseModel):
    """A single message in the Anthropic messages array.

    `content` may be a string (short form) or a list of content blocks.
    """

    model_config = ConfigDict(extra="allow")

    role: Literal["user", "assistant"]
    content: str | list[dict[str, Any]]


class AnthropicTool(BaseModel):
    """Tool definition as sent by the client in Anthropic format."""

    model_config = ConfigDict(extra="allow")

    name: str
    description: str | None = None
    # Anthropic's field name (OpenAI calls it `parameters`).
    input_schema: dict[str, Any] = Field(default_factory=dict)


# ============================================================
# Role normalization (Claude Code CLI >= 2.1.154 workaround)
# ============================================================

_SPEC_MESSAGE_ROLES = frozenset({"user", "assistant"})


def _content_as_text(content: Any) -> str:
    """Best-effort plain-text extraction from a message ``content`` field.

    Strings pass through; block lists contribute their ``text`` blocks
    joined with newlines; anything else yields "".
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        return "\n".join(p for p in parts if p)
    return ""


def normalize_message_roles(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize non-spec roles inside ``messages`` before validation.

    Claude Code CLI >= 2.1.154 has a regression where it emits messages
    with ``role: "system"`` (and reportedly ``ctx`` / ``msg``) inside the
    ``messages`` array. The Anthropic Messages API spec allows only
    ``user`` / ``assistant`` there, so without this hop those requests
    die in validation with "Input should be 'user' or 'assistant'"
    (see anthropics/claude-code#63469, vllm-project/vllm#44000).

    Policy:
        - leading ``role: "system"`` (before any user/assistant turn) →
          text content merged into the top-level ``system`` field
          (appended after any existing system prompt; same join rule as
          ``convert.to_anthropic_request``). This is a genuine system
          prompt from an OpenAI-style client, and it stays at position 0.
        - mid-conversation ``role: "system"`` → coerced to ``user`` **in
          place**, like any other non-spec role. Claude Code's
          system-reminders are volatile per-turn text; hoisting them to
          the top-level field moves content that changes every turn ahead
          of the whole conversation, which invalidates the prefix cache of
          local backends (llama.cpp / LM Studio) and forces a full prompt
          reprocess on every request.
        - any other non-spec role (``ctx``, ``msg``, ...) → coerced to
          ``user`` so conversation position is preserved. Anthropic
          merges consecutive same-role turns, so this is safe.
        - messages whose salvaged content is empty are dropped entirely
          (Anthropic rejects empty turns).

    Returns a shallow-copied payload; the caller's dict is not mutated.
    Non-dict message entries (already-validated models) pass through.
    """
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return payload

    system_texts: list[str] = []
    messages_out: list[Any] = []
    coerced_roles: list[str] = []

    for msg in messages:
        if not isinstance(msg, dict):
            # Already a validated AnthropicMessage (internal construction
            # path, e.g. convert.to_anthropic_request) — spec roles only.
            messages_out.append(msg)
            continue
        role = msg.get("role")
        if role in _SPEC_MESSAGE_ROLES:
            messages_out.append(msg)
            continue
        if role == "system":
            text = _content_as_text(msg.get("content"))
            coerced_roles.append("system")
            if text:
                if not messages_out:
                    # Leading system message — a genuine system prompt from an
                    # OpenAI-style client. Hoisting to the top-level field is
                    # correct and prefix-safe (it stays at position 0).
                    system_texts.append(text)
                else:
                    # Mid-conversation system message — volatile per-turn text
                    # (Claude Code >= 2.1.154 system-reminders). Keep it where
                    # the client put it. Hoisting moves text that changes every
                    # turn ahead of the entire conversation, which invalidates
                    # the prefix cache of local backends (llama.cpp / LM Studio)
                    # and forces a full prompt reprocess on every request.
                    messages_out.append({"role": "user", "content": text})
            continue
        # Unknown role (ctx / msg / future surprises): keep its position
        # in the conversation as a user turn; drop if nothing salvageable.
        text = _content_as_text(msg.get("content"))
        coerced_roles.append(str(role))
        if text:
            messages_out.append({"role": "user", "content": text})

    if not coerced_roles:
        return payload

    out = dict(payload)
    out["messages"] = messages_out

    if system_texts:
        joined = "\n".join(system_texts)
        existing = out.get("system")
        if existing is None:
            out["system"] = joined
        elif isinstance(existing, str):
            out["system"] = f"{existing}\n{joined}" if existing else joined
        elif isinstance(existing, list):
            out["system"] = [*existing, {"type": "text", "text": joined}]
        else:  # unexpected shape — don't lose the client's value
            out["system"] = existing

    logger.warning(
        "normalized-nonspec-message-roles",
        extra={
            "roles": coerced_roles,
            "system_merged": bool(system_texts),
            "hint": "client is likely Claude Code CLI >= 2.1.154 (known regression)",
        },
    )
    return out


# ============================================================
# Request
# ============================================================


class AnthropicRequest(BaseModel):
    """Inbound request body for POST /v1/messages.

    Required fields per Anthropic spec: model, max_tokens, messages.
    Everything else is optional.

    CodeRouter specifics:
        - `model` is ignored for routing decisions (same rule as OpenAI ingress).
        - `profile` is a CodeRouter extension (same as OpenAI ingress).
    """

    model_config = ConfigDict(extra="allow")

    model: str | None = None  # ignored for routing (see docstring)
    max_tokens: int
    messages: list[AnthropicMessage]
    system: str | list[dict[str, Any]] | None = None
    tools: list[AnthropicTool] | None = None
    tool_choice: dict[str, Any] | None = None
    temperature: float | None = None
    top_p: float | None = None
    top_k: int | None = None
    stop_sequences: list[str] | None = None
    stream: bool = False
    metadata: dict[str, Any] | None = None

    # CodeRouter-specific extension, not sent upstream.
    profile: str | None = Field(default=None, exclude=True)

    # Populated from the `anthropic-beta` HTTP header by the Anthropic
    # ingress (v0.4-D). Not a wire field — it's a header passthrough
    # hop, not part of the JSON body. When set, the native adapter
    # forwards it to `api.anthropic.com` verbatim. This is what unlocks
    # beta-gated body fields like `context_management`, `cache_control`,
    # `thinking` beyond what the default minor version accepts.
    anthropic_beta: str | None = Field(default=None, exclude=True)
    # v2.18.0: conversation key for KV-slot affinity (Claude Code's
    # ``x-claude-code-session-id``). Never serialized.
    session_key: str | None = Field(default=None, exclude=True)

    @model_validator(mode="before")
    @classmethod
    def _normalize_roles(cls, data: Any) -> Any:
        """Claude Code >= 2.1.154 sends system/ctx/msg roles in messages.

        Normalize them before field validation so the request doesn't
        422 at ingress (and doesn't 400 upstream at api.anthropic.com
        via the native adapter). See ``normalize_message_roles``.
        """
        if isinstance(data, dict):
            return normalize_message_roles(data)
        return data


# ============================================================
# Response
# ============================================================


class AnthropicUsage(BaseModel):
    """Token accounting on an Anthropic response / ``message_delta`` event.

    Cache-hit / cache-creation tokens aren't modeled explicitly —
    ``extra="allow"`` lets them round-trip when present so the
    Anthropic ⇄ OpenAI translation preserves them verbatim.
    """

    model_config = ConfigDict(extra="allow")

    input_tokens: int = 0
    output_tokens: int = 0


class AnthropicResponse(BaseModel):
    """Non-streaming response for POST /v1/messages."""

    model_config = ConfigDict(extra="allow")

    id: str
    type: Literal["message"] = "message"
    role: Literal["assistant"] = "assistant"
    model: str
    content: list[dict[str, Any]]
    # H-6: intentionally `str | None`, NOT a closed `Literal[...]`. Anthropic
    # adds new stop_reason values over time (observed additions: pause_turn
    # for long-running server-side-tool turns, refusal for safety-classifier
    # stops, model_context_window_exceeded); a closed Literal rejects any
    # response using a value minted after this code was written, which
    # pydantic surfaces as a ValidationError that the adapter turns into a
    # retryable AdapterError — silently discarding an already-billed,
    # perfectly valid response and falling back to another provider. Known
    # values as of writing: end_turn, max_tokens, stop_sequence, tool_use,
    # pause_turn, refusal, model_context_window_exceeded. Other models in
    # this module already use `extra="allow"` for the same forward-
    # compatibility reason; this keeps that policy consistent for a
    # declared field, which `extra="allow"` alone does not cover.
    stop_reason: str | None = None
    stop_sequence: str | None = None
    usage: AnthropicUsage = Field(default_factory=AnthropicUsage)

    # Routing metadata — added by CodeRouter, not from upstream.
    coderouter_provider: str | None = None


# ============================================================
# Streaming events
# ============================================================


class AnthropicStreamEvent(BaseModel):
    """Generic envelope for an SSE event.

    The actual wire emission is `event: <type>\\ndata: <json>\\n\\n`; we store
    the event type separately for routing and the payload as a plain dict so
    the translator can build any event shape without a matrix of subclasses.
    """

    model_config = ConfigDict(extra="allow")

    type: str
    data: dict[str, Any]
