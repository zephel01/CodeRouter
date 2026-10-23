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
from typing import Any


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
