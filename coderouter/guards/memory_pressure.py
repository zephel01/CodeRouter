"""Memory-pressure detection guard (v1.9-E phase 2, L2).

Local backends (Ollama / LM Studio / llama.cpp) report VRAM
exhaustion via HTTP 5xx with error bodies that include phrases like
``"out of memory"``, ``"CUDA out of memory"``, ``"insufficient
memory"``, etc. Without intervention, the engine retries against the
same backend on the very next request and trips the same OOM — wasted
latency, wasted tokens (when the failure happens after partial
generation), and an operator-visible cascade of 5xx in the dashboard.

This module gives the engine two pieces:

  1. A **stateless detector** :func:`is_memory_pressure_error` that
     decides "is this AdapterError an OOM-coded failure" from the
     error message text. Pure, no observability dependencies.
  2. A **stateful tracker** :class:`MemoryPressureGuard` that records
     "provider X is pressured until ts" entries (TTL-based cooldown)
     and answers ``is_pressured(provider)`` at chain-resolve time.

The combination lets the engine react to an observed OOM by skipping
the same provider for ``memory_pressure_cooldown_s`` seconds — the
chain falls through to the next provider, which is typically a
lighter-weight model or a remote fallback with the headroom.

Detection patterns
==================

Case-insensitive substring match against a curated phrase list; no
regex backtracking. Patterns chosen to match the actual error bodies
observed across:

  * **Ollama** ``/api/generate`` and ``/v1/chat/completions``:
    ``"model requires more system memory"``,
    ``"out of memory"``.
  * **LM Studio** ``/v1/messages`` and ``/v1/chat/completions``:
    ``"insufficient memory"``, ``"failed to load model"``.
  * **llama.cpp** ``llama-server``:
    ``"failed to allocate"``, ``"CUDA out of memory"``.
  * Generic CUDA / Metal patterns:
    ``"out of memory"``, ``"OOM"``.

False-positive risk is low because all patterns require the
substring "memory" or the literal "OOM" in the failure body — generic
HTTP errors don't include those words.

Concurrency
===========

The tracker is safe across asyncio tasks within one event loop and
across worker threads — every public method holds an ``RLock`` for
the body of its read/write.
"""

from __future__ import annotations

import threading
import time

from coderouter.adapters.base import AdapterError

# ---------------------------------------------------------------------------
# Detection patterns
# ---------------------------------------------------------------------------


# Lowercased substrings — tested against ``str(adapter_error).lower()``.
# Order doesn't matter (any-match short-circuits). Each entry is a phrase
# observed in a real backend OOM response. New patterns can be added
# defensively (false-positive risk is low; see module docstring).
_MEMORY_PRESSURE_PHRASES: tuple[str, ...] = (
    "out of memory",
    "cuda out of memory",
    "metal out of memory",
    "insufficient memory",
    "model requires more system memory",
    "failed to allocate",
    "failed to load model",
    "ggml_cuda_host_malloc",  # llama.cpp specific
    "oom",
)


def is_memory_pressure_error(exc: AdapterError) -> bool:
    """Return True iff ``exc`` looks like a backend OOM signal.

    Pure function. Operates on ``str(exc).lower()`` and checks for
    any of the curated phrases in :data:`_MEMORY_PRESSURE_PHRASES`.
    Callers in the engine wrap each ``provider-failed`` site to
    decide whether to mark the provider pressured.

    The check is intentionally **only** about the message text, not
    the HTTP status. Backends sometimes return 500 vs 503 for OOM
    inconsistently, and a 500 carrying ``"missing model"`` should
    NOT be treated as memory pressure (no cooldown helps recover
    from a config error). The phrase-match keeps the detector
    focused on the actual signal.
    """
    text = str(exc).lower()
    return any(phrase in text for phrase in _MEMORY_PRESSURE_PHRASES)


# ---------------------------------------------------------------------------
# Tracker
# ---------------------------------------------------------------------------


class MemoryPressureGuard:
    """In-memory per-provider OOM cooldown tracker.

    Public API:

    - :meth:`mark_pressured(provider, cooldown_s)` — start (or extend)
      a cooldown window for ``provider``. Idempotent: re-marking a
      pressured provider extends the deadline to ``now + cooldown_s``.
    - :meth:`is_pressured(provider)` — True iff the provider's
      cooldown deadline is in the future. Lazy expiry: when an
      expired entry is observed, it's swept out so subsequent reads
      see the entry-less default.
    - :meth:`pressured_until(provider)` — monotonic timestamp of the
      cooldown deadline (or 0.0 if not pressured). Useful for log
      payloads that want to surface the human-readable expiry.
    - :meth:`reset()` — drop all entries. Mainly for tests.

    Internal lock: an ``RLock`` covers every read/write pair so a
    concurrent ``mark_pressured`` from a failed call and an
    ``is_pressured`` from a chain resolve can't observe a torn
    state.

    Time source: ``time.monotonic`` so cooldowns are immune to
    wall-clock skew. Tests inject ``now=`` for determinism.
    """

    def __init__(self) -> None:
        self._lock: threading.RLock = threading.RLock()
        self._until: dict[str, float] = {}

    # ------------------------------------------------------------------
    # Mutating API
    # ------------------------------------------------------------------

    def mark_pressured(
        self,
        provider: str,
        cooldown_s: float,
        *,
        now: float | None = None,
    ) -> float:
        """Mark ``provider`` as pressured for ``cooldown_s`` seconds.

        Returns the resulting cooldown deadline (monotonic ts), so
        callers that want to log the expiry can pull it out without
        a second locked read. Idempotent — re-marking extends the
        deadline.
        """
        ts = now if now is not None else time.monotonic()
        deadline = ts + cooldown_s
        with self._lock:
            self._until[provider] = deadline
        return deadline

    def reset(self) -> None:
        """Drop all cooldown entries immediately."""
        with self._lock:
            self._until.clear()

    # ------------------------------------------------------------------
    # Read-only API
    # ------------------------------------------------------------------

    def is_pressured(self, provider: str, *, now: float | None = None) -> bool:
        """True iff ``provider`` is currently in cooldown.

        Lazy expiry: if the recorded deadline has passed, the entry
        is dropped before this call returns False. Callers don't see
        stale "pressured" entries.
        """
        ts = now if now is not None else time.monotonic()
        with self._lock:
            deadline = self._until.get(provider)
            if deadline is None:
                return False
            if deadline <= ts:
                # Cooldown elapsed — sweep so subsequent calls don't
                # re-take the lock for an empty entry.
                del self._until[provider]
                return False
            return True

    def pressured_until(
        self, provider: str, *, now: float | None = None
    ) -> float:
        """Return ``provider``'s cooldown deadline, or 0.0 if not pressured.

        Same lazy-expiry behavior as :meth:`is_pressured`.
        """
        ts = now if now is not None else time.monotonic()
        with self._lock:
            deadline = self._until.get(provider)
            if deadline is None:
                return 0.0
            if deadline <= ts:
                del self._until[provider]
                return 0.0
            return deadline


__all__ = [
    "MemoryPressureGuard",
    "is_memory_pressure_error",
]
