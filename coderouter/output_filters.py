"""v1.0-A: Declarative output filter chain.

Context
-------
Quantized local models (and some cloud families) leak harness-internal
markers into the assistant text stream that spec-strict clients either
display verbatim or outright reject:

    ``<think>...</think>``       — Qwen3 / DeepSeek-R1-Distill thinking track.
    ``<|turn|>`` / ``<|end|>``   — Gemma-ish turn separators.
    ``<|python_tag|>``           — Llama 3.x tool-use marker.
    ``<|im_end|>``               — ChatML / Qwen end-of-turn.
    ``<|eot_id|>``               — Llama 3.x end-of-turn.
    ``<|channel>thought``        — OpenAI-harmony channel marker.

``capabilities.reasoning_passthrough`` (v0.5-C) only governs the
non-standard ``message.reasoning`` field; markers embedded in the
assistant *content* slip past it. v1.0-A adds a declarative, per-
provider opt-in chain that scrubs them at the adapter boundary.

Design decisions
----------------
- Opt-in per provider via ``output_filters: [strip_thinking, ...]``.
  Empty by default — the existing v0.5-C passive strip remains
  orthogonal, and providers that WANT the thinking track (e.g.
  CodeRouter fronting a reasoning-aware downstream) stay untouched.
- Stateful streaming: a filter instance holds state across ``feed()``
  calls so a ``<think>...</think>`` block spanning multiple SSE deltas
  is coalesced. Safe-to-emit suffix management is explicit — callers
  never see partial tags.
- Non-streaming convenience: ``apply_output_filters(names, text)``
  creates a chain, feeds with ``eof=True``, returns the scrubbed text.
- Unknown filter names raise ``ValueError`` at chain construction, so
  ``schemas.ProviderConfig`` wires this through a ``model_validator``
  and bad configs fail at load time rather than on first request.
- Pairs with ``coderouter doctor`` (v0.7-B): the reasoning-leak probe
  is extended in this same sub-release to detect content-embedded
  ``<think>`` and suggest ``output_filters: [strip_thinking]``.

Reference: plan.md §10.2 "出力クリーニング" / docs/retrospectives/v0.7.md
"transformation には probe が伴う".
"""

from __future__ import annotations

from typing import Protocol

__all__ = [
    "DEFAULT_STOP_MARKERS",
    "KNOWN_FILTERS",
    "OutputFilter",
    "OutputFilterChain",
    "StripStopMarkersFilter",
    "StripThinkingFilter",
    "apply_output_filters",
    "validate_output_filters",
]


# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------


DEFAULT_STOP_MARKERS: tuple[str, ...] = (
    "<|turn|>",
    "<|end|>",
    "<|python_tag|>",
    "<|im_end|>",
    "<|eot_id|>",
    "<|channel>thought",
)
"""Default stop/harness markers stripped by ``strip_stop_markers``.

Covers Llama 3.x (``<|python_tag|>``, ``<|eot_id|>``), ChatML / Qwen
(``<|im_end|>``, ``<|end|>``), Gemma-ish (``<|turn|>``) and OpenAI-
harmony (``<|channel>thought``). Extending this tuple is an ABI change
— users who need a bespoke set can add a dedicated filter entry in
a later minor; for v1.0-A the fixed list covers observed leaks.
"""


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


class OutputFilter(Protocol):
    """Stateful streaming text filter.

    Implementations MUST:
        - Tolerate arbitrary chunking: partial tags at the end of a
          ``feed`` input must be buffered and re-examined on the next
          call. The caller will invoke ``feed(..., eof=True)`` exactly
          once at the end to flush any remaining buffer.
        - Set ``modified`` to True the first time any character in the
          input stream would be suppressed or rewritten (regardless of
          whether that specific ``feed`` call produced visible output).
        - Be cheap to construct — one instance per request/stream.
    """

    name: str
    modified: bool

    def feed(self, text: str, *, eof: bool = False) -> str:
        """Consume ``text`` and return the portion safe to emit now."""
        ...


# ---------------------------------------------------------------------------
# Helper: find how much of the trailing buffer could be a prefix of `needle`
# ---------------------------------------------------------------------------


def _max_suffix_overlap(buffer: str, needle: str) -> int:
    """Return the longest N where ``buffer[-N:]`` equals ``needle[:N]``.

    Used to decide how much of the trailing buffer to hold back so a
    partial tag spanning chunk boundaries is not prematurely emitted.
    Zero means the buffer ends on a character that cannot be the start
    of ``needle``, so every byte is safe to release.
    """
    max_k = min(len(buffer), len(needle) - 1)
    for k in range(max_k, 0, -1):
        if buffer.endswith(needle[:k]):
            return k
    return 0


def _max_suffix_overlap_multi(buffer: str, needles: tuple[str, ...]) -> int:
    """``_max_suffix_overlap`` lifted over a tuple of needles — take the max."""
    best = 0
    for needle in needles:
        k = _max_suffix_overlap(buffer, needle)
        if k > best:
            best = k
    return best


# ---------------------------------------------------------------------------
# strip_thinking
# ---------------------------------------------------------------------------


_THINK_OPEN = "<think>"
_THINK_CLOSE = "</think>"


class StripThinkingFilter:
    """Remove ``<think>...</think>`` blocks from assistant content.

    State spans ``feed`` calls so a block split across SSE chunks (or
    across a single prose paragraph containing both tags) is handled
    correctly. The filter does NOT attempt to preserve balanced nesting
    — the first ``</think>`` after a ``<think>`` closes the block.
    Unmatched open tag at EOF is suppressed (the entire remainder of
    the stream is treated as thinking).

    ``modified`` flips True on the first ``<think>`` observed in the
    stream, not on every suppressed character — the adapter uses it to
    gate a log-once info line.
    """

    name = "strip_thinking"

    def __init__(self) -> None:
        """Initialize the per-request buffer + in-think state to empty."""
        self.modified: bool = False
        self._in_think: bool = False
        self._buffer: str = ""

    def feed(self, text: str, *, eof: bool = False) -> str:
        """Append ``text`` to the buffer and return the safe-to-emit prefix.

        Tags are matched greedily; a partial prefix at the buffer end
        is held back across calls so a ``<think>`` split across two
        SSE deltas is still recognized. At ``eof`` any unmatched open
        tag is silently dropped (remainder treated as thinking).
        """
        self._buffer += text
        out_parts: list[str] = []

        while True:
            if not self._in_think:
                idx = self._buffer.find(_THINK_OPEN)
                if idx != -1:
                    out_parts.append(self._buffer[:idx])
                    self._buffer = self._buffer[idx + len(_THINK_OPEN) :]
                    self._in_think = True
                    self.modified = True
                    continue
                # No open tag — emit all but a potential partial prefix.
                overlap = _max_suffix_overlap(self._buffer, _THINK_OPEN)
                if overlap:
                    out_parts.append(self._buffer[:-overlap])
                    self._buffer = self._buffer[-overlap:]
                else:
                    out_parts.append(self._buffer)
                    self._buffer = ""
                break
            # in_think: suppress until we find the close tag.
            idx = self._buffer.find(_THINK_CLOSE)
            if idx != -1:
                self._buffer = self._buffer[idx + len(_THINK_CLOSE) :]
                self._in_think = False
                continue
            # No close tag — retain potential partial suffix, drop the rest.
            overlap = _max_suffix_overlap(self._buffer, _THINK_CLOSE)
            self._buffer = self._buffer[-overlap:] if overlap else ""
            break

        if eof:
            if not self._in_think:
                # Flush any remaining buffer (known-safe at eof).
                out_parts.append(self._buffer)
            # If still in_think at eof, silently drop the partial block.
            self._buffer = ""
        return "".join(out_parts)


# ---------------------------------------------------------------------------
# strip_stop_markers
# ---------------------------------------------------------------------------


class StripStopMarkersFilter:
    """Remove harness/turn markers (``<|python_tag|>``, ``<|eot_id|>``, ...).

    Unlike ``strip_thinking`` this is a set of point deletions rather
    than a block suppression. The streaming concern is the same: any
    chunk boundary might land inside a marker, so a trailing partial
    prefix is held back until the next ``feed``.

    The marker list is :data:`DEFAULT_STOP_MARKERS` — fixed for v1.0-A.
    """

    name = "strip_stop_markers"

    def __init__(self, markers: tuple[str, ...] = DEFAULT_STOP_MARKERS) -> None:
        """Initialize with an optional custom marker set.

        The default :data:`DEFAULT_STOP_MARKERS` covers the observed
        Llama 3.x / ChatML / Qwen / Gemma / harmony leaks. Tests and
        future extensions may pass a bespoke tuple; v1.0-A does not
        expose this knob via providers.yaml.
        """
        self.modified: bool = False
        self._buffer: str = ""
        self._markers: tuple[str, ...] = markers

    def _earliest_match(self, buffer: str) -> tuple[int, str] | None:
        """Return (position, marker) of the earliest marker in ``buffer``."""
        best: tuple[int, str] | None = None
        for m in self._markers:
            idx = buffer.find(m)
            if idx == -1:
                continue
            if best is None or idx < best[0]:
                best = (idx, m)
        return best

    def feed(self, text: str, *, eof: bool = False) -> str:
        """Emit ``text`` minus any marker matches; buffer partial prefixes.

        A complete marker anywhere in the buffer is excised in place.
        A trailing partial prefix that could complete on the next
        :meth:`feed` is held back; at ``eof`` it is flushed verbatim
        (we only hide bytes that are definitively part of a marker).
        """
        self._buffer += text
        out_parts: list[str] = []

        while True:
            hit = self._earliest_match(self._buffer)
            if hit is None:
                break
            idx, marker = hit
            if idx:
                out_parts.append(self._buffer[:idx])
            self._buffer = self._buffer[idx + len(marker) :]
            self.modified = True

        # No complete match — hold back a potential partial suffix.
        overlap = _max_suffix_overlap_multi(self._buffer, self._markers)
        if overlap and not eof:
            out_parts.append(self._buffer[:-overlap])
            self._buffer = self._buffer[-overlap:]
        else:
            out_parts.append(self._buffer)
            self._buffer = ""

        return "".join(out_parts)


# ---------------------------------------------------------------------------
# Registry + chain
# ---------------------------------------------------------------------------


KNOWN_FILTERS: dict[str, type[OutputFilter]] = {
    StripThinkingFilter.name: StripThinkingFilter,
    StripStopMarkersFilter.name: StripStopMarkersFilter,
}
"""Registry of string-name → filter class.

Declared as a dict rather than a frozen mapping so tests and future
extensions can poke in additional filters without a schema change, but
adapter callers should treat it as read-only.
"""


def validate_output_filters(names: list[str]) -> None:
    """Raise ``ValueError`` if any name in ``names`` is not registered.

    Called from ``ProviderConfig`` at config-load time so a typo like
    ``output_filters: [strp_thinking]`` fails at startup rather than
    silently no-op'ing forever. The error message lists all known
    filter names so the fix is one line.
    """
    unknown = [n for n in names if n not in KNOWN_FILTERS]
    if unknown:
        raise ValueError(
            f"Unknown output_filters entries: {unknown}. Known filters: {sorted(KNOWN_FILTERS)}"
        )


class OutputFilterChain:
    """Ordered composition of ``OutputFilter`` instances.

    Each ``feed`` call pipes text through every filter in declaration
    order. ``any_applied`` is the disjunction of per-filter ``modified``
    flags — the adapter uses it to emit a single ``output-filter-
    applied`` info log the first time a request would have been
    affected (dedupe mirrors the v0.5-C reasoning-strip log-once).

    An empty chain is a legal no-op: ``feed`` returns ``text`` verbatim
    and ``any_applied`` never becomes True. Adapters can unconditionally
    thread a chain through their hot path without branching on
    ``output_filters == []``.
    """

    def __init__(self, filter_names: list[str]) -> None:
        """Construct a fresh chain of filters by name.

        Raises :class:`ValueError` via :func:`validate_output_filters`
        if any name is unknown — callers should be
        :class:`ProviderConfig` (validation happens at config-load
        time) so bad configs fail loudly at startup.
        """
        validate_output_filters(filter_names)
        self._filters: list[OutputFilter] = [KNOWN_FILTERS[n]() for n in filter_names]
        self._names: list[str] = list(filter_names)

    @property
    def names(self) -> list[str]:
        """Ordered list of filter names (for log payloads / debugging)."""
        return list(self._names)

    @property
    def is_empty(self) -> bool:
        """True when no filters were configured — lets callers skip the hot path."""
        return not self._filters

    @property
    def any_applied(self) -> bool:
        """True if ANY filter has modified text since construction."""
        return any(f.modified for f in self._filters)

    def applied_filters(self) -> list[str]:
        """Names of filters that actually modified text (subset of ``names``).

        Stable order — matches construction order. Useful in the log
        payload so operators can see exactly which filter triggered.
        """
        return [f.name for f in self._filters if f.modified]

    def feed(self, text: str, *, eof: bool = False) -> str:
        """Pipe ``text`` through every filter. ``eof`` flushes at end."""
        for f in self._filters:
            text = f.feed(text, eof=eof)
        return text


# ---------------------------------------------------------------------------
# Non-streaming convenience
# ---------------------------------------------------------------------------


def apply_output_filters(filter_names: list[str], text: str) -> tuple[str, list[str]]:
    """Run a one-shot chain over a complete text.

    Returns ``(scrubbed_text, applied_filter_names)``. The second
    element is the subset of ``filter_names`` that actually modified
    ``text`` — the adapter passes it into the log-once helper on
    non-streaming paths (streaming paths keep a live chain instead).

    This is equivalent to::

        chain = OutputFilterChain(filter_names)
        out = chain.feed(text, eof=True)
        applied = chain.applied_filters()
    """
    if not filter_names:
        return text, []
    chain = OutputFilterChain(filter_names)
    out = chain.feed(text, eof=True)
    return out, chain.applied_filters()
