"""Wire-format translators between Anthropic Messages and internal ChatRequest.

The internal ChatRequest / ChatResponse / StreamChunk shapes mirror OpenAI
Chat Completions (see coderouter/adapters/base.py). This package contains the
bidirectional translation layer used by the Anthropic ingress:

    AnthropicRequest  --to_chat_request-->  ChatRequest          --> adapter
    ChatResponse      --to_anthropic_response-->  AnthropicResponse
    StreamChunk...    --stream_to_anthropic_events-->  AnthropicStreamEvent...

v0.2 scope: spec-level translation for text + tool_use content blocks.
v1.0 scope: tool_call JSON repair / format normalization across local models.
"""

from coderouter.translation.anthropic import (
    AnthropicContentBlock,
    AnthropicImageBlock,
    AnthropicMessage,
    AnthropicRequest,
    AnthropicResponse,
    AnthropicStreamEvent,
    AnthropicTextBlock,
    AnthropicTool,
    AnthropicToolResultBlock,
    AnthropicToolUseBlock,
    AnthropicUsage,
)
from coderouter.translation.convert import (
    stream_anthropic_to_chat_chunks,
    stream_chat_to_anthropic_events,
    synthesize_anthropic_stream_from_response,
    to_anthropic_request,
    to_anthropic_response,
    to_chat_request,
    to_chat_response,
)

__all__ = [
    "AnthropicContentBlock",
    "AnthropicImageBlock",
    "AnthropicMessage",
    "AnthropicRequest",
    "AnthropicResponse",
    "AnthropicStreamEvent",
    "AnthropicTextBlock",
    "AnthropicTool",
    "AnthropicToolResultBlock",
    "AnthropicToolUseBlock",
    "AnthropicUsage",
    "stream_anthropic_to_chat_chunks",
    "stream_chat_to_anthropic_events",
    "synthesize_anthropic_stream_from_response",
    "to_anthropic_request",
    "to_anthropic_response",
    "to_chat_request",
    "to_chat_response",
]
