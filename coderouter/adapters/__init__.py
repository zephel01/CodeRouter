"""Provider adapters."""

from coderouter.adapters.base import (
    AdapterError,
    BaseAdapter,
    ChatRequest,
    ChatResponse,
    Message,
    StreamChunk,
)
from coderouter.adapters.openai_compat import OpenAICompatAdapter
from coderouter.adapters.registry import build_adapter

__all__ = [
    "AdapterError",
    "BaseAdapter",
    "ChatRequest",
    "ChatResponse",
    "Message",
    "OpenAICompatAdapter",
    "StreamChunk",
    "build_adapter",
]
