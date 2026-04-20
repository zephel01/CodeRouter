"""Capability gate for request-level block normalization (v0.5-A).

Purpose
    Claude Code sends requests that carry Anthropic-specific body fields
    (`thinking: {type: enabled}`, `cache_control: ...`) which only a subset
    of models accept. Hitting a non-supporting model returns a 400 like
    ``"adaptive thinking is not supported on this model"`` (v0.4-D retro).

    v0.5-A introduces a capability gate that:
      1. Declares per-provider support via ``Capabilities.thinking`` in
         ``providers.yaml`` (explicit — honored verbatim).
      2. Falls back to a model-name heuristic when unset. The heuristic
         covers the families we've verified accept the feature today; new
         families should be added here when Anthropic releases them.
      3. Lets the fallback engine prefer capable providers and silently
         strip the block when it has to hand off to a non-capable one,
         logging the degradation so operators can see it after the fact.

Design decisions
    - Pure functions, no I/O. Easy to unit test.
    - Heuristic lives in one place (this module) rather than scattered
      across adapters. Updates are a single edit.
    - ``strip_thinking`` returns a new ``AnthropicRequest`` instance (does
      not mutate) — fallback chains may revisit the original.
    - OpenAI-compat providers are always considered incapable, since the
      OpenAI wire format has no equivalent field and the existing
      ``to_chat_request`` translation already drops it on the way out.
"""

from __future__ import annotations

import re
from typing import Final

from coderouter.config.schemas import ProviderConfig
from coderouter.logging import (
    CapabilityDegradedPayload,
    CapabilityDegradedReason,
    log_capability_degraded,
)
from coderouter.translation.anthropic import AnthropicRequest

# Re-export the v0.5.1 log-shape contract so consumers that already think
# of it as a capability concept can import it from here. The canonical
# home is ``coderouter.logging`` — see that module's docstring for why
# (short version: avoids a routing ↔ adapter import cycle).
__all__ = [
    "CapabilityDegradedPayload",
    "CapabilityDegradedReason",
    "anthropic_request_has_cache_control",
    "anthropic_request_requires_thinking",
    "log_capability_degraded",
    "provider_supports_cache_control",
    "provider_supports_thinking",
    "strip_thinking",
]

# ---------------------------------------------------------------------------
# Heuristic: model families known to accept Anthropic's `thinking` body field.
#
# Verified 2026-04 against api.anthropic.com:
#   claude-sonnet-4-6       → accepts adaptive (`{type: enabled}` no budget)
#   claude-sonnet-4-5-*     → 400 "adaptive thinking is not supported"
#   claude-opus-4-*         → accepts (all 4.x opus)
#   claude-haiku-4-*        → accepts (all 4.x haiku)
#
# When Anthropic releases a new family that supports thinking, add its
# regex here. When a family is deprecated, leave it — the check is an
# allow-list, so stale patterns don't matter.
# ---------------------------------------------------------------------------

_THINKING_CAPABLE_PATTERNS: Final[tuple[str, ...]] = (
    r"^claude-opus-4-",          # all 4.x opus
    r"^claude-sonnet-4-6",       # 4.6 — first sonnet family to accept thinking
    r"^claude-sonnet-4-7",       # future 4.7 (forward-compat)
    r"^claude-haiku-4-",         # all 4.x haiku
)

_THINKING_CAPABLE_RE: Final[re.Pattern[str]] = re.compile(
    "|".join(_THINKING_CAPABLE_PATTERNS)
)


def provider_supports_thinking(provider: ProviderConfig) -> bool:
    """Does this provider accept ``thinking: {type: enabled}`` blocks?

    Resolution order:
        1. If ``provider.capabilities.thinking`` is True → True (explicit opt-in).
        2. Otherwise apply heuristic:
           - ``kind: openai_compat``: always False (no such wire field).
           - ``kind: anthropic``: True iff model name matches one of the
             known-capable regex families.

    Explicit ``thinking: false`` in YAML is indistinguishable from the
    default (both produce False); the heuristic only promotes to True. A
    user who wants to hard-disable thinking on a capable model can change
    the model to an incapable one, or set ``extra_body.thinking: null``
    at the provider level (not handled here — that's a future feature).
    """
    if provider.capabilities.thinking:
        return True
    if provider.kind != "anthropic":
        return False
    return bool(_THINKING_CAPABLE_RE.match(provider.model or ""))


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


def provider_supports_cache_control(provider: ProviderConfig) -> bool:
    """Does this provider preserve ``cache_control`` blocks end-to-end?

    Resolution order:
        1. If ``provider.capabilities.prompt_cache`` is True → True. This
           is an explicit opt-in for any future ``openai_compat``
           upstream that extends the wire format to preserve cache
           markers (not known to exist today, 2026-04).
        2. Otherwise:
           - ``kind: anthropic``: True. Native passthrough via
             ``/v1/messages`` keeps cache_control intact. Verified real-
             machine against api.anthropic.com on 2026-04-20 (v0.4
             retro §3: 1321 tokens written on call 1, 1321 read on call 2).
           - ``kind: openai_compat``: False. The OpenAI Chat Completions
             wire has no equivalent marker, so the existing
             ``to_chat_request`` translation drops cache_control during
             the Anthropic → OpenAI hop. The upstream itself might have
             prompt caching, but CodeRouter can't currently carry the
             marker through.

    This routine does not inspect the request — it's a per-provider
    capability. Combine with ``anthropic_request_has_cache_control`` in
    the engine to decide whether to log a degradation.
    """
    if provider.capabilities.prompt_cache:
        return True
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
