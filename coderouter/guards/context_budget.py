"""Context budget guard (v2.0-F, L1).

Long-running agent sessions (Claude Code, Cline, OpenClaw, etc.)
accumulate messages that eventually exceed the target model's context
window. Without intervention, the backend returns a 400 error
(Anthropic: ``max_tokens`` violation) or silently truncates the
prompt (Ollama), killing the agent session.

This module provides the engine two pieces:

  1. A **stateless estimator** :func:`estimate_context_usage` that
     computes the approximate context-window fill ratio for a given
     Anthropic request against a declared ``max_context_tokens``.
     Pure function, no I/O.
  2. A **stateless trimmer** :func:`trim_to_budget` that returns a
     new request with old messages removed until the estimated usage
     drops below a target ratio. Pure function, no mutation of the
     input.

Integration with the fallback engine
=====================================

The engine calls these at the ``_apply_context_budget_guard`` site —
**after** tool-loop detection but **before** chain dispatch. The
guard reads the resolved profile's ``context_budget_action`` field:

  * ``off``  — guard is a no-op (default).
  * ``warn`` — compute estimate; if over warn threshold, emit a
               structured log + attach a response header.
  * ``trim`` — ``warn`` behavior + if over trim threshold, call
               :func:`trim_to_budget` and return the shortened
               request to the engine.

Token estimation
================

Uses the shared :func:`~coderouter.token_estimation.estimate_tokens_from_anthropic_request`
(char/4 heuristic, 5-deps invariant). See that module's docstring
for the CJK caveat and recommended threshold compensation.

Since H-5 the estimator also counts ``tool_result`` / ``tool_use`` /
``thinking`` blocks, so a Claude-Code-shaped session now reports a
number 5-29x larger than it did in v2.11.x — i.e. the guard finally
fires on the sessions it was written for.

Trim algorithm
==============

  1. Always preserve the system prompt (not counted toward removal).
  2. Always preserve the last ``preserve_last_n`` messages.
  3. Remove messages from the front (oldest first).
  4. Preserve tool_use / tool_result pairs atomically — if a kept
     message contains a ``tool_result``, also keep the preceding
     ``tool_use`` assistant message (and vice versa).
  5. After removal, re-estimate; if still over ``trim_target``,
     reduce ``preserve_last_n`` by 1 and retry (minimum floor: 2).
  6. The result is **never** an empty message list — see
     :func:`_normalize_head`.

Cost
====

The incremental loop pre-computes each message's character count once
and subtracts as units are dropped, so a trim is O(total chars) rather
than O(messages x total chars). Before that change an 800-message trim
blocked the event loop for ~0.2s.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from coderouter.token_estimation import (
    chars_to_tokens,
    count_message_chars,
    count_system_chars,
    estimate_tokens_from_anthropic_request,
)

if TYPE_CHECKING:
    from coderouter.translation.anthropic import AnthropicRequest


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


class ContextBudgetRejectError(Exception):
    """v2.16.0: ``context_budget_action: reject`` refused an oversized request.

    The ingress answers with Anthropic's overflow error (HTTP 400,
    ``prompt is too long: ...``) so the client compacts the conversation
    itself. Carries the numbers for the message and the log line.
    """

    def __init__(self, *, estimated_tokens: int, max_context_tokens: int, profile: str) -> None:
        self.estimated_tokens = estimated_tokens
        self.max_context_tokens = max_context_tokens
        self.profile = profile
        super().__init__(
            f"prompt is too long: ~{estimated_tokens} tokens > "
            f"{max_context_tokens} maximum (CodeRouter profile {profile!r}, "
            "estimated)"
        )


@dataclass(frozen=True, slots=True)
class ContextBudgetEstimate:
    """Result of a context-budget estimation check."""

    #: Estimated token count for the full request (system + messages).
    estimated_tokens: int
    #: Declared maximum context window for the target provider.
    max_context_tokens: int
    #: Ratio: estimated_tokens / max_context_tokens (0.0 to ∞).
    usage_ratio: float
    #: True when usage_ratio >= the profile's warn threshold.
    over_warn_threshold: bool
    #: True when usage_ratio >= the profile's trim threshold.
    over_trim_threshold: bool


@dataclass(frozen=True, slots=True)
class TrimResult:
    """Metadata about a trim operation (for logging)."""

    #: Number of messages before trim.
    messages_before: int
    #: Number of messages after trim.
    messages_after: int
    #: Number of messages removed.
    messages_removed: int
    #: Estimated tokens before trim.
    estimated_tokens_before: int
    #: Estimated tokens after trim.
    estimated_tokens_after: int


# ---------------------------------------------------------------------------
# Public API: estimation
# ---------------------------------------------------------------------------


def estimate_context_usage(
    request: AnthropicRequest,
    *,
    max_context_tokens: int,
    warn_threshold: float = 0.80,
    trim_threshold: float = 0.90,
) -> ContextBudgetEstimate:
    """Estimate how full the target provider's context window is.

    Pure function. Does not mutate the request. Returns a
    :class:`ContextBudgetEstimate` with precomputed threshold booleans
    so callers can branch without re-computing ratios.

    Parameters
    ----------
    request
        The inbound Anthropic request to evaluate.
    max_context_tokens
        Declared context window of the target provider (from
        ProviderConfig.max_context_tokens, registry, or fallback 128K).
    warn_threshold
        Ratio at or above which ``over_warn_threshold`` is True.
    trim_threshold
        Ratio at or above which ``over_trim_threshold`` is True.
    """
    estimated = estimate_tokens_from_anthropic_request(
        system=request.system,
        messages=request.messages,
    )
    ratio = estimated / max_context_tokens if max_context_tokens > 0 else 0.0
    return ContextBudgetEstimate(
        estimated_tokens=estimated,
        max_context_tokens=max_context_tokens,
        usage_ratio=ratio,
        over_warn_threshold=ratio >= warn_threshold,
        over_trim_threshold=ratio >= trim_threshold,
    )


# ---------------------------------------------------------------------------
# Public API: trimming
# ---------------------------------------------------------------------------


def trim_to_budget(
    request: AnthropicRequest,
    *,
    max_context_tokens: int,
    trim_target: float = 0.75,
    preserve_last_n: int = 4,
) -> tuple[AnthropicRequest, TrimResult]:
    """Return a new request with old messages removed to fit the budget.

    Pure function — does NOT mutate the input request.

    Algorithm:
      1. Compute target token count = max_context_tokens * trim_target.
      2. Identify messages that MUST be preserved:
         - Last ``preserve_last_n`` messages.
         - Any tool_use / tool_result pairs linked to preserved messages.
      3. Remove messages from the front until estimated tokens ≤ target.
      4. If still over target after removing all removable messages,
         reduce preserve_last_n by 1 and retry (floor: 2 messages).

    Returns
    -------
    tuple[AnthropicRequest, TrimResult]
        The trimmed request (new instance) and metadata about the trim.
    """
    messages = list(request.messages)
    estimated_before = estimate_tokens_from_anthropic_request(
        system=request.system,
        messages=messages,
    )
    target_tokens = int(max_context_tokens * trim_target)
    effective_preserve = min(preserve_last_n, len(messages))

    # Iteratively trim until under target or preserve floor reached
    trimmed_messages = _do_trim(
        messages=messages,
        system=request.system,
        target_tokens=target_tokens,
        preserve_last_n=effective_preserve,
    )

    estimated_after = estimate_tokens_from_anthropic_request(
        system=request.system,
        messages=trimmed_messages,
    )

    result = TrimResult(
        messages_before=len(messages),
        messages_after=len(trimmed_messages),
        messages_removed=len(messages) - len(trimmed_messages),
        estimated_tokens_before=estimated_before,
        estimated_tokens_after=estimated_after,
    )

    # Build new request with trimmed messages.
    # Import here to avoid circular import at module level.
    from coderouter.translation.anthropic import AnthropicMessage

    new_request = request.model_copy(
        update={"messages": [AnthropicMessage(**_msg_to_dict(m)) for m in trimmed_messages]},
    )
    return new_request, result


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _msg_to_dict(msg: Any) -> dict[str, Any]:
    """Convert an AnthropicMessage (or dict) to a plain dict for reconstruction."""
    if hasattr(msg, "model_dump"):
        return msg.model_dump()
    if isinstance(msg, dict):
        return msg
    return {"role": "user", "content": ""}


def _get_content(msg: Any) -> Any:
    """Extract the content field from a message (Pydantic model or dict)."""
    if hasattr(msg, "content"):
        return msg.content
    if isinstance(msg, dict):
        return msg.get("content")
    return None


def _extract_tool_use_ids(msg: Any) -> set[str]:
    """Extract all tool_use IDs from a message's content blocks."""
    content = _get_content(msg)
    ids: set[str] = set()
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                tid = block.get("id")
                if isinstance(tid, str):
                    ids.add(tid)
    return ids


def _extract_tool_result_ids(msg: Any) -> set[str]:
    """Extract all tool_use_ids referenced by tool_result blocks."""
    content = _get_content(msg)
    ids: set[str] = set()
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                tid = block.get("tool_use_id")
                if isinstance(tid, str):
                    ids.add(tid)
    return ids


def _has_tool_use(msg: Any) -> bool:
    """True if the message contains a tool_use content block."""
    return len(_extract_tool_use_ids(msg)) > 0


def _has_tool_result(msg: Any) -> bool:
    """True if the message contains a tool_result content block."""
    return len(_extract_tool_result_ids(msg)) > 0


def _compute_preserve_set(
    messages: list[Any],
    preserve_last_n: int,
) -> set[int]:
    """Compute indices of messages that must be preserved.

    Preserves:
      - The last ``preserve_last_n`` messages.
      - tool_use/tool_result pair integrity via ``tool_use_id`` matching:
        if a preserved message has a tool_result referencing an ID, also
        preserve the message (anywhere in the conversation) that emitted
        the matching tool_use. Conversely, if a preserved message has a
        tool_use, also preserve the message carrying the matching
        tool_result. This handles multi-tool calls (one assistant message
        with N tool_use blocks, one user message with N tool_results) and
        non-adjacent pairs.

    The algorithm iterates until stable (fixpoint), handling chains of
    dependencies (e.g., preserving a tool_result pulls in its tool_use
    assistant message, which might have another tool_use whose tool_result
    also needs preserving).
    """
    n = len(messages)
    if preserve_last_n >= n:
        return set(range(n))

    # Pre-compute tool ID mappings for efficient lookup
    # tool_use_id → index of the message containing that tool_use
    tool_use_index: dict[str, int] = {}
    # tool_use_id → index of the message containing the matching tool_result
    tool_result_index: dict[str, int] = {}

    for i, msg in enumerate(messages):
        for tid in _extract_tool_use_ids(msg):
            tool_use_index[tid] = i
        for tid in _extract_tool_result_ids(msg):
            tool_result_index[tid] = i

    preserved: set[int] = set(range(n - preserve_last_n, n))

    # Expand to cover tool pairs via ID matching — iterate until stable
    changed = True
    while changed:
        changed = False
        for idx in list(preserved):
            msg = messages[idx]

            # If this message has tool_results, preserve the messages
            # that contain the matching tool_use blocks
            for tid in _extract_tool_result_ids(msg):
                use_idx = tool_use_index.get(tid)
                if use_idx is not None and use_idx not in preserved:
                    preserved.add(use_idx)
                    changed = True

            # If this message has tool_use blocks, preserve the messages
            # that contain the matching tool_results
            for tid in _extract_tool_use_ids(msg):
                result_idx = tool_result_index.get(tid)
                if result_idx is not None and result_idx not in preserved:
                    preserved.add(result_idx)
                    changed = True

    return preserved


def _group_removable_units(
    messages: list[Any],
    removable: list[int],
) -> list[list[int]]:
    """Group removable message indices into atomic tool-pair units.

    ``removable`` is the sorted list of indices eligible for removal
    (i.e. not in the preserve set). A tool_use assistant message and the
    user message carrying its matching tool_result must be removed
    together so we never leave an orphaned tool_use or tool_result in the
    trimmed history (Anthropic rejects both with a 400). Because the
    preserve-set computation already keeps pairs atomic, both halves of a
    pair are always either both preserved or both removable — here we just
    coalesce the removable halves into a single unit so the incremental
    loop drops them in one step.

    Returns a list of units (each a sorted list of indices), ordered by
    the position of the unit's earliest message so the caller can peel
    them off the front oldest-first.
    """
    removable_set = set(removable)

    # Map every tool_use_id to the indices of the message(s) that emit the
    # tool_use and the message that carries the matching tool_result.
    partner: dict[int, set[int]] = {idx: set() for idx in removable}
    use_index: dict[str, int] = {}
    result_index: dict[str, int] = {}
    for idx in removable:
        for tid in _extract_tool_use_ids(messages[idx]):
            use_index[tid] = idx
        for tid in _extract_tool_result_ids(messages[idx]):
            result_index[tid] = idx
    for tid, use_idx in use_index.items():
        res_idx = result_index.get(tid)
        if res_idx is not None and res_idx in removable_set:
            partner[use_idx].add(res_idx)
            partner[res_idx].add(use_idx)

    # Union-find style grouping over the partner graph.
    seen: set[int] = set()
    units: list[list[int]] = []
    for idx in removable:
        if idx in seen:
            continue
        stack = [idx]
        group: set[int] = set()
        while stack:
            cur = stack.pop()
            if cur in group:
                continue
            group.add(cur)
            seen.add(cur)
            stack.extend(p for p in partner[cur] if p not in group)
        units.append(sorted(group))

    units.sort(key=lambda unit: unit[0])
    return units


def _role_of(msg: Any) -> Any:
    """Role of a message (Pydantic model or dict)."""
    if hasattr(msg, "role"):
        return msg.role
    if isinstance(msg, dict):
        return msg.get("role")
    return None


def _is_clean_user(msg: Any) -> bool:
    """True for a ``user`` message that does not open with a tool_result."""
    return _role_of(msg) == "user" and not _has_tool_result(msg)


#: Content of the synthetic head inserted when no real clean ``user``
#: message can be re-attached within budget. Short on purpose (~17
#: tokens): its only job is to give Anthropic the valid opening turn the
#: wire format demands while telling the model why history is missing.
TRIM_PLACEHOLDER_HEAD_TEXT = "[earlier conversation trimmed to fit the context budget]"


def _synthetic_head() -> dict[str, Any]:
    """A minimal, always-affordable clean ``user`` message."""
    return {"role": "user", "content": TRIM_PLACEHOLDER_HEAD_TEXT}


def _normalize_head(
    messages: list[Any],
    *,
    fallback_head: Any | None = None,
) -> list[Any]:
    """Drop leading messages until the first is a clean ``user`` message.

    Anthropic requires the first message to be a ``user`` message, and a
    leading user message must not open with a dangling ``tool_result``
    (its ``tool_use`` was trimmed away). We therefore drop from the front
    any assistant message and any user message that contains a
    tool_result, stopping at the first ``user`` message with no
    tool_result block.

    **Never returns an empty list for a non-empty input.** In a
    tool-heavy agent session the preserved tail can legitimately look
    like ``[assistant(tool_use), user(tool_result), assistant(tool_use),
    user(tool_result)]`` — every single message matches the drop
    condition, and the naive loop returned ``[]``. ``AnthropicRequest``
    declares no ``min_length`` on ``messages``, so pydantic happily
    forwarded that and the upstream API answered 400, destroying the
    session the guard exists to protect. (Latent before H-5 only
    because ``tool_result`` blocks estimated to 0 chars, so trim never
    fired on exactly those conversations.)

    When no surviving message can serve as the head, ``fallback_head``
    is prepended. The caller picks it under the token budget — see
    :func:`_pick_fallback_head`; this function deliberately does no
    budget arithmetic of its own.
    """
    if not messages:
        return list(messages)

    for start, msg in enumerate(messages):
        if _is_clean_user(msg):
            return messages[start:]

    # Every surviving message is an assistant turn or a tool_result-
    # carrying user turn: normalizing by dropping would empty the list.
    if fallback_head is not None:
        return [fallback_head, *messages]
    return list(messages)


def _pick_fallback_head(
    messages: list[Any],
    msg_chars: list[int],
    dropped: set[int],
    *,
    before: int,
    kept_chars: int,
    target_tokens: int,
) -> Any:
    """Choose a head message that keeps the result inside the budget.

    Called only when nothing in the surviving window is a clean ``user``
    message, so *something* has to be put in front or the request is a
    guaranteed 400.

    Selection order:

    1. The **most recent** dropped clean ``user`` message that still
       fits under ``target_tokens`` — recency matters because it is
       usually the operator's latest standing instruction.
    2. Failing that, a synthetic placeholder (:func:`_synthetic_head`).

    The budget check is the whole point. Re-attaching the *newest*
    candidate unconditionally is a trap: the most recent clean user
    message in an agent session is very often a huge pasted file, and
    restoring it undoes the entire trim. Measured on the canonical
    "200 KB paste then 25 tool calls" shape, that path turned a
    67,066-token request into a 67,061-token request against a 24,576
    target — reported as ``status="trimmed"`` and then rejected
    upstream. A budget-blind restore does not avoid the 400, it
    guarantees it.
    """
    for idx in sorted(dropped, reverse=True):
        if idx >= before:
            continue
        if not _is_clean_user(messages[idx]):
            continue
        if chars_to_tokens(kept_chars + msg_chars[idx]) <= target_tokens:
            return messages[idx]
    return _synthetic_head()


def _do_trim(
    messages: list[Any],
    system: Any,
    target_tokens: int,
    preserve_last_n: int,
) -> list[Any]:
    """Incrementally drop oldest messages until under ``target_tokens``.

    Unlike the previous implementation (which deleted every non-preserved
    message in one shot the moment the threshold was crossed), this peels
    messages off the front one atomic unit at a time and re-estimates
    after each removal, stopping as soon as the estimate is at or below
    the target. The preserve set (last N messages + their tool pairs) is a
    hard floor that is never removed. After trimming, the head is
    normalized so the first surviving message is a ``user`` message
    without a leading ``tool_result`` (avoids upstream 400s). Bug H5.

    The loop is incremental in *characters*: every message's character
    contribution is computed exactly once up front and subtracted as
    units are dropped, instead of rebuilding the kept list and
    re-walking the whole conversation on every iteration (the old
    O(messages x chars) shape cost ~0.2s of event-loop time for an
    800-message session). Because the char/4 heuristic floor-divides
    the grand total exactly once, the running sum stays bit-identical
    to a full recompute.
    """
    if not messages:
        return messages

    system_chars = count_system_chars(system)
    msg_chars = [count_message_chars(m) for m in messages]
    total_chars = system_chars + sum(msg_chars)
    if chars_to_tokens(total_chars) <= target_tokens:
        # Nothing to do — return the input unchanged.
        return messages

    preserved_indices = _compute_preserve_set(messages, preserve_last_n)
    removable = [i for i in range(len(messages)) if i not in preserved_indices]
    units = _group_removable_units(messages, removable)

    # Indices we have decided to drop; peel atomic units from the front.
    dropped: set[int] = set()
    for unit in units:
        if chars_to_tokens(total_chars) <= target_tokens:
            break
        unit_chars = sum(msg_chars[i] for i in unit)
        if unit_chars <= 0:
            # Dropping this unit would not move the estimate at all
            # (e.g. an image-only turn, which counts as 0 chars by
            # design). Destroying history for no budget gain is pure
            # loss — skip it and try the next unit.
            continue
        dropped.update(unit)
        total_chars -= unit_chars

    kept_indices = [i for i in range(len(messages)) if i not in dropped]
    trimmed = [messages[i] for i in kept_indices]
    if not trimmed:
        # Structural fail-safe: never hand back an empty history.
        return list(messages[-max(preserve_last_n, 1) :])

    if any(_is_clean_user(m) for m in trimmed):
        # A real head survives — no fallback needed, and computing one
        # would only cost time.
        return _normalize_head(trimmed)

    # Nothing in the surviving window can open the request. Pick a head
    # that fits the remaining budget (``total_chars`` already reflects
    # every drop above), falling back to a synthetic marker.
    fallback_head = _pick_fallback_head(
        messages,
        msg_chars,
        dropped,
        before=kept_indices[0],
        kept_chars=total_chars,
        target_tokens=target_tokens,
    )
    return _normalize_head(trimmed, fallback_head=fallback_head)


__all__ = [
    "ContextBudgetEstimate",
    "TrimResult",
    "estimate_context_usage",
    "trim_to_budget",
]
