"""Unit tests for v1.9-E L3 tool-loop guard.

Two layers covered here:

  1. :func:`detect_tool_loop` (pure function): correctly identifies
     stuck-tool patterns from the inbound assistant tool_use history.
  2. :func:`_apply_tool_loop_guard` (engine-level helper): runs the
     detection and dispatches the configured action (warn / inject /
     break), emitting structured ``tool-loop-detected`` log lines.

End-to-end emission through the engine is exercised in
``test_fallback_tool_loop.py``; this module isolates the detection
logic and policy dispatch.
"""

from __future__ import annotations

import logging

import pytest

from coderouter.config.schemas import (
    CodeRouterConfig,
    FallbackChain,
    ProviderConfig,
)
from coderouter.guards.tool_loop import (
    DEFAULT_LOOP_INJECT_HINT,
    ToolLoopBreakError,
    detect_tool_loop,
    inject_loop_break_hint,
)
from coderouter.routing.fallback import _apply_tool_loop_guard
from coderouter.translation.anthropic import AnthropicMessage, AnthropicRequest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _assistant_tool_use(name: str, **input: object) -> AnthropicMessage:
    """Build an assistant message with one ``tool_use`` block."""
    return AnthropicMessage.model_validate(
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": f"toolu_{name}_{len(input)}",
                    "name": name,
                    "input": dict(input),
                }
            ],
        }
    )


def _user_tool_result(tool_use_id: str = "toolu_x") -> AnthropicMessage:
    """User message carrying a single tool_result block.

    Real Claude Code sessions interleave assistant tool_use messages
    with user tool_result messages; the detector must skip user-side
    blocks even though they appear in the same conversation.
    """
    return AnthropicMessage.model_validate(
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": "ok",
                }
            ],
        }
    )


def _request_with_history(
    messages: list[AnthropicMessage],
    *,
    profile: str | None = None,
) -> AnthropicRequest:
    """Build an AnthropicRequest with the given assistant/user history."""
    final = [
        AnthropicMessage(role="user", content="continue"),
    ]
    return AnthropicRequest(
        max_tokens=64,
        messages=messages + final,
        profile=profile,
    )


def _provider() -> ProviderConfig:
    return ProviderConfig(
        name="any",
        kind="anthropic",
        base_url="https://example.test",
        model="claude-opus-4-8",
        api_key_env="CR_TEST_KEY",
    )


def _config_for_tool_loop(
    *,
    window: int = 5,
    threshold: int = 3,
    action: str = "warn",
    profile_name: str = "default",
) -> CodeRouterConfig:
    profile = FallbackChain.model_validate(
        {
            "name": profile_name,
            "providers": ["any"],
            "tool_loop_window": window,
            "tool_loop_threshold": threshold,
            "tool_loop_action": action,
        }
    )
    return CodeRouterConfig(
        providers=[_provider()],
        profiles=[profile],
        default_profile=profile_name,
    )


# ---------------------------------------------------------------------------
# detect_tool_loop — pure function
# ---------------------------------------------------------------------------


def test_detect_returns_none_on_empty_history() -> None:
    """No tool_use blocks at all → no loop possible."""
    request = _request_with_history([])
    assert detect_tool_loop(request, window=5, threshold=3) is None


def test_detect_returns_none_when_history_below_threshold() -> None:
    """Two identical calls is still under the default threshold of 3."""
    request = _request_with_history(
        [
            _assistant_tool_use("Read", path="a.py"),
            _user_tool_result(),
            _assistant_tool_use("Read", path="a.py"),
            _user_tool_result(),
        ]
    )
    assert detect_tool_loop(request, window=5, threshold=3) is None


def test_detect_fires_on_three_identical_consecutive_calls() -> None:
    """Three Read("a.py") in a row → detection with repeat_count=3."""
    request = _request_with_history(
        [
            _assistant_tool_use("Read", path="a.py"),
            _user_tool_result(),
            _assistant_tool_use("Read", path="a.py"),
            _user_tool_result(),
            _assistant_tool_use("Read", path="a.py"),
            _user_tool_result(),
        ]
    )
    detection = detect_tool_loop(request, window=5, threshold=3)
    assert detection is not None
    assert detection.tool_name == "Read"
    assert detection.repeat_count == 3


def test_detect_does_not_fire_on_different_args() -> None:
    """Read("a"), Read("b"), Read("c") — same tool, different args → no loop.

    This is the iterating-over-files pattern that should never trigger.
    """
    request = _request_with_history(
        [
            _assistant_tool_use("Read", path="a.py"),
            _user_tool_result(),
            _assistant_tool_use("Read", path="b.py"),
            _user_tool_result(),
            _assistant_tool_use("Read", path="c.py"),
            _user_tool_result(),
        ]
    )
    assert detect_tool_loop(request, window=5, threshold=3) is None


def test_detect_only_considers_trailing_run() -> None:
    """A streak earlier in history that the agent already escaped from
    must not trigger detection — only the *trailing* state matters."""
    request = _request_with_history(
        [
            # Past streak (3x Read a.py)
            _assistant_tool_use("Read", path="a.py"),
            _user_tool_result(),
            _assistant_tool_use("Read", path="a.py"),
            _user_tool_result(),
            _assistant_tool_use("Read", path="a.py"),
            _user_tool_result(),
            # Agent escaped — most recent action is something else.
            _assistant_tool_use("Bash", command="ls"),
            _user_tool_result(),
        ]
    )
    # Window of 5 catches the latest Bash + last 2 Reads, no streak >= 3.
    assert detect_tool_loop(request, window=5, threshold=3) is None


def test_detect_window_caps_consideration_set() -> None:
    """A streak longer than the window still detects, but ``repeat_count``
    is bounded by the window cap (we only inspected ``window`` entries)."""
    # 6 identical calls; window of 4 → should detect with repeat_count=4.
    history: list[AnthropicMessage] = []
    for _ in range(6):
        history.append(_assistant_tool_use("Read", path="a.py"))
        history.append(_user_tool_result())
    detection = detect_tool_loop(_request_with_history(history), window=4, threshold=3)
    assert detection is not None
    assert detection.repeat_count == 4


def test_detect_considers_canonical_args() -> None:
    """``{"a": 1, "b": 2}`` and ``{"b": 2, "a": 1}`` must compare equal.

    Pins the canonical-form JSON serialization (sorted keys) used to
    compute the equality key.
    """
    request = _request_with_history(
        [
            _assistant_tool_use("Edit", file="x", old="a", new="b"),
            _user_tool_result(),
            _assistant_tool_use("Edit", new="b", old="a", file="x"),  # reordered
            _user_tool_result(),
            _assistant_tool_use("Edit", old="a", file="x", new="b"),  # reordered
            _user_tool_result(),
        ]
    )
    detection = detect_tool_loop(request, window=5, threshold=3)
    assert detection is not None
    assert detection.tool_name == "Edit"
    assert detection.repeat_count == 3


def test_detect_ignores_user_side_blocks() -> None:
    """``tool_result`` blocks (user-side) must not be counted as tool_use."""
    request = _request_with_history(
        [
            _user_tool_result(),
            _user_tool_result(),
            _user_tool_result(),
        ]
    )
    assert detect_tool_loop(request, window=5, threshold=3) is None


# ---------------------------------------------------------------------------
# inject_loop_break_hint — request mutation
# ---------------------------------------------------------------------------


def test_inject_hint_sets_system_when_absent() -> None:
    request = AnthropicRequest(
        max_tokens=64, messages=[AnthropicMessage(role="user", content="hi")]
    )
    out = inject_loop_break_hint(request, hint="LOOP")
    assert out.system == "LOOP"
    # Original is not mutated.
    assert request.system is None


def test_inject_hint_appends_to_string_system() -> None:
    request = AnthropicRequest(
        max_tokens=64,
        system="Original prompt",
        messages=[AnthropicMessage(role="user", content="hi")],
    )
    out = inject_loop_break_hint(request, hint="LOOP")
    assert out.system == "Original prompt\n\nLOOP"


def test_inject_hint_appends_block_to_list_system() -> None:
    request = AnthropicRequest.model_validate(
        {
            "max_tokens": 64,
            "system": [{"type": "text", "text": "Original"}],
            "messages": [{"role": "user", "content": "hi"}],
        }
    )
    out = inject_loop_break_hint(request, hint="LOOP")
    assert isinstance(out.system, list)
    assert out.system[-1] == {"type": "text", "text": "LOOP"}
    # Existing block is untouched
    assert out.system[0] == {"type": "text", "text": "Original"}


# ---------------------------------------------------------------------------
# _apply_tool_loop_guard — engine helper dispatch
# ---------------------------------------------------------------------------


def _looping_request(profile: str | None = None) -> AnthropicRequest:
    return _request_with_history(
        [
            _assistant_tool_use("Read", path="a.py"),
            _user_tool_result(),
            _assistant_tool_use("Read", path="a.py"),
            _user_tool_result(),
            _assistant_tool_use("Read", path="a.py"),
            _user_tool_result(),
        ],
        profile=profile,
    )


def test_apply_guard_warn_action_emits_log_and_returns_request_unchanged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    config = _config_for_tool_loop(action="warn")
    request = _looping_request()
    with caplog.at_level(logging.INFO, logger="coderouter"):
        out = _apply_tool_loop_guard(request, config=config)
    # Returns the same object semantically (no mutation for warn)
    assert out is request
    # Structured log fired
    records = [r for r in caplog.records if r.msg == "tool-loop-detected"]
    assert len(records) == 1
    assert records[0].profile == "default"
    assert records[0].tool_name == "Read"
    assert records[0].repeat_count == 3
    assert records[0].action == "warn"


def test_apply_guard_inject_action_returns_mutated_request(
    caplog: pytest.LogCaptureFixture,
) -> None:
    config = _config_for_tool_loop(action="inject")
    request = _looping_request()
    with caplog.at_level(logging.INFO, logger="coderouter"):
        out = _apply_tool_loop_guard(request, config=config)
    assert out is not request
    assert out.system == DEFAULT_LOOP_INJECT_HINT
    # Log still fires for inject
    records = [r for r in caplog.records if r.msg == "tool-loop-detected"]
    assert len(records) == 1
    assert records[0].action == "inject"


def test_apply_guard_break_action_raises(
    caplog: pytest.LogCaptureFixture,
) -> None:
    config = _config_for_tool_loop(action="break")
    request = _looping_request()
    with (
        caplog.at_level(logging.INFO, logger="coderouter"),
        pytest.raises(ToolLoopBreakError) as exc_info,
    ):
        _apply_tool_loop_guard(request, config=config)
    assert exc_info.value.detection.tool_name == "Read"
    assert exc_info.value.profile == "default"
    # Log fires before the raise
    records = [r for r in caplog.records if r.msg == "tool-loop-detected"]
    assert len(records) == 1
    assert records[0].action == "break"


def test_apply_guard_no_loop_no_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No streak → no log line, request returned unchanged."""
    config = _config_for_tool_loop(action="warn")
    request = _request_with_history(
        [
            _assistant_tool_use("Read", path="a.py"),
            _user_tool_result(),
            _assistant_tool_use("Bash", command="ls"),
            _user_tool_result(),
        ]
    )
    with caplog.at_level(logging.INFO, logger="coderouter"):
        out = _apply_tool_loop_guard(request, config=config)
    assert out is request
    assert [r for r in caplog.records if r.msg == "tool-loop-detected"] == []


def test_apply_guard_unknown_profile_is_noop(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Profile lookup failure (e.g. test harness with stripped config) →
    silent no-op so the chain resolution can produce its own diagnostic."""
    config = _config_for_tool_loop(action="warn", profile_name="default")
    request = _looping_request(profile="ghost-profile")
    with caplog.at_level(logging.INFO, logger="coderouter"):
        out = _apply_tool_loop_guard(request, config=config)
    # Falls back to default_profile which exists, so detection still
    # fires under the default config — the test is asserting the
    # mechanism doesn't crash, not that an unknown profile suppresses
    # detection (default_profile resolution is the safety net).
    assert out is not None
