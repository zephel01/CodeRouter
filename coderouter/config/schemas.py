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

import re
from typing import Literal, Self

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


class CostConfig(BaseModel):
    """v1.9-D: per-provider unit pricing for cost aggregation.

    All fields are optional. When :attr:`ProviderConfig.cost` is unset,
    the provider contributes zero to the cost dashboard but still
    appears in token-count totals — same shape as a free local model.

    Pricing model
    -------------

    Anthropic's prompt-cache pricing (verified 2026-04 docs.anthropic.com):

      * Normal input  : 1.0x ``input_tokens_per_million``
      * Normal output : 1.0x ``output_tokens_per_million``
      * Cache read    : ``cache_read_discount`` x normal input
      * Cache creation: ``cache_creation_premium`` x normal input

    The 4-class breakdown (cache_hit / cache_creation / no_cache /
    unknown) recorded by v1.9-A's ``cache-observed`` log lets the
    cost aggregator apply the right multiplier per token, and the
    "savings" figure in the dashboard is computed as
    ``cache_read_input_tokens x normal x (1 - cache_read_discount)``
    — i.e. what the operator *would have* paid without prompt
    caching.

    LiteLLM's cost tracker (verified 2026-04) does not implement
    cache-aware breakdown; it bills ``cache_read_input_tokens`` at
    full input rate, overstating spend on cache-heavy workloads. The
    CodeRouter dashboard's selling point is correctness here.
    """

    model_config = ConfigDict(extra="forbid")

    input_tokens_per_million: float | None = Field(
        default=None,
        ge=0.0,
        description=(
            "USD per million input tokens at normal (uncached) rate. "
            "Anthropic Sonnet 4.x is around 3.00, Opus 4.x around 15.00 "
            "(check the upstream's pricing page — values change)."
        ),
    )
    output_tokens_per_million: float | None = Field(
        default=None,
        ge=0.0,
        description=(
            "USD per million output tokens. Output is invariably the "
            "expensive side of the meter — for coding workloads with "
            "large completions this dominates the bill."
        ),
    )
    cache_read_discount: float = Field(
        default=0.10,
        ge=0.0,
        le=1.0,
        description=(
            "Multiplier applied to ``input_tokens_per_million`` for "
            "tokens served from prompt cache. Anthropic's 2026-04 "
            "pricing is 0.10 (i.e. cache reads are billed at 10% of "
            "normal input rate). LM Studio /v1/messages locally "
            "honors the cache_read field but local backends usually "
            "have ``input_tokens_per_million`` of 0.0, so this field "
            "is moot there."
        ),
    )
    cache_creation_premium: float = Field(
        default=1.25,
        ge=0.0,
        description=(
            "Multiplier applied to ``input_tokens_per_million`` for "
            "tokens *written* to the prompt cache on the first hit. "
            "Anthropic's 2026-04 pricing is 1.25 (cache writes cost "
            "25% more than normal input on the writeback call; "
            "subsequent reads then cost ``cache_read_discount`` x, "
            "amortizing the writeback). Above 1.0 means premium, "
            "1.0 = no premium, below 1.0 = discount on creation "
            "(unusual but theoretically supported by the schema)."
        ),
    )
    monthly_budget_usd: float | None = Field(
        default=None,
        ge=0.0,
        description=(
            "v1.10 (LiteLLM 由来 / v1.9-D の累積版): per-provider "
            "monthly USD spend cap. When set, the engine's chain "
            "resolver skips this provider and emits "
            "``skip-budget-exceeded`` once the running per-provider "
            "total for the current calendar month (UTC) reaches or "
            "exceeds this value. Unset (None) = no cap (default). "
            "\n\n"
            "Reset semantics: in-memory only — running totals zero "
            "out on process restart and on UTC calendar-month "
            "rollover. Operators who need durable budget state "
            "across restarts should pair this with external "
            "monitoring on the cost dashboard's ``cost_total_usd`` "
            "panel; persistent budget state is out of scope for "
            "v1.10 (no on-disk store, no Redis, etc., per the "
            "5-deps invariant in plan.md §5.4)."
        ),
    )


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

    cost: CostConfig | None = Field(
        default=None,
        description=(
            "v1.9-D: per-provider unit pricing for cost aggregation. "
            "Unset = provider contributes zero to the cost dashboard "
            "(typical for local models). Set on paid endpoints to "
            "feed the ``/dashboard`` cost panel and the "
            "``coderouter stats --cost`` TUI summary. Cache-aware "
            "calculation differentiates cache_read (90% discount on "
            "Anthropic) from normal input — see :class:`CostConfig`."
        ),
    )

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
    # v1.9-E (L3): tool-loop detection guard.
    #
    # Long-running agent loops can fall into "tool stuck" states where
    # the assistant repeatedly calls the same tool with identical args
    # because it can't make progress. The guard inspects the assistant
    # tool_use history in the inbound request and, when the same call
    # repeats above the threshold, takes the configured action.
    #
    # Three actions trade off intervention against UX disruption:
    #   * ``warn``   — emit a structured ``tool-loop-detected`` log only.
    #                  Diagnostic; default for v1.9-E.
    #   * ``inject`` — append a system message reminder ("you appear to
    #                  be looping, try a different approach") so the
    #                  next assistant turn has a chance to course-correct.
    #   * ``break``  — short-circuit the request with an error response.
    #                  Use when downstream cost / context exhaustion is
    #                  worse than telling the agent to stop.
    tool_loop_window: int = Field(
        default=5,
        ge=2,
        le=50,
        description=(
            "v1.9-E (L3): how many of the most recent assistant tool_use "
            "blocks to inspect for a loop. Default 5 covers the typical "
            "Claude Code agent step depth without false-positiving on "
            "legitimate same-tool repetition (e.g. iterating Read on "
            "different files)."
        ),
    )
    tool_loop_threshold: int = Field(
        default=3,
        ge=2,
        le=50,
        description=(
            "v1.9-E (L3): how many *consecutive identical* tool calls "
            "(same name + same args) trigger a loop verdict. Default 3 "
            "catches the most common stuck patterns (Read same file 3x, "
            "Bash same command 3x) while leaving headroom for "
            "intentional repetition with intermediate observations."
        ),
    )
    tool_loop_action: Literal["warn", "inject", "break"] = Field(
        default="warn",
        description=(
            "v1.9-E (L3): action when a loop is detected. ``warn`` (default) "
            "emits a log line only; ``inject`` adds a ``you-are-looping`` "
            "system message reminder to the request; ``break`` returns an "
            "error response. See FallbackChain comment for trade-offs."
        ),
    )
    # v1.9-E phase 2 (L2): memory-pressure detection + cooldown.
    #
    # Local backends (Ollama / LM Studio / llama.cpp) report VRAM
    # exhaustion via 5xx responses with bodies like "out of memory" /
    # "CUDA out of memory" / "insufficient memory". When the chain
    # encounters one of these, marking the provider as "pressured"
    # for a cooldown window prevents the engine from re-hammering the
    # same exhausted backend on the very next request — the chain
    # falls through to the next provider, which is typically a
    # lighter-weight model or a remote fallback that has the headroom.
    #
    # Three actions trade off intervention against operator preference:
    #   * ``off``   — no detection / no logging / no skip. Backward-compat default.
    #   * ``warn``  — emit ``memory-pressure-detected`` log when an OOM
    #                 error is observed; do not skip on subsequent calls.
    #   * ``skip``  — ``warn`` + put the provider in a cooldown window;
    #                 subsequent chain resolves filter it out and emit
    #                 ``skip-memory-pressure`` until the cooldown expires.
    memory_pressure_action: Literal["off", "warn", "skip"] = Field(
        default="warn",
        description=(
            "v1.9-E (L2 phase 2): action on observed backend OOM "
            "(provider failure with an out-of-memory error body). "
            "``warn`` (default) logs only — diagnostic, no chain "
            "behavior change. ``skip`` enters a cooldown window so "
            "the next request's chain resolver filters the pressured "
            "provider out and falls through to the next entry. "
            "``off`` disables the detector entirely (zero "
            "observation overhead, identical to v1.9.x behavior)."
        ),
    )
    memory_pressure_cooldown_s: int = Field(
        default=120,
        ge=10,
        le=3600,
        description=(
            "v1.9-E (L2 phase 2): cooldown window in seconds applied "
            "after an OOM detection when ``memory_pressure_action`` "
            "is ``skip``. Default 120 s gives the local backend "
            "enough time to release model state from VRAM before the "
            "engine re-attempts. Capped at 3600 s (1 hour) — anything "
            "longer is better expressed as marking the provider "
            "``paid: true`` and bouncing the process."
        ),
    )
    # v1.9-E phase 2 (L5): backend health monitoring (passive).
    #
    # A consecutive-failure state machine per provider:
    #   * HEALTHY   — no recent failures (initial state).
    #   * DEGRADED  — ``backend_health_threshold`` consecutive failures
    #                 observed; the provider has lost its "fresh" status
    #                 but is still attempted in chain order.
    #   * UNHEALTHY — ``2 x backend_health_threshold`` consecutive
    #                 failures; depending on the action, the provider
    #                 is either demoted to chain end or skipped entirely.
    # A single success on ``provider-ok`` resets the counter and the
    # state to HEALTHY immediately — no rolling window, no debounce.
    # Distinct from the v1.9-C ``adaptive`` gradient (continuous
    # latency / error-rate buffer with debounce) which handles the
    # "slow but alive" case; L5 handles the "hard crash" case.
    backend_health_action: Literal["off", "warn", "demote"] = Field(
        default="warn",
        description=(
            "v1.9-E (L5 phase 2): action when a provider transitions "
            "to UNHEALTHY (consecutive failures crossed the threshold). "
            "``warn`` (default) emits a state-change log line only — "
            "diagnostic, no chain reorder. ``demote`` additionally "
            "moves the UNHEALTHY provider to the back of the chain "
            "for the next ``_resolve_chain`` (similar to v1.9-C "
            "adaptive demotion but state-machine-based, not "
            "rolling-window-based). ``off`` disables the monitor "
            "entirely (zero observation overhead, identical to "
            "v1.9.x behavior)."
        ),
    )
    backend_health_threshold: int = Field(
        default=3,
        ge=2,
        le=20,
        description=(
            "v1.9-E (L5 phase 2): consecutive-failure count that "
            "triggers the HEALTHY → DEGRADED transition. The "
            "DEGRADED → UNHEALTHY transition fires at ``2x`` this "
            "value. Default 3 catches "
            "Ollama / LM Studio crashes (which produce a deterministic "
            "5xx pattern on every retry) without flapping on transient "
            "blips that the v1.9-C adaptive adjuster already handles."
        ),
    )
    adaptive: bool = Field(
        default=False,
        description=(
            "v1.9-C: enable health-based dynamic chain reordering for "
            "this profile. When True, the engine consults its "
            "AdaptiveAdjuster and may demote providers whose rolling-"
            "window median latency or error rate exceeds the configured "
            "thresholds (1.5x global median / 10% errors). Demotions are "
            "debounced (30 s minimum between rank changes per provider) "
            "so a transient blip cannot oscillate the chain. When False "
            "(default), the static ``providers`` order is honored "
            "verbatim — no observation overhead. Orthogonal to L5 "
            "(binary HEALTHY/UNHEALTHY backend swap, planned for "
            "v1.9-E phase 3): C handles the gradient case during normal "
            "operation, L5 handles hard crashes."
        ),
    )


# ---------------------------------------------------------------------------
# v1.6-A: auto_router — declarative request-body classifier
# ---------------------------------------------------------------------------


class RuleMatcher(BaseModel):
    """One-of matcher for an :class:`AutoRouteRule`.

    Exactly one of the matcher fields must be set; the ``_exactly_one``
    validator enforces this at load. Adding a new matcher type means
    adding a new optional field — the single-field invariant enforces
    discriminated-union semantics without pydantic's tagged-union syntax.

    Variants (v1.6-A):

    - ``has_image: True`` — any ``image_url`` / ``image`` /
      ``input_image`` content block in the latest user message.
    - ``code_fence_ratio_min: 0.3`` — triple-backtick span chars ÷ total
      chars of latest user message is ``>=`` this threshold.
    - ``content_contains: "foo"`` — substring match (case-sensitive).
    - ``content_regex: r"..."`` — Python ``re.search``; compiled at
      model-construction time so typos fail startup.

    Variants ([Unreleased] / per-model auto-routing, free-claude-code 由来):

    - ``model_pattern: r"claude-3-5-haiku.*"`` — Python ``re.fullmatch``
      against the request body's ``model`` field. Lets clients route on
      the model identifier the agent (Claude Code / Cursor) sent
      (Opus / Sonnet / Haiku → different profiles) without needing an
      explicit ``profile`` field on the wire. Compiled at load like
      ``content_regex``. ``fullmatch`` semantics (vs ``search`` for
      ``content_regex``) because model identifiers are structured tokens
      — users typically describe the whole identifier with a wildcard
      tail, not an arbitrary substring.

    Variants ([Unreleased] / longContext auto-switch, claude-code-router
    由来):

    - ``content_token_count_min: 32000`` — char-count ÷ 4 heuristic
      across **all** messages in the request body (not just the
      latest user message — this matcher describes the request's
      overall size). When the estimated token count is ``>=`` the
      threshold, route to a long-context profile (typically pointing
      at Gemini Flash 1M ctx, Haiku 200K, etc.). Distinct from the
      other content matchers which operate on the latest user
      message only — context-window pressure is a request-shape
      property, not a per-turn property. The estimator deliberately
      avoids tiktoken / SentencePiece (forbidden by the 5-deps
      invariant in plan.md §5.4); operators with non-English-heavy
      workloads can compensate by tuning the threshold, since the
      char/4 heuristic is conservative for CJK and looser for
      English code.

    Variants ([Unreleased] / tool-aware routing, OpenClaw + Pi 由来):

    - ``has_tools: True`` — the request body declares one or more
      tools (OpenAI ``tools[]`` / Anthropic ``tools[]`` / OpenAI legacy
      ``functions[]``). Lets operators send tool-laden requests to a
      tool-capable cloud profile while keeping plain chat on a small
      local model (typical Raspberry Pi / low-spec deployment shape:
      a 1-4B local model that cannot reliably tool-call paired with a
      free-tier cloud chain that can). Distinct from the
      ``capabilities.tools`` flag on a provider — that flag is read by
      ``coderouter doctor`` for diagnostics but does NOT gate the
      fallback chain (the chain just iterates providers in order and
      engages the v0.3-D tool-downgrade path on non-native ones with
      ``request.tools`` set). The ``has_tools`` matcher is the
      profile-level lever for steering tool-laden traffic to the right
      chain entirely.
    """

    model_config = ConfigDict(extra="forbid")

    has_image: bool | None = None
    code_fence_ratio_min: float | None = Field(default=None, ge=0.0, le=1.0)
    content_contains: str | None = None
    content_regex: str | None = None
    model_pattern: str | None = None
    content_token_count_min: int | None = Field(default=None, ge=1)
    # [Unreleased]: tool-aware routing (OpenClaw + Raspberry Pi 由来).
    # See class docstring "Variants ([Unreleased] / tool-aware routing)"
    # above for the full rationale. Boolean shape mirrors ``has_image`` —
    # only the ``True`` value is meaningful (matches when the body
    # declares any tools); ``False`` is rejected by ``_exactly_one``
    # since a "no-tools" rule would shadow the default fall-through.
    has_tools: bool | None = None

    _MATCHER_FIELDS: tuple[str, ...] = (
        "has_image",
        "code_fence_ratio_min",
        "content_contains",
        "content_regex",
        "model_pattern",
        "content_token_count_min",
        "has_tools",
    )

    @model_validator(mode="after")
    def _exactly_one(self) -> Self:
        set_fields = [
            name for name in self._MATCHER_FIELDS if getattr(self, name) is not None
        ]
        if len(set_fields) != 1:
            raise ValueError(
                f"RuleMatcher must have exactly one matcher field set, "
                f"got {len(set_fields)}: {set_fields}"
            )
        return self

    @model_validator(mode="after")
    def _compile_regex_eagerly(self) -> Self:
        """Compile ``content_regex`` / ``model_pattern`` at load so bad
        patterns fail startup rather than at first request.
        """
        if self.content_regex is not None:
            try:
                re.compile(self.content_regex)
            except re.error as exc:
                raise ValueError(
                    f"Invalid regex for content_regex {self.content_regex!r}: {exc}"
                ) from exc
        if self.model_pattern is not None:
            try:
                re.compile(self.model_pattern)
            except re.error as exc:
                raise ValueError(
                    f"Invalid regex for model_pattern {self.model_pattern!r}: {exc}"
                ) from exc
        return self


class AutoRouteRule(BaseModel):
    """One rule in ``auto_router.rules``: matcher → profile."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(
        description=(
            "Stable identifier surfaced in the auto-router-resolved log "
            "payload. Recommended prefixes: ``builtin:`` for bundled "
            "rules, ``user:`` for YAML-defined rules."
        ),
    )
    profile: str = Field(
        description="Profile name to resolve to. Must exist in profiles[].",
    )
    match: RuleMatcher


class AutoRouterConfig(BaseModel):
    """The ``auto_router:`` block in providers.yaml.

    When absent and ``default_profile == "auto"``, the bundled ruleset
    (``BUNDLED_RULES`` in :mod:`coderouter.routing.auto_router`) applies.
    When present, ``rules`` entirely **replaces** bundled rules (no
    merge) — see ``docs/designs/v1.6-auto-router.md`` §7 for rationale.
    """

    model_config = ConfigDict(extra="forbid")

    disabled: bool = Field(
        default=False,
        description=(
            "Hard off-switch. When True, classification is skipped and "
            "``default_rule_profile`` is used unconditionally."
        ),
    )
    rules: list[AutoRouteRule] = Field(
        default_factory=list,
        description="Ordered rules; first match wins.",
    )
    default_rule_profile: str = Field(
        default="writing",
        description=(
            "Profile used when no rule matches (or when ``disabled`` is "
            "True). Must exist in profiles[]."
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
    # v1.5-E: display-time timezone for dashboard + ``coderouter stats``.
    # The metrics ring keeps timestamps in UTC ISO form (stable wire format,
    # matches JsonLineFormatter); this field only affects rendering. When
    # unset, consumers default to UTC (no behavior change from v1.5-D). An
    # IANA name is required — offset strings like ``+09:00`` are rejected to
    # keep DST semantics unambiguous. Validated via ``zoneinfo.ZoneInfo`` at
    # load time so a typo like ``Asia/Tokyoo`` fails fast rather than 500'ing
    # the first dashboard poll.
    display_timezone: str | None = Field(
        default=None,
        description=(
            "v1.5-E: IANA timezone name used for rendering timestamps in "
            "``/dashboard`` and ``coderouter stats``. Example: ``Asia/Tokyo`` "
            "or ``America/New_York``. None → UTC. The underlying "
            "``/metrics.json`` snapshot keeps UTC ISO timestamps; conversion "
            "is display-only."
        ),
    )
    # v1.6-A: optional auto-routing rules. When ``default_profile == "auto"``
    # and this field is None, the bundled ruleset (image → multi /
    # code-fence → coding / fallthrough → writing) applies. When set,
    # ``rules`` is a complete replacement (no merge with bundled).
    auto_router: AutoRouterConfig | None = Field(
        default=None,
        description=(
            "v1.6-A: classifier rules consulted only when "
            "``default_profile == 'auto'``. None + auto → bundled rules "
            "apply (requires multi/coding/writing profiles to exist). "
            "Set to override bundled behavior."
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

        v1.6-A: ``default_profile == "auto"`` is a reserved sentinel
        that triggers the auto-router; it never maps to a declared
        profile directly and is therefore exempt from this existence
        check.
        """
        if self.default_profile == "auto":
            return self
        names = {p.name for p in self.profiles}
        if self.default_profile not in names:
            raise ValueError(
                f"default_profile {self.default_profile!r} is not declared in "
                f"profiles: known={sorted(names)}"
            )
        return self

    @model_validator(mode="after")
    def _check_auto_is_reserved(self) -> CodeRouterConfig:
        """v1.6-A: ``auto`` is a reserved sentinel for the auto-router.

        Users cannot define a profile named ``auto`` — it would collide
        with the ``default_profile: auto`` trigger. Fast-fail at load
        with a pointer to rename.
        """
        for prof in self.profiles:
            if prof.name == "auto":
                raise ValueError(
                    "'auto' is reserved as a profile name in v1.6+ "
                    "(it is the sentinel that activates auto_router). "
                    "Rename this profile to something else, e.g. "
                    "'auto-route' or 'smart'."
                )
        return self

    @model_validator(mode="after")
    def _check_auto_router_profiles_exist(self) -> CodeRouterConfig:
        """v1.6-A: every ``auto_router.rules[*].profile`` must be declared.

        Also validates ``default_rule_profile``. Same fast-fail
        philosophy as :meth:`_check_default_profile_exists` and
        :meth:`_check_mode_alias_targets_exist`.
        """
        if self.auto_router is None:
            return self
        names = {p.name for p in self.profiles}
        bad = sorted(
            {
                r.profile
                for r in self.auto_router.rules
                if r.profile not in names
            }
        )
        if bad:
            raise ValueError(
                f"auto_router.rules points to unknown profile(s): {bad}. "
                f"known profiles={sorted(names)}"
            )
        if self.auto_router.default_rule_profile not in names:
            raise ValueError(
                f"auto_router.default_rule_profile "
                f"{self.auto_router.default_rule_profile!r} is not declared "
                f"in profiles: known={sorted(names)}"
            )
        return self

    @model_validator(mode="after")
    def _check_bundled_auto_router_requirements(self) -> CodeRouterConfig:
        """v1.6-A: bundled ruleset needs multi/coding/writing to exist.

        Only fires when the user opted into auto routing
        (``default_profile == 'auto'``) without supplying a custom
        ``auto_router`` block. In that path the classifier falls back to
        the bundled rules (see
        :mod:`coderouter.routing.auto_router`), which reference three
        named profiles. Missing any of them would 500 on the first
        request, so we surface it at load instead.
        """
        if self.default_profile != "auto" or self.auto_router is not None:
            return self
        names = {p.name for p in self.profiles}
        required = ("multi", "coding", "writing")
        missing = [r for r in required if r not in names]
        if missing:
            raise ValueError(
                f"bundled auto_router requires profiles {list(required)} to "
                f"exist, but missing: {missing}. "
                f"Either (a) define all three profiles in providers.yaml, or "
                f"(b) override with a custom ``auto_router:`` block, or "
                f"(c) set ``default_profile`` to a non-auto profile name."
            )
        return self

    @model_validator(mode="after")
    def _check_display_timezone_resolves(self) -> CodeRouterConfig:
        """v1.5-E: fail fast on a typo'd IANA zone name.

        Same philosophy as the other ``_check_*`` validators — a broken
        ``display_timezone`` would otherwise silently fall back to UTC
        (or worse, blow up the first dashboard poll with a stack trace).
        Checking at load time converts that into a startup error with the
        offending value in the message.
        """
        if self.display_timezone is None:
            return self
        # Imported locally to sidestep the slow ``zoneinfo`` cold-import
        # cost on machines that never set a display timezone.
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(self.display_timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(
                f"display_timezone={self.display_timezone!r} is not a known "
                f"IANA zone (try 'Asia/Tokyo', 'America/New_York', 'UTC'): {exc}"
            ) from exc
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
