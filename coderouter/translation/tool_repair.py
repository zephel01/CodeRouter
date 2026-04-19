"""Tool-call repair: extract tool invocations that a model wrote as plain text.

Background
----------
Small coding models (e.g. qwen2.5-coder) sometimes respond to a tool-bearing
prompt by *writing* a JSON object describing the tool call in the assistant
message body instead of populating the structured `tool_calls` field. The
downstream Anthropic/OpenAI clients then see regular text and never execute
the tool.

This module scans assistant text for such embedded tool-call JSON and pulls
it back into the OpenAI-shape `tool_calls` list, so the rest of the
translation pipeline (`to_anthropic_response`, stream event emitter) can
produce real `tool_use` content blocks.

Recognised shapes
-----------------
1. Fenced code blocks:
    ```json
    {"name": "Bash", "arguments": {"command": "pwd"}}
    ```
   (the language tag is optional: ``` ...``` also works.)
2. Bare JSON objects embedded in text:
    "Let me check the current directory. {\"name\":\"Bash\",\"arguments\":{}}"
3. Multiple JSON objects in sequence (for multi-call turns).

Each candidate is accepted only if it parses to one of:
    {"name": <str>, "arguments": <dict | str>}          # direct shape
    {"function": {"name": <str>, "arguments": ...}}     # OpenAI shape

If `allowed_tool_names` is provided, the `name` must be in that set;
otherwise any tool-shaped JSON is accepted. Passing the allow-list is
strongly recommended to avoid false positives (a model legitimately
discussing JSON in prose).
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

__all__ = ["repair_tool_calls_in_text"]


# ------------------------------------------------------------------
# Tool-call shape detection + normalisation
# ------------------------------------------------------------------


def _looks_like_tool_call(
    obj: Any, allowed: set[str] | None
) -> tuple[str, Any] | None:
    """Return (name, arguments) if obj looks like a tool call, else None."""
    if not isinstance(obj, dict):
        return None

    # Direct shape: {"name": "...", "arguments": ...}
    name = obj.get("name")
    if isinstance(name, str) and "arguments" in obj:
        if allowed is None or name in allowed:
            return name, obj["arguments"]

    # OpenAI function shape: {"function": {"name": "...", "arguments": ...}}
    fn = obj.get("function")
    if isinstance(fn, dict):
        inner_name = fn.get("name")
        if isinstance(inner_name, str) and "arguments" in fn:
            if allowed is None or inner_name in allowed:
                return inner_name, fn["arguments"]

    return None


def _normalise_to_openai_tool_call(name: str, arguments: Any) -> dict[str, Any]:
    """Build an OpenAI-shape tool_calls entry."""
    if isinstance(arguments, str):
        args_str = arguments
    elif isinstance(arguments, dict):
        args_str = json.dumps(arguments, ensure_ascii=False)
    else:
        # list / None / anything else — fall back to serialising what we got.
        args_str = json.dumps(arguments, ensure_ascii=False)
    return {
        "id": f"call_{uuid.uuid4().hex[:16]}",
        "type": "function",
        "function": {"name": name, "arguments": args_str},
    }


# ------------------------------------------------------------------
# Scanners: fenced code blocks, then balanced braces in remaining text
# ------------------------------------------------------------------

# Match ```json ... ``` or ``` ... ``` with anything after the fence tag line.
# Group 1 captures the body.
_FENCED_RE = re.compile(
    r"```(?:\w+)?[ \t]*\r?\n(.*?)\r?\n?```",
    re.DOTALL,
)


def _extract_fenced_blocks(text: str) -> tuple[str, list[str]]:
    """Pull ```...``` blocks out of text. Returns (text_without_fences, bodies)."""
    bodies: list[str] = []

    def _collect(match: re.Match[str]) -> str:
        bodies.append(match.group(1))
        return ""  # remove the fenced block from the text entirely

    cleaned = _FENCED_RE.sub(_collect, text)
    return cleaned, bodies


def _find_balanced_json_objects(text: str) -> list[tuple[int, int, str]]:
    """Find top-level `{...}` JSON substrings by a brace-counter scan.

    Returns a list of (start, end_exclusive, substring). Handles escape
    sequences and string literals so braces inside JSON strings do not
    confuse the counter. Malformed (unclosed) candidates are skipped.
    """
    out: list[tuple[int, int, str]] = []
    n = len(text)
    i = 0
    while i < n:
        if text[i] != "{":
            i += 1
            continue
        # Scan forward to find a balanced close.
        depth = 0
        j = i
        in_str = False
        escape = False
        while j < n:
            c = text[j]
            if escape:
                escape = False
            elif in_str:
                if c == "\\":
                    escape = True
                elif c == '"':
                    in_str = False
            else:
                if c == '"':
                    in_str = True
                elif c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        out.append((i, j + 1, text[i : j + 1]))
                        i = j + 1
                        break
            j += 1
        else:
            # Ran off the end without closing — skip this `{` and move on.
            i += 1
            continue
    return out


# ------------------------------------------------------------------
# Public API
# ------------------------------------------------------------------


def repair_tool_calls_in_text(
    text: str,
    allowed_tool_names: list[str] | set[str] | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Extract embedded tool-call JSON from assistant text.

    Returns:
        (cleaned_text, tool_calls)
          cleaned_text  : the input with recognised tool-call JSON removed,
                          stripped of surrounding whitespace.
          tool_calls    : OpenAI-shape tool_calls entries, in the order they
                          appeared in the original text. Each entry has a
                          freshly minted `id` (the source JSON did not carry one).

    If nothing repairable is found, returns (text, []).
    """
    if not isinstance(text, str) or not text:
        return text, []

    allowed: set[str] | None
    if allowed_tool_names is None:
        allowed = None
    else:
        allowed = set(allowed_tool_names)

    extracted: list[dict[str, Any]] = []

    # 1. Pull fenced code blocks out first — they're the most common shape
    #    when a chat-tuned model explains what it's doing.
    cleaned, fenced_bodies = _extract_fenced_blocks(text)
    for body in fenced_bodies:
        body = body.strip()
        if not body.startswith("{"):
            continue
        try:
            obj = json.loads(body)
        except json.JSONDecodeError:
            continue
        hit = _looks_like_tool_call(obj, allowed)
        if hit is not None:
            name, args = hit
            extracted.append(_normalise_to_openai_tool_call(name, args))

    # 2. Scan remaining text for bare JSON objects.
    #    We walk from back to front so removals by slicing don't shift
    #    the indices of earlier matches.
    candidates = _find_balanced_json_objects(cleaned)
    # Tentatively evaluate each; keep only the ones that are tool-call-shaped.
    spans_to_remove: list[tuple[int, int]] = []
    repaired_from_bare: list[dict[str, Any]] = []
    for start, end, substr in candidates:
        try:
            obj = json.loads(substr)
        except json.JSONDecodeError:
            continue
        hit = _looks_like_tool_call(obj, allowed)
        if hit is None:
            continue
        name, args = hit
        repaired_from_bare.append(_normalise_to_openai_tool_call(name, args))
        spans_to_remove.append((start, end))

    # Remove the matched spans from the text back-to-front.
    for start, end in reversed(spans_to_remove):
        cleaned = cleaned[:start] + cleaned[end:]

    extracted.extend(repaired_from_bare)

    # Collapse the whitespace left behind by removals.
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()

    return cleaned, extracted
