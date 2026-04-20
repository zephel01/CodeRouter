"""Pydantic schemas for providers.yaml and runtime config.

Design notes (see plan.md §2 / §5.4):
- Capability flags let providers declare what they support.
- `paid: true` providers are blocked unless ALLOW_PAID=true (memo.txt §2.3).
- Adapter `kind` in v0.3.x:
    - "openai_compat": llama.cpp / Ollama / OpenRouter / LM Studio / Together / Groq ...
    - "anthropic":     native Anthropic Messages API passthrough (api.anthropic.com,
                       or any server speaking the Anthropic wire format). When the
                       Anthropic ingress routes to this provider, no translation is
                       performed — request and response flow through verbatim.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator


class Capabilities(BaseModel):
    """Capability flags per provider (plan.md §2.5)."""

    model_config = ConfigDict(extra="forbid")

    chat: bool = True
    streaming: bool = True
    tools: bool = False
    vision: bool = False
    prompt_cache: bool = False
    # v0.5-A: Anthropic's extended-thinking body field (`thinking: {type:
    # enabled, budget_tokens: N}` or `{type: enabled}` adaptive). Narrow,
    # per-model flag — when unset, the capability gate falls back to a
    # model-name heuristic (see coderouter/routing/capability.py). Distinct
    # from `reasoning_control` below, which is the v1.0+ abstract interface.
    thinking: bool = False
    # v0.5-C: opt out of the openai_compat adapter's passive `reasoning`
    # field strip. By default (False), the adapter removes non-standard
    # `message.reasoning` / `delta.reasoning` fields emitted by some
    # OpenRouter free-tier models (gpt-oss-120b:free confirmed 2026-04)
    # because strict OpenAI clients reject the unknown key. Set True when
    # you explicitly want the raw reasoning text to flow to the client
    # (e.g. CodeRouter is fronting a reasoning-aware downstream).
    reasoning_passthrough: bool = False
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
    kind: Literal["openai_compat", "anthropic"] = Field(
        default="openai_compat",
        description=(
            "Adapter type. 'openai_compat' covers llama.cpp / Ollama / "
            "OpenRouter / LM Studio / Together / Groq. 'anthropic' is the "
            "native Anthropic Messages API passthrough (v0.3.x)."
        ),
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

    # v1.0-A: declarative output cleaning chain. Names map to filter
    # implementations in ``coderouter/output_filters.py`` — currently
    # ``strip_thinking`` (``<think>...</think>`` blocks) and
    # ``strip_stop_markers`` (``<|python_tag|>`` / ``<|eot_id|>`` /
    # ``<|im_end|>`` / ``<|turn|>`` / ``<|end|>`` / ``<|channel>thought``).
    # Empty = no scrubbing (backward compatible with v0.7.x). Applied at
    # the adapter boundary on both streaming and non-streaming paths;
    # stateful across SSE chunk boundaries. Unknown names fail at load.
    output_filters: list[str] = Field(
        default_factory=list,
        description=(
            "v1.0-A: ordered filter chain applied to assistant content. "
            "Known: strip_thinking, strip_stop_markers. Empty = off."
        ),
    )

    capabilities: Capabilities = Field(default_factory=Capabilities)

    @model_validator(mode="after")
    def _check_output_filters_known(self) -> ProviderConfig:
        """v1.0-A: fail at config-load on a typo'd filter name.

        Same fast-fail pattern as ``_check_default_profile_exists`` —
        surfaces ``output_filters: [strp_thinking]`` at startup rather
        than silently no-op'ing forever.
        """
        # Import locally to avoid a hard package-level cycle
        # (output_filters imports nothing from config).
        from coderouter.output_filters import validate_output_filters

        validate_output_filters(self.output_filters)
        return self


class FallbackChain(BaseModel):
    """An ordered list of provider names to try in sequence.

    v0.6-B: optional profile-level overrides for ``timeout_s`` and
    ``append_system_prompt``. When set, these REPLACE the provider's own
    values for calls routed through this profile — "replace" rather than
    "append" semantics keeps debugging predictable and matches how
    ``timeout_s`` (a scalar limit) naturally behaves. Unset fields leave
    the provider's own defaults in effect. The ``retry_max`` field is
    deferred to a later minor until a retry mechanism exists at the
    adapter layer (§9.3 #4 partial).
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., description="Profile name, e.g. 'default', 'coding'")
    providers: list[str] = Field(
        ...,
        min_length=1,
        description="Provider names in fallback order. First success wins.",
    )
    timeout_s: float | None = Field(
        default=None,
        ge=1.0,
        le=600.0,
        description=(
            "v0.6-B: profile-level HTTP timeout override (seconds). When "
            "set, replaces ``ProviderConfig.timeout_s`` for every call "
            "routed through this profile. Unset = provider default."
        ),
    )
    append_system_prompt: str | None = Field(
        default=None,
        description=(
            "v0.6-B: profile-level override for the provider's "
            "``append_system_prompt`` directive. When set, REPLACES the "
            "provider's directive for this profile (not appended). Pass "
            "an empty string to explicitly clear the provider directive "
            "for this profile."
        ),
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
    mode_aliases: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "v0.6-D: intent-to-profile mapping. Clients send "
            "``X-CodeRouter-Mode: coding`` and the ingress resolves it to "
            "the aliased profile name. Lets clients name their intent "
            "(``coding`` / ``long`` / ``fast``) independently of the "
            "underlying profile names — you can rewire the chain without "
            "touching client code. Keys = mode names, values = profile "
            "names (must exist in ``profiles``). Empty dict = feature off."
        ),
    )

    @model_validator(mode="after")
    def _check_default_profile_exists(self) -> CodeRouterConfig:
        """v0.6-A: surface a typo'd ``default_profile`` at load time.

        Previously a bad ``default_profile`` only blew up on the first
        request (``profile_by_name`` → KeyError → 500). Checking here
        converts a silent-until-used misconfig into a fast-fail at
        startup, which matches how ``--mode`` / ``CODEROUTER_MODE`` are
        validated in ``loader.py``.
        """
        names = {p.name for p in self.profiles}
        if self.default_profile not in names:
            raise ValueError(
                f"default_profile {self.default_profile!r} is not declared in "
                f"profiles: known={sorted(names)}"
            )
        return self

    @model_validator(mode="after")
    def _check_mode_alias_targets_exist(self) -> CodeRouterConfig:
        """v0.6-D: every ``mode_aliases`` value must point to a declared profile.

        Same fast-fail philosophy as ``_check_default_profile_exists``: a
        broken alias should 500 at load, not silently 400 for every
        request that uses that mode.
        """
        names = {p.name for p in self.profiles}
        bad = {mode: profile for mode, profile in self.mode_aliases.items() if profile not in names}
        if bad:
            raise ValueError(
                f"mode_aliases points to unknown profile(s): {bad}. known profiles={sorted(names)}"
            )
        return self

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

    def resolve_mode(self, mode: str) -> str:
        """v0.6-D: resolve a mode alias to a profile name.

        The startup validator guarantees every alias target exists in
        ``profiles``, so callers can pass the returned value straight to
        ``profile_by_name`` without a second existence check.

        Raises ``KeyError`` when ``mode`` is not in ``mode_aliases`` —
        the ingress layer catches it and returns 400 with the list of
        available modes.
        """
        if mode in self.mode_aliases:
            return self.mode_aliases[mode]
        raise KeyError(f"Unknown mode alias: {mode!r}")
