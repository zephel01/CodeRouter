"""v2.18.0: KV-slot affinity for llama-server (``id_slot``).

llama-server started with ``--parallel N`` serves N slots, each holding its
own prompt cache. Without a hint it picks a free slot, so consecutive turns
of one conversation can land on different slots and re-prefill the whole
prompt. Sending ``id_slot`` pins a conversation to one slot.

The slot is ``sha256(key) % N`` — stable across processes and restarts
(Python's ``hash`` is salted per process).
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

# Request classes whose prompt shares the main conversation's prefix: they
# belong on the main conversation's slot. Everything else (sub-agents,
# auxiliary calls such as the auto-mode classifier or titles, workflows) has
# its own prompt and gets its own key, so it neither waits on the main slot
# nor evicts its cache.
_MAIN_CLASSES = frozenset({"", "main", "compaction"})


def affinity_key(hints: Mapping[str, str]) -> str | None:
    """Conversation key for slot affinity, from Claude Code's hint headers.

    ``None`` when there is no session id. ``main`` / ``compaction`` (and a
    missing class) share ``<session>``; other classes get
    ``<session>:<class>:<agent_type>``.
    """
    session = hints.get("session_id")
    if not session:
        return None
    request_class = hints.get("request_class", "").lower()
    if request_class in _MAIN_CLASSES:
        return session
    return f"{session}:{request_class}:{hints.get('agent_type', '').lower()}"


def slot_for(key: str, slots: int) -> int:
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % slots


def apply_slot_affinity(body: dict[str, Any], *, slots: int | None, key: str | None) -> None:
    """Set ``body["id_slot"]`` when affinity is configured and a key exists.

    An ``id_slot`` already present (e.g. from ``extra_body``) wins.
    """
    if not slots or not key or "id_slot" in body:
        return
    body["id_slot"] = slot_for(key, slots)
