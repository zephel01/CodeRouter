"""Declarative model-capabilities.yaml registry (v0.7-A).

Motivation
    v0.5-A hardcoded the "which Anthropic models accept the `thinking`
    body field" heuristic as a regex literal inside
    ``coderouter.routing.capability``. Adding a new family (or a new
    capability flag) meant a code change. v0.7-A externalizes that
    heuristic into a YAML registry so the common maintenance action —
    "Anthropic shipped 4-8, which accepts thinking" — becomes a one-line
    YAML edit instead of a Python patch.

    The registry answers "given (kind, model), what capability flags are
    declared?" and is consulted by the v0.5 capability gate functions
    (``provider_supports_thinking`` et al.) after the per-provider
    explicit flag on ``providers.yaml`` has been checked. Precedence:

        providers.yaml capabilities.*      ─── highest (explicit opt-in)
        user file ~/.coderouter/mcy        ─── per-deployment overrides
        bundled  coderouter/data/mcy       ─── shipped defaults
        unset                               ─── flag defaults to False

Design notes
    - Pure functions + a small Pydantic schema. No I/O outside the
      loader entry points. The registry instance is loaded once at
      startup (via ``CapabilityRegistry.load_default``) and reused.
    - fnmatch globs, not regex. Globs cover the real-world pattern
      ("all claude-opus-4-x", "qwen3-coder:*") without making the YAML
      author learn escape rules.
    - First-match-per-flag semantics: rules are walked top-to-bottom
      per flag, first rule whose glob matches AND declares that flag
      determines the value. A rule may declare only a subset of flags;
      undeclared flags keep looking further down the list. This means a
      specific early rule can override one capability while letting a
      broader later rule handle the rest.
    - ``kind`` filter is optional (``"any"`` default). When set, only
      providers of that adapter kind are candidates. This replaces the
      old hardcoded ``if kind != "anthropic": return False`` guard —
      the bundled YAML simply does not declare any openai_compat rules
      for thinking, so openai_compat providers get ``thinking=None``
      from the registry, which the gate function treats as False.
    - No mutation. Registry instances are immutable; ``load_default``
      re-reads disk each call (cheap), but the capability module caches
      a single instance.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# YAML schema
# ---------------------------------------------------------------------------


class RegistryCapabilities(BaseModel):
    """Per-rule capability flag declarations (all optional).

    ``None`` means "this rule does not have an opinion about this flag".
    Rules that omit a flag let the lookup fall through to later rules
    (or to the Python fallback of False / None) for that specific flag.
    """

    model_config = ConfigDict(extra="forbid")

    thinking: bool | None = Field(
        default=None,
        description=(
            "Anthropic `thinking: {type: enabled}` body field support. "
            "When True, the capability gate treats this (kind, model) as "
            "able to receive the block without translation loss."
        ),
    )
    reasoning_passthrough: bool | None = Field(
        default=None,
        description=(
            "Opt-OUT of the openai_compat adapter's passive strip of the "
            "non-standard `message.reasoning` field. True = let the raw "
            "reasoning text flow to the client. Default (None/False) = "
            "strip it (v0.5-C behavior)."
        ),
    )
    tools: bool | None = Field(
        default=None,
        description=(
            "Model reliably emits structured tool_calls. Declared here "
            'as a glob default (e.g. "qwen3-coder:*" tools=true); the '
            "actual adapter-level handling sits in v0.3-A tool repair."
        ),
    )
    max_context_tokens: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Declared context window for this model. Used by v0.7-B "
            "doctor --check-model num_ctx probe (not consumed in v0.7-A)."
        ),
    )
    claude_code_suitability: Literal["ok", "degraded"] | None = Field(
        default=None,
        description=(
            "v1.7-B: hint for use behind Claude Code's agentic-coding "
            "harness. ``degraded`` = the model over-eagerly invokes "
            "tools/skills when given Claude Code's system prompt — e.g. "
            "Llama-3.3-70B treating small talk like ``こんにちは`` as "
            "``Skill(hello)`` invocations (see docs/troubleshooting.md "
            "§4-1 for the symptom log). ``ok`` = explicitly verified "
            "clean. ``None`` = no opinion (treated as ``ok`` at the "
            "startup check)."
        ),
    )


class CapabilityRule(BaseModel):
    """One entry in the registry YAML ``rules:`` list."""

    model_config = ConfigDict(extra="forbid")

    match: str = Field(
        ...,
        min_length=1,
        description=(
            "fnmatch-style glob applied case-sensitively against "
            "ProviderConfig.model. Supported wildcards: *, ?, [seq]."
        ),
    )
    kind: Literal["anthropic", "openai_compat", "any"] = Field(
        default="any",
        description=(
            "Restrict rule to providers of this adapter kind. 'any' "
            "means the rule matches regardless of kind."
        ),
    )
    capabilities: RegistryCapabilities = Field(default_factory=RegistryCapabilities)

    def kind_matches(self, provider_kind: str) -> bool:
        """True if this rule's kind filter admits ``provider_kind``."""
        return self.kind == "any" or self.kind == provider_kind

    def glob_matches(self, model: str) -> bool:
        """True if this rule's glob matches ``model`` (case-sensitive)."""
        return fnmatch.fnmatchcase(model, self.match)


class CapabilityRegistryFile(BaseModel):
    """Top-level shape of a model-capabilities.yaml file."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = Field(
        default=1,
        description="Registry format version — bump on breaking schema changes.",
    )
    rules: list[CapabilityRule] = Field(
        default_factory=list,
        description="First-match-per-flag rule list.",
    )


# ---------------------------------------------------------------------------
# Lookup result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResolvedCapabilities:
    """Result of a registry lookup — merged per-flag from the rule chain.

    Each field is ``None`` when no matching rule declared that flag; the
    gate function coerces ``None`` to the conservative "not supported"
    answer (False), matching the pre-v0.7-A regex fallback.
    """

    thinking: bool | None = None
    reasoning_passthrough: bool | None = None
    tools: bool | None = None
    max_context_tokens: int | None = None
    claude_code_suitability: Literal["ok", "degraded"] | None = None


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


_BUNDLED_PACKAGE = "coderouter.data"
_BUNDLED_NAME = "model-capabilities.yaml"
_USER_PATH = Path.home() / ".coderouter" / "model-capabilities.yaml"


class CapabilityRegistry:
    """Layered, glob-based capability registry.

    The rule list is ordered with user rules first (higher priority),
    bundled rules second. Lookup walks the combined list top-to-bottom;
    for each declared flag, the first rule whose ``(kind, match)`` fits
    the query wins. A rule that does not declare a given flag is
    transparent for that flag — the walk continues past it.

    Thread-safety: the instance is read-only after construction, so
    concurrent ``lookup`` calls are safe. Loading is not thread-safe but
    is expected to happen once at process startup.
    """

    def __init__(self, rules: list[CapabilityRule]) -> None:
        self._rules: list[CapabilityRule] = list(rules)

    @property
    def rules(self) -> list[CapabilityRule]:
        """Return a copy of the rule list in evaluation order."""
        return list(self._rules)

    def lookup(self, *, kind: str, model: str) -> ResolvedCapabilities:
        """Resolve capability flags for ``(kind, model)``.

        Per-flag first-match: for each of the four flags, returns the
        value from the first rule whose ``kind`` filter admits the
        query AND whose glob matches ``model`` AND which explicitly
        declares that flag.

        ``model`` may be the empty string; globs that match "" still
        apply (but no bundled rule does today). Callers pass
        ``provider.model or ""`` directly.
        """
        resolved_thinking: bool | None = None
        resolved_reasoning: bool | None = None
        resolved_tools: bool | None = None
        resolved_max_ctx: int | None = None
        resolved_suitability: Literal["ok", "degraded"] | None = None

        thinking_locked = False
        reasoning_locked = False
        tools_locked = False
        max_ctx_locked = False
        suitability_locked = False

        for rule in self._rules:
            if not rule.kind_matches(kind):
                continue
            if not rule.glob_matches(model):
                continue
            caps = rule.capabilities
            if not thinking_locked and caps.thinking is not None:
                resolved_thinking = caps.thinking
                thinking_locked = True
            if not reasoning_locked and caps.reasoning_passthrough is not None:
                resolved_reasoning = caps.reasoning_passthrough
                reasoning_locked = True
            if not tools_locked and caps.tools is not None:
                resolved_tools = caps.tools
                tools_locked = True
            if not max_ctx_locked and caps.max_context_tokens is not None:
                resolved_max_ctx = caps.max_context_tokens
                max_ctx_locked = True
            if not suitability_locked and caps.claude_code_suitability is not None:
                resolved_suitability = caps.claude_code_suitability
                suitability_locked = True
            if (
                thinking_locked
                and reasoning_locked
                and tools_locked
                and max_ctx_locked
                and suitability_locked
            ):
                break

        return ResolvedCapabilities(
            thinking=resolved_thinking,
            reasoning_passthrough=resolved_reasoning,
            tools=resolved_tools,
            max_context_tokens=resolved_max_ctx,
            claude_code_suitability=resolved_suitability,
        )

    # ------------------------------------------------------------------
    # Loaders
    # ------------------------------------------------------------------

    @classmethod
    def from_rule_lists(
        cls,
        *,
        user: list[CapabilityRule] | None = None,
        bundled: list[CapabilityRule] | None = None,
    ) -> CapabilityRegistry:
        """Build a registry from pre-loaded rule lists.

        Primarily useful for tests that want to inject rule data without
        touching disk. The order in the returned registry is
        ``user + bundled`` — user rules are evaluated first.
        """
        return cls((user or []) + (bundled or []))

    @classmethod
    def load_default(cls) -> CapabilityRegistry:
        """Load the bundled YAML + optional user override.

        User file is resolved at ``~/.coderouter/model-capabilities.yaml``;
        missing = empty user layer. Bundled file is required — if it
        cannot be read, this raises ``RuntimeError`` (the package is
        broken). Both files are validated against
        ``CapabilityRegistryFile``; schema errors propagate as
        Pydantic ``ValidationError`` so the failure is visible at load.
        """
        bundled = cls._read_bundled_file()
        user = cls._read_user_file()
        return cls.from_rule_lists(user=user, bundled=bundled)

    @staticmethod
    def _read_bundled_file() -> list[CapabilityRule]:
        try:
            text = (
                resources.files(_BUNDLED_PACKAGE)
                .joinpath(_BUNDLED_NAME)
                .read_text(encoding="utf-8")
            )
        except (FileNotFoundError, ModuleNotFoundError) as exc:
            raise RuntimeError(
                "Bundled model-capabilities.yaml is missing from the "
                f"'{_BUNDLED_PACKAGE}' package — installation is "
                "incomplete or corrupted."
            ) from exc
        raw = yaml.safe_load(text) or {}
        return CapabilityRegistryFile.model_validate(raw).rules

    @staticmethod
    def _read_user_file(path: Path | None = None) -> list[CapabilityRule]:
        target = path or _USER_PATH
        if not target.is_file():
            return []
        raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
        return CapabilityRegistryFile.model_validate(raw).rules

    @classmethod
    def load_from_paths(
        cls,
        *,
        bundled_path: Path,
        user_path: Path | None = None,
    ) -> CapabilityRegistry:
        """Test-friendly loader that reads YAML from explicit paths.

        Production code uses :meth:`load_default`; this variant lets
        tests stage a custom bundled file alongside an optional user
        file without relying on the package data location.
        """
        bundled_raw = yaml.safe_load(bundled_path.read_text(encoding="utf-8")) or {}
        bundled = CapabilityRegistryFile.model_validate(bundled_raw).rules
        user = cls._read_user_file(user_path)
        return cls.from_rule_lists(user=user, bundled=bundled)


__all__ = [
    "CapabilityRegistry",
    "CapabilityRegistryFile",
    "CapabilityRule",
    "RegistryCapabilities",
    "ResolvedCapabilities",
]
