"""Tool-loop detection guard (v1.9-E L3).

Long-running agents — Claude Code, Cline, OpenClaw, etc. — sometimes
fall into "stuck loops" where the assistant repeatedly calls the same
tool with identical arguments because it has no clean exit signal. The
typical symptom is a Read of the same file 5+ times, or a Bash with
the same command, with no intermediate observation that would change
behavior.

This module gives the engine a way to detect that pattern from the
inbound request alone — Claude Code (and most agent harnesses) re-send
the full conversation history on every turn, so the assistant's
``tool_use`` blocks accumulate in ``request.messages`` and a tail-only
inspection is enough to catch a stuck loop.

Three policy actions:

  ``warn``   — log only. Diagnostic; default for v1.9-E.
  ``inject`` — append a ``you-are-looping`` system reminder so the
               next assistant turn has a chance to course-correct.
  ``break``  — short-circuit the request via :class:`ToolLoopBreakError`.

All three actions emit the same structured ``tool-loop-detected``
log line so dashboards see every detection regardless of policy.

Detection algorithm (intentionally simple in v1.9-E)
====================================================

For the most-recent ``window`` assistant ``tool_use`` blocks, count
the longest *trailing* run of identical ``(name, args)`` pairs. If
that run is at least ``threshold`` long, return a detection.

Why "trailing run" instead of any-run-of-N-anywhere
    A stuck-loop signal is the *current* state of the agent — past
    streaks that the agent already escaped from are noise. Trailing
    keeps the detection actionable: the next action that would have
    been taken is a continuation of the streak.

Why ``threshold >= 2``
    A single tool call can never form a "loop" by itself. The schema
    enforces ``ge=2`` to keep the detection meaningful, and the
    default of 3 catches the common stuck patterns without false-
    positive on legitimate same-tool repetition (iterating Read on
    different files yields different args, so they don't streak).

Argument equality
    Args are compared via canonical-form JSON serialization (sorted
    keys). This is exact-match: ``{"path": "a"}`` and
    ``{"path": "b"}`` do not streak. Future versions may add
    similarity (jaccard / edit distance) for near-duplicates, but
    exact is conservative and matches operator intuition for v1.9-E.

Forward references
==================

The engine integration calls :func:`detect_tool_loop` early in
``generate_anthropic`` / ``stream_anthropic`` (before adapter
dispatch) so the guard sees the request as the operator's profile
declared it, before any chain-side mutation. See
``coderouter/routing/fallback.py`` for the call site.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from coderouter.errors import CodeRouterError

if TYPE_CHECKING:
    # Schema-level imports only used for type checking. Importing the
    # ``coderouter.translation`` package eagerly would trigger a
    # pre-existing cycle (translation → convert → adapters →
    # anthropic_native → translation.convert) when this guard is the
    # first leaf module loaded by a test. The runtime operations
    # (``request.model_copy``, attribute walks) work via duck typing
    # so the runtime imports aren't needed.
    from coderouter.translation import (
        AnthropicMessage,
        AnthropicRequest,
    )


DEFAULT_LOOP_INJECT_HINT: str = (
    "You appear to be calling the same tool with the same arguments "
    "repeatedly. This often means the previous results did not provide "
    "what you needed. Try a different approach: a different tool, a "
    "different argument, or asking the user for clarification."
)
"""Default system message appended when ``tool_loop_action: inject``.

Phrased as instruction rather than scolding so the model treats it as
an in-context hint rather than a failure signal that derails the rest
of the response. The engine reads this constant when no operator-
provided override is supplied; future versions may surface a
``tool_loop_inject_hint`` schema field for per-profile customization.
"""


@dataclass(frozen=True)
class ToolUseRecord:
    """A normalized assistant tool_use observation from request history.

    Carries only the fields the loop detector needs — ``id`` /
    block-type metadata are dropped because they vary between
    otherwise-identical calls and would suppress the loop signal.
    """

    name: str
    args_canonical: str
    """JSON-serialized args with sorted keys — the equality key.

    Stored as a string (not the raw dict) so identity comparisons are
    cheap even when args contain nested dicts. Operators reading the
    log don't see this field; they see the ``tool_name`` + a
    ``repeat_count`` summary.
    """


@dataclass(frozen=True)
class ToolLoopDetection:
    """The outcome of a positive loop detection.

    The :func:`detect_tool_loop` entry returns ``None`` when no loop is
    found and an instance of this class otherwise. The engine routes
    the value into the configured action (``warn`` / ``inject`` /
    ``break``).
    """

    tool_name: str
    repeat_count: int
    """Length of the trailing streak of identical calls.

    Always >= ``threshold`` (the trigger condition). May exceed it
    when the loop has been running for several turns before the
    request was received — operators see the actual run length, not
    just "threshold reached".
    """
    args_canonical: str
    """The canonical-JSON form of the repeated args.

    Useful for the ``inject`` action's hint message and for log-line
    correlation across turns. Operators would see this in the log if
    we surfaced it; v1.9-E's typed payload deliberately omits it
    (often contains user data) and lets the WARN log line carry only
    aggregate fields.
    """


class ToolLoopBreakError(CodeRouterError):
    """Raised when a loop is detected and the configured action is ``break``.

    The Anthropic ingress layer catches this and converts it into a
    structured ``400`` response (with ``error: "tool_loop_detected"`` +
    detection fields in ``detail``) so the client sees a programmable
    failure rather than a 5xx. Subclasses
    :class:`coderouter.errors.CodeRouterError` so callers that grep
    for the broad base class catch this too.

    The ``threshold`` / ``window`` fields are carried on the exception
    (rather than re-looked-up from the config in the ingress) so the
    ingress doesn't need to take a config dependency just to render
    the 400 detail. The values are the profile's at the moment of
    detection — they parameterize the detection that fired.
    """

    def __init__(
        self,
        detection: ToolLoopDetection,
        profile: str,
        *,
        threshold: int,
        window: int,
    ) -> None:
        super().__init__(
            f"tool loop detected on profile={profile!r}: "
            f"tool {detection.tool_name!r} repeated "
            f"{detection.repeat_count} times consecutively."
        )
        self.detection = detection
        self.profile = profile
        self.threshold = threshold
        self.window = window


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def _canonical_args(args: Any) -> str:
    """Serialize ``args`` to a canonical-form JSON string.

    ``sort_keys=True`` makes ordering deterministic so semantically
    identical dicts compare equal regardless of the assistant's whim.
    Falls back to ``str(args)`` for objects that aren't JSON-
    serializable (vanishingly rare in practice; tool args are normally
    plain dicts of primitives + nested structures of the same).
    """
    try:
        return json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        # Defensive fallback — never raise from the guard's hot path.
        return str(args)


def _extract_tool_uses_from_message(message: AnthropicMessage) -> list[ToolUseRecord]:
    """Pull every ``tool_use`` block out of an assistant message.

    A user message can carry ``tool_result`` blocks but not
    ``tool_use`` (the latter is assistant-only by the Anthropic
    spec), so we cheaply gate on the role first.
    """
    if message.role != "assistant":
        return []
    content = message.content
    if not isinstance(content, list):
        return []
    out: list[ToolUseRecord] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") != "tool_use":
            continue
        name = block.get("name")
        if not isinstance(name, str):
            continue
        args = block.get("input", {})
        out.append(
            ToolUseRecord(
                name=name,
                args_canonical=_canonical_args(args),
            )
        )
    return out


def _extract_tool_use_history(request: AnthropicRequest) -> list[ToolUseRecord]:
    """Flatten every assistant tool_use in the inbound request.

    Order is preserved — the first item in the returned list is the
    earliest tool_use in the conversation, the last item is the most
    recent. Tail-window detection slices the back of this list.
    """
    out: list[ToolUseRecord] = []
    for msg in request.messages:
        out.extend(_extract_tool_uses_from_message(msg))
    return out


def detect_tool_loop(
    request: AnthropicRequest,
    *,
    window: int,
    threshold: int,
) -> ToolLoopDetection | None:
    """Return a detection if the trailing ``window`` shows a streak.

    Walks the last ``window`` assistant ``tool_use`` blocks from the
    inbound request and computes the length of the trailing run of
    identical ``(name, args)`` pairs. When the run reaches
    ``threshold``, returns a :class:`ToolLoopDetection` describing it.

    Return ``None`` when:
      * the conversation has fewer than ``threshold`` tool_use blocks
      * the trailing run is shorter than ``threshold``

    The function is pure — it never mutates the request and never
    raises. Schema validators on :class:`coderouter.config.schemas.FallbackChain`
    enforce ``threshold >= 2`` and ``window >= 2`` so callers can
    treat both as positive integers without re-validating.
    """
    history = _extract_tool_use_history(request)
    if not history:
        return None

    tail = history[-window:]
    if len(tail) < threshold:
        return None

    # Walk the tail in reverse and count how many trailing entries
    # match the very last one. Exit on the first mismatch.
    last = tail[-1]
    streak = 1
    for record in reversed(tail[:-1]):
        if record.name == last.name and record.args_canonical == last.args_canonical:
            streak += 1
        else:
            break

    if streak < threshold:
        return None

    return ToolLoopDetection(
        tool_name=last.name,
        repeat_count=streak,
        args_canonical=last.args_canonical,
    )


# ---------------------------------------------------------------------------
# Action — `inject` system message
# ---------------------------------------------------------------------------


def inject_loop_break_hint(
    request: AnthropicRequest,
    *,
    hint: str,
) -> AnthropicRequest:
    """Return a copy of ``request`` with the loop-break hint appended.

    The hint is added to the ``system`` field. Three input shapes are
    handled:

      * ``system: None``                         → set to ``hint``
      * ``system: str``                          → ``"<existing>\\n\\n<hint>"``
      * ``system: list[block]`` (Anthropic blocks) → append a text block

    The original request is not mutated; the engine receives the new
    object and dispatches to the chain as normal. CodeRouter-only
    fields (``profile`` / ``anthropic_beta``) are preserved via
    Pydantic ``model_copy``.
    """
    system = request.system
    new_system: str | list[dict[str, Any]]
    if system is None:
        new_system = hint
    elif isinstance(system, str):
        # Two newlines between original prompt and hint so the model
        # sees them as separate paragraphs.
        new_system = f"{system}\n\n{hint}" if system else hint
    else:
        # List-of-blocks form. Append a fresh text block; do not
        # touch existing block-level cache_control markers.
        new_system = [*list(system), {"type": "text", "text": hint}]

    return request.model_copy(update={"system": new_system})
