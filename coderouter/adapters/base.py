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
    # OpenAI spec allows content: null on assistant messages that carry only
    # tool_calls. Anthropic → OpenAI translation also produces this when an
    # assistant turn has only tool_use blocks (no text).
    content: str | list[dict[str, Any]] | None = None
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


# v0.6-B: per-call overrides resolved from the active profile. The engine
# builds one instance per request (since a profile is invariant across its
# chain) and threads it through every adapter call on that chain. Adapters
# use :meth:`effective_timeout` / :meth:`effective_append_system_prompt` to
# pick the winning value (profile override > provider default).
#
# Design notes:
#   - Both fields are Optional. ``None`` means "leave the provider default
#     alone" — so ``ProviderCallOverrides()`` is a safe no-op default and
#     legacy call sites that pass nothing keep their old behavior.
#   - ``append_system_prompt=""`` is a meaningful explicit value: "for
#     this profile, clear the provider's directive". The adapter must
#     distinguish ``None`` (no override) from ``""`` (override-to-empty).
class ProviderCallOverrides(BaseModel):
    """Per-call provider overrides, resolved from the active profile."""

    model_config = ConfigDict(extra="forbid")

    timeout_s: float | None = None
    append_system_prompt: str | None = None


class BaseAdapter(ABC):
    """Provider-specific adapter. Subclasses implement HTTP plumbing."""

    def __init__(self, config: ProviderConfig) -> None:
        self.config = config

    @property
    def name(self) -> str:
        return self.config.name

    # ---- v0.6-B override resolution helpers -----------------------------
    def effective_timeout(
        self, overrides: ProviderCallOverrides | None
    ) -> float:
        """Profile override wins when set; else provider default."""
        if overrides is not None and overrides.timeout_s is not None:
            return overrides.timeout_s
        return self.config.timeout_s

    def effective_append_system_prompt(
        self, overrides: ProviderCallOverrides | None
    ) -> str | None:
        """Profile override replaces provider directive when set.

        ``None`` means no override → fall through to provider. ``""``
        (explicit empty) means "clear the provider directive for this
        profile" → return None so the caller skips injection entirely.
        """
        if overrides is not None and overrides.append_system_prompt is not None:
            return overrides.append_system_prompt or None
        return self.config.append_system_prompt

    @abstractmethod
    async def healthcheck(self) -> bool:
        """Lightweight check that the upstream is reachable. Return True if healthy."""

    @abstractmethod
    async def generate(
        self,
        request: ChatRequest,
        *,
        overrides: ProviderCallOverrides | None = None,
    ) -> ChatResponse:
        """Non-streaming completion. Raise AdapterError on failure.

        ``overrides`` carries profile-level timeouts / directives (v0.6-B).
        Legacy callers that pass nothing keep the pre-v0.6-B behavior.
        """

    @abstractmethod
    def stream(
        self,
        request: ChatRequest,
        *,
        overrides: ProviderCallOverrides | None = None,
    ) -> AsyncIterator[StreamChunk]:
        """Streaming completion. Yield StreamChunks. Raise AdapterError on failure."""
