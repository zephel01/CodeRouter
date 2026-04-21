"""Pydantic models for the Anthropic Messages API wire format.

Reference: https://docs.anthropic.com/en/api/messages

v0.2 scope: text, image, tool_use, tool_result content blocks + streaming.
Out of scope (v0.3+): thinking blocks, cache_control, documents, citations.
These remaining shapes are represented with extra="allow" so they pass
through unchanged if a client sends them.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

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
    stop_reason: Literal["end_turn", "max_tokens", "stop_sequence", "tool_use"] | None = None
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
