"""Unit tests for tool-call repair.

The helper is responsible for pulling embedded tool-call JSON out of
assistant text (a failure mode of smaller coding models) and returning
it as OpenAI-shape tool_calls entries alongside a cleaned text.
"""

from __future__ import annotations

import json

from coderouter.translation.tool_repair import repair_tool_calls_in_text

# ----------------------------------------------------------------------
# Empty / no-match cases
# ----------------------------------------------------------------------


def test_empty_text_returns_empty_tuple() -> None:
    cleaned, calls = repair_tool_calls_in_text("")
    assert cleaned == ""
    assert calls == []


def test_plain_text_with_no_json_is_untouched() -> None:
    cleaned, calls = repair_tool_calls_in_text("Hello! I can help with that.")
    assert cleaned == "Hello! I can help with that."
    assert calls == []


def test_non_tool_shaped_json_is_ignored() -> None:
    """A JSON object that doesn't match the tool-call shape stays in the text."""
    original = 'Here is an example: {"foo": "bar", "baz": 1}'
    cleaned, calls = repair_tool_calls_in_text(original)
    assert cleaned == original
    assert calls == []


def test_allowlist_rejects_unknown_tool_names() -> None:
    text = '{"name": "NukeEverything", "arguments": {}}'
    cleaned, calls = repair_tool_calls_in_text(text, allowed_tool_names=["Bash", "Read"])
    # Not in the allow-list — left in place as text.
    assert cleaned == text
    assert calls == []


# ----------------------------------------------------------------------
# Bare JSON (the qwen2.5-coder failure mode)
# ----------------------------------------------------------------------


def test_bare_json_with_preamble_is_extracted() -> None:
    text = (
        "Let me check the current working directory.\n"
        '{"name": "Bash", "arguments": {"command": "pwd"}}'
    )
    cleaned, calls = repair_tool_calls_in_text(text)
    assert cleaned == "Let me check the current working directory."
    assert len(calls) == 1
    fn = calls[0]["function"]
    assert fn["name"] == "Bash"
    assert json.loads(fn["arguments"]) == {"command": "pwd"}
    assert calls[0]["type"] == "function"
    assert calls[0]["id"].startswith("call_")


def test_bare_json_without_preamble_leaves_empty_text() -> None:
    text = '{"name": "Bash", "arguments": {"command": "ls -la"}}'
    cleaned, calls = repair_tool_calls_in_text(text)
    assert cleaned == ""
    assert len(calls) == 1
    assert calls[0]["function"]["name"] == "Bash"


def test_multiple_bare_json_calls_preserve_order() -> None:
    text = (
        "First:\n"
        '{"name": "Bash", "arguments": {"command": "pwd"}}\n'
        "Then:\n"
        '{"name": "Read", "arguments": {"path": "/etc/hosts"}}'
    )
    cleaned, calls = repair_tool_calls_in_text(text)
    assert "Bash" not in cleaned and "Read" not in cleaned
    assert [c["function"]["name"] for c in calls] == ["Bash", "Read"]


# ----------------------------------------------------------------------
# Fenced code blocks
# ----------------------------------------------------------------------


def test_fenced_json_block_is_extracted() -> None:
    text = (
        "I'll run the command.\n"
        "```json\n"
        '{"name": "Bash", "arguments": {"command": "pwd"}}\n'
        "```\n"
        "Then I can check the output."
    )
    cleaned, calls = repair_tool_calls_in_text(text)
    assert "```" not in cleaned
    assert "I'll run the command." in cleaned
    assert "Then I can check the output." in cleaned
    assert len(calls) == 1
    assert calls[0]["function"]["name"] == "Bash"


def test_fenced_block_without_language_tag_is_extracted() -> None:
    text = '```\n{"name": "Read", "arguments": {"path": "/tmp/foo"}}\n```'
    cleaned, calls = repair_tool_calls_in_text(text)
    assert cleaned == ""
    assert len(calls) == 1
    assert calls[0]["function"]["name"] == "Read"


# ----------------------------------------------------------------------
# OpenAI-function shape
# ----------------------------------------------------------------------


def test_openai_function_shape_is_normalised() -> None:
    text = '{"function": {"name": "Bash", "arguments": "{\\"command\\": \\"pwd\\"}"}}'
    cleaned, calls = repair_tool_calls_in_text(text)
    assert cleaned == ""
    assert len(calls) == 1
    fn = calls[0]["function"]
    assert fn["name"] == "Bash"
    assert json.loads(fn["arguments"]) == {"command": "pwd"}


# ----------------------------------------------------------------------
# String-argument variants and edge cases
# ----------------------------------------------------------------------


def test_string_arguments_are_passed_through() -> None:
    """OpenAI native tool_calls use stringified arguments. If the model happens
    to write the same shape (arguments as a JSON-encoded string), we keep it."""
    text = '{"name": "Bash", "arguments": "{\\"command\\": \\"pwd\\"}"}'
    _, calls = repair_tool_calls_in_text(text)
    assert len(calls) == 1
    assert json.loads(calls[0]["function"]["arguments"]) == {"command": "pwd"}


def test_malformed_json_is_left_alone() -> None:
    text = 'Here: {"name": "Bash", "arguments":'
    cleaned, calls = repair_tool_calls_in_text(text)
    # Unclosed JSON — repair can't do anything, leave as text.
    assert cleaned == text
    assert calls == []


def test_braces_inside_string_do_not_break_balance() -> None:
    """Ensure JSON containing { or } inside a quoted string doesn't confuse
    the balanced-brace scanner."""
    text = '{"name": "Bash", "arguments": {"command": "echo \\"{hello}\\""}}'
    cleaned, calls = repair_tool_calls_in_text(text)
    assert cleaned == ""
    assert len(calls) == 1
    args = json.loads(calls[0]["function"]["arguments"])
    assert args == {"command": 'echo "{hello}"'}
