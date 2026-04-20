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
from coderouter.translation.anthropic import AnthropicRequest

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
