"""v2.0-F (L1): context budget guard tests.

Three test groups:

- **Estimation (pure)**: ``estimate_context_usage`` computes ratios
  and threshold booleans correctly.
- **Trim (pure)**: ``trim_to_budget`` removes old messages, preserves
  last N, handles tool pairs atomically.
- **Config validation**: schema fields have correct bounds and defaults.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

# Import adapters/routing first to resolve the circular import between
# coderouter.translation.convert ↔ coderouter.adapters.anthropic_native.
# conftest loads schemas first, then test_memory_pressure loads these in
# this order and it works; replicating the pattern here.
from coderouter.adapters.base import BaseAdapter  # noqa: F401
from coderouter.config.schemas import CodeRouterConfig, FallbackChain, ProviderConfig
from coderouter.guards.context_budget import (
    estimate_context_usage,
    trim_to_budget,
)
from coderouter.translation.anthropic import AnthropicMessage, AnthropicRequest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_request(
    messages: list[tuple[str, str | list]],
    system: str | None = None,
) -> AnthropicRequest:
    """Build a minimal AnthropicRequest from (role, content) pairs."""
    return AnthropicRequest(
        model="test-model",
        max_tokens=1024,
        system=system,
        messages=[
            AnthropicMessage(role=role, content=content)
            for role, content in messages
        ],
    )


def _make_long_request(n_messages: int, chars_per_msg: int = 400) -> AnthropicRequest:
    """Build a request with many messages for trim testing."""
    messages = []
    for i in range(n_messages):
        role = "user" if i % 2 == 0 else "assistant"
        content = f"Message {i}: " + "x" * chars_per_msg
        messages.append((role, content))
    return _make_request(messages, system="System prompt " + "y" * 100)


# ---------------------------------------------------------------------------
# Group 1: Estimation (pure)
# ---------------------------------------------------------------------------


class TestEstimateContextUsage:
    def test_empty_request(self):
        request = _make_request([("user", "Hi")])
        estimate = estimate_context_usage(
            request, max_context_tokens=128000
        )
        assert estimate.estimated_tokens == len("Hi") // 4
        assert estimate.max_context_tokens == 128000
        assert estimate.usage_ratio < 0.01
        assert not estimate.over_warn_threshold
        assert not estimate.over_trim_threshold

    def test_over_warn_threshold(self):
        # Create a request that uses ~85% of a 100-token context window
        # 85 tokens * 4 chars/token = 340 chars
        request = _make_request([("user", "x" * 340)])
        estimate = estimate_context_usage(
            request, max_context_tokens=100, warn_threshold=0.80
        )
        assert estimate.usage_ratio >= 0.80
        assert estimate.over_warn_threshold
        assert not estimate.over_trim_threshold  # default trim is 0.90

    def test_over_trim_threshold(self):
        # 95 tokens * 4 = 380 chars
        request = _make_request([("user", "x" * 380)])
        estimate = estimate_context_usage(
            request, max_context_tokens=100, warn_threshold=0.80, trim_threshold=0.90
        )
        assert estimate.over_warn_threshold
        assert estimate.over_trim_threshold

    def test_below_all_thresholds(self):
        # 50 tokens * 4 = 200 chars
        request = _make_request([("user", "x" * 200)])
        estimate = estimate_context_usage(
            request, max_context_tokens=100, warn_threshold=0.80
        )
        assert estimate.usage_ratio == pytest.approx(0.50, abs=0.01)
        assert not estimate.over_warn_threshold
        assert not estimate.over_trim_threshold

    def test_system_prompt_counted(self):
        # system: 200 chars = 50 tokens, message: 200 chars = 50 tokens
        # total: 100 tokens / 100 max = 1.0 ratio
        request = _make_request([("user", "x" * 200)], system="y" * 200)
        estimate = estimate_context_usage(
            request, max_context_tokens=100
        )
        assert estimate.usage_ratio == pytest.approx(1.0, abs=0.01)

    def test_zero_max_context_safe(self):
        """max_context_tokens=0 should not crash (defensive)."""
        request = _make_request([("user", "Hello")])
        estimate = estimate_context_usage(request, max_context_tokens=0)
        assert estimate.usage_ratio == 0.0


# ---------------------------------------------------------------------------
# Group 2: Trim (pure)
# ---------------------------------------------------------------------------


class TestTrimToBudget:
    def test_basic_trim(self):
        # 10 messages of ~100 tokens each = 1000 tokens total
        # max_context = 500, target = 0.75 → target_tokens = 375
        # Should trim most messages, preserve last 4
        request = _make_long_request(10, chars_per_msg=396)  # ~100 tokens each
        trimmed, result = trim_to_budget(
            request,
            max_context_tokens=500,
            trim_target=0.75,
            preserve_last_n=4,
        )
        assert result.messages_removed > 0
        assert result.messages_after <= result.messages_before
        assert result.messages_after >= 2  # minimum floor
        assert len(trimmed.messages) == result.messages_after

    def test_preserve_last_n(self):
        # 20 messages of ~25 tokens each = ~500 tokens + system ~30 tokens
        # max_context = 1000, target = 0.75 → target_tokens = 750
        # Should keep last 4 and remove the rest
        request = _make_long_request(20, chars_per_msg=96)  # ~25 tokens each
        trimmed, _result = trim_to_budget(
            request,
            max_context_tokens=1000,
            trim_target=0.75,
            preserve_last_n=4,
        )
        original_msgs = request.messages
        trimmed_msgs = trimmed.messages
        # The last N messages in trimmed should be the last N from original
        # (at minimum, the final messages are always preserved)
        n_preserved = len(trimmed_msgs)
        for i in range(n_preserved):
            assert trimmed_msgs[i].content == original_msgs[-(n_preserved - i)].content

    def test_no_trim_needed(self):
        # Small request, large context window → no trim
        request = _make_request([("user", "Hi"), ("assistant", "Hello")])
        trimmed, result = trim_to_budget(
            request,
            max_context_tokens=128000,
            trim_target=0.75,
            preserve_last_n=4,
        )
        assert result.messages_removed == 0
        assert len(trimmed.messages) == 2

    def test_tool_pair_preservation(self):
        """tool_use + tool_result pairs should be preserved atomically."""
        messages = [
            ("user", "Do something"),
            ("assistant", [
                {"type": "text", "text": "I'll use a tool"},
                {"type": "tool_use", "id": "tu_1", "name": "read", "input": {}},
            ]),
            ("user", [
                {"type": "tool_result", "tool_use_id": "tu_1", "content": "file contents"},
            ]),
            ("assistant", "Based on the file..."),
            ("user", "Thanks, now do another thing"),
            ("assistant", [
                {"type": "text", "text": "Let me try"},
                {"type": "tool_use", "id": "tu_2", "name": "write", "input": {}},
            ]),
            ("user", [
                {"type": "tool_result", "tool_use_id": "tu_2", "content": "done"},
            ]),
            ("assistant", "All done!"),
        ]
        request = _make_request(messages, system="System " + "z" * 100)
        trimmed, _result = trim_to_budget(
            request,
            max_context_tokens=100,  # force aggressive trim
            trim_target=0.50,
            preserve_last_n=4,
        )
        # Verify tool pairs are intact in the trimmed result
        trimmed_msgs = trimmed.messages
        for i, msg in enumerate(trimmed_msgs):
            content = msg.content
            if isinstance(content, list):
                has_tool_result = any(
                    isinstance(b, dict) and b.get("type") == "tool_result"
                    for b in content
                )
                if has_tool_result and i > 0:
                    # Previous message should be assistant with tool_use
                    prev = trimmed_msgs[i - 1]
                    prev_content = prev.content
                    if isinstance(prev_content, list):
                        has_tool_use = any(
                            isinstance(b, dict) and b.get("type") == "tool_use"
                            for b in prev_content
                        )
                        assert has_tool_use, (
                            f"tool_result at index {i} has no preceding tool_use"
                        )

    def test_minimum_floor(self):
        """Even with aggressive trim, at least 2 messages are kept."""
        request = _make_long_request(20, chars_per_msg=400)
        trimmed, _result = trim_to_budget(
            request,
            max_context_tokens=10,  # impossibly small
            trim_target=0.50,
            preserve_last_n=4,
        )
        assert len(trimmed.messages) >= 2

    def test_system_prompt_preserved(self):
        """System prompt is never removed by trim."""
        request = _make_long_request(10, chars_per_msg=400)
        trimmed, _ = trim_to_budget(
            request,
            max_context_tokens=200,
            trim_target=0.50,
            preserve_last_n=4,
        )
        assert trimmed.system == request.system


# ---------------------------------------------------------------------------
# Group 3: Config schema validation
# ---------------------------------------------------------------------------


class TestConfigSchema:
    def test_default_values(self):
        chain = FallbackChain(name="test", providers=["p1"])
        assert chain.context_budget_action == "off"
        assert chain.context_budget_warn_threshold == 0.80
        assert chain.context_budget_trim_threshold == 0.90
        assert chain.context_budget_trim_target == 0.75
        assert chain.context_budget_preserve_last_n == 4

    def test_provider_max_context_tokens_default(self):
        provider = ProviderConfig(
            name="test",
            base_url="http://localhost:8080/v1",
            model="qwen3",
        )
        assert provider.max_context_tokens is None

    def test_provider_max_context_tokens_explicit(self):
        provider = ProviderConfig(
            name="test",
            base_url="http://localhost:8080/v1",
            model="qwen3",
            max_context_tokens=32768,
        )
        assert provider.max_context_tokens == 32768

    def test_threshold_bounds(self):
        """Thresholds must be between 0.1 and 1.0."""
        with pytest.raises((ValueError, ValidationError)):
            FallbackChain(
                name="test",
                providers=["p1"],
                context_budget_warn_threshold=0.0,  # below min
            )

    def test_action_literals(self):
        """Only off/warn/trim are accepted."""
        with pytest.raises((ValueError, ValidationError)):
            FallbackChain(
                name="test",
                providers=["p1"],
                context_budget_action="compress",  # not valid
            )


# ---------------------------------------------------------------------------
# Group 4: Engine integration (apply_context_budget public method)
# ---------------------------------------------------------------------------


class TestEngineApplyContextBudget:
    """Integration tests for FallbackEngine.apply_context_budget().

    Exercises the full path: profile resolution → chain lookup →
    guard dispatch → status string, without network calls.
    """

    def _make_config(
        self,
        action: str = "off",
        max_context_tokens: int = 100,
        warn_threshold: float = 0.80,
        trim_threshold: float = 0.90,
        trim_target: float = 0.75,
    ) -> CodeRouterConfig:
        return CodeRouterConfig(
            allow_paid=False,
            default_profile="default",
            providers=[
                ProviderConfig(
                    name="local",
                    base_url="http://localhost:8080/v1",
                    model="qwen3",
                    max_context_tokens=max_context_tokens,
                ),
            ],
            profiles=[
                FallbackChain(
                    name="default",
                    providers=["local"],
                    context_budget_action=action,
                    context_budget_warn_threshold=warn_threshold,
                    context_budget_trim_threshold=trim_threshold,
                    context_budget_trim_target=trim_target,
                ),
            ],
        )

    def _make_engine(self, config: CodeRouterConfig):
        from coderouter.routing import FallbackEngine

        return FallbackEngine(config)

    def test_off_returns_none(self):
        """action=off → guard is no-op, status is None."""
        config = self._make_config(action="off")
        engine = self._make_engine(config)
        request = _make_request([("user", "x" * 400)])  # 100 tokens = 100% of 100
        result_req, status = engine.apply_context_budget(request)
        assert status is None
        assert len(result_req.messages) == 1  # unchanged

    def test_warn_below_threshold_returns_none(self):
        """action=warn but below warn threshold → None."""
        config = self._make_config(action="warn", max_context_tokens=1000)
        engine = self._make_engine(config)
        request = _make_request([("user", "x" * 100)])  # 25 tokens / 1000 = 2.5%
        _, status = engine.apply_context_budget(request)
        assert status is None

    def test_warn_over_threshold_returns_warning(self):
        """action=warn and over warn threshold → 'warning'."""
        config = self._make_config(action="warn", max_context_tokens=100)
        engine = self._make_engine(config)
        # 85 tokens * 4 = 340 chars → 85% > 80% warn threshold
        request = _make_request([("user", "x" * 340)])
        result_req, status = engine.apply_context_budget(request)
        assert status == "warning"
        assert len(result_req.messages) == 1  # not trimmed (warn only)

    def test_trim_over_trim_threshold_returns_trimmed(self):
        """action=trim and over trim threshold → 'trimmed'."""
        config = self._make_config(
            action="trim",
            max_context_tokens=100,
            warn_threshold=0.80,
            trim_threshold=0.90,
            trim_target=0.50,
        )
        engine = self._make_engine(config)
        # 10 messages of ~25 tokens each = ~250 tokens, well over 90%
        request = _make_long_request(10, chars_per_msg=96)
        result_req, status = engine.apply_context_budget(request)
        assert status == "trimmed"
        assert len(result_req.messages) < 10  # messages were removed

    def test_trim_over_warn_but_below_trim_returns_warning(self):
        """action=trim, over warn but below trim → 'warning' (no trim)."""
        config = self._make_config(
            action="trim",
            max_context_tokens=100,
            warn_threshold=0.80,
            trim_threshold=0.95,
        )
        engine = self._make_engine(config)
        # 85% usage → over warn (80%) but under trim (95%)
        request = _make_request([("user", "x" * 340)])
        result_req, status = engine.apply_context_budget(request)
        assert status == "warning"
        assert len(result_req.messages) == 1  # not trimmed


# ---------------------------------------------------------------------------
# Group 5: H-5 — tool-heavy sessions (the case the guard was written for)
#
# Up to v2.11.x the estimator counted only ``text`` blocks, so a Claude
# Code style conversation (context almost entirely in tool_result /
# tool_use) never crossed a threshold and the trim path was effectively
# dead code for exactly its target workload.
# ---------------------------------------------------------------------------

import gc  # noqa: E402
import time  # noqa: E402

from coderouter.token_estimation import (  # noqa: E402
    chars_to_tokens,
    count_message_chars,
    count_system_chars,
    estimate_tokens_from_anthropic_request,
)


def _tool_turn(
    index: int,
    *,
    assistant_text_chars: int = 200,
    tool_result_chars: int = 4000,
) -> list[tuple[str, str | list]]:
    """One agentic turn: assistant(text + tool_use) → user(tool_result)."""
    return [
        (
            "assistant",
            [
                {"type": "text", "text": "step " + "t" * assistant_text_chars},
                {
                    "type": "tool_use",
                    "id": f"toolu_{index}",
                    "name": "Read",
                    "input": {"file_path": f"/repo/src/mod_{index}.py"},
                },
            ],
        ),
        (
            "user",
            [
                {
                    "type": "tool_result",
                    "tool_use_id": f"toolu_{index}",
                    "content": "f" * tool_result_chars,
                }
            ],
        ),
    ]


def _tool_heavy_request(
    turns: int,
    *,
    lead_user: bool = True,
    assistant_text_chars: int = 200,
    tool_result_chars: int = 4000,
    system: str | None = "You are a coding agent. " + "s" * 200,
) -> AnthropicRequest:
    messages: list[tuple[str, str | list]] = []
    if lead_user:
        messages.append(("user", "please refactor the repo " + "q" * 100))
    for i in range(turns):
        messages.extend(
            _tool_turn(
                i,
                assistant_text_chars=assistant_text_chars,
                tool_result_chars=tool_result_chars,
            )
        )
    return _make_request(messages, system=system)


class TestToolHeavySessions:
    def test_tool_heavy_session_crosses_warn_threshold(self):
        """60 tool-driven turns cross 80% of a 32K window (v2.11.x: never).

        The same conversation scored under 6% with the old text-only
        estimator, which is why the guard never fired for agent clients.
        """
        request = _tool_heavy_request(60)
        estimate = estimate_context_usage(
            request, max_context_tokens=32768, warn_threshold=0.80
        )
        assert estimate.over_warn_threshold, (
            f"tool-heavy session estimated at only {estimate.estimated_tokens} "
            "tokens — tool_result content is not being counted"
        )

        # v2.11.x reference: the exact same request with tool content off.
        v2_11 = estimate_tokens_from_anthropic_request(
            system=request.system,
            messages=request.messages,
            include_tool_content=False,
        )
        assert v2_11 / 32768 < 0.80, "fixture no longer isolates the H-5 delta"
        assert estimate.estimated_tokens > v2_11 * 5

    def test_trim_never_returns_empty_messages(self):
        """Regression: trim must never hand back ``messages: []``.

        With a preserved tail of ``[assistant(tool_use),
        user(tool_result), assistant(tool_use), user(tool_result)]``
        every surviving message matches ``_normalize_head``'s drop
        condition, so the naive loop returned ``[]``.
        ``AnthropicRequest`` declares no ``min_length`` on ``messages``,
        so pydantic forwards it and the upstream API answers 400 —
        killing the very session the guard exists to protect.
        """
        request = _tool_heavy_request(40, assistant_text_chars=2000)
        trimmed, result = trim_to_budget(
            request,
            max_context_tokens=8000,
            trim_target=0.75,
            preserve_last_n=4,
        )
        assert result.messages_removed > 0, "fixture did not trigger a trim"
        assert len(trimmed.messages) >= 1, (
            f"trim emptied the conversation: {result}"
        )
        assert trimmed.messages != []

    def test_trim_head_is_clean_user_with_tool_heavy_tail(self):
        """The surviving head is a ``user`` message without a tool_result."""
        request = _tool_heavy_request(40, assistant_text_chars=2000)
        trimmed, _result = trim_to_budget(
            request,
            max_context_tokens=8000,
            trim_target=0.75,
            preserve_last_n=4,
        )
        head = trimmed.messages[0]
        assert head.role == "user"
        assert not _msg_has_tool_result(head)

    def test_trim_head_clean_user_under_aggressive_budget(self):
        """Same invariant with an impossibly small window."""
        request = _tool_heavy_request(20, assistant_text_chars=1000)
        trimmed, _result = trim_to_budget(
            request,
            max_context_tokens=100,
            trim_target=0.50,
            preserve_last_n=4,
        )
        assert len(trimmed.messages) >= 1
        assert trimmed.messages[0].role == "user"
        assert not _msg_has_tool_result(trimmed.messages[0])

    def test_trim_keeps_tool_pairs_intact_when_tool_content_counted(self):
        """No orphaned tool_result survives the tool-heavy trim path."""
        request = _tool_heavy_request(30, assistant_text_chars=800)
        trimmed, _result = trim_to_budget(
            request,
            max_context_tokens=6000,
            trim_target=0.60,
            preserve_last_n=4,
        )
        live_use_ids: set[str] = set()
        for msg in trimmed.messages:
            content = msg.content
            if not isinstance(content, list):
                continue
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    live_use_ids.add(block["id"])
        for msg in trimmed.messages:
            content = msg.content
            if not isinstance(content, list):
                continue
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    assert block["tool_use_id"] in live_use_ids, (
                        "orphaned tool_result survived the trim"
                    )


def _msg_has_tool_result(msg) -> bool:
    content = msg.content
    if not isinstance(content, list):
        return False
    return any(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in content
    )


# ---------------------------------------------------------------------------
# Group 6: trim cost + incremental-estimate equivalence
# ---------------------------------------------------------------------------


class TestTrimPerformance:
    def test_trim_is_linear_in_message_count(self):
        """800 messages must trim in well under 50ms.

        The old loop rebuilt the kept list and re-walked every surviving
        message's characters after each dropped unit — O(units x chars).
        On this fixture (a realistic tool-heavy session, now that
        tool_result content actually has weight) that measured 217ms of
        blocked event loop, which the ingress pays synchronously on the
        request path. The incremental version does it in ~4ms.
        """
        request = _tool_heavy_request(400)  # 801 messages
        # Measure the algorithm, not a gen-2 GC pass that happens to land in
        # the window: earlier tests leave enough live objects that a full
        # collection here took ~100 ms on CI (v2.18.0 PR) and flaked the bound.
        gc.collect()
        gc.disable()
        try:
            start = time.perf_counter()
            trimmed, result = trim_to_budget(
                request,
                max_context_tokens=8000,
                trim_target=0.75,
                preserve_last_n=4,
            )
            elapsed = time.perf_counter() - start
        finally:
            gc.enable()
        assert result.messages_removed > 700, "fixture did not exercise the loop"
        assert len(trimmed.messages) >= 1
        assert elapsed < 0.05, f"trim of 801 messages took {elapsed * 1000:.1f}ms"

    def test_trim_is_linear_for_plain_text_history_too(self):
        """Same cost bound for a plain (non-tool) 800-message history."""
        request = _make_long_request(800, chars_per_msg=400)
        gc.collect()
        gc.disable()  # see test_trim_is_linear_in_message_count
        try:
            start = time.perf_counter()
            _trimmed, result = trim_to_budget(
                request,
                max_context_tokens=1000,
                trim_target=0.75,
                preserve_last_n=4,
            )
            elapsed = time.perf_counter() - start
        finally:
            gc.enable()
        assert result.messages_removed > 700
        assert elapsed < 0.05, f"trim of 800 messages took {elapsed * 1000:.1f}ms"

    def test_trim_estimate_matches_full_recompute(self):
        """Incremental char accounting == a full re-estimate, exactly.

        The loop subtracts per-message character counts as it drops
        units instead of re-summing. That is only equivalent because the
        char/4 floor division is applied once to the grand total — this
        test pins that invariant (and the reported ``estimated_tokens_
        after``) rather than trusting it.
        """
        for turns, budget in ((10, 4000), (25, 8000), (60, 20000)):
            request = _tool_heavy_request(turns, assistant_text_chars=300)
            trimmed, result = trim_to_budget(
                request,
                max_context_tokens=budget,
                trim_target=0.75,
                preserve_last_n=4,
            )
            full = estimate_tokens_from_anthropic_request(
                system=trimmed.system, messages=trimmed.messages
            )
            assert result.estimated_tokens_after == full

            incremental = chars_to_tokens(
                count_system_chars(trimmed.system)
                + sum(count_message_chars(m) for m in trimmed.messages)
            )
            assert incremental == full

            before_incremental = chars_to_tokens(
                count_system_chars(request.system)
                + sum(count_message_chars(m) for m in request.messages)
            )
            assert result.estimated_tokens_before == before_incremental

    def test_zero_char_units_are_not_dropped(self):
        """A unit that frees no budget is not sacrificed for nothing."""
        image_block = {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": "Z" * 2000},
        }
        messages: list[tuple[str, str | list]] = [
            ("user", "A" * 8000),
            # A pasted screenshot with no prose: 0 chars by design, so
            # dropping it frees no budget whatsoever.
            ("user", [image_block]),
            ("user", "B" * 8000),
            # preserve_last_n=4 pins the four messages below.
            ("user", "x" * 400),
            ("assistant", "y" * 400),
            ("user", "z" * 400),
            ("assistant", "w" * 400),
        ]
        request = _make_request(messages, system="sys")
        trimmed, _result = trim_to_budget(
            request,
            max_context_tokens=1000,
            trim_target=0.50,
            preserve_last_n=4,
        )
        kept_is_image_only = [
            m
            for m in trimmed.messages
            if isinstance(m.content, list)
            and m.content
            and all(
                isinstance(b, dict) and b.get("type") == "image" for b in m.content
            )
        ]
        assert kept_is_image_only, "an image-only turn was dropped for zero budget gain"


# ---------------------------------------------------------------------------
# Group 7: the restored head must itself fit the budget
#
# Re-attaching a dropped clean ``user`` message keeps the request legal,
# but picking the *newest* one unconditionally is a trap: in an agent
# session the newest clean user message is very often a huge pasted
# file, so restoring it undoes the whole trim and reports
# ``status="trimmed"`` on a request that the upstream API still rejects.
# ---------------------------------------------------------------------------


def _paste_then_tool_loop(
    paste_chars: int = 200_000,
    turns: int = 25,
    *,
    lead: str | None = "help me refactor this",
) -> AnthropicRequest:
    """The canonical Claude Code shape: big paste, then a tool loop.

    Every message after the paste is either an assistant ``tool_use`` or
    a ``tool_result``-carrying user turn, so once the paste is dropped
    nothing in the surviving window can serve as the request head.
    """
    messages: list[tuple[str, str | list]] = []
    if lead is not None:
        messages.append(("user", lead))
    messages.append(("user", "Here is the file:\n" + "x" * paste_chars))
    for i in range(turns):
        messages.append(
            (
                "assistant",
                [
                    {
                        "type": "tool_use",
                        "id": f"t{i}",
                        "name": "Read",
                        "input": {"file_path": "/a/b.py"},
                    }
                ],
            )
        )
        messages.append(
            (
                "user",
                [
                    {
                        "type": "tool_result",
                        "tool_use_id": f"t{i}",
                        "content": "src line\n" * 300,
                    }
                ],
            )
        )
    return _make_request(messages, system="sys")


class TestRestoredHeadRespectsBudget:
    def test_trim_restore_head_stays_within_budget(self):
        """Restoring a head must not undo the trim.

        Budget-blind "newest clean user wins" put the 200 KB paste back
        on the front: 67,066 tokens in, 67,061 tokens out, against a
        24,576 target and a 32,768 window — logged as ``trimmed`` and
        then 400'd upstream.
        """
        request = _paste_then_tool_loop()
        trimmed, result = trim_to_budget(
            request,
            max_context_tokens=32768,
            trim_target=0.75,
            preserve_last_n=4,
        )
        target_tokens = int(32768 * 0.75)
        assert result.estimated_tokens_before > 32768, "fixture is not over budget"
        assert result.estimated_tokens_after <= target_tokens, (
            f"trim did not reach its target: {result}"
        )
        assert result.estimated_tokens_after <= 32768
        # And the head is still a legal opening turn.
        assert len(trimmed.messages) >= 1
        assert trimmed.messages[0].role == "user"
        assert not _msg_has_tool_result(trimmed.messages[0])

    def test_trim_restores_an_affordable_clean_user_not_the_newest(self):
        """Among dropped clean users, pick the newest one that *fits*."""
        request = _paste_then_tool_loop(lead="help me refactor this")
        trimmed, _result = trim_to_budget(
            request,
            max_context_tokens=32768,
            trim_target=0.75,
            preserve_last_n=4,
        )
        head = trimmed.messages[0]
        assert head.content == "help me refactor this"

    def test_trim_prefers_most_recent_affordable_candidate(self):
        """Recency still wins between two candidates that both fit."""
        messages: list[tuple[str, str | list]] = [
            ("user", "OLD standing instruction"),
            ("user", "NEW standing instruction"),
            ("user", "Here is the file:\n" + "x" * 200_000),
        ]
        for i in range(25):
            messages.append(
                (
                    "assistant",
                    [{"type": "tool_use", "id": f"t{i}", "name": "Read", "input": {}}],
                )
            )
            messages.append(
                (
                    "user",
                    [
                        {
                            "type": "tool_result",
                            "tool_use_id": f"t{i}",
                            "content": "src line\n" * 300,
                        }
                    ],
                )
            )
        request = _make_request(messages, system="sys")
        trimmed, result = trim_to_budget(
            request,
            max_context_tokens=32768,
            trim_target=0.75,
            preserve_last_n=4,
        )
        assert trimmed.messages[0].content == "NEW standing instruction"
        assert result.estimated_tokens_after <= int(32768 * 0.75)

    def test_trim_uses_synthetic_head_when_no_candidate_fits(self):
        """No affordable real head → a tiny synthetic one, still in budget."""
        from coderouter.guards.context_budget import TRIM_PLACEHOLDER_HEAD_TEXT

        # The only clean user message in the whole conversation is the
        # 200 KB paste, so nothing droppable can be afforded back.
        request = _paste_then_tool_loop(lead=None)
        trimmed, result = trim_to_budget(
            request,
            max_context_tokens=32768,
            trim_target=0.75,
            preserve_last_n=4,
        )
        head = trimmed.messages[0]
        assert head.role == "user"
        assert head.content == TRIM_PLACEHOLDER_HEAD_TEXT
        assert not _msg_has_tool_result(head)
        assert result.estimated_tokens_after <= int(32768 * 0.75)

    @pytest.mark.parametrize("window", [4096, 8192, 16384, 32768, 65536])
    def test_trim_lands_inside_the_window_across_budgets(self, window: int):
        """Whatever the window, the trimmed request fits inside it."""
        request = _paste_then_tool_loop()
        trimmed, result = trim_to_budget(
            request,
            max_context_tokens=window,
            trim_target=0.75,
            preserve_last_n=4,
        )
        assert len(trimmed.messages) >= 1
        if result.estimated_tokens_after > window:
            # Only ever acceptable when the preserve floor alone cannot
            # fit — never because of the restored head.
            floor = estimate_tokens_from_anthropic_request(
                system=request.system, messages=request.messages[-4:]
            )
            assert floor > window, (
                f"over-window at {window} but the preserve floor ({floor}) fits — "
                f"the restored head caused it: {result}"
            )


# ---------------------------------------------------------------------------
# v2.16.0: context_budget_action: reject
# ---------------------------------------------------------------------------


class TestContextBudgetReject:
    # Reuse the builders without re-running the parent class's tests.
    _make_config = TestEngineApplyContextBudget._make_config
    _make_engine = TestEngineApplyContextBudget._make_engine

    def test_reject_over_threshold_raises_prompt_is_too_long(self):
        from coderouter.guards.context_budget import ContextBudgetRejectError

        config = self._make_config(action="reject", max_context_tokens=100)
        engine = self._make_engine(config)
        request = _make_long_request(10, chars_per_msg=96)
        with pytest.raises(ContextBudgetRejectError) as info:
            engine.apply_context_budget(request)
        assert str(info.value).startswith("prompt is too long")
        assert info.value.max_context_tokens == 100

    def test_reject_between_warn_and_threshold_only_warns(self):
        config = self._make_config(
            action="reject", max_context_tokens=100, warn_threshold=0.80, trim_threshold=0.95
        )
        engine = self._make_engine(config)
        request = _make_request([("user", "x" * 340)])  # 85%
        result_req, status = engine.apply_context_budget(request)
        assert status == "warning"
        assert len(result_req.messages) == 1

    def test_reject_reaches_client_as_anthropic_400(self, monkeypatch):
        from fastapi.testclient import TestClient

        from coderouter.ingress.app import create_app
        from coderouter.routing import FallbackEngine

        config = self._make_config(action="reject", max_context_tokens=100)
        monkeypatch.setattr("coderouter.ingress.app.load_config", lambda path=None: config)
        app = create_app()
        app.state.engine = FallbackEngine(config)
        app.state.config = config
        body = {
            "model": "claude-sonnet-5",
            "max_tokens": 16,
            "messages": [{"role": "user", "content": "x" * 2000}],
        }
        for stream in (False, True):
            resp = TestClient(app).post("/v1/messages", json={**body, "stream": stream})
            assert resp.status_code == 400, resp.text
            payload = resp.json()
            assert payload["error"]["type"] == "invalid_request_error"
            assert payload["error"]["message"].startswith("prompt is too long")
