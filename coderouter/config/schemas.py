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
import warnings
from typing import Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    field_validator,
    model_validator,
)

from coderouter.errors import ConfigValidationError
from coderouter.logging import get_logger
from coderouter.messages import tr
from coderouter.token_estimation import set_include_tool_content

logger = get_logger(__name__)


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
    # S2 (shim): Anthropic's ``tool_choice`` forcing modes (``{type: any}``
    # / ``{type: tool, name: ...}``). Only a subset of backends honor a
    # forced tool_choice on the wire; openai_compat translation drops it.
    # This narrow per-model flag mirrors ``thinking`` / ``prompt_cache``:
    # when unset (None), the capability gate falls back to the registry and
    # then to a ``kind == "anthropic"`` heuristic (see
    # ``coderouter/routing/capability.py``). Motivation: let the fallback
    # engine's ``tool_choice_action`` emulate forced tool calls via a
    # system-prompt directive on backends that would otherwise silently
    # ignore the field. Backward compatible — None leaves v2.x behavior
    # untouched (no gate, no emulation).
    tool_choice: bool | None = None
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


class AgentCliConfig(BaseModel):
    """External coding-agent CLI settings for ``kind="agent_cli"`` providers.

    Introduced by the external-agents-adapter design (Phase 1). As of the
    agent_cli plugin extraction's Phase 2c
    (``docs/designs/agent-cli-plugin-extraction.md`` §4.4 case (b), §7),
    the adapter that actually invokes the CLI (``AgentCliAdapter``,
    argv construction / output parsing / sandbox flag tables) has moved
    to the separately-distributed ``coderouter-plugin-agents`` plugin
    (``pip install coderouter-plugin-agents`` + ``plugins.enabled:
    [agents]``) — Core no longer ships it in-core. ``AgentCliConfig``
    itself REMAINS in Core: it is a stable schema contract that has not
    changed since Phase 1 (unlike the adapter body, which absorbs
    per-CLI churn), so keeping it here preserves ``extra="forbid"``
    fail-fast validation at config-load time regardless of whether the
    plugin is installed.

    One ``agent_cli`` sub-config drives the plugin's adapter, which
    invokes an external coding-agent CLI (codex / gemini / grok / claude /
    antigravity) in a single one-shot ``exec`` and returns the final answer
    as one ``prompt in → text out`` transformation. ``claude`` (Claude Code
    CLI, Phase 1a), ``codex`` (codex CLI, Phase 1b), ``grok`` (grok CLI,
    Phase 1d) and ``antigravity`` (Antigravity CLI, Phase 1c, in lieu of
    ``gemini``) are implemented. ``gemini`` is declared at the schema level
    for backward-compatible config parsing, but the adapter rejects it with
    a migration pointer: Google discontinued the (legacy) Gemini CLI's
    OAuth for individual accounts in June 2026 (``IneligibleTierError`` /
    ``UNSUPPORTED_CLIENT`` on the real client) and its successor is the
    Antigravity CLI (command ``agy``, a separate Go implementation, not a
    gemini-cli fork) — set ``agent: "antigravity"`` instead.

    Auth note (grok): the grok CLI uses OAuth credentials stored under
    ``~/.grok`` (``grok login``), which the adapter's HOME inheritance
    already covers — no extra config needed. For CI / API-key setups, list
    ``GROK_CODE_XAI_API_KEY`` in ``passthrough_env`` (this is grok's key
    env var — NOT ``XAI_API_KEY``).

    Auth note (codex): the codex CLI uses a ChatGPT-plan OAuth login stored
    under ``~/.codex`` (``codex login``), which the adapter's HOME
    inheritance already covers — the credentials go stale after roughly 8
    days and are auto-refreshed on use, so no extra config is needed for
    interactive/subscription setups. For CI / API-key setups, list
    ``CODEX_API_KEY`` (exec-only) or ``OPENAI_API_KEY`` (general) in
    ``passthrough_env``.

    Auth note (antigravity): the Antigravity CLI uses a Google-account OAuth
    login (free tier included), with credentials preferentially stored in
    the OS keyring and mirrored under ``~/.gemini/antigravity-cli/``
    (``credentials.enc`` / ``settings.json``) — the adapter's HOME (and, on
    macOS, USER) inheritance already covers this, no extra config needed.
    Any API-key environment variable for CI / non-interactive setups is
    UNCONFIRMED (field reports disagree on the variable name) — this
    docstring deliberately does not name one as authoritative; if you find
    one that works, list it in ``passthrough_env``.

    Follows the ``extra="forbid"`` convention used across this module so a
    typo'd key fails at config-load rather than being silently ignored.
    """

    model_config = ConfigDict(extra="forbid")

    agent: Literal["codex", "gemini", "grok", "claude", "antigravity"] = Field(
        ...,
        description=(
            "External coding-agent CLI to invoke. 'claude' (Phase 1a), "
            "'codex' (Phase 1b), 'grok' (Phase 1d) and 'antigravity' "
            "(Phase 1c, Google's Antigravity CLI, command 'agy') are "
            "implemented. 'gemini' is rejected by the adapter — Google "
            "discontinued the Gemini CLI for individual accounts in June "
            "2026; use 'antigravity' instead."
        ),
    )
    command: str | None = Field(
        default=None,
        description=(
            "CLI executable name or absolute path (resolved via PATH). "
            "When unset, defaults to the ``agent`` name — EXCEPT "
            "'antigravity', whose binary is named ``agy`` (the product is "
            "'Antigravity CLI' but the executable keeps the short, "
            "pre-rename command name)."
        ),
    )
    workdir: str | None = Field(
        default=None,
        description=(
            "Working directory for the one-shot exec. ``~`` / env-var "
            "expansion is applied. When unset, a dedicated isolated "
            "directory (``~/.coderouter-t/agents/<name>``) is used."
        ),
    )
    exec_timeout_s: float = Field(
        default=600.0,
        ge=1.0,
        le=1800.0,
        description=(
            "Forced timeout (seconds) for the one-shot exec. Independent "
            "of ``ProviderConfig.timeout_s`` — the CLI has no built-in "
            "wall clock so this is enforced with an asyncio watchdog + "
            "process-group SIGKILL."
        ),
    )
    allow_file_writes: bool = Field(
        default=False,
        description=(
            "Allow the agent to write to the filesystem. Default False "
            "(read-only). When False the sandbox mapping is clamped to "
            "read-only regardless of ``sandbox_mode`` (defense in depth)."
        ),
    )
    sandbox_mode: Literal["read_only", "edit", "full_auto"] = Field(
        default="read_only",
        description=(
            "Source mode mapped onto each CLI's sandbox / approval flags. "
            "Default read_only maps to claude ``--permission-mode plan``."
        ),
    )
    model: str | None = Field(
        default=None,
        description=(
            "Model name passed to the CLI's ``--model``. When unset, "
            "``ProviderConfig.model`` is used."
        ),
    )
    max_turns: int | None = Field(
        default=8,
        ge=1,
        le=50,
        description=(
            "Turn cap. Passed to the CLI's ``--max-turns`` where supported "
            "(codex ignores it — the CLI has no such flag)."
        ),
    )
    passthrough_env: list[str] = Field(
        default_factory=list,
        description=(
            "Allowlist of environment variable NAMES forwarded from the "
            "parent process into the child. The child otherwise inherits "
            "no parent environment (subscription-first auth policy). "
            "``ANTHROPIC_API_KEY`` is NOT forwarded unless listed here. "
            "For grok in CI, list ``GROK_CODE_XAI_API_KEY`` here; OAuth "
            "logins under ``~/.grok`` work without it (HOME is inherited)."
        ),
    )
    agent_depth_limit: int = Field(
        default=2,
        ge=1,
        le=4,
        description=(
            "Recursion nesting cap. ``CODEROUTER_AGENT_DEPTH`` is "
            "propagated (incremented) into the child; ``generate()`` "
            "refuses when the current depth is at or above this limit."
        ),
    )

    @model_validator(mode="after")
    def _resolve_and_check(self) -> AgentCliConfig:
        """Default ``command`` to ``agent`` and reject contradictory sandbox.

        Rule (design §5.2.1 #2): ``allow_file_writes=True`` together with
        ``sandbox_mode="read_only"`` is contradictory — the operator asked
        for writes while pinning a read-only sandbox. Fail fast at load,
        matching the module's other cross-field validators.

        ``command`` defaults to the ``agent`` name for every agent EXCEPT
        ``antigravity``, whose binary is ``agy`` — the product renamed from
        Gemini CLI to Antigravity CLI, but the executable kept its short
        pre-rename name.
        """
        if self.command is None:
            self.command = "agy" if self.agent == "antigravity" else self.agent
        if self.allow_file_writes and self.sandbox_mode == "read_only":
            raise ConfigValidationError(tr("E1409_SANDBOX_CONFLICT"), message_id="E1409_SANDBOX_CONFLICT")
        return self


# v2.13.0: patterns used by ProviderConfig's
# ``_warn_restart_command_shell_syntax`` validator to flag
# ``restart_command`` values that rely on shell syntax not honored by the
# ``shlex.split`` + ``shell=False`` dispatch (see that validator's
# docstring and the field description below for the full rationale).
# Public (no leading underscore) because ``coderouter.guards.self_healing``
# imports ``RESTART_COMMAND_SHELL_META_RE`` to *refuse* to run any
# restart_command containing shell metacharacters — under shell=False a
# value like ``touch a; b`` would create a file literally named ``a;`` and
# exit 0, i.e. report a "successful" restart while only half-executing.
# Compiled once at module scope since every ProviderConfig validation
# reuses them.
RESTART_COMMAND_SHELL_META_RE = re.compile(r"(&&|\|\||\||;|>|<|\$\(|`)")
RESTART_COMMAND_ENV_PREFIX_RE = re.compile(r"^\w+=")


class CredentialRefresh(BaseModel):
    """How to make a vendor CLI rotate the token it wrote to disk.

    Deliberately not an OAuth client. Every vendor has its own endpoint,
    client id, rotation policy and error shape, and all of them change —
    delegating to the CLI that owns the file is the version that survives
    contact with a moving vendor.

    ``command`` is an argv **list**, dispatched with ``shell=False``. Same
    trust decision as v2.13.0's ``restart_command``: a string that goes
    through a shell turns a config file into arbitrary code execution.
    There is no string form here to have to refuse.
    """

    model_config = ConfigDict(extra="forbid")

    command: list[str] = Field(
        ...,
        min_length=1,
        description=(
            "argv for the vendor CLI, e.g. ['grok', 'models']. Run with "
            "shell=False; no shell metacharacters are interpreted."
        ),
    )
    early_ratio: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description=(
            "Refresh when the remaining lifetime drops below this fraction "
            "of itself. Early refresh avoids a token dying mid-request."
        ),
    )
    min_lead_s: float = Field(
        default=300.0,
        ge=0.0,
        description="Always refresh at least this many seconds before expiry.",
    )
    timeout_s: float = Field(
        default=30.0,
        gt=0.0,
        le=600.0,
        description="Hard timeout for the refresh command.",
    )


class ProviderCredential(BaseModel):
    """Where this provider's credential comes from (v2.14.0).

    ``source: env`` is the historical behaviour, spelled out. ``source:
    cli_session`` borrows the token a vendor CLI already wrote to disk, so
    a subscription-authenticated provider can be an ordinary
    ``openai_compat`` entry instead of a ``kind: agent_cli`` island — and
    therefore takes part in fallback chains, auto-routing, the budget
    tracker and every other routing feature.
    """

    model_config = ConfigDict(extra="forbid")

    source: Literal["env", "cli_session"] = Field(
        default="env",
        description="'env' reads an environment variable; 'cli_session' reads a JSON file.",
    )
    env: str | None = Field(
        default=None, description="source=env: the environment variable name."
    )
    path: str | None = Field(
        default=None,
        description=(
            "source=cli_session: the JSON file the vendor CLI writes, e.g. "
            "~/.kimi-code/credentials/kimi-code.json. Must live under $HOME."
        ),
    )
    field: str = Field(
        default="access_token",
        description="Dotted path to the token inside that JSON (e.g. 'tokens.access').",
    )
    expiry_field: str = Field(
        default="expires_at",
        description=(
            "Dotted path to the expiry. Epoch seconds or milliseconds; both "
            "are accepted. Absent means 'no expiry info' — the upstream 401 "
            "becomes the signal instead."
        ),
    )
    refresh: CredentialRefresh | None = Field(
        default=None,
        description="Omit to never refresh (a long-lived token, or one you rotate yourself).",
    )

    @model_validator(mode="after")
    def _check_source_requirements(self) -> Self:
        """Each source needs its own field, and only its own field."""
        if self.source == "env":
            if not self.env:
                raise ConfigValidationError(tr("E1402_CREDENTIAL_ENV_REQUIRED"), message_id="E1402_CREDENTIAL_ENV_REQUIRED")
            if self.path:
                raise ConfigValidationError(tr("E1403_CREDENTIAL_PATH_MEANINGLESS"), message_id="E1403_CREDENTIAL_PATH_MEANINGLESS")
        if self.source == "cli_session":
            if not self.path:
                raise ConfigValidationError(tr("E1404_CREDENTIAL_PATH_REQUIRED"), message_id="E1404_CREDENTIAL_PATH_REQUIRED")
            if self.env:
                raise ConfigValidationError(tr("E1405_CREDENTIAL_ENV_MEANINGLESS"), message_id="E1405_CREDENTIAL_ENV_MEANINGLESS")
            from coderouter.credentials import session_path_is_sane

            if not session_path_is_sane(self.path):
                raise ConfigValidationError(tr("E1401_CREDENTIAL_PATH_HOME", path=self.path), message_id="E1401_CREDENTIAL_PATH_HOME")
        return self


class ProviderConfig(BaseModel):
    """A single provider entry from providers.yaml.

    Examples:
        - Local llama.cpp server: kind=openai_compat, base_url=http://localhost:8080/v1
        - OpenRouter free: kind=openai_compat, base_url=https://openrouter.ai/api/v1
        - (future) Anthropic: kind=anthropic, base_url=https://api.anthropic.com
        - External agent CLI: kind=agent_cli, agent_cli={agent: claude, ...}
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., description="Unique identifier used in profiles.yaml")
    # v2.8.0: widened from Literal["openai_compat", "anthropic", "agent_cli"]
    # to str so a provider can name a kind served by an adapter plugin
    # (docs/designs/agent-cli-plugin-extraction.md §3). Config loading
    # happens before plugin discovery (coderouter/ingress/app.py
    # ``load_config`` then ``discover_and_load``), so pydantic can't know
    # the full set of valid kinds at this point — an unknown kind still
    # fails fast, just one step later, in ``build_adapter`` when the
    # engine builds its adapter cache at startup.
    kind: str = Field(
        default="openai_compat",
        description=(
            "Adapter type. 'openai_compat' covers llama.cpp / Ollama / "
            "OpenRouter / LM Studio / Together / Groq. 'anthropic' is the "
            "native Anthropic Messages API passthrough (v0.3.x). These two "
            "are Core's only in-core kinds. 'agent_cli' invokes an external "
            "coding-agent CLI one-shot (see AgentCliConfig) but, as of "
            "Phase 2c, is served by the coderouter-plugin-agents adapter "
            "plugin — install it and list 'agents' in plugins.enabled. "
            "Any other value must likewise be served by an adapter plugin "
            "listed in plugins.enabled."
        ),
    )
    # base_url is required for HTTP-backed adapters (openai_compat / anthropic)
    # but meaningless for agent_cli (which shells out to a local CLI rather
    # than calling a URL). It is optional at the field level and enforced per
    # kind by ``_check_kind_requirements`` below.
    base_url: HttpUrl | None = None
    model: str = Field(..., description="Upstream model id sent in the request body")
    api_key_env: str | None = Field(
        default=None,
        description="Env var name holding the API key. None = no auth (e.g. local).",
    )
    # v2.14.0: the general form of the above. Mutually exclusive with
    # api_key_env — a resolver that silently fell back between sources would
    # make "which credential is this request actually using?" unanswerable,
    # which is the question an operator asks at exactly the wrong moment.
    credential: ProviderCredential | None = Field(
        default=None,
        description=(
            "Where the credential comes from. Omit to use api_key_env. "
            "source='cli_session' reads the token a vendor CLI already wrote "
            "to disk, so a subscription provider can be a plain "
            "openai_compat entry that takes part in fallback chains."
        ),
    )

    # Routing-relevant flags
    paid: bool = Field(
        default=False,
        description="If true, only used when ALLOW_PAID=true (plan.md §2.3).",
    )
    timeout_s: float = Field(default=30.0, ge=1.0, le=86400.0)

    # v2.6 language-tax track: path to a LOCAL ``tokenizer.json`` for this
    # provider's model, used to measure the CJK over-count vs the char/4
    # baseline (see ``coderouter.language_tax``). Loaded local-file-only —
    # never contacts the HuggingFace Hub. When unset, language-tax falls
    # back to char/4 (multiplier 1.0) and the feature is silently inert.
    tokenizer_path: str | None = Field(
        default=None,
        description=(
            "Local tokenizer.json for accurate (language-tax) token "
            "counting. No network access. Requires the 'accuracy' extra."
        ),
    )

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

    # kind="agent_cli": required external coding-agent CLI settings. Opt-in
    # (default None) exactly like ``restart_command`` — only meaningful when
    # ``kind == "agent_cli"``, and enforced by ``_check_kind_requirements``.
    # The schema (this field + AgentCliConfig + the kind literal) stays in
    # Core; the adapter that consumes it is the coderouter-plugin-agents
    # plugin as of Phase 2c (docs/designs/agent-cli-plugin-extraction.md
    # §4.4 case (b)).
    agent_cli: AgentCliConfig | None = Field(
        default=None,
        description=(
            "Required when kind='agent_cli': external agent CLI settings. "
            "Served by the coderouter-plugin-agents adapter plugin — "
            "`pip install coderouter-plugin-agents` and add 'agents' to "
            "plugins.enabled."
        ),
    )

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
    max_context_tokens: int | None = Field(
        default=None,
        ge=1,
        description=(
            "v2.0-F (L1): explicit declaration of this provider's "
            "context window size in tokens. When set, takes precedence "
            "over the ``model-capabilities.yaml`` registry lookup. "
            "When both are unset, the context budget guard falls back "
            "to 128000 (128K). Examples: Ollama Qwen3 32K → 32768, "
            "LM Studio Qwen3.5 128K → 131072, Anthropic Claude → 200000."
        ),
    )
    # v2.0-J: optional shell command to restart this provider's backend
    # process when it becomes UNHEALTHY. Executed via subprocess when
    # self-healing is enabled and the provider crosses the UNHEALTHY
    # threshold. Security: opt-in only — unset means no restart attempt.
    restart_command: str | None = Field(
        default=None,
        description=(
            "v2.0-J (Self-healing): command to restart this provider's "
            "backend process. Examples: 'ollama serve', "
            "'open -a LM\\ Studio'. Only executed when the profile's "
            "backend_health_action is 'exclude' and the provider "
            "transitions to UNHEALTHY. Unset = no automatic restart "
            "(recovery probe still runs, waiting for manual restart). "
            "\n\n"
            "v2.13.0: run as ``subprocess.run(shlex.split(command), "
            "shell=False)`` (coderouter/guards/self_healing.py) — argv "
            "dispatch, no shell in between. Pipelines "
            "(``pkill ollama && ollama serve``), redirects, ``~`` "
            "expansion, and leading env-var assignments "
            "(``OLLAMA_HOST=0.0.0.0 ollama serve``) are NOT supported: "
            "``~/...`` paths are not expanded and ``FOO=bar cmd`` fails to "
            "exec. A value containing shell metacharacters (``&&``, ``||``, "
            "``|``, ``;``, ``>``, ``<``, ``$(...)``, ```...```) is refused "
            "outright (not run) rather than half-executed. If your command "
            "needs shell features, wrap it in a small script and point "
            "restart_command at that script (e.g. "
            "``restart_command: /path/to/restart-ollama.sh``), or write it "
            "as ``/bin/sh -c '...'`` explicitly."
        ),
    )

    @model_validator(mode="after")
    def _warn_restart_command_shell_syntax(self) -> ProviderConfig:
        """v2.13.0: warn that this ``restart_command`` will not run as written.

        ``coderouter/guards/self_healing.py`` runs ``restart_command`` via
        ``subprocess.run(shlex.split(command), shell=False)`` — argv
        dispatch, no shell. A shell-syntax ``restart_command`` therefore
        does NOT do what the shell would do:

          * shell metacharacters (``&&`` / ``||`` / ``|`` / ``;`` /
            redirects / ``$(...)`` / backticks) → the value is *refused*
            (not run) by self_healing, because under shell=False they would
            otherwise become literal argv tokens and half-execute a
            provider "restart" that quietly does the wrong thing;
          * a leading ``~/...`` path or an ``ENV=val cmd`` prefix →
            ``exec`` fails (``FileNotFoundError``) because there is no shell
            to expand ``~`` or apply the assignment.

        Deliberately never raises: this validator does not enforce
        anything, it only warns so an existing providers.yaml still loads
        (rather than becoming un-startable). Migrate ``restart_command`` to
        a plain argv, a wrapper script, or an explicit ``/bin/sh -c '...'``.
        """
        cmd = self.restart_command
        if not cmd:
            return self
        stripped = cmd.strip()
        reasons: list[str] = []
        if RESTART_COMMAND_SHELL_META_RE.search(cmd):
            reasons.append("shell metacharacter (&&, ||, |, ;, >, <, $(...), or `...`)")
        if stripped.startswith("~/"):
            reasons.append("leading '~/' path (no shell expansion under shell=False)")
        if RESTART_COMMAND_ENV_PREFIX_RE.match(stripped):
            reasons.append("leading environment-variable assignment (FOO=bar ...)")
        if reasons:
            logger.warning(
                "restart-command-shell-syntax",
                extra={
                    "provider": self.name,
                    "restart_command": cmd,
                    "reasons": reasons,
                    "hint": (
                        "restart_command runs via "
                        "subprocess.run(shlex.split(...), shell=False) as of "
                        "v2.13.0, which does not support this command's "
                        "shell syntax: values with shell metacharacters are "
                        "refused (not run), and '~/' / 'FOO=bar' prefixes "
                        "fail to exec. Rewrite it as plain argv, wrap it in "
                        "a script, or use /bin/sh -c '...'."
                    ),
                },
            )
        return self

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

    @model_validator(mode="after")
    def _check_credential_exclusive(self) -> Self:
        """``api_key_env`` and ``credential`` cannot both be set.

        Failing at load time is the whole point: two credential sources on
        one provider is a question ("which one won?") that should never
        reach a request, let alone a log line an operator has to reverse
        engineer at 2am.
        """
        if self.credential is not None and self.api_key_env:
            raise ConfigValidationError(tr("E1406_API_KEY_EXCLUSIVE", name=self.name), message_id="E1406_API_KEY_EXCLUSIVE")
        return self

    @model_validator(mode="after")
    def _check_kind_requirements(self) -> ProviderConfig:
        """Enforce per-``kind`` field requirements (external-agents design §5.2.2).

        - HTTP-backed adapters (``openai_compat`` / ``anthropic``) require a
          ``base_url`` — they have nowhere to send the request otherwise.
          ``base_url`` was relaxed to Optional so ``agent_cli`` providers can
          omit it; this validator restores the required-ness for the HTTP kinds.
        - ``agent_cli`` requires the ``agent_cli`` sub-config (the adapter has
          no CLI to invoke without it).

        Same fast-fail philosophy as ``_check_output_filters_known`` — a
        misconfigured provider surfaces at config-load, not at first request.
        """
        if self.kind in ("openai_compat", "anthropic") and self.base_url is None:
            raise ConfigValidationError(tr("E1407_BASE_URL_REQUIRED", name=self.name, kind=self.kind), message_id="E1407_BASE_URL_REQUIRED")
        if self.kind == "agent_cli" and self.agent_cli is None:
            raise ConfigValidationError(tr("E1408_AGENT_CLI_REQUIRED", name=self.name), message_id="E1408_AGENT_CLI_REQUIRED")
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
        description=(
            "Provider names in fallback order. First success wins. "
            "(The launcher-swap placeholder profiles are the sole "
            "empty-chain exception; they bypass this validator via "
            "``model_construct`` in "
            "``CodeRouterConfig._inject_swap_profiles_and_auto_router_rules`` "
            "— user-declared chains still fail fast at load when empty.)"
        ),
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
    # v2.2: total tool-call count hard cap. A safety valve against
    # runaway agents that call many *different* tools without looping
    # (which the streak-based L3 detector misses). Set to 0 to
    # disable the cap entirely.
    max_tool_calls: int = Field(
        default=50,
        ge=0,
        le=1000,
        description=(
            "v2.2: maximum total tool_use blocks allowed in the "
            "conversation. When exceeded, the request is rejected with "
            "a ``tool_count_exceeded`` error (if tool_loop_action is "
            "``break``) or logged (if ``warn``). Set to 0 to disable. "
            "Default 50 is deliberately more permissive than Unsloth "
            "Studio's 25 — Claude Code agent sessions routinely reach "
            "25+ calls in normal operation."
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
    backend_health_action: Literal["off", "warn", "demote", "exclude", "skip"] = Field(
        default="warn",
        description=(
            "v1.9-E (L5 phase 2): action when a provider transitions "
            "to UNHEALTHY (consecutive failures crossed the threshold). "
            "``warn`` (default) emits a state-change log line only — "
            "diagnostic, no chain reorder. ``demote`` additionally "
            "moves the UNHEALTHY provider to the back of the chain "
            "for the next ``_resolve_chain`` (similar to v1.9-C "
            "adaptive demotion but state-machine-based, not "
            "rolling-window-based). ``exclude`` (v2.0-J) removes the "
            "UNHEALTHY provider from the chain entirely + triggers "
            "self-healing (restart helper if configured, recovery "
            "probe with exponential backoff). On recovery, the "
            "provider is automatically restored to its original "
            "chain position. ``skip`` (v2.x) filters UNHEALTHY providers "
            "out of the chain like ``exclude`` but with a self-contained "
            "half-open circuit breaker — no self-healing orchestrator "
            "required: every ``backend_health_half_open_s`` window one "
            "trial request is let through, and a single success snaps the "
            "provider back into rotation. If skipping would empty the "
            "chain, the unfiltered chain is used as a last resort so a "
            "uniformly-UNHEALTHY chain still attempts every provider "
            "rather than 502-ing outright. ``off`` disables the monitor "
            "entirely (zero observation overhead, identical to "
            "v1.9.x behavior)."
        ),
    )
    backend_health_half_open_s: float = Field(
        default=30.0,
        ge=5.0,
        le=600.0,
        description=(
            "v2.x: half-open interval (seconds) for the ``skip`` "
            "backend-health action. While a provider is UNHEALTHY, at most "
            "one trial request is let through per interval — the built-in "
            "circuit breaker's half-open probe. A successful trial resets "
            "the provider to HEALTHY immediately; a failed trial keeps it "
            "skipped until the next interval elapses. Ignored for every "
            "other ``backend_health_action``. Default 30 s balances quick "
            "recovery against hammering a still-down backend; capped at "
            "600 s (10 min)."
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
    # v2.0-J: self-healing recovery probe configuration.
    recovery_probe_initial_s: float = Field(
        default=30.0,
        ge=5.0,
        le=600.0,
        description=(
            "v2.0-J: initial interval (seconds) for recovery probes "
            "sent to an UNHEALTHY-excluded provider. Each failed probe "
            "doubles the interval up to ``recovery_probe_max_s``. "
            "A successful probe restores the provider to its original "
            "chain position immediately."
        ),
    )
    recovery_probe_max_s: float = Field(
        default=300.0,
        ge=30.0,
        le=3600.0,
        description=(
            "v2.0-J: maximum interval (seconds) for recovery probe "
            "exponential backoff. Default 300 s (5 min) means a dead "
            "backend is probed at most every 5 minutes indefinitely "
            "until it recovers or the server shuts down."
        ),
    )
    restart_timeout_s: float = Field(
        default=30.0,
        ge=5.0,
        le=120.0,
        description=(
            "v2.0-J: timeout (seconds) for the restart_command "
            "subprocess. If the command doesn't complete within this "
            "window, it is killed. Prevents hung restart commands from "
            "blocking recovery."
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
    # v2.0-F (L1): context budget guard.
    #
    # Long-running agent sessions accumulate messages that eventually
    # exceed the target model's context window. Without intervention,
    # the backend returns a 400 (Anthropic) or silently truncates
    # (Ollama), killing the agent session. The context budget guard
    # estimates the request's token count (char/4 heuristic, shared
    # with the auto_router longContext matcher) and compares it against
    # the target provider's declared max_context_tokens.
    #
    # Three actions:
    #   * ``off``  — no detection, no logging. Backward-compat default.
    #   * ``warn`` — emit ``context-budget-warning`` log + attach
    #                ``X-CodeRouter-Context-Budget: warning`` response
    #                header. No request mutation.
    #   * ``trim`` — ``warn`` + remove oldest non-system messages until
    #                the estimated token count drops below
    #                ``context_budget_trim_target``. Recent messages
    #                (``context_budget_preserve_last_n``) are always
    #                kept, and tool_use / tool_result pairs are preserved
    #                atomically to avoid breaking agent loops.
    context_budget_action: Literal["off", "warn", "trim"] = Field(
        default="off",
        description=(
            "v2.0-F (L1): action when estimated request tokens approach "
            "the target provider's context window. ``off`` (default) "
            "disables the guard entirely. ``warn`` emits a log and "
            "response header at ``context_budget_warn_threshold``. "
            "``trim`` additionally removes old messages at "
            "``context_budget_trim_threshold`` to reclaim context space."
        ),
    )
    context_budget_warn_threshold: float = Field(
        default=0.80,
        ge=0.1,
        le=1.0,
        description=(
            "v2.0-F (L1): context usage ratio (estimated_tokens / "
            "max_context_tokens) at which a warning is emitted. "
            "Default 0.80 (80%) gives early notice before trim fires."
        ),
    )
    context_budget_trim_threshold: float = Field(
        default=0.90,
        ge=0.1,
        le=1.0,
        description=(
            "v2.0-F (L1): context usage ratio at which trim fires "
            "(only when ``context_budget_action`` is ``trim``). "
            "Default 0.90 (90%) leaves a 10% margin for the backend's "
            "own token counting to differ from the char/4 estimate."
        ),
    )
    context_budget_trim_target: float = Field(
        default=0.75,
        ge=0.1,
        le=1.0,
        description=(
            "v2.0-F (L1): target context usage ratio after trim. "
            "Messages are removed from the front until the estimate "
            "drops below this ratio. Default 0.75 (75%) gives headroom "
            "for several more turns before trim fires again."
        ),
    )
    context_budget_preserve_last_n: int = Field(
        default=4,
        ge=1,
        le=100,
        description=(
            "v2.0-F (L1): minimum number of recent messages to always "
            "preserve when trimming. Default 4 (2 user-assistant pairs) "
            "keeps the agent's immediate working context intact."
        ),
    )

    # ------------------------------------------------------------------
    # v2.0-G (L4): Drift detection — response quality degradation guard
    # ------------------------------------------------------------------
    #
    # Long-running sessions on local LLMs can suffer gradual quality
    # decay (KV cache pressure, thermal throttling, VRAM fragmentation)
    # where the model "succeeds" but produces empty/short/toolless
    # responses. This guard observes response quality signals in a
    # rolling window and detects statistical drift.
    #
    # Four actions:
    #   * ``off``     — no detection (default).
    #   * ``warn``    — emit structured log + response header.
    #   * ``promote`` — ``warn`` + demote drifted provider in chain.
    #   * ``reload``  — ``promote`` + attempt KV cache flush (Ollama).
    drift_detection_action: Literal["off", "warn", "promote", "reload"] = Field(
        default="off",
        description=(
            "v2.0-G (L4): action on response quality drift detection. "
            "``off`` (default) disables drift detection. ``warn`` emits "
            "a log and response header. ``promote`` additionally demotes "
            "the drifted provider in the chain. ``reload`` attempts to "
            "flush the provider's KV cache (Ollama only) before promoting."
        ),
    )
    drift_detection_window_size: int = Field(
        default=20,
        ge=4,
        le=200,
        description=(
            "v2.0-G (L4): number of recent responses to keep in the "
            "rolling observation window per provider. Larger windows "
            "are more robust to noise but slower to detect drift."
        ),
    )
    drift_detection_cooldown_s: int = Field(
        default=300,
        ge=10,
        le=3600,
        description=(
            "v2.0-G (L4): seconds after a promote/reload action before "
            "the drifted provider's rank is reset for recovery check. "
            "Default 300s (5 min) gives the model time to stabilize."
        ),
    )
    drift_detection_sensitivity: Literal["low", "normal", "high"] = Field(
        default="normal",
        description=(
            "v2.0-G (L4): threshold preset for drift signals. "
            "``low`` tolerates more degradation before triggering, "
            "``high`` is stricter (fewer bad responses needed)."
        ),
    )

    # --- P1-5: goal_mode — tighter drift thresholds for /goal sessions -------
    #
    # When True, the drift detector automatically switches to the
    # ``THRESHOLDS_GOAL`` preset regardless of ``drift_detection_sensitivity``,
    # and lowers ``min_window_fill`` to 4 so stall detection fires faster.
    #
    # Intended for profiles routed by the ``/goal`` meta-command where
    # the agent is expected to make steady forward progress. Repetition and
    # length collapse are much more meaningful signals in that context than
    # in a general-purpose chat session.
    goal_mode: bool = Field(
        default=False,
        description=(
            "P1-5: when True, automatically applies the ``goal`` drift "
            "threshold preset (stricter thresholds, lower ``min_window_fill`` "
            "of 4) for this profile. Overrides ``drift_detection_sensitivity`` "
            "when drift_detection_action is not ``off``. Designed for "
            "agent/goal sessions where forward-progress stalls are more "
            "actionable than in ad-hoc chat."
        ),
    )

    # --- v2.0-H (L6): Mid-stream partial stitching --------------------------
    #   * ``off``      — discard partial content on mid-stream failure (legacy).
    #   * ``surface``  — return partial content as a truncated-but-valid response.
    partial_stitch_action: Literal["off", "surface"] = Field(
        default="off",
        description=(
            "v2.0-H (L6): action when a streaming response fails mid-stream. "
            "``off`` discards partial content (legacy error event). "
            "``surface`` returns accumulated text as a graceful stream "
            "termination with a ``coderouter_partial`` metadata event."
        ),
    )

    # --- v2.15.0 (stream-truncation): upstream SSE cut without a terminator --
    #
    # A streaming upstream can end its HTTP body *cleanly* while the LLM
    # protocol carried inside it is still mid-message: no ``message_stop`` on
    # the Anthropic wire, no ``data: [DONE]`` and no ``finish_reason`` on the
    # OpenAI wire. llama.cpp slot preemption, an ``--n-predict`` cut-off, a
    # front proxy closing an EOF-delimited body, or an OOM'd local server all
    # produce exactly this shape. Transport-level breakage (timeout,
    # ``httpx.RemoteProtocolError``) was already caught; this is the layer the
    # transport cannot see.
    #
    # Before v2.15.0 that stream was indistinguishable from a complete one:
    # the adapters never recorded whether a terminator arrived, and the
    # translation layer's terminator-synthesis guards (H6 / M9 in
    # ``coderouter.translation.convert``) then fabricated a ``stop_reason:
    # end_turn`` / ``finish_reason: "stop"`` so the client would not hang. The
    # synthesis is correct and stays; what was missing is telling the *engine*
    # that it happened.
    #
    #   * ``off``   — no detection, no log, no metric. Backward-compatible
    #                 default: byte-for-byte identical to v2.14.0 on every path.
    #   * ``warn``  — emit a ``stream-truncation-detected`` log line (and the
    #                 ``stream_truncated_total`` metric); the stream still
    #                 terminates through the legacy synthesis path, so the
    #                 client sees exactly what it saw before.
    #   * ``error`` — the adapter raises ``StreamTruncatedError`` (a retryable
    #                 ``AdapterError``) at the point the terminator should have
    #                 arrived. The engine's existing branches take over: no
    #                 bytes forwarded yet → fall back to the next provider with
    #                 reason ``stream-truncated``; bytes already forwarded →
    #                 ``MidStreamError``, which ``partial_stitch_action:
    #                 surface`` renders as a graceful close carrying a
    #                 ``coderouter_partial`` event with reason
    #                 ``stream_truncated``.
    #
    # Caveats for ``error``:
    #   * False positives are possible against an upstream that legitimately
    #     omits the terminator. A ``message_delta`` carrying a ``stop_reason``
    #     (Anthropic) and a ``finish_reason`` on any choice (OpenAI) are both
    #     accepted as terminators to keep that risk low, but running ``warn``
    #     first to measure the real rate is the recommended rollout.
    #   * Falling back re-generates the answer on the next provider, so the
    #     tokens the truncated attempt burned are paid twice.
    #   * On the Anthropic streaming path the pre-content fallback requires
    #     ``empty_response_action: fallback`` as well — that is the knob that
    #     withholds the opening events from the client. Without it the opening
    #     ``message_start`` has already shipped, so a truncation is by
    #     definition mid-stream.
    stream_truncation_action: Literal["off", "warn", "error"] = Field(
        default="off",
        description=(
            "v2.15.0: action when an upstream SSE stream ends without its "
            "protocol terminator (no ``message_stop`` / no ``[DONE]`` and no "
            "``finish_reason``). ``off`` (default) does nothing — identical to "
            "v2.14.0. ``warn`` emits a ``stream-truncation-detected`` log and "
            "metric while the stream still ends through the legacy terminator "
            "synthesis. ``error`` raises a retryable ``StreamTruncatedError`` "
            "so the engine falls back to the next provider (reason "
            "``stream-truncated``) when nothing has reached the client yet, or "
            "raises ``MidStreamError`` when it has. Pair with "
            "``empty_response_action: fallback`` for pre-content fallback on "
            "the Anthropic streaming path."
        ),
    )

    # --- ⑧ (empty-response): per-request empty-response fallback ------------
    #
    # Some local backends (observed: gemma4:26b on ``no_tool_temptation``
    # prompts) return a 200 with a structurally-valid but *content-empty*
    # Anthropic response — no tool_use and no non-whitespace text — for a
    # fraction of requests. The drift guard's ``empty_response_rate`` is a
    # windowed aggregate (default threshold 0.3, ``min_window_fill`` 6): it
    # promotes/reloads a backend once the *rate* is bad, but cannot rescue a
    # single blank turn in-flight. This knob adds a per-request in-flight
    # fallback that re-dispatches the *same* request to the next provider in
    # the chain the moment an empty response is detected.
    #
    # Design: "empty" is judged on *content*, not usage.output_tokens (which
    # some backends report unreliably). A response is empty when its content
    # list is empty, or every block is either a whitespace-only ``text`` block
    # or a ``thinking`` block — i.e. nothing the client can act on. A single
    # ``tool_use`` block or one non-whitespace ``text`` block makes it non-empty.
    #
    #   * ``off``      — no detection, no fallback, no log. Backward-compatible
    #                    default (byte-for-byte identical to pre-⑧ behavior).
    #   * ``warn``     — detect + emit an ``empty-response-detected`` log line
    #                    only; the empty response is returned unchanged.
    #   * ``fallback`` — on an empty response, log ``empty-response-detected``
    #                    and continue to the next provider (the empty response
    #                    is *not* recorded as an error). If every provider in
    #                    the chain returns empty, the last empty response is
    #                    returned as-is (a 200 blank is a legitimate answer)
    #                    with ``chain_exhausted=True`` on the log line. On the
    #                    streaming path, ``fallback`` buffers events until real
    #                    content is observed, so an empty stream can be swapped
    #                    to the next provider without the client seeing bytes.
    #
    # Default ``off`` preserves complete backward compatibility; the original
    # request object is never mutated, so a later provider always receives the
    # untouched request.
    empty_response_action: Literal["off", "warn", "fallback"] = Field(
        default="off",
        description=(
            "⑧ (empty-response): action when a provider returns a 200 with "
            "empty content (no tool_use and no non-whitespace text; a "
            "thinking-only response counts as empty). ``off`` (default) does "
            "nothing. ``warn`` emits an ``empty-response-detected`` log only "
            "and returns the empty response. ``fallback`` re-dispatches the "
            "same request to the next provider (empty is not counted as an "
            "error); if the whole chain returns empty, the last empty response "
            "is returned unchanged. Streaming buffers events until real "
            "content appears so empty streams can be swapped provider-side "
            "without the client seeing any bytes."
        ),
    )

    # --- S2 (shim): tool_choice capability gate + emulation -----------------
    #
    # Anthropic clients (Claude Code, SDKs) can pin the model to a specific
    # tool with ``tool_choice: {type: "tool", name: "X"}`` or force *some*
    # tool with ``{type: "any"}``. Native Anthropic honors this; most
    # openai_compat backends silently ignore it after translation, so the
    # model may answer with plain text where the client expected a tool
    # call. This knob decides what the fallback engine does when a forced
    # tool_choice request is routed to a provider that does not support it
    # (per ``provider_supports_tool_choice``):
    #
    #   * ``off``     — no detection / no mutation / no log. Backward-compat
    #                   default (identical to pre-shim behavior).
    #   * ``warn``    — emit a ``capability-degraded`` log line only; the
    #                   request is sent unchanged.
    #   * ``emulate`` — strip the ``tool_choice`` field and inject an English
    #                   directive into the system prompt instructing the
    #                   model to call the requested tool. Best-effort
    #                   forcing for backends without native support. The
    #                   original request object is left untouched so a later
    #                   capable provider in the chain still receives the real
    #                   ``tool_choice``.
    tool_choice_action: Literal["off", "warn", "emulate"] = Field(
        default="off",
        description=(
            "S2 (shim): action when a request carries a forced "
            "``tool_choice`` (``{type: any}`` or ``{type: tool}``) and the "
            "target provider does not support it. ``off`` (default) leaves "
            "the request unchanged. ``warn`` emits a ``capability-degraded`` "
            "log only. ``emulate`` strips ``tool_choice`` and injects a "
            "system-prompt directive to coax the model into calling the "
            "tool. Per-provider — capable providers are never mutated."
        ),
    )

    # --- S3 (shim): cache_control strip -------------------------------------
    #
    # By default cache_control markers are simply lost during Anthropic →
    # OpenAI translation and the ``capability-degraded`` gate logs it
    # (v0.5-B, observability only). Some strict openai_compat backends,
    # however, 400 when an unexpected ``cache_control`` key rides along on a
    # content block (rather than silently ignoring it). ``strip`` proactively
    # removes those keys from a deep copy of the request before dispatch to
    # a non-supporting provider, so the marker never reaches the wire.
    #
    #   * ``off``   — legacy behavior: leave the request as-is and rely on
    #                 the existing ``capability-degraded`` observability log.
    #   * ``strip`` — deep-copy the request and remove every ``cache_control``
    #                 key from system / tools / message blocks before sending
    #                 to a non-supporting provider; emit a
    #                 ``cache-control-stripped`` log with the marker count.
    #                 Does NOT emit a tokens-saved event (this is not token
    #                 savings, just a wire-compatibility strip).
    cache_control_action: Literal["off", "strip"] = Field(
        default="off",
        description=(
            "S3 (shim): action when a request carries ``cache_control`` "
            "markers and the target provider does not support them. ``off`` "
            "(default) keeps legacy behavior (marker dropped in translation, "
            "``capability-degraded`` log only). ``strip`` removes the "
            "``cache_control`` keys from a deep copy before dispatch and "
            "emits a ``cache-control-stripped`` log. Per-provider — capable "
            "providers are never mutated."
        ),
    )

    @model_validator(mode="after")
    def _check_context_budget_thresholds_ordered(self) -> FallbackChain:
        """v2.0-F (mE): cross-field sanity of the context-budget ratios.

        The three ratios describe a strict staircase: a warning fires
        first, then trimming kicks in at a higher usage, and trimming
        reclaims space down to a target below the trim point. If they are
        mis-ordered the guard is either a dead knob (warn above trim never
        fires) or an infinite trim loop (target >= trim never converges).
        Same fast-fail philosophy as ``_check_default_profile_exists`` —
        surface the mistake at load with a concrete pointer to the fix
        rather than at the first request that trips the guard.

        Required invariants:
          * ``context_budget_warn_threshold`` <= ``context_budget_trim_threshold``
          * ``context_budget_trim_target``    <  ``context_budget_trim_threshold``
        """
        if self.context_budget_warn_threshold > self.context_budget_trim_threshold:
            raise ValueError(
                f"profile {self.name!r}: context_budget_warn_threshold "
                f"({self.context_budget_warn_threshold}) must be <= "
                f"context_budget_trim_threshold "
                f"({self.context_budget_trim_threshold}) — the warning has to "
                f"fire at or before trimming, otherwise it can never fire. "
                f"Lower warn_threshold or raise trim_threshold."
            )
        if self.context_budget_trim_target >= self.context_budget_trim_threshold:
            raise ValueError(
                f"profile {self.name!r}: context_budget_trim_target "
                f"({self.context_budget_trim_target}) must be < "
                f"context_budget_trim_threshold "
                f"({self.context_budget_trim_threshold}) — trimming must "
                f"reclaim space *below* the trigger point, otherwise trim "
                f"never converges. Lower trim_target below trim_threshold."
            )
        return self

    @model_validator(mode="after")
    def _check_recovery_probe_interval_ordered(self) -> FallbackChain:
        """v2.0-J (mE): the recovery-probe backoff floor must not exceed its cap.

        ``recovery_probe_initial_s`` is the first probe interval and each
        failed probe doubles it up to ``recovery_probe_max_s``. When the
        initial interval already exceeds the max, the exponential-backoff
        ceiling is below its own floor — a nonsensical configuration that
        the backoff loop would silently clamp. Surface it at load instead.
        """
        if self.recovery_probe_initial_s > self.recovery_probe_max_s:
            raise ValueError(
                f"profile {self.name!r}: recovery_probe_initial_s "
                f"({self.recovery_probe_initial_s}) must be <= "
                f"recovery_probe_max_s ({self.recovery_probe_max_s}) — the "
                f"initial probe interval cannot exceed the backoff ceiling. "
                f"Lower recovery_probe_initial_s or raise recovery_probe_max_s."
            )
        return self


# ---------------------------------------------------------------------------
# v1.6-A: auto_router — declarative request-body classifier
# ---------------------------------------------------------------------------


class RuleMatcher(BaseModel):
    """One-of matcher for an :class:`AutoRouteRule`.

    Exactly one of the matcher fields must be set; the ``_exactly_one``
    validator enforces this at load. Adding a new matcher type means
    adding a new optional field — the single-field invariant enforces
    discriminated-union semantics without pydantic's tagged-union syntax.

    Boolean matchers (``has_image`` / ``has_tools``) only carry meaning at
    ``True``: the runtime evaluator matches with ``is True`` (see
    ``coderouter.routing.auto_router._match_rule``), so a ``False`` value
    would construct without error yet never match anything — a dead rule
    that silently shadows nothing and confuses operators. ``_exactly_one``
    therefore rejects ``False`` for these fields at load (``None`` remains
    the "unset" sentinel).

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

    Variants (v2.6 / language-tax routing):

    - ``cjk_ratio_min: 0.3`` — CJK character ratio of the latest user
      message is ``>=`` this threshold. Routes CJK-heavy turns (which
      pay the cloud "language tax" of ~1.2-1.5x more tokens) to a local
      model that bills nothing per token, while ASCII/code turns fall
      through to the cloud chain. Per-turn property like
      ``code_fence_ratio_min``; see
      :func:`coderouter.language_tax.cjk_char_ratio`.
    """

    model_config = ConfigDict(extra="forbid")

    has_image: bool | None = None
    code_fence_ratio_min: float | None = Field(default=None, ge=0.0, le=1.0)
    content_contains: str | None = None
    content_regex: str | None = None
    model_pattern: str | None = None
    content_token_count_min: int | None = Field(default=None, ge=1)
    # v2.6 language-tax routing: CJK character ratio of the latest user
    # message >= this threshold. Lets operators steer CJK-heavy traffic
    # (which carries the cloud language tax) to a local model that bills
    # nothing per token. Operates on the latest user message like
    # ``code_fence_ratio_min`` (a per-turn property), not the whole
    # request. See ``coderouter.language_tax.cjk_char_ratio``.
    cjk_ratio_min: float | None = Field(default=None, ge=0.0, le=1.0)
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
        "cjk_ratio_min",
    )

    # Boolean matcher fields carry meaning only at ``True`` — the runtime
    # evaluator uses ``is True`` — so a ``False`` value is a dead rule that
    # never matches. ``_exactly_one`` rejects it explicitly (see below).
    _BOOL_MATCHER_FIELDS: tuple[str, ...] = ("has_image", "has_tools")

    @model_validator(mode="after")
    def _exactly_one(self) -> Self:
        # Reject the dead-rule shape first so the error names the specific
        # field the operator got wrong, rather than the generic
        # "exactly one" message (``False`` counts as "set" below).
        false_bools = [
            name
            for name in self._BOOL_MATCHER_FIELDS
            if getattr(self, name) is False
        ]
        if false_bools:
            raise ValueError(
                f"RuleMatcher boolean matcher(s) set to False: {false_bools}. "
                f"These matchers are evaluated with ``is True``, so a False "
                f"value is a dead rule that never matches (it would silently "
                f"shadow nothing). Use True to match, or omit the field "
                f"(leave it None) to not use this matcher."
            )
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


class LauncherBackendConfig(BaseModel):
    """Per-backend binary path configuration for the Launcher.

    When ``binary`` is unset, the Launcher falls back to the default
    executable name (``llama-server`` for llama.cpp, ``python`` for vllm)
    and relies on ``$PATH`` resolution — which works when the tool is
    globally installed.  Set ``binary`` when:

    - llama.cpp was built from source (e.g. ``~/llama.cpp/build/bin/llama-server``)
    - vllm lives in a virtualenv (e.g. ``~/.venv/bin/python``)
    - Multiple builds coexist and you want to pin a specific one

    Tilde (``~``) and environment variables are expanded at launch time.

    Example::

        backends:
          llama.cpp:
            binary: ~/llama.cpp/build/bin/llama-server
          vllm:
            binary: ~/.venv/bin/python

    **Backend variants** (v2.11.0+, docs/designs/launcher-multi-build.md):
    a key may also be ``<base>-<variant>`` to register an additional build of
    the same backend — typically llama.cpp compiled for a specific GPU
    runtime. Each variant appears as its own entry in the Launcher's backend
    select, gets its own ``--list-devices`` probe (device IDs differ per
    build: ``CUDA0`` and ``Vulkan0`` are not the same GPU) and can carry its
    own ``option_profiles``. ``binary`` is **required** for a variant::

        backends:
          llama.cpp:                    # 素のビルド (binary 省略可 = PATH)
            binary: ~/llm/apps/llama.cpp/build/bin/llama-server
          llama.cpp-cuda:
            binary: ~/llm/apps/llama.cpp/build-cuda/bin/llama-server
          llama.cpp-vulkan:
            binary: ~/llm/apps/llama.cpp/build-vulkan/bin/llama-server
          llama.cpp-rocm:
            binary: ~/llm/apps/llama.cpp/build-rocm/bin/llama-server

    Variants are an advanced option: the matching GPU runtime (CUDA Toolkit /
    Vulkan runtime + ICD / ROCm) must already be installed by the operator.
    CodeRouter never selects a variant implicitly — omit them and both the UI
    and the resulting argv are identical to before.
    """

    model_config = ConfigDict(extra="forbid")

    binary: str | None = Field(
        default=None,
        description=(
            "Absolute or ``~``-relative path to the backend executable. "
            "llama.cpp default: ``llama-server`` (PATH). "
            "vllm default: ``python`` (PATH). "
            "Expanded at launch time."
        ),
    )


class LauncherOptionProfile(BaseModel):
    """One named option preset for a launcher backend (e.g. llama.cpp / vllm).

    ``args`` maps CLI flag strings to their values.  A bool value of
    ``True`` means "include the flag without a value" (e.g. ``--no-mmap``);
    ``False`` means "omit the flag entirely".  All other value types are
    converted to strings and appended as ``--flag value`` pairs.

    Example::

        name: "GPU速度重視"
        args:
          "-ngl": 99
          "--ctx-size": 4096
          "--no-mmap": false
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., description="Display name shown in the Launcher UI dropdown.")
    args: dict[str, str | int | float | bool] = Field(
        default_factory=dict,
        description=(
            "CLI flag → value mapping. "
            "bool True = flag only (no value). "
            "bool False = omit flag. "
            "All other types are stringified and passed as '--flag value'."
        ),
    )


class LauncherBenchPreset(BaseModel):
    """launcher.bench.presets[<key>] の1エントリ。H-2: スイープAPIが受け付けるのはキー名だけ。"""

    model_config = ConfigDict(extra="forbid")
    name: str = Field(..., min_length=1, description="Launcher UI に出す表示名。")
    command_template: str = Field(
        ...,
        min_length=1,
        description="外部ベンチコマンドのテンプレ。{port} {config} {base_url} {results_dir} {runs} を単純置換で展開。",
    )
    runs: int | None = Field(
        default=None,
        ge=1,
        le=1000,
        description="このプリセット固有の実行回数。None なら launcher.bench.runs。",
    )
    results_dir: str | None = Field(
        default=None,
        description="このプリセット固有の results ディレクトリ。None なら launcher.bench.results_dir。リクエストからは指定できない。",
    )


class LauncherBenchConfig(BaseModel):
    """The ``launcher.bench:`` block — defaults for the bench sweep feature.

    設計 §4.1。デバイス構成スイープ(起動→readiness→外部ベンチ→停止→次)の
    既定値を providers.yaml から供給する。未設定(``LauncherConfig.bench`` が
    None)ならスイープ UI はハードコード既定を使う(完全後方互換)。すべての
    フィールドに ``default`` があるので、既存 YAML は無改変で通過する。
    """

    model_config = ConfigDict(extra="forbid")

    command_template: str = Field(
        default="llmbench run --model local-openai --runs {runs}",
        description=(
            "外部ベンチコマンドのテンプレ。``{port}`` ``{config}`` "
            "``{base_url}`` ``{results_dir}`` ``{runs}`` を単純置換で展開して "
            "argv 化する(``str.format`` ではないので JSON 波括弧で誤爆しない)。"
        ),
    )
    runs: int = Field(
        default=5,
        ge=1,
        le=1000,
        description="1 構成あたりのベンチ実行回数。``{runs}`` に展開される。",
    )
    results_dir: str | None = Field(
        default=None,
        description=(
            "llmbench の results/ ディレクトリ。相対はサーバ CWD 基準。"
            "設定時のみスイープ結果 JSON を読み比較サマリを付与する。"
        ),
    )
    readiness_timeout_s: float = Field(
        default=300.0,
        ge=5.0,
        le=3600.0,
        description=(
            "スイープの各構成でサーバが ready になるのを待つ最大秒数。"
            "大きな GGUF ロードを考慮した既定 5 分。"
        ),
    )
    presets: dict[str, LauncherBenchPreset] = Field(
        default_factory=dict,
        description="名前付きベンチコマンド。キーが POST /api/launcher/sweep/start の bench_preset が取りうる唯一の値。",
    )
    default_preset: str | None = Field(
        default=None,
        description="bench_preset 未指定時に使うプリセットキー。None なら command_template 由来の暗黙 default。",
    )

    @model_validator(mode="after")
    def _check_default_preset(self) -> Self:
        if self.default_preset is not None and self.default_preset not in self.presets:
            raise ValueError(
                f"launcher.bench.default_preset={self.default_preset!r} is not a key of "
                f"launcher.bench.presets (known: {sorted(self.presets) or 'none'})"
            )
        return self


class SwapModelSpec(BaseModel):
    """One entry in ``launcher.swap.models`` (docs/designs/launcher-model-swap.md).

    Security (§7 of the design): this is the ONLY surface through which
    the on-demand swap manager (``coderouter/launcher_swap.py``) is
    allowed to start a process. ``model_path`` is static config here —
    never taken from a request body — and is re-validated against
    ``launcher.model_dirs`` via ``_resolve_within_model_dirs`` at spawn
    time, exactly like the manual ``/api/launcher/start`` UI. A
    request's ``model`` field is purely a catalog lookup key; it can
    never select an arbitrary path, backend flag, or command.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        ...,
        min_length=1,
        description=(
            "Logical model name matched against the request body's "
            "``model`` field. Also becomes the provider name and the "
            "dedicated single-model profile name "
            "(``launcher-swap-<name>``, see :attr:`provider_name` / "
            ":attr:`profile_name`) — the profile is pre-declared at "
            "config load (empty) so both ingress routes' "
            "\"profile must exist\" check passes even before the first "
            "on-demand spawn."
        ),
    )
    model_pattern: str | None = Field(
        default=None,
        description=(
            "Optional additional catalog match: ``re.fullmatch`` against "
            "the request's ``model`` field, checked when an exact "
            "``name`` match fails. Compiled at load time, same as "
            "``AutoRouteRule.model_pattern``. NOTE: the auto-injected "
            "auto_router rule (§10 Q7) always keys on ``re.escape(name)`` "
            "— an exact match — regardless of this field; this field "
            "only widens *catalog* matching for ``SwapManager.match``."
        ),
    )
    backend: str = Field(
        ...,
        description=(
            "Same backend set as the manual launcher UI / _build_cmd: "
            "'llama.cpp' / 'vllm' / 'mlx', optionally with a '-<variant>' "
            "suffix naming a specific build declared in launcher.backends "
            "(e.g. 'llama.cpp-cuda' to pin this model to the CUDA build). "
            "Validated by the field validator below plus "
            "LauncherConfig._check_swap_backends_declared."
        ),
    )

    @field_validator("backend")
    @classmethod
    def _check_backend_name(cls, v: str) -> str:
        """基底名 or 既知基底名 + 妥当なバリアントのみ許す。

        以前は ``Literal["llama.cpp","vllm","mlx"]`` だった。バリアント
        (``llama.cpp-cuda``) を通すため str に緩めたので、代わりに形式検証を
        ここで行う。既存の 3 値はそのまま通る (後方互換)。
        """
        from coderouter.launcher_devices import (
            KNOWN_BASE_BACKENDS,
            is_valid_backend_name,
        )

        if not is_valid_backend_name(v):
            raise ValueError(
                f"backend {v!r} is not valid. Expected one of "
                f"{list(KNOWN_BASE_BACKENDS)} or '<base>-<variant>' "
                "(e.g. 'llama.cpp-cuda')."
            )
        return v
    model_path: str = Field(
        ...,
        description=(
            "Absolute or ``~``-relative model file path. Re-validated "
            "against launcher.model_dirs at spawn time via "
            "_resolve_within_model_dirs — never taken from a request."
        ),
    )
    port: int | None = Field(
        default=None,
        ge=1024,
        le=65535,
        description=(
            "Fixed port (recommended — §10 Q2). When unset, the swap "
            "manager picks an OS-assigned ephemeral port and retries, "
            "each time on a freshly picked port, up to "
            "``LauncherSwapConfig.port_retry_attempts`` additional "
            "times if the backend fails to become ready. Best-effort "
            "only — no strong TOCTOU guarantee (§6.6 known-trap #4)."
        ),
    )
    ttl_seconds: float | None = Field(
        default=None,
        ge=0.0,
        description=(
            "[Unreleased] Per-model override of "
            "``LauncherSwapConfig.ttl_seconds``. ``None`` (default) = "
            "use the global value. ``0`` = unload as soon as the last "
            "in-flight lease for THIS model releases — same meaning as "
            "the global field's ``0``, just scoped to one catalog "
            "entry. Lets a large/expensive model unload sooner (or "
            "stay resident longer) than the rest of the catalog "
            "without changing the global default."
        ),
    )
    option_profile: str | None = Field(
        default=None,
        description=(
            "Name of a ``launcher.option_profiles[backend]`` preset to "
            "apply. Must exist — checked at load by "
            "``LauncherConfig._check_swap_option_profiles_exist``."
        ),
    )
    extra_args: str = Field(
        default="",
        description="One-off extra CLI flags (shlex.split; model-override guarded).",
    )
    draft_model_path: str | None = Field(
        default=None, description="MTP/draft gguf companion model (resolve_speculative).",
    )
    mtp_mode: Literal["auto", "off"] = Field(default="auto")

    # ---- Phase 2 (schema only for Phase 1 — no eviction/accounting logic
    #      wired up yet; declared now so the config surface is forward-
    #      compatible without a second migration) ----
    group: Literal["swap", "persistent", "exclusive"] = Field(
        default="swap",
        description=(
            "llama-swap groups equivalent (§10 Q3: 3 fixed values). "
            "Not consulted by Phase 1 (every model is treated as 'swap')."
        ),
    )
    est_weights_gb: float | None = Field(default=None, ge=0.0)
    num_ctx: int = Field(default=8192, ge=256)

    @property
    def provider_name(self) -> str:
        """Runtime ``ProviderConfig.name`` this model registers under."""
        return f"launcher-swap-{self.name}"

    @property
    def profile_name(self) -> str:
        """Dedicated single-model profile name (pre-declared at load).

        Deliberately identical to :attr:`provider_name` — each swap
        model gets its OWN single-provider chain rather than sharing one
        "swap" profile across models (a shared chain would let requests
        for model A route to whichever model was registered most
        recently, since ``register_provider`` only reorders providers
        within one chain — see the design-deviation note in
        docs/designs/launcher-model-swap.md's implementation report).
        """
        return f"launcher-swap-{self.name}"

    @model_validator(mode="after")
    def _compile_model_pattern(self) -> SwapModelSpec:
        """Fail fast on a bad ``model_pattern`` regex (load time, not first request)."""
        if self.model_pattern is not None:
            try:
                re.compile(self.model_pattern)
            except re.error as exc:
                raise ValueError(
                    f"swap model {self.name!r}: invalid model_pattern "
                    f"{self.model_pattern!r}: {exc}"
                ) from exc
        return self


class LauncherSwapConfig(BaseModel):
    """The ``launcher.swap`` block — Phase 1 on-demand model swap.

    See docs/designs/launcher-model-swap.md. Disabled by default
    (``enabled: false``); existing manual-launcher deployments are
    unaffected until an operator opts in.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(
        default=False,
        description=(
            "Enable on-demand swap. False (default): Launcher behaves "
            "exactly as before (manual start/stop only)."
        ),
    )
    ttl_seconds: float | None = Field(
        default=1800.0,
        ge=0.0,
        description=(
            "Seconds of no in-flight requests after which an idle swap "
            "process is auto-stopped. None = TTL disabled (runs until "
            "explicitly stopped). 0 = unload as soon as the last "
            "in-flight lease releases. §10 Q1: this is the GLOBAL "
            "default; a catalog entry's ``SwapModelSpec.ttl_seconds`` "
            "overrides it for that one model when set "
            "([Unreleased] per-model TTL override)."
        ),
    )
    readiness_timeout_s: float = Field(
        default=120.0,
        ge=1.0,
        le=1800.0,
        description=(
            "Upper bound (seconds) a request will wait for an on-demand "
            "spawn to become ready before the dispatch hook raises a "
            "retryable AdapterError."
        ),
    )
    sweep_interval_s: float = Field(
        default=15.0,
        ge=1.0,
        le=600.0,
        description="How often the TTL sweeper background task scans for idle processes.",
    )
    port_retry_attempts: int = Field(
        default=2,
        ge=0,
        le=5,
        description=(
            "[Unreleased] Number of ADDITIONAL spawn attempts (each on a "
            "freshly picked ephemeral port) after the first, used only "
            "when a catalog entry's ``port`` is unset and the previous "
            "attempt fails to become ready. 0 = no retry. Ignored when "
            "``port`` is set — a fixed port never retries (a second "
            "attempt would just collide on the same port again). This "
            "does not close the pick-then-bind TOCTOU window (see "
            "``coderouter.launcher_swap._pick_ephemeral_port``); it only "
            "bounds how many times the swap manager re-rolls the dice. "
            "A fixed ``port`` remains the recommended way to eliminate "
            "the race entirely (§10 Q2)."
        ),
    )
    inject_auto_router_rules: bool = Field(
        default=True,
        description=(
            "§10 Q7: auto-generate one auto_router rule per catalog "
            "model (id ``swap:<name>``, ``model_pattern=re.escape(name)``) "
            "so a request naming the model reaches it without any "
            "manual profile/header wiring. Inserted after any "
            "user-declared auto_router.rules (first-match-wins keeps "
            "hand-written rules authoritative) and only consulted when "
            "``default_profile: auto`` (existing auto_router constraint, "
            "unchanged). Set False to wire routing yourself (e.g. "
            "X-CodeRouter-Profile: launcher-swap-<name>)."
        ),
    )
    # ---- Phase 2 (schema only) ----
    memory_budget_gb: float | None = Field(
        default=None,
        ge=0.0,
        description="Phase 2: explicit combined-memory budget override (GB). Not used by Phase 1.",
    )
    max_loaded: int | None = Field(
        default=None,
        ge=1,
        description="Phase 2: cap on simultaneously loaded swap models. Not used by Phase 1.",
    )
    models: list[SwapModelSpec] = Field(
        default_factory=list,
        description="The static swap catalog. Only models listed here can ever be spawned on demand.",
    )

    @model_validator(mode="after")
    def _check_models(self) -> LauncherSwapConfig:
        """Fail fast on duplicate names / ports; warn on a useless enable."""
        names = [m.name for m in self.models]
        dupe_names = sorted({n for n in names if names.count(n) > 1})
        if dupe_names:
            raise ValueError(
                f"launcher.swap.models: duplicate name(s): {dupe_names}"
            )
        ports = [m.port for m in self.models if m.port is not None]
        dupe_ports = sorted({p for p in ports if ports.count(p) > 1})
        if dupe_ports:
            raise ValueError(
                f"launcher.swap.models: duplicate port(s): {dupe_ports}"
            )
        if self.enabled and not self.models:
            warnings.warn(
                "launcher.swap.enabled is True but launcher.swap.models "
                "is empty — swap has nothing to serve.",
                stacklevel=2,
            )
        return self


class LauncherConfig(BaseModel):
    """The ``launcher:`` block in providers.yaml.

    Controls the Launcher UI available at ``/launcher``.

    Example::

        launcher:
          model_dirs:
            - ~/models
            - /data/gguf
          option_profiles:
            llama.cpp:
              - name: "GPU速度重視"
                args:
                  "-ngl": 99
                  "--ctx-size": 4096
            vllm:
              - name: "標準"
                args:
                  "--dtype": "auto"
                  "--max-model-len": 4096
    """

    model_config = ConfigDict(extra="forbid")

    model_dirs: list[str] = Field(
        default_factory=list,
        description=(
            "Directories to scan for model files "
            "(.gguf, .safetensors, .bin, .pt, .ggml). "
            "Paths are expanded (~ and env vars) at scan time, not at load. "
            "Non-existent paths are silently skipped."
        ),
    )
    backends: dict[str, LauncherBackendConfig] = Field(
        default_factory=dict,
        description=(
            "Per-backend binary path overrides. "
            "Keys are backend names ('llama.cpp', 'vllm', 'mlx') or backend "
            "variants ('llama.cpp-cuda', 'llama.cpp-vulkan', ...) naming an "
            "additional build of the same backend — see "
            "LauncherBackendConfig. When a base key is absent, the default "
            "executable is used ('llama-server' / 'python') and resolved via "
            "PATH. Variants must set 'binary' explicitly and additionally "
            "appear in the Launcher's backend select."
        ),
    )
    option_profiles: dict[str, list[LauncherOptionProfile]] = Field(
        default_factory=dict,
        description=(
            "Named option presets per backend. "
            "Keys should be backend names: 'llama.cpp', 'vllm'. "
            "A backend-variant key ('llama.cpp-cuda') is also accepted: its "
            "presets are appended to the base backend's list, and a preset "
            "whose 'name' collides replaces the inherited one in place "
            "(see launcher_devices.resolve_option_profiles). "
            "Each key maps to an ordered list of named presets. "
            "A free-form 'extra args' field is always available in the UI "
            "for one-off overrides without touching this config."
        ),
    )
    # --- readiness gating (fixes: launcher registered a provider the
    #     instant the child process spawned, before llama-server / vllm had
    #     actually finished loading the model into memory — requests routed
    #     there during load returned connection-refused / 503) ---------------
    readiness_timeout_s: float = Field(
        default=300.0,
        ge=5.0,
        le=3600.0,
        description=(
            "Maximum seconds to wait for a launched backend to become "
            "ready (llama.cpp / vllm: GET /health returns 200; other "
            "backends: a bare TCP connect succeeds) before registering it "
            "as a routable provider. Default 5 min accounts for large "
            "GGUF model loads. The process is left running but the "
            "provider is never registered and status becomes 'error' if "
            "the deadline is exceeded — see ``ManagedProcess.status``."
        ),
    )
    readiness_poll_interval_s: float = Field(
        default=2.0,
        ge=0.2,
        le=60.0,
        description=(
            "Seconds between readiness probes while a launched backend's "
            "status is 'loading'."
        ),
    )
    # --- auto-restart (fixes: a crashed launcher process was left in
    #     status='error' forever — only the one-shot MTP startup-crash
    #     retry existed, and it does not apply to ordinary crashes) --------
    auto_restart: bool = Field(
        default=False,
        description=(
            "When True, a launcher-managed backend that crashes (non-zero "
            "exit, after any MTP startup-crash fallback has already been "
            "tried) is automatically relaunched with the same command, "
            "up to ``auto_restart_max_attempts`` times with exponential "
            "backoff. An intentional stop (the UI's Stop button, or "
            "server shutdown) is never treated as a crash. Default False: "
            "unlike ``readiness_timeout_s`` above (a pure bugfix with no "
            "new side effect), automatically re-spawning a process is a "
            "new side effect — a backend that crashes because it is "
            "genuinely misconfigured (bad flags, missing model file) "
            "would otherwise be silently re-spawned every few seconds "
            "until the attempt budget is exhausted, burning CPU/ports "
            "without the operator noticing. Mirrors the opt-in posture of "
            "``ProviderConfig.restart_command`` in the self-healing "
            "guard (v2.0-J) — set this to True once the launch config is "
            "known-stable."
        ),
    )
    auto_restart_max_attempts: int = Field(
        default=3,
        ge=0,
        le=20,
        description=(
            "Maximum consecutive auto-restart attempts before giving up "
            "and leaving the process in status='error'. Resets to 0 once "
            "a restarted process passes its readiness check. Ignored "
            "when ``auto_restart`` is False."
        ),
    )
    auto_restart_backoff_s: float = Field(
        default=2.0,
        ge=0.1,
        le=300.0,
        description=(
            "Initial backoff (seconds) before the first auto-restart "
            "attempt. Doubles on each subsequent attempt up to "
            "``auto_restart_backoff_max_s``."
        ),
    )
    auto_restart_backoff_max_s: float = Field(
        default=30.0,
        ge=1.0,
        le=600.0,
        description=(
            "Cap (seconds) on the exponential auto-restart backoff."
        ),
    )
    swap: LauncherSwapConfig | None = Field(
        default=None,
        description=(
            "Phase 1 on-demand model swap (docs/designs/launcher-model-"
            "swap.md). None (default) = disabled, identical to pre-swap "
            "behavior."
        ),
    )
    bench: LauncherBenchConfig | None = Field(
        default=None,
        description=(
            "デバイス構成ベンチスイープ(設計 §4)の既定値。None(既定)なら "
            "スイープ UI はハードコード既定を使う=完全後方互換。"
        ),
    )

    @model_validator(mode="after")
    def _check_auto_restart_backoff_ordered(self) -> LauncherConfig:
        """Fail fast when the backoff floor exceeds its own ceiling.

        Same fast-fail philosophy as
        ``FallbackChain._check_recovery_probe_interval_ordered``.
        """
        if self.auto_restart_backoff_s > self.auto_restart_backoff_max_s:
            raise ValueError(
                "launcher: auto_restart_backoff_s "
                f"({self.auto_restart_backoff_s}) must be <= "
                f"auto_restart_backoff_max_s ({self.auto_restart_backoff_max_s})."
            )
        return self

    @model_validator(mode="after")
    def _check_backend_names_and_variant_binaries(self) -> LauncherConfig:
        """``backends`` のキー形式と、バリアントの ``binary`` 必須を検証する。

        設計: docs/designs/launcher-multi-build.md §5.2。

        1. キーは既知基底名 (``llama.cpp`` / ``vllm`` / ``mlx``) そのもの、
           または ``<既知基底名>-<バリアント>`` (``llama.cpp-cuda`` 等) のみ。
           **破壊的変更**: これまで ``llamacpp:`` のような typo は黙って無視
           されていた (バックエンド一覧が固定 3 キーだったため)。今後は
           ロード時エラーになる。
        2. **バリアントは ``binary`` 必須**。``llama.cpp-cuda: {}`` を許すと
           既定名フォールバックで PATH の ``llama-server`` が使われ、CUDA
           ビルドを指定したつもりで素のビルドが静かに動く —— 本機能で最も
           気づきにくい事故なのでロード時に殺す。基底名は従来どおり
           ``binary`` 省略可 (PATH 解決)。
        """
        from coderouter.launcher_devices import (
            KNOWN_BASE_BACKENDS,
            is_valid_backend_name,
            is_variant,
        )

        for name, bc in self.backends.items():
            if not is_valid_backend_name(name):
                raise ValueError(
                    f"launcher.backends: invalid backend key {name!r}. "
                    f"Expected one of {list(KNOWN_BASE_BACKENDS)} or "
                    "'<base>-<variant>' where variant matches "
                    "[a-z0-9][a-z0-9._-]* (e.g. 'llama.cpp-cuda')."
                )
            if is_variant(name) and not bc.binary:
                raise ValueError(
                    f"launcher.backends[{name!r}]: 'binary' is required for a "
                    "backend variant. Without it the launcher would fall back "
                    "to the base default on PATH and silently run a different "
                    "build than the one you selected."
                )
        return self

    @model_validator(mode="after")
    def _check_swap_backends_declared(self) -> LauncherConfig:
        """swap のモデルがバリアントを指すなら ``backends`` に実在すること。

        設計 §5.2-3。基底名 (``llama.cpp`` 等) は ``backends`` に書かなくても
        PATH 解決で動くので従来どおり不要。バリアントは実行ファイルパスが
        ``backends`` にしか無いので、宣言漏れをロード時に弾く。
        """
        if self.swap is None:
            return self
        from coderouter.launcher_devices import is_variant

        for spec in self.swap.models:
            if is_variant(spec.backend) and spec.backend not in self.backends:
                raise ValueError(
                    f"launcher.swap.models[{spec.name!r}]: backend "
                    f"{spec.backend!r} is a variant but is not declared in "
                    "launcher.backends (its binary path is unknown)."
                )
        return self

    @model_validator(mode="after")
    def _check_swap_option_profiles_exist(self) -> LauncherConfig:
        """§5.4 #2: a swap model's ``option_profile`` must be a real preset.

        バリアント backend の場合は基底名のプロファイルも継承されるので
        (:func:`resolve_option_profiles`)、マージ後の一覧で判定する。
        """
        if self.swap is None:
            return self
        from coderouter.launcher_devices import resolve_option_profiles

        for spec in self.swap.models:
            if spec.option_profile is None:
                continue
            profiles = resolve_option_profiles(self.option_profiles, spec.backend)
            if not any(p.name == spec.option_profile for p in profiles):
                raise ValueError(
                    f"launcher.swap.models[{spec.name!r}]: option_profile "
                    f"{spec.option_profile!r} not found in "
                    f"launcher.option_profiles[{spec.backend!r}]."
                )
        return self

    @model_validator(mode="after")
    def _check_swap_model_paths_within_model_dirs(self) -> LauncherConfig:
        """Review fix L-1: fail fast on a swap ``model_path`` outside model_dirs.

        The spawn path re-validates via ``_resolve_within_model_dirs``
        (defense in depth, M14 traversal guard), but that only surfaces
        at the first request for the model — as a retryable 502. A
        static catalog entry is fully checkable at load, so check it
        here with the same containment rule (resolve ``~`` / symlinks /
        ``..`` on both sides, then require the path to be the dir or
        under it). Only enforced when swap is enabled — a disabled swap
        block stays zero-impact.
        """
        if self.swap is None or not self.swap.enabled or not self.swap.models:
            return self
        from pathlib import Path

        if not self.model_dirs:
            raise ValueError(
                "launcher.swap is enabled but launcher.model_dirs is empty — "
                "swap model_path values cannot be validated (and spawn would "
                "refuse them at request time). Configure model_dirs."
            )
        bases = [Path(d).expanduser().resolve() for d in self.model_dirs]
        for spec in self.swap.models:
            candidate = Path(spec.model_path).expanduser().resolve()
            if not any(candidate == b or b in candidate.parents for b in bases):
                raise ValueError(
                    f"launcher.swap.models[{spec.name!r}]: model_path "
                    f"{spec.model_path!r} is not under any configured "
                    f"launcher.model_dirs {self.model_dirs!r}."
                )
        return self


class PluginsConfig(BaseModel):
    """The ``plugins:`` block in providers.yaml (v2.3.0).

    Declarative opt-in for in-process plugins distributed as separate
    PyPI packages (``coderouter-plugin-*``). Two-step gating:

    1. ``pip install coderouter-plugin-X`` makes the entry point
       discoverable.
    2. The plugin's entry-point name MUST appear in :attr:`enabled`
       before the loader will instantiate it.

    Step 2 is the supply-chain defense: a malicious transitive dep
    cannot wedge itself into the request flow without an explicit
    user action in providers.yaml. See
    :mod:`coderouter.plugins.loader` for the full discovery logic.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: list[str] = Field(
        default_factory=list,
        description=(
            "v2.3.0: ordered list of plugin entry-point names to load. "
            "An entry-point name is the LHS of an entry in a plugin's "
            "``[project.entry-points.\"coderouter.<group>\"]`` block — "
            "e.g. ``memory`` for ``coderouter-plugin-memory``. Order "
            "controls the order InputFilter chains apply (each filter "
            "sees the previous filter's output). Empty list = no "
            "plugins active (default behavior, identical to v2.2.0)."
        ),
    )
    config: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        description=(
            "v2.3.0: per-plugin keyword arguments. The dict at "
            "``config[<plugin-name>]`` is splatted into the plugin's "
            "``__init__`` as ``**kwargs``. Validation of each "
            "sub-dict's schema is the plugin's responsibility — Core "
            "stays out of plugin-specific config shapes. Plugins not "
            "listed in :attr:`enabled` are ignored even if they have "
            "config entries here."
        ),
    )


class TranslationConfig(BaseModel):
    """Translation layer config (doc/翻訳層設計書.md §4, providers.yaml)."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(
        default=True,
        description="Enable JA↔EN translation layer. True = CodeRouter-t default (translate), False = pass-through.",
    )
    # Literal["cpu"] is for type-checker; validator provides runtime message (S-1)
    device: Literal["cpu"] = Field(
        default="cpu",
        description='Execution device. Only "cpu" allowed (VRAM zero guarantee). providers.yaml setting.',
    )
    log_translations: bool = Field(
        default=True,
        description="Enable detailed translation logging (debug, redacted). CodeRouter-t default True.",
    )
    model_dir: str | None = Field(
        default=None,
        description="Argos model directory. None = Argos standard cache.",
    )

    @field_validator("device")
    @classmethod
    def _check_device(cls, v: str) -> str:
        if v != "cpu":
            raise ValueError("translation.device must be 'cpu' (VRAM zero guarantee)")
        return v


class CodeRouterConfig(BaseModel):
    """Top-level config loaded from providers.yaml."""

    model_config = ConfigDict(extra="forbid")

    allow_paid: bool = Field(
        default=False,
        description="Master switch. ALLOW_PAID=false blocks all paid providers (plan.md §2.3).",
    )
    default_profile: str = Field(default="default")
    # [Unreleased]: relaxed from ``Field(..., min_length=1)`` so a
    # swap-only deployment (``launcher.swap.enabled`` with a non-empty
    # ``models`` catalog) can omit both fields entirely instead of
    # writing an unreachable dummy provider/profile just to satisfy the
    # schema. The "at least one" invariant is NOT dropped — it moves to
    # ``_check_providers_and_profiles_nonempty`` below, which still
    # fail-fasts at load for every other deployment shape (same
    # philosophy as the min_length constraint it replaces).
    providers: list[ProviderConfig] = Field(default_factory=list)
    profiles: list[FallbackChain] = Field(default_factory=list)
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

    # H-5 escape hatch: token-estimation scope.
    #
    # Deliberately top-level rather than per-``FallbackChain``, even
    # though ``context_budget_action`` lives on the chain. The shared
    # char/4 estimator has four consumers and three of them run with no
    # profile in hand: the auto-router runs *before* a profile exists
    # (it is what picks one), ``POST /v1/messages/count_tokens`` answers
    # a client question that must not depend on which chain would have
    # served it, and language-tax measurement is profile-agnostic. A
    # per-profile switch would let the router and the guard disagree
    # about the size of the very same request — exactly the class of bug
    # this key exists to let operators back out of.
    token_estimation_include_tool_content: bool = Field(
        default=True,
        description=(
            "v2.12 (H-5): count ``tool_result`` / ``tool_use`` / "
            "``thinking`` content blocks in the shared char/4 token "
            "estimator. True (default) is the correct behavior — up to "
            "v2.11.x these blocks estimated to 0 chars, under-counting a "
            "Claude Code style session by 5x at 20 turns and ~29x at 200. "
            "Image blocks stay at 0 either way. Set to false as an escape "
            "hatch to restore v2.11.x-identical estimates (and therefore "
            "v2.11.x context-budget firing, auto-router "
            "``content_token_count_min`` matching and "
            "``/v1/messages/count_tokens`` numbers) if the corrected "
            "estimate disrupts a tuned deployment. Compatibility shim "
            "only — scheduled for removal in a future release."
        ),
    )

    # v2.0-I: Continuous probing — background health checks for idle periods.
    continuous_probe: Literal["off", "active"] = Field(
        default="off",
        description=(
            "v2.0-I: enable background health probes. 'active' starts a "
            "background task that periodically sends 1-token requests to "
            "each provider, feeding results into the L5 backend health "
            "state machine. 'off' = no probing (backward-compatible default)."
        ),
    )
    probe_interval_s: float = Field(
        default=60.0,
        ge=5.0,
        le=3600.0,
        description=(
            "v2.0-I: seconds between probe rounds. Lower = faster detection "
            "but more probe traffic. 60s is a good balance for local models."
        ),
    )
    probe_paid: bool = Field(
        default=False,
        description=(
            "v2.0-I: whether to probe providers marked ``paid: true``. "
            "Default false protects operators from accidental API charges."
        ),
    )
    probe_timeout_s: float = Field(
        default=10.0,
        ge=1.0,
        le=60.0,
        description=(
            "v2.0-I: per-provider timeout for probe requests. A provider "
            "that doesn't respond within this window is recorded as failed."
        ),
    )

    # v2.0-K: Persistent state — survive restarts.
    state_dir: str | None = Field(
        default=None,
        description=(
            "v2.0-K: directory for persistent state (sqlite3 KV store + "
            "audit log). None = in-memory only (no persistence, backward-"
            "compatible). Set to a path like '~/.coderouter-t/state/' to "
            "enable cross-restart durability for budget totals, health "
            "state, and self-healing exclusions. The directory is created "
            "automatically if it doesn't exist."
        ),
    )
    audit_log: Literal["off", "active"] = Field(
        default="off",
        description=(
            "v2.0-K: structured audit log. 'active' writes guard "
            "activations, chain fallbacks, budget warnings, self-healing "
            "events, and drift transitions to a JSONL file in state_dir. "
            "'off' = no audit log (backward-compatible default). Requires "
            "state_dir to be set."
        ),
    )
    audit_log_max_bytes: int = Field(
        default=10_485_760,
        ge=1_048_576,
        le=1_073_741_824,
        description=(
            "v2.0-K: maximum audit log file size before rotation (bytes). "
            "Default 10 MiB. When exceeded, the current file is renamed "
            "to audit.jsonl.1 and a fresh file is started. Only one "
            "backup is kept."
        ),
    )
    request_log: Literal["off", "active"] = Field(
        default="off",
        description=(
            "v2.0-K (Replay): request metadata journal. 'active' records "
            "per-request metadata (provider, token counts, cost, streaming "
            "flag) to a JSONL file in state_dir on every successful "
            "response. Request/response bodies are NOT recorded (privacy "
            "+ size). Used by ``coderouter replay`` for statistical A/B "
            "analysis. 'off' = no journal (backward-compatible default). "
            "Requires state_dir to be set."
        ),
    )
    request_log_max_bytes: int = Field(
        default=52_428_800,
        ge=1_048_576,
        le=1_073_741_824,
        description=(
            "v2.0-K (Replay): maximum request journal file size before "
            "rotation (bytes). Default 50 MiB. Same single-backup "
            "rotation as audit_log — when exceeded, the current file is "
            "renamed to requests.jsonl.1 and a fresh file is started."
        ),
    )

    # v2.3.0: in-process plugin SDK. Optional — when None, the engine
    # builds an empty ``PluginRegistry`` and the hook chains are
    # short-circuited (zero-cost path, identical to v2.2.0 behavior).
    plugins: PluginsConfig | None = Field(
        default=None,
        description=(
            "v2.3.0: in-process plugin configuration. Plugins are "
            "distributed as separate PyPI packages (e.g. "
            "``coderouter-plugin-memory``); this block lists which of "
            "the installed plugins to actually activate, and supplies "
            "their per-plugin keyword arguments. Absent or empty = no "
            "plugins (zero-cost, backward-compatible default)."
        ),
    )
    launcher: LauncherConfig | None = Field(
        default=None,
        description=(
            "Launcher configuration for the /launcher UI. "
            "Defines model_dirs to scan and option_profiles per backend "
            "('llama.cpp', 'vllm'). "
            "Unset (None) = Launcher UI shows empty model list and no profiles. "
            "The Launcher UI itself is always available at /launcher "
            "regardless of this setting."
        ),
    )
    translation: TranslationConfig = Field(
        default_factory=TranslationConfig,
        description="JA↔EN translation layer (CPU Argos Translate, providers.yaml). Disabled by default.",
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
    def _check_providers_and_profiles_nonempty(self) -> CodeRouterConfig:
        """[Unreleased]: enforce "at least one" unless launcher.swap covers it.

        ``providers`` / ``profiles`` were relaxed from ``min_length=1`` to
        ``default_factory=list`` (see the field comments above) so a
        swap-only deployment can omit them — every request in that shape
        is routed through a ``launcher-swap-<name>`` profile that
        :meth:`_inject_swap_profiles_and_auto_router_rules` synthesizes
        below, and its backing provider is registered at runtime on first
        spawn (``SwapManager.register_provider``), so there is genuinely
        nothing to declare statically.

        Runs BEFORE the swap injection (which only ever *adds* profiles,
        never providers) so it observes the operator's raw, undecorated
        input rather than the post-injection state — the injected
        profiles are not a substitute for a real provider declaration in
        every other deployment shape, only in the swap-only one this
        validator carves out.

        Outside that carve-out, an empty ``providers`` or ``profiles``
        list has always been a load-time error (there would be nothing to
        route to); this validator keeps that fail-fast guarantee instead
        of silently trading it away when the ``min_length=1`` field
        constraint was dropped.
        """
        swap_cfg = self.launcher.swap if self.launcher is not None else None
        swap_covers_empty = (
            swap_cfg is not None and swap_cfg.enabled and bool(swap_cfg.models)
        )
        if swap_covers_empty:
            return self
        if not self.providers:
            raise ValueError(
                "providers: at least one entry is required (empty/omitted "
                "is only allowed when launcher.swap.enabled=true and "
                "launcher.swap.models has at least one entry)."
            )
        if not self.profiles:
            raise ValueError(
                "profiles: at least one entry is required (empty/omitted "
                "is only allowed when launcher.swap.enabled=true and "
                "launcher.swap.models has at least one entry)."
            )
        return self

    @model_validator(mode="after")
    def _inject_swap_profiles_and_auto_router_rules(self) -> CodeRouterConfig:
        """§10 Q7 + design-deviation: bootstrap swap's dedicated profiles.

        For every ``launcher.swap.models[*]`` entry (when
        ``launcher.swap.enabled``), pre-declares an EMPTY placeholder
        profile named ``SwapModelSpec.profile_name``
        (``launcher-swap-<name>``) so both ingress routes'
        "profile must already exist" pre-dispatch check passes even on
        the very first (cold-start) request for that model — the
        on-demand spawn itself only happens later, inside
        ``FallbackEngine``'s dispatch entry points, which run AFTER that
        check. ``SwapManager.register_provider`` fills the chain in on
        first spawn (mirrors the existing lazy-profile-creation path in
        ``FallbackEngine.register_provider``).

        Deliberately ONE profile PER model (not a single shared "swap"
        profile as sketched in the design's §5.5 YAML example): a
        shared chain would make ``register_provider``'s "insert at
        front" behavior route a request for model A to whichever model
        was registered most recently, since chain dispatch never
        cross-checks the request's ``model`` against the provider it
        picks. See the implementation report for the full rationale.

        When ``inject_auto_router_rules`` is True (default), also
        generates one ``AutoRouteRule`` per model (id ``swap:<name>``,
        ``model_pattern=re.escape(name)`` — an exact match, independent
        of the catalog's own broader ``model_pattern`` field) targeting
        that profile, appended AFTER any user-declared
        ``auto_router.rules`` (first-match-wins keeps hand-written rules
        authoritative).

        Review fix C-1: when NO ``auto_router:`` block was declared and
        ``default_profile == "auto"``, the operator was relying on the
        BUNDLED ruleset — synthesizing a swap-only block here would
        silently replace it (image → multi and code-fence → coding
        classification would vanish the moment swap was enabled). The
        synthesized block therefore carries
        ``[*BUNDLED_RULES, *swap_rules]`` with the bundled fallthrough
        profile preserved, so enabling swap is purely additive. Outside
        the ``default_profile: auto`` case the bundled rules never
        applied anyway, so the block holds only the swap rules and
        falls through to ``default_profile`` itself. Runs before
        :meth:`_check_auto_router_profiles_exist` so the injected rules
        are validated too, and before
        :meth:`_check_bundled_auto_router_requirements` so injecting a
        swap-only auto_router doesn't spuriously demand the bundled
        trio (multi/coding/writing) for non-auto deployments.
        """
        swap_cfg = self.launcher.swap if self.launcher is not None else None
        if swap_cfg is None or not swap_cfg.enabled or not swap_cfg.models:
            return self

        provider_names = {p.name for p in self.providers}
        collisions = sorted(
            {
                spec.provider_name
                for spec in swap_cfg.models
                if spec.provider_name in provider_names
            }
        )
        if collisions:
            raise ValueError(
                "launcher.swap.models produce provider name(s) that "
                f"collide with statically-declared providers: {collisions}. "
                "Rename the swap model (or the conflicting provider)."
            )

        existing_profiles = {p.name for p in self.profiles}
        for spec in swap_cfg.models:
            if spec.profile_name not in existing_profiles:
                self.profiles.append(
                    FallbackChain.model_construct(
                        name=spec.profile_name, providers=[]
                    )
                )
                existing_profiles.add(spec.profile_name)

        if not swap_cfg.inject_auto_router_rules:
            return self

        swap_rules = [
            AutoRouteRule(
                id=f"swap:{spec.name}",
                profile=spec.profile_name,
                match=RuleMatcher(model_pattern=re.escape(spec.name)),
            )
            for spec in swap_cfg.models
        ]
        if self.auto_router is None:
            if self.default_profile == "auto":
                # C-1: the operator is on the bundled ruleset. Merge it
                # into the synthesized block and keep the bundled
                # fallthrough profile, so enabling swap never silently
                # disables image/code-fence classification. SWAP RULES
                # FIRST (coordinator-reviewed order): a swap rule is an
                # ``re.escape`` exact model-name match, firing only when
                # the client explicitly named that model — a stronger
                # signal than the bundled content heuristics. Bundled-
                # first would let a code-fence-dense body hijack an
                # explicitly-named swap model over to 'coding'; swap-
                # first cannot affect any request that doesn't name a
                # swap model (exact match). User-declared auto_router
                # blocks keep append-after semantics below (operators
                # control their own ordering there).
                # Function-local import: routing.auto_router imports
                # this module at ITS module level, so schemas must not
                # import it at module level (cycle). By the time any
                # CodeRouterConfig is constructed both modules are
                # importable, so the runtime import here is safe.
                from coderouter.routing.auto_router import (
                    BUNDLED_DEFAULT_RULE_PROFILE,
                    BUNDLED_RULES,
                )

                self.auto_router = AutoRouterConfig(
                    rules=[*swap_rules, *BUNDLED_RULES],
                    default_rule_profile=BUNDLED_DEFAULT_RULE_PROFILE,
                )
            else:
                # Non-auto deployments never consulted the bundled rules
                # (classify() only fires under ``default_profile: auto``),
                # so the block holds only the swap rules and falls
                # through to ``default_profile`` itself (guaranteed to
                # exist per ``_check_default_profile_exists``, which
                # already ran) — enabling swap never forces an unrelated
                # static-profile deployment to declare a "writing"
                # profile it has no other use for.
                self.auto_router = AutoRouterConfig(
                    rules=swap_rules, default_rule_profile=self.default_profile
                )
        else:
            self.auto_router.rules = [*self.auto_router.rules, *swap_rules]
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

    @model_validator(mode="after")
    def _check_names_unique(self) -> CodeRouterConfig:
        """mE: provider and profile names must each be unique.

        ``provider_by_name`` / ``profile_by_name`` return the *first*
        match, so a duplicate name silently shadows every later entry with
        the same key — the operator's second ``local:`` block is loaded,
        validated, and then never reachable. Reject duplicates at load with
        the offending names, matching the fast-fail philosophy of the other
        ``_check_*`` validators.
        """
        seen_p: set[str] = set()
        dup_p: list[str] = []
        for p in self.providers:
            if p.name in seen_p and p.name not in dup_p:
                dup_p.append(p.name)
            seen_p.add(p.name)
        if dup_p:
            raise ValueError(
                f"duplicate provider name(s): {sorted(dup_p)}. Provider names "
                f"must be unique — a duplicate silently shadows the later "
                f"entry (provider_by_name returns the first match). Rename or "
                f"remove the duplicate(s)."
            )

        seen_f: set[str] = set()
        dup_f: list[str] = []
        for prof in self.profiles:
            if prof.name in seen_f and prof.name not in dup_f:
                dup_f.append(prof.name)
            seen_f.add(prof.name)
        if dup_f:
            raise ValueError(
                f"duplicate profile name(s): {sorted(dup_f)}. Profile names "
                f"must be unique — a duplicate silently shadows the later "
                f"entry (profile_by_name returns the first match). Rename or "
                f"remove the duplicate(s)."
            )
        return self

    @model_validator(mode="after")
    def _check_profile_providers_exist(self) -> CodeRouterConfig:
        """mE: every provider named in a profile chain must be declared.

        Previously a typo in ``profiles[].providers`` was tolerated at load
        and only surfaced at runtime as a ``skip-unknown-provider`` warning;
        if *every* entry in a chain was typo'd the profile silently had no
        usable providers and only failed once fully drained. Rejecting at
        load — same philosophy as ``_check_default_profile_exists`` and
        ``_check_mode_alias_targets_exist`` — turns a silent-until-drained
        misconfig into a startup error naming the profile and the bad
        provider(s).
        """
        names = {p.name for p in self.providers}
        errors: list[str] = []
        for prof in self.profiles:
            missing = [name for name in prof.providers if name not in names]
            if missing:
                errors.append(f"{prof.name!r} -> {missing}")
        if errors:
            raise ValueError(
                f"profile(s) reference undeclared provider(s): "
                f"{'; '.join(errors)}. known providers={sorted(names)}. "
                f"Fix the typo in profiles[].providers or add the missing "
                f"provider(s)."
            )
        return self

    @model_validator(mode="after")
    def _apply_token_estimation_scope(self) -> CodeRouterConfig:
        """Publish ``token_estimation_include_tool_content`` process-wide.

        The char/4 estimator is a dependency-free leaf module that every
        consumer imports directly as a plain function; there is no
        object graph to thread a flag through, and three of the four
        consumers have no profile/config handle at their call site. So
        the loaded config pushes the setting into the module once, here.

        Idempotent and deterministic: the value always reflects the most
        recently constructed config, which in a server process is the
        single config that was loaded at startup. Declared last so a
        config that fails any other validator never takes effect. Tests
        that flip it should restore the default (True) afterwards.
        """
        set_include_tool_content(self.token_estimation_include_tool_content)
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

    def resolve_model_to_profile(self, model_name: str | None) -> str | None:
        """Resolve a model name string to a profile name.

        Priority order:
        1. Exact match with a profile's primary (first) provider `model` or `name`.
        2. Exact match with any provider's `model` or `name`.
        3. Exact match with declared profile name.
        4. Exact match in `mode_aliases`.
        5. Substring match in `mode_aliases` (longest key first).
        6. Substring match with provider's `model` or `name`.

        v2.15.1 refinements (review A-M1/A-M2):
        - Step 6 substring threshold raised to 5 chars to avoid short-token
          false positives (e.g. "haiku" 5 chars is now the minimum for the
          reverse direction; 3-char fragments like "gpt" no longer hijack).
        - Vendor prefix stripping (`openrouter/anthropic/claude-...`,
          `model:tag` forms) via split on "/" and ":" so alias / provider
          matching works on the bare model id.
        """
        if not model_name:
            return None

        model_lower = model_name.lower().strip()
        # Bare model id for substring steps: strip vendor prefixes and tags.
        # e.g. "openrouter/anthropic/claude-3-opus" -> "claude-3-opus"
        #      "gemma4:e4b-it-qat" -> "e4b-it-qat" is NOT stripped for exact
        #      steps (they already matched above), but the bare form helps
        #      substring fallback without losing the full-string check.
        model_bare = model_lower.split("/")[-1].split(":")[-1] if "/" in model_lower or ":" in model_lower else model_lower
        provider_map = {p.name: p for p in self.providers}

        # 1. Exact match with primary (first) provider of each profile
        for prof in self.profiles:
            if not prof.providers:
                continue
            primary_name = prof.providers[0]
            prov_cfg = provider_map.get(primary_name)
            if prov_cfg:
                if prov_cfg.model and prov_cfg.model.lower().strip() == model_lower:
                    return prof.name
                if prov_cfg.name.lower().strip() == model_lower:
                    return prof.name

        # 2. Exact match with any configured provider in any profile
        for prof in self.profiles:
            for prov_name in prof.providers:
                prov_cfg = provider_map.get(prov_name)
                if prov_cfg:
                    if prov_cfg.model and prov_cfg.model.lower().strip() == model_lower:
                        return prof.name
                    if prov_cfg.name.lower().strip() == model_lower:
                        return prof.name

        # 3. Exact match with declared profile name
        for prof in self.profiles:
            if prof.name.lower().strip() == model_lower:
                return prof.name

        # 4. Exact match in mode_aliases (case-sensitive as before, plus lower fallback)
        if model_name in self.mode_aliases:
            return self.mode_aliases[model_name]
        if model_lower in (k.lower() for k in self.mode_aliases):
            # case-insensitive exact alias match
            for k, v in self.mode_aliases.items():
                if k.lower() == model_lower:
                    return v

        # 5. Substring match in mode_aliases (case-insensitive, longest alias first)
        sorted_aliases = sorted(self.mode_aliases.keys(), key=len, reverse=True)
        for alias_key in sorted_aliases:
            alias_lower = alias_key.lower()
            if alias_lower in model_lower or alias_lower in model_bare:
                return self.mode_aliases[alias_key]

        # 6. Substring match with provider's model or name (bidirectional, 5-char floor)
        # A-M1: raised from 3 to 5 to prevent short fragments from hijacking.
        for prof in self.profiles:
            for prov_name in prof.providers:
                prov_cfg = provider_map.get(prov_name)
                if prov_cfg:
                    target_model = (prov_cfg.model or "").lower().strip()
                    target_name = prov_cfg.name.lower().strip()
                    # Check against both full and bare forms; prefer bare to avoid
                    # vendor prefix noise, but keep full as fallback.
                    for candidate in (model_lower, model_bare):
                        if target_model and (target_model in candidate or (len(candidate) >= 5 and candidate in target_model)):
                            return prof.name
                        if target_name and (target_name in candidate or (len(candidate) >= 5 and candidate in target_name)):
                            return prof.name

        return None



