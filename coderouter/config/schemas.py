"""Pydantic schemas for providers.yaml and runtime config.

Design notes (see plan.md §2 / §5.4):
- Capability flags let providers declare what they support.
- `paid: true` providers are blocked unless ALLOW_PAID=true (memo.txt §2.3).
- Adapter `kind` is intentionally narrow in v0.1: only "openai_compat".
  v0.2+ will add "anthropic" (separate adapter — memo.txt §2.4).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class Capabilities(BaseModel):
    """Capability flags per provider (plan.md §2.5)."""

    model_config = ConfigDict(extra="forbid")

    chat: bool = True
    streaming: bool = True
    tools: bool = False
    vision: bool = False
    prompt_cache: bool = False
    # v1.0+ fields, declared early so providers.yaml can future-proof
    reasoning_control: Literal["none", "openai", "anthropic", "provider_specific"] = "none"
    mcp: Literal["none", "anthropic", "provider_specific"] = "none"
    openai_compatible: bool = True


class ProviderConfig(BaseModel):
    """A single provider entry from providers.yaml.

    Examples:
        - Local llama.cpp server: kind=openai_compat, base_url=http://localhost:8080/v1
        - OpenRouter free: kind=openai_compat, base_url=https://openrouter.ai/api/v1
        - (future) Anthropic: kind=anthropic, base_url=https://api.anthropic.com
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., description="Unique identifier used in profiles.yaml")
    kind: Literal["openai_compat"] = Field(
        default="openai_compat",
        description="Adapter type. v0.1 supports only openai_compat.",
    )
    base_url: HttpUrl
    model: str = Field(..., description="Upstream model id sent in the request body")
    api_key_env: str | None = Field(
        default=None,
        description="Env var name holding the API key. None = no auth (e.g. local).",
    )

    # Routing-relevant flags
    paid: bool = Field(
        default=False,
        description="If true, only used when ALLOW_PAID=true (plan.md §2.3).",
    )
    timeout_s: float = Field(default=30.0, ge=1.0, le=600.0)

    # Provider-specific extras merged into the outbound request body.
    # Use for non-standard fields like Ollama's `think: false`, `keep_alive`,
    # `options.num_ctx`, or any vendor-specific toggle. User-supplied request
    # fields take precedence over these defaults.
    extra_body: dict[str, object] = Field(default_factory=dict)

    # Directive appended to the system message content before sending.
    # Use for model-intrinsic switches that travel reliably through any API
    # layer — e.g. Qwen3's "/no_think" to skip the reasoning track, since
    # Ollama's OpenAI-compat endpoint silently drops the native `think` flag.
    append_system_prompt: str | None = Field(
        default=None,
        description="Appended to existing system message (or added as a new one).",
    )

    capabilities: Capabilities = Field(default_factory=Capabilities)


class FallbackChain(BaseModel):
    """An ordered list of provider names to try in sequence."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., description="Profile name, e.g. 'default', 'coding'")
    providers: list[str] = Field(
        ...,
        min_length=1,
        description="Provider names in fallback order. First success wins.",
    )


class CodeRouterConfig(BaseModel):
    """Top-level config loaded from providers.yaml."""

    model_config = ConfigDict(extra="forbid")

    allow_paid: bool = Field(
        default=False,
        description="Master switch. ALLOW_PAID=false blocks all paid providers (plan.md §2.3).",
    )
    default_profile: str = Field(default="default")
    providers: list[ProviderConfig] = Field(..., min_length=1)
    profiles: list[FallbackChain] = Field(..., min_length=1)

    def provider_by_name(self, name: str) -> ProviderConfig:
        """Look up a provider config by name. Raises KeyError if not found."""
        for p in self.providers:
            if p.name == name:
                return p
        raise KeyError(f"Provider not found: {name!r}")

    def profile_by_name(self, name: str) -> FallbackChain:
        """Look up a profile (fallback chain) by name."""
        for prof in self.profiles:
            if prof.name == name:
                return prof
        raise KeyError(f"Profile not found: {name!r}")
