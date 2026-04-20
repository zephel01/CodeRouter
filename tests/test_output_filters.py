"""Unit tests for the v1.0-A ``output_filters`` filter chain.

Covers the three public surfaces:
    1. Streaming filter state — chunk-boundary correctness for
       ``strip_thinking`` and ``strip_stop_markers``.
    2. Chain composition — ordered application + ``any_applied`` /
       ``applied_filters`` dedupe shape.
    3. Schema validation — bad filter names fail at config load
       (tested directly via ``ProviderConfig`` in test_config.py;
       here we exercise the module's own validate_output_filters).

Adapter integration tests live in test_openai_compat_output_filters.py
and test_adapter_anthropic.py so this file stays the "pure unit" layer.
"""

from __future__ import annotations

import pytest

from coderouter.output_filters import (
    DEFAULT_STOP_MARKERS,
    KNOWN_FILTERS,
    OutputFilterChain,
    StripStopMarkersFilter,
    StripThinkingFilter,
    apply_output_filters,
    validate_output_filters,
)

# ======================================================================
# validate_output_filters
# ======================================================================


def test_validate_accepts_empty_list() -> None:
    # Default config has `output_filters: []` — must validate silently.
    validate_output_filters([])


def test_validate_accepts_known_filters() -> None:
    validate_output_filters(["strip_thinking"])
    validate_output_filters(["strip_stop_markers"])
    validate_output_filters(["strip_thinking", "strip_stop_markers"])


def test_validate_rejects_unknown_filter() -> None:
    """Typo like ``strp_thinking`` must fail loudly with a hint."""
    with pytest.raises(ValueError) as excinfo:
        validate_output_filters(["strp_thinking"])
    msg = str(excinfo.value)
    assert "strp_thinking" in msg
    # The error enumerates known filters so fixing the typo is one line.
    assert "strip_thinking" in msg


def test_validate_rejects_when_mixing_known_and_unknown() -> None:
    with pytest.raises(ValueError):
        validate_output_filters(["strip_thinking", "bogus"])


def test_registry_matches_expected_set() -> None:
    """Regression: if a filter is added, this test reminds us to doc it."""
    assert set(KNOWN_FILTERS) == {"strip_thinking", "strip_stop_markers"}


# ======================================================================
# StripThinkingFilter — non-streaming (eof=True on the only feed)
# ======================================================================


def test_strip_thinking_removes_block_in_single_feed() -> None:
    f = StripThinkingFilter()
    out = f.feed("hello <think>hidden</think> world", eof=True)
    assert out == "hello  world"
    assert f.modified is True


def test_strip_thinking_no_match_passes_through() -> None:
    f = StripThinkingFilter()
    out = f.feed("plain reply, no tags", eof=True)
    assert out == "plain reply, no tags"
    assert f.modified is False


def test_strip_thinking_handles_multiple_blocks() -> None:
    f = StripThinkingFilter()
    out = f.feed(
        "<think>a</think>visible<think>b</think>more",
        eof=True,
    )
    assert out == "visiblemore"
    assert f.modified is True


def test_strip_thinking_unmatched_open_at_eof_drops_tail() -> None:
    """Dangling ``<think>`` at EOF suppresses the remaining stream."""
    f = StripThinkingFilter()
    out = f.feed("ok <think>never closed", eof=True)
    assert out == "ok "
    assert f.modified is True


# ======================================================================
# StripThinkingFilter — streaming (chunk-boundary correctness)
# ======================================================================


def test_strip_thinking_streaming_tag_split_across_chunks() -> None:
    """``<think>`` split mid-tag between two chunks must still be scrubbed."""
    f = StripThinkingFilter()
    out1 = f.feed("hello <thi")
    out2 = f.feed("nk>hidden</think> world", eof=True)
    assert (out1 + out2) == "hello  world"
    assert f.modified is True


def test_strip_thinking_streaming_close_split_across_chunks() -> None:
    f = StripThinkingFilter()
    out1 = f.feed("<think>a")
    out2 = f.feed("b</thi")
    out3 = f.feed("nk>tail", eof=True)
    assert (out1 + out2 + out3) == "tail"
    assert f.modified is True


def test_strip_thinking_streaming_holds_back_lt_without_tag() -> None:
    """A bare ``<`` at a chunk boundary must be held back, not emitted early."""
    f = StripThinkingFilter()
    out1 = f.feed("abc<")
    # Can't know yet — could be `<think>` or just `<foo>` prose.
    assert "<" not in out1
    out2 = f.feed("def", eof=True)
    assert (out1 + out2) == "abc<def"


def test_strip_thinking_streaming_emits_prefix_before_open() -> None:
    """Text before the first ``<think>`` must stream through immediately."""
    f = StripThinkingFilter()
    out1 = f.feed("hello world ")
    assert out1 == "hello world "
    out2 = f.feed("<think>hidden</think>done", eof=True)
    assert (out1 + out2) == "hello world done"


def test_strip_thinking_eof_without_anything_pending() -> None:
    """EOF on an empty stream must not crash."""
    f = StripThinkingFilter()
    assert f.feed("", eof=True) == ""
    assert f.modified is False


# ======================================================================
# StripStopMarkersFilter
# ======================================================================


def test_strip_stop_markers_removes_all_defaults() -> None:
    f = StripStopMarkersFilter()
    text = "a<|turn|>b<|end|>c<|python_tag|>d<|im_end|>e<|eot_id|>f<|channel>thoughtg"
    out = f.feed(text, eof=True)
    assert out == "abcdefg"
    assert f.modified is True


def test_strip_stop_markers_no_op_on_clean_text() -> None:
    f = StripStopMarkersFilter()
    out = f.feed("plain response with no markers", eof=True)
    assert out == "plain response with no markers"
    assert f.modified is False


def test_strip_stop_markers_streaming_split_marker() -> None:
    """A marker split across two chunks must still be scrubbed."""
    f = StripStopMarkersFilter()
    out1 = f.feed("a<|pyth")
    out2 = f.feed("on_tag|>b", eof=True)
    assert (out1 + out2) == "ab"
    assert f.modified is True


def test_strip_stop_markers_streaming_holds_back_lt_pipe() -> None:
    """``<|`` at chunk end must be held back — could be start of any marker."""
    f = StripStopMarkersFilter()
    out1 = f.feed("abc<|")
    assert "<|" not in out1
    out2 = f.feed("turn|>tail", eof=True)
    assert (out1 + out2) == "abctail"


def test_strip_stop_markers_earliest_match_wins() -> None:
    """Two markers in the same chunk — both get stripped, order preserved."""
    f = StripStopMarkersFilter()
    out = f.feed("x<|turn|>y<|end|>z", eof=True)
    assert out == "xyz"


def test_strip_stop_markers_eof_flushes_non_marker_suffix() -> None:
    """Text ending mid-``<|`` at EOF → the held-back suffix is flushed."""
    f = StripStopMarkersFilter()
    out = f.feed("prefix<|", eof=True)
    # `<|` alone is not a marker — at EOF it's safe to release.
    assert out == "prefix<|"


def test_default_stop_markers_contents() -> None:
    """Lock the default marker set — changes require a CHANGELOG note."""
    assert set(DEFAULT_STOP_MARKERS) == {
        "<|turn|>",
        "<|end|>",
        "<|python_tag|>",
        "<|im_end|>",
        "<|eot_id|>",
        "<|channel>thought",
    }


# ======================================================================
# OutputFilterChain — composition + bookkeeping
# ======================================================================


def test_chain_empty_is_identity_and_not_applied() -> None:
    chain = OutputFilterChain([])
    assert chain.is_empty is True
    assert chain.feed("hello", eof=True) == "hello"
    assert chain.any_applied is False
    assert chain.applied_filters() == []


def test_chain_single_filter_tracks_applied() -> None:
    chain = OutputFilterChain(["strip_thinking"])
    out = chain.feed("a<think>b</think>c", eof=True)
    assert out == "ac"
    assert chain.any_applied is True
    assert chain.applied_filters() == ["strip_thinking"]


def test_chain_two_filters_apply_in_order() -> None:
    """``strip_thinking`` must run before ``strip_stop_markers`` — the
    markers inside a thinking block get suppressed by the first filter,
    so the second never sees them (and its ``modified`` stays False)."""
    chain = OutputFilterChain(["strip_thinking", "strip_stop_markers"])
    out = chain.feed("a<think>b<|turn|>c</think>d", eof=True)
    assert out == "ad"
    assert chain.any_applied is True
    assert chain.applied_filters() == ["strip_thinking"]


def test_chain_second_filter_still_fires_on_visible_text() -> None:
    chain = OutputFilterChain(["strip_thinking", "strip_stop_markers"])
    out = chain.feed("a<think>x</think>b<|end|>c", eof=True)
    assert out == "abc"
    assert chain.applied_filters() == ["strip_thinking", "strip_stop_markers"]


def test_chain_names_returns_declaration_order() -> None:
    chain = OutputFilterChain(["strip_stop_markers", "strip_thinking"])
    assert chain.names == ["strip_stop_markers", "strip_thinking"]


def test_chain_validates_unknown_at_construction() -> None:
    with pytest.raises(ValueError):
        OutputFilterChain(["bogus"])


def test_chain_preserves_state_across_feeds() -> None:
    """A chain is intentionally stateful — the caller holds it for the
    lifetime of a stream and feeds each SSE delta through it."""
    chain = OutputFilterChain(["strip_thinking"])
    out_a = chain.feed("hello <thi")
    out_b = chain.feed("nk>hidden</think> world", eof=True)
    assert (out_a + out_b) == "hello  world"


# ======================================================================
# apply_output_filters — non-streaming convenience
# ======================================================================


def test_apply_output_filters_returns_applied_subset() -> None:
    out, applied = apply_output_filters(
        ["strip_thinking", "strip_stop_markers"],
        "a<think>x</think>b",
    )
    assert out == "ab"
    assert applied == ["strip_thinking"]


def test_apply_output_filters_empty_chain_is_identity() -> None:
    out, applied = apply_output_filters([], "a<|turn|>b")
    # Empty list → no filtering; markers survive.
    assert out == "a<|turn|>b"
    assert applied == []


def test_apply_output_filters_clean_text_no_applied() -> None:
    out, applied = apply_output_filters(["strip_thinking", "strip_stop_markers"], "plain text")
    assert out == "plain text"
    assert applied == []
