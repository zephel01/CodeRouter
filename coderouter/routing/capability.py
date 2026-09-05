"""Capability gate for request-level block normalization (v0.5-A, v0.7-A).

Purpose
    Claude Code sends requests that carry Anthropic-specific body fields
    (`thinking: {type: enabled}`, `cache_control: ...`) which only a subset
    of models accept. Hitting a non-supporting model returns a 400 like
    ``"adaptive thinking is not supported on this model"`` (v0.4-D retro).

    v0.5-A introduced a capability gate that:
      1. Declares per-provider support via ``Capabilities.thinking`` in
         ``providers.yaml`` (explicit — honored verbatim).
      2. Falls back to a declarative registry when unset (v0.7-A: was a
         Python-literal regex in v0.5-A). The bundled default registry
         at ``coderouter/data/model-capabilities.yaml`` encodes the
         families we've verified accept the feature; users can extend /
         override via ``~/.coderouter-t/model-capabilities.yaml``.
      3. Lets the fallback engine prefer capable providers and silently
         strip the block when it has to hand off to a non-capable one,
         logging the degradation so operators can see it after the fact.

Design decisions
    - Pure functions, no I/O at the gate level. The registry is a module-
      level lazy-loaded singleton (one disk read per process); tests can
      inject a custom registry via the ``registry=`` kwarg on each gate
      function.
    - Heuristic lives in YAML (v0.7-A) rather than scattered across
      adapters or baked into regex. Adding a new Anthropic family is a
      one-line YAML edit.
    - ``strip_thinking`` returns a new ``AnthropicRequest`` instance (does
      not mutate) — fallback chains may revisit the original.
    - OpenAI-compat providers are not rejected by a hardcoded ``kind``
      check anymore (v0.7-A); the registry simply does not declare any
      openai_compat rules for thinking, so the lookup returns
      ``thinking=None`` which the gate treats as False. The per-provider
      YAML escape hatch still lets users opt in explicitly.
"""

from __future__ import annotations

import logging
from typing import Any

from coderouter.config.capability_registry import (
    CapabilityRegistry,
    ResolvedCapabilities,
)
from coderouter.config.schemas import CodeRouterConfig, ProviderConfig
from coderouter.logging import (
    CapabilityDegradedPayload,
    CapabilityDegradedReason,
    log_capability_degraded,
    log_chain_claude_code_suitability_degraded,
)
from coderouter.translation.anthropic import AnthropicRequest

# Re-export the v0.5.1 log-shape contract so consumers that already think
# of it as a capability concept can import it from here. The canonical
# home is ``coderouter.logging`` — see that module's docstring for why
# (short version: avoids a routing ↔ adapter import cycle).
__all__ = [
    "CLAUDE_CODE_PROFILE_PREFIX",
    "CapabilityDegradedPayload",
    "CapabilityDegradedReason",
    "CapabilityRegistry",
    "ResolvedCapabilities",
    "anthropic_request_has_cache_control",
    "anthropic_request_has_forced_tool_choice",
    "anthropic_request_requires_thinking",
    "check_claude_code_chain_suitability",
    "emulate_tool_choice",
    "get_default_registry",
    "log_capability_degraded",
    "provider_supports_cache_control",
    "provider_supports_thinking",
    "provider_supports_tool_choice",
    "reset_default_registry",
    "strip_cache_control",
    "strip_thinking",
]

# ---------------------------------------------------------------------------
# Registry: declarative model-capabilities.yaml (v0.7-A)
#
# Loaded lazily once per process. Tests can inject a custom registry via
# the ``registry=`` kwarg on the gate functions, or call
# ``reset_default_registry()`` to force a reload (picks up a user YAML
# written in a test fixture). See ``coderouter.config.capability_registry``
# for the schema and lookup semantics.
# ---------------------------------------------------------------------------

_DEFAULT_REGISTRY: CapabilityRegistry | None = None


def get_default_registry() -> CapabilityRegistry:
    """Return the process-wide default capability registry.

    First call loads ``coderouter/data/model-capabilities.yaml`` +
    optional ``~/.coderouter-t/model-capabilities.yaml``; subsequent calls
    return the cached instance.
    """
    global _DEFAULT_REGISTRY
    if _DEFAULT_REGISTRY is None:
        _DEFAULT_REGISTRY = CapabilityRegistry.load_default()
    return _DEFAULT_REGISTRY


def reset_default_registry() -> None:
    """Forget the cached default registry; next lookup re-reads disk.

    Intended for tests that stage a user YAML and want the gate to pick
    it up. Production code never needs this.
    """
    global _DEFAULT_REGISTRY
    _DEFAULT_REGISTRY = None


def _resolve(
    provider: ProviderConfig,
    registry: CapabilityRegistry | None,
) -> ResolvedCapabilities:
    """Consult the registry for ``provider``. ``registry=None`` uses the default."""
    reg = registry if registry is not None else get_default_registry()
    return reg.lookup(kind=provider.kind, model=provider.model or "")


def provider_supports_thinking(
    provider: ProviderConfig,
    *,
    registry: CapabilityRegistry | None = None,
) -> bool:
    """Does this provider accept ``thinking: {type: enabled}`` blocks?

    Resolution order:
        1. If ``provider.capabilities.thinking`` is True → True (explicit
           per-provider opt-in from providers.yaml — highest precedence).
        2. Otherwise consult the registry via
           :func:`coderouter.config.capability_registry.CapabilityRegistry.lookup`.
           The registry returns ``thinking=True`` when any matching rule
           declares it, or ``None`` when no rule matches. ``None`` →
           treated as False (conservative default; capability gate then
           strips the block and logs degradation before the call).

    Explicit ``thinking: false`` in YAML is indistinguishable from the
    default (both produce False); the registry only promotes to True. A
    user who wants to hard-disable thinking on a registry-capable model
    can change the provider's model to one that isn't declared, or add a
    more-specific rule to ``~/.coderouter-t/model-capabilities.yaml`` that
    declares ``thinking: false`` earlier in the chain.

    The ``registry`` kwarg is for tests — production callers pass
    nothing and get the module-level default.
    """
    if provider.capabilities.thinking:
        return True
    return _resolve(provider, registry).thinking is True


def anthropic_request_requires_thinking(request: AnthropicRequest) -> bool:
    """True iff the request carries a ``thinking: {type: enabled, ...}`` block.

    The ``thinking`` field isn't declared on AnthropicRequest (it's a
    beta-evolving shape), so it arrives via Pydantic's ``extra="allow"``
    mechanism and is read from ``model_extra``.

    A disabled or absent block (``{type: disabled}``, ``None``, missing)
    returns False — gating only fires for actual requests that would
    trigger the upstream's extended-thinking mode.
    """
    extra = request.model_extra or {}
    thinking = extra.get("thinking")
    if not isinstance(thinking, dict):
        return False
    return thinking.get("type") == "enabled"


def strip_thinking(request: AnthropicRequest) -> AnthropicRequest:
    """Return a copy of ``request`` with the ``thinking`` field removed.

    No-op (returns a distinct-but-equivalent copy) when ``thinking`` is
    absent. Preserves the CodeRouter-internal ``profile`` and
    ``anthropic_beta`` fields since those are excluded from the body but
    still needed by the engine / adapter.

    The original request is not mutated — callers that iterate a fallback
    chain can keep the original around for retries against capable
    providers later in the chain (though the default chain ordering puts
    capable providers first, so this mostly matters for tests).
    """
    extra = request.model_extra or {}
    if "thinking" not in extra:
        # Still return a fresh copy for consistency with the mutation-free
        # contract. model_copy() preserves extras.
        return request.model_copy(deep=True)

    # model_dump() serializes extras; roundtripping via validate rebuilds a
    # clean instance without the dropped key. exclude=True fields (profile,
    # anthropic_beta) are omitted by model_dump, so we reassign them.
    dumped = request.model_dump()
    dumped.pop("thinking", None)
    stripped = AnthropicRequest.model_validate(dumped)
    stripped.profile = request.profile
    stripped.anthropic_beta = request.anthropic_beta
    return stripped


# ---------------------------------------------------------------------------
# v0.5-B: cache_control observability
#
# Unlike `thinking`, cache_control doesn't produce a 400 on non-supporting
# providers — it's silently lost during Anthropic → OpenAI translation
# (the cache_control marker lives on content blocks and has no OpenAI
# wire equivalent). So the gate here is observability-only: we detect
# when cache_control is present, check whether the outgoing provider can
# honor it, and emit a structured log when it's about to be lost. We do
# NOT reorder the chain — the user's ordering almost certainly reflects
# a latency / cost intent that outweighs cache-hit savings.
#
# Footgun to be aware of (from the v0.4 retro §What was sharp):
#   Anthropic's prompt cache has a 1024-token minimum. System prompts
#   shorter than that silently report 0 cached tokens even on supported
#   providers. That's an Anthropic-side constraint, not something this
#   gate can fix — but it's worth noting here so nobody blames CodeRouter
#   for 0 hits on small prompts.
# ---------------------------------------------------------------------------


def provider_supports_cache_control(
    provider: ProviderConfig,
    *,
    registry: CapabilityRegistry | None = None,
) -> bool:
    """Does this provider preserve ``cache_control`` blocks end-to-end?

    Resolution order (v1.9-B extended):
        1. If ``provider.capabilities.prompt_cache`` is True → True
           (explicit per-provider opt-in from providers.yaml — highest
           precedence; a deployment opting a future upstream in by hand).
        2. Consult the capability registry for an explicit
           ``cache_control: true|false`` declaration on this
           ``(kind, model)``. v1.9-B ships these defaults:
              - ``claude-sonnet-*`` / ``claude-opus-*`` (kind=anthropic): True
              - ``qwen3.5-*`` / ``qwen3.6-*`` on LM Studio (kind=anthropic
                with port 1234): True (verified live in v1.8.4,
                ``cache_read_input_tokens: 280`` observed end-to-end)
              - other openai_compat models: undeclared (= None)
           A registry value of ``False`` hard-disables the capability
           even on a provider whose ``kind`` would normally pass — useful
           when an upstream regresses and the operator wants the
           ``capability-degraded`` log to fire during the regression.
        3. Fall back to the original heuristic:
              - ``kind: anthropic`` → True (native ``/v1/messages``
                passthrough; verified against api.anthropic.com on
                2026-04-20)
              - ``kind: openai_compat`` → False (the OpenAI Chat
                Completions wire has no equivalent marker; the existing
                ``to_chat_request`` translation drops it).

    This routine does not inspect the request — it's a per-provider
    capability. Combine with ``anthropic_request_has_cache_control`` in
    the engine to decide whether to log a degradation. The ``registry``
    kwarg is for tests; production callers pass nothing and get the
    module-level default.
    """
    if provider.capabilities.prompt_cache:
        return True
    resolved = _resolve(provider, registry)
    if resolved.cache_control is True:
        return True
    if resolved.cache_control is False:
        return False
    return provider.kind == "anthropic"


def _block_has_cache_control(block: object) -> bool:
    """True if ``block`` is a dict that carries a ``cache_control`` key."""
    return isinstance(block, dict) and "cache_control" in block


def anthropic_request_has_cache_control(request: AnthropicRequest) -> bool:
    """True iff the request carries any ``cache_control`` markers.

    Checks all three locations Anthropic allows:
        - ``system`` as a list of blocks (each block may have
          ``cache_control``; the shorthand ``str`` form cannot).
        - ``tools[*]`` as Anthropic tools — ``cache_control`` arrives via
          Pydantic's ``extra="allow"`` on ``AnthropicTool``.
        - ``messages[*].content`` when it's a list of blocks (the
          shorthand ``str`` form, again, cannot carry the marker).

    A single cache_control marker anywhere in the request returns True.
    """
    # system blocks
    system = request.system
    if isinstance(system, list):
        for block in system:
            if _block_has_cache_control(block):
                return True

    # tool definitions
    for tool in request.tools or []:
        extra = tool.model_extra or {}
        if "cache_control" in extra:
            return True

    # message content blocks
    for msg in request.messages:
        content = msg.content
        if isinstance(content, list):
            for block in content:
                if _block_has_cache_control(block):
                    return True

    return False


# ---------------------------------------------------------------------------
# S2 (shim): tool_choice capability gate + emulation
#
# Anthropic's ``tool_choice`` forces the model to call a tool: ``{type:
# "any"}`` (call *some* provided tool) or ``{type: "tool", name: "X"}``
# (call the named tool). Native Anthropic honors this on the wire; most
# openai_compat backends silently ignore it after Anthropic → OpenAI
# translation, so the model can answer with plain text where the client
# expected a tool call. The gate here answers "does this provider honor a
# forced tool_choice?" with the same 3-tier resolution as the other v0.5
# gates. The fallback engine's ``tool_choice_action`` uses the answer to
# either warn or emulate the forcing via a system-prompt directive.
#
# ``auto`` / ``none`` / absent tool_choice never trips the gate — only the
# forcing modes matter (a non-forced choice loses nothing meaningful in
# translation).
# ---------------------------------------------------------------------------


def provider_supports_tool_choice(
    provider: ProviderConfig,
    *,
    registry: CapabilityRegistry | None = None,
) -> bool:
    """Does this provider honor a forced Anthropic ``tool_choice``?

    Resolution order (mirrors :func:`provider_supports_cache_control`):
        1. If ``provider.capabilities.tool_choice`` is True → True
           (explicit per-provider opt-in from providers.yaml — highest
           precedence).
        2. Consult the capability registry for an explicit
           ``tool_choice: true|false`` declaration on this
           ``(kind, model)``. A registry value of ``False`` hard-disables
           the capability even on a provider whose ``kind`` would normally
           pass.
        3. Fall back to the heuristic ``kind == "anthropic"`` — native
           ``/v1/messages`` passthrough forwards ``tool_choice`` verbatim,
           whereas openai_compat translation drops the forcing semantics.

    This routine does not inspect the request — it's a per-provider
    capability. Combine with :func:`anthropic_request_has_forced_tool_choice`
    in the engine to decide whether to warn / emulate. The ``registry``
    kwarg is for tests; production callers pass nothing.
    """
    if provider.capabilities.tool_choice:
        return True
    resolved = _resolve(provider, registry)
    if resolved.tool_choice is True:
        return True
    if resolved.tool_choice is False:
        return False
    return provider.kind == "anthropic"


def anthropic_request_has_forced_tool_choice(request: AnthropicRequest) -> bool:
    """True iff the request forces a tool call via ``tool_choice``.

    Anthropic's ``tool_choice.type`` is one of ``auto`` / ``none`` /
    ``any`` / ``tool``. Only ``any`` (force *some* tool) and ``tool``
    (force the named tool) are "forcing" modes that a non-supporting
    backend would silently ignore. ``auto`` / ``none`` / a missing field
    return False — there is nothing to emulate.
    """
    choice = request.tool_choice
    if not isinstance(choice, dict):
        return False
    return choice.get("type") in ("any", "tool")


# English directive templates injected into the system prompt when
# emulating a forced tool_choice on a non-supporting backend. Kept as
# module constants so tests can assert the exact text and operators can
# grep for it.
_EMULATE_TOOL_DIRECTIVE = (
    '\n\nIMPORTANT: You MUST respond by calling the tool named "{name}". '
    "Do not respond with plain text."
)
_EMULATE_ANY_DIRECTIVE = (
    "\n\nIMPORTANT: You MUST respond by calling one of the provided tools. "
    "Do not respond with plain text."
)


def _inject_system_directive(system: Any, directive: str) -> str | list[dict[str, Any]]:
    """Append ``directive`` to an Anthropic ``system`` field.

    Handles both wire shapes:
        - ``str`` (short form) → returns ``system + directive`` (a new
          ``str``; ``None`` is treated as empty).
        - ``list[dict]`` (block form) → returns a new list with the
          directive appended as a trailing ``text`` block, so existing
          blocks (including any ``cache_control`` markers) are untouched.
    """
    if system is None:
        return directive
    if isinstance(system, str):
        return system + directive
    if isinstance(system, list):
        return [*system, {"type": "text", "text": directive}]
    # Unexpected shape — fall back to a fresh string so we never lose the
    # forcing directive (the original object is left untouched upstream).
    return directive


def emulate_tool_choice(request: AnthropicRequest) -> AnthropicRequest:
    """Return a copy of ``request`` with tool_choice emulated in the prompt.

    Used by the fallback engine's ``tool_choice_action: "emulate"`` path.
    The returned request has (1) its ``tool_choice`` field removed and (2)
    an English directive appended to ``system`` instructing the model to
    call the requested tool. The directive text differs for ``type ==
    "tool"`` (names the tool) vs ``type == "any"`` (any provided tool).

    The original request is NOT mutated — the engine iterates a fallback
    chain and a later capable provider must still receive the real
    ``tool_choice``. Like :func:`strip_thinking`, the CodeRouter-internal
    ``profile`` / ``anthropic_beta`` fields are preserved.

    No-op-safe: when the request carries no forced tool_choice this still
    returns a deep copy (callers gate on
    :func:`anthropic_request_has_forced_tool_choice` first, so this is
    just defensive).
    """
    choice = request.tool_choice if isinstance(request.tool_choice, dict) else {}
    ctype = choice.get("type")
    if ctype == "tool":
        name = choice.get("name", "")
        directive = _EMULATE_TOOL_DIRECTIVE.format(name=name)
    else:  # "any" (or defensive fallback)
        directive = _EMULATE_ANY_DIRECTIVE

    dumped = request.model_dump()
    dumped.pop("tool_choice", None)
    dumped["system"] = _inject_system_directive(request.system, directive)
    emulated = AnthropicRequest.model_validate(dumped)
    emulated.profile = request.profile
    emulated.anthropic_beta = request.anthropic_beta
    return emulated


# ---------------------------------------------------------------------------
# S3 (shim): cache_control strip
#
# The v0.5-B gate (above) is observability-only — it logs that cache_control
# will be lost in translation but never mutates the request, because most
# openai_compat backends simply ignore the unknown key. Some strict backends
# 400 instead. ``strip_cache_control`` proactively removes every
# ``cache_control`` key from a deep copy of the request so the marker never
# reaches such a backend. Returns ``(request, markers_removed)`` so the
# engine can log the count. The original request is not mutated (a later
# capable provider must still receive the markers).
# ---------------------------------------------------------------------------


def _strip_cache_control_from_blocks(blocks: Any) -> int:
    """Remove ``cache_control`` from each dict block in ``blocks`` in place.

    Returns the number of keys removed. Non-list / non-dict entries are
    ignored. Mutates the dicts in the given list (the caller passes a deep
    copy's containers).
    """
    removed = 0
    if not isinstance(blocks, list):
        return 0
    for block in blocks:
        if isinstance(block, dict) and "cache_control" in block:
            del block["cache_control"]
            removed += 1
    return removed


def strip_cache_control(request: AnthropicRequest) -> tuple[AnthropicRequest, int]:
    """Return ``(copy, markers_removed)`` with all cache_control keys gone.

    Walks the same three locations as
    :func:`anthropic_request_has_cache_control` — ``system`` blocks,
    ``tools[*]`` (where the marker rides via ``extra="allow"``), and each
    list-form ``messages[*].content`` — and removes every ``cache_control``
    key from a deep copy. The original request is untouched so the fallback
    chain can hand the real markers to a later capable provider.

    ``markers_removed`` is the total count across all three locations,
    surfaced by the engine in the ``cache-control-stripped`` log. When the
    request carried no markers this returns ``(copy, 0)``.
    """
    dumped = request.model_dump()
    removed = 0

    # system blocks (list form only — the str short form can't carry it).
    removed += _strip_cache_control_from_blocks(dumped.get("system"))

    # tool definitions — cache_control arrives via extra="allow".
    tools = dumped.get("tools")
    if isinstance(tools, list):
        for tool in tools:
            if isinstance(tool, dict) and "cache_control" in tool:
                del tool["cache_control"]
                removed += 1

    # message content blocks (list form only).
    messages = dumped.get("messages")
    if isinstance(messages, list):
        for msg in messages:
            if isinstance(msg, dict):
                removed += _strip_cache_control_from_blocks(msg.get("content"))

    stripped = AnthropicRequest.model_validate(dumped)
    stripped.profile = request.profile
    stripped.anthropic_beta = request.anthropic_beta
    return stripped, removed


# ---------------------------------------------------------------------------
# v1.7-B: claude_code_suitability startup check
#
# Motivation (plan.md §11.B.4 #2):
#   v1.6.2 documented in docs/guides/troubleshooting.md §4-1 the "Llama-3.3-70B
#   over-eagerly invokes Skill() for small talk under Claude Code"
#   symptom. v1.7-B promotes that hint from prose-only to a structured
#   automatic startup WARN: at app startup we scan every profile whose
#   name starts with ``claude-code`` and emit ONE warn per profile that
#   contains a provider whose registry-resolved
#   ``claude_code_suitability == "degraded"``.
#
# Design notes
#   - Profile-name prefix gate, not request-time. Iterating all
#     ``claude-code-*`` profiles at startup means the operator sees the
#     warning regardless of whether they're currently routing to Claude
#     Code or another mode (the chain might be activated later via
#     ``X-CodeRouter-Mode``).
#   - One log per profile (not one per provider). The payload carries the
#     full list of degraded provider/model pairs so a single grep returns
#     the actionable set.
#   - Returns a structured list so callers (tests, future ``coderouter
#     doctor`` subcommands) can introspect the result without parsing
#     log lines.
# ---------------------------------------------------------------------------


CLAUDE_CODE_PROFILE_PREFIX: str = "claude-code"
"""Profile-name prefix that triggers the suitability check at startup.

Case-sensitive prefix match against ``FallbackChain.name``. Covers the
canonical names used in ``examples/providers.nvidia-nim.yaml``
(``claude-code-nim``) and ``examples/providers.yaml``
(``claude-code-local``); operators with custom profile names that should
trigger the same check should adopt the prefix or override per-provider
in ``~/.coderouter-t/model-capabilities.yaml`` (declaring
``claude_code_suitability: ok`` to opt out of the warn).
"""


def check_claude_code_chain_suitability(
    config: CodeRouterConfig,
    *,
    logger: logging.Logger,
    registry: CapabilityRegistry | None = None,
) -> dict[str, list[tuple[str, str]]]:
    """Scan ``claude-code-*`` profiles and warn about degraded providers.

    Walks every profile whose name starts with
    :data:`CLAUDE_CODE_PROFILE_PREFIX`. For each such profile, looks up
    every provider in the chain in the capability registry; collects the
    ``(provider_name, model)`` pairs whose
    ``claude_code_suitability`` resolved to ``"degraded"``; and emits ONE
    ``chain-claude-code-suitability-degraded`` warn per profile via
    :func:`coderouter.logging.log_chain_claude_code_suitability_degraded`.

    Profiles with zero degraded entries stay quiet. A profile that
    references a provider name not declared in ``providers`` is skipped
    silently here — the existing ``FallbackEngine`` validation will
    catch that on first request with a clearer error.

    The return value is a dict mapping ``profile_name`` →
    ``[(provider_name, model), …]`` for every profile that had at least
    one degraded entry. Empty dict means "no warnings emitted, all
    claude-code chains look clean (or there are no such chains)". Tests
    use this to assert the gate fires (or doesn't) without grepping
    logs.

    The ``registry`` kwarg is for tests — production callers (the
    FastAPI lifespan in ``ingress.app``) pass nothing and get the
    module-level default. The ``logger`` is required: callers pass the
    logger of the module that should appear in the JSON-line ``logger``
    field, matching the existing ``capability-degraded`` /
    ``chain-paid-gate-blocked`` patterns.
    """
    reg = registry if registry is not None else get_default_registry()

    # Cheap O(N) name → ProviderConfig lookup; profiles reference providers
    # by name only. Skipping unknown names mirrors what FallbackEngine
    # would do (the v0.6 startup validators already check structural
    # integrity, so the lookup miss path here is purely defensive).
    by_name: dict[str, ProviderConfig] = {p.name: p for p in config.providers}

    flagged: dict[str, list[tuple[str, str]]] = {}
    for profile in config.profiles:
        if not profile.name.startswith(CLAUDE_CODE_PROFILE_PREFIX):
            continue
        degraded: list[tuple[str, str]] = []
        for provider_name in profile.providers:
            provider = by_name.get(provider_name)
            if provider is None:
                continue  # FallbackEngine validation will surface this
            resolved = reg.lookup(kind=provider.kind, model=provider.model or "")
            if resolved.claude_code_suitability == "degraded":
                degraded.append((provider.name, provider.model or ""))
        if not degraded:
            continue
        flagged[profile.name] = degraded
        log_chain_claude_code_suitability_degraded(
            logger,
            profile=profile.name,
            degraded_providers=[p for p, _ in degraded],
            degraded_models=[m for _, m in degraded],
        )
    return flagged
