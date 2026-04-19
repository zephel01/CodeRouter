"""Common intermediate format + BaseAdapter ABC.

The shape mirrors OpenAI's Chat Completions API since memo.txt §2.4 chose
OpenAI-compat as the standard ingress. v0.2+ will add a separate Anthropic
adapter that converts Messages API into / out of this same format.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from coderouter.config.schemas import ProviderConfig


class Message(BaseModel):
    model_config = ConfigDict(extra="allow")

    role: Literal["system", "user", "assistant", "tool"]
    content: str | list[dict[str, Any]]
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: list[dict[str, Any]] | None = None


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str | None = None
    messages: list[Message]
    stream: bool = False
    temperature: float | None = None
    max_tokens: int | None = None
    top_p: float | None = None
    stop: list[str] | None = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: Any | None = None

    # CodeRouter-specific extension (not sent upstream)
    profile: str | None = Field(default=None, exclude=True)


class ChatResponse(BaseModel):
    """A non-streaming response in OpenAI Chat Completions shape."""

    model_config = ConfigDict(extra="allow")

    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: list[dict[str, Any]]
    usage: dict[str, Any] | None = None

    # Routing metadata — added by CodeRouter, not from upstream
    coderouter_provider: str | None = Field(default=None)


class StreamChunk(BaseModel):
    """A single SSE chunk in OpenAI streaming format."""

    model_config = ConfigDict(extra="allow")

    id: str
    object: str = "chat.completion.chunk"
    created: int
    model: str
    choices: list[dict[str, Any]]


class AdapterError(Exception):
    """Raised when a provider call fails in a way the fallback engine should retry on."""

    def __init__(
        self,
        message: str,
        *,
        provider: str,
        status_code: int | None = None,
        retryable: bool = True,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.status_code = status_code
        self.retryable = retryable

    def __str__(self) -> str:
        sc = f" status={self.status_code}" if self.status_code is not None else ""
        return f"[{self.provider}{sc}] {super().__str__()}"


class BaseAdapter(ABC):
    """Provider-specific adapter. Subclasses implement HTTP plumbing."""

    def __init__(self, config: ProviderConfig) -> None:
        self.config = config

    @property
    def name(self) -> str:
        return self.config.name

    @abstractmethod
    async def healthcheck(self) -> bool:
        """Lightweight check that the upstream is reachable. Return True if healthy."""

    @abstractmethod
    async def generate(self, request: ChatRequest) -> ChatResponse:
        """Non-streaming completion. Raise AdapterError on failure."""

    @abstractmethod
    def stream(self, request: ChatRequest) -> AsyncIterator[StreamChunk]:
        """Streaming completion. Yield StreamChunks. Raise AdapterError on failure."""
