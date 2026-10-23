"""v2.18.0: llama-server KV-slot affinity (``slot_affinity_slots`` → ``id_slot``)."""

from __future__ import annotations

import pytest

from coderouter.adapters.anthropic_native import AnthropicAdapter
from coderouter.adapters.base import ChatRequest
from coderouter.adapters.openai_compat import OpenAICompatAdapter
from coderouter.adapters.slot_affinity import apply_slot_affinity, slot_for
from coderouter.config.schemas import ProviderConfig
from coderouter.translation import AnthropicRequest
from coderouter.translation.convert import to_chat_request


def test_slot_is_stable_and_in_range() -> None:
    assert slot_for("session-abc", 4) == slot_for("session-abc", 4)
    assert all(0 <= slot_for(f"s{i}", 3) < 3 for i in range(50))
    assert len({slot_for(f"s{i}", 4) for i in range(200)}) == 4  # spreads


def test_apply_respects_off_missing_key_and_explicit_id_slot() -> None:
    body: dict = {}
    apply_slot_affinity(body, slots=None, key="k")
    apply_slot_affinity(body, slots=4, key=None)
    assert "id_slot" not in body
    body = {"id_slot": 2}
    apply_slot_affinity(body, slots=4, key="k")
    assert body["id_slot"] == 2


def _anth(session: str | None) -> AnthropicRequest:
    req = AnthropicRequest.model_validate(
        {"max_tokens": 8, "messages": [{"role": "user", "content": "hi"}]}
    )
    req.session_key = session
    return req


def test_native_payload_gets_id_slot_and_never_leaks_session_key() -> None:
    cfg = ProviderConfig(
        name="l", kind="anthropic", base_url="http://127.0.0.1:8080", model="m", slot_affinity_slots=4
    )
    body = AnthropicAdapter(cfg)._payload(_anth("sess-1"), stream=True)
    assert body["id_slot"] == slot_for("sess-1", 4)
    assert "session_key" not in body


def test_openai_payload_gets_id_slot_via_translation() -> None:
    cfg = ProviderConfig(name="l", base_url="http://127.0.0.1:8080/v1", model="m", slot_affinity_slots=2)
    chat = to_chat_request(_anth("sess-2"))
    assert isinstance(chat, ChatRequest)
    body = OpenAICompatAdapter(cfg)._payload(chat, stream=False)
    assert body["id_slot"] == slot_for("sess-2", 2)
    assert "session_key" not in body


def test_disabled_by_default() -> None:
    cfg = ProviderConfig(name="l", kind="anthropic", base_url="http://127.0.0.1:8080", model="m")
    assert "id_slot" not in AnthropicAdapter(cfg)._payload(_anth("sess"), stream=False)


# ---------------------------------------------------------------------------
# Review fixes (v2.18.0)
# ---------------------------------------------------------------------------


def test_rebuild_paths_keep_session_key() -> None:
    """strip_thinking / emulate_tool_choice / strip_cache_control /
    to_anthropic_request rebuild the request; the key must survive or
    affinity silently stops for exactly the traffic it targets."""
    from coderouter.routing.capability import (
        emulate_tool_choice,
        strip_cache_control,
        strip_thinking,
    )
    from coderouter.translation.convert import to_anthropic_request

    req = AnthropicRequest.model_validate(
        {
            "max_tokens": 8,
            "messages": [{"role": "user", "content": "hi"}],
            "thinking": {"type": "enabled", "budget_tokens": 1024},
            "tools": [{"name": "t", "input_schema": {"type": "object"}}],
            "tool_choice": {"type": "tool", "name": "t"},
        }
    )
    req.session_key = "sess-x"
    assert strip_thinking(req).session_key == "sess-x"
    assert emulate_tool_choice(req).session_key == "sess-x"
    assert strip_cache_control(req)[0].session_key == "sess-x"
    chat = ChatRequest.model_validate({"messages": [{"role": "user", "content": "hi"}]})
    chat.session_key = "sess-y"
    assert to_anthropic_request(chat).session_key == "sess-y"


def test_thinking_request_through_engine_gets_id_slot() -> None:
    """End to end: a Claude Code turn with thinking to a llama-server that
    does not support thinking (strip_thinking path) still gets id_slot."""
    import asyncio

    from coderouter.config.schemas import CodeRouterConfig, FallbackChain
    from tests.test_fallback_anthropic import FakeAnthropicAdapter, _engine_with_adapters

    cfg = CodeRouterConfig(
        allow_paid=False,
        default_profile="default",
        providers=[
            ProviderConfig(
                name="l", kind="anthropic", base_url="http://127.0.0.1:8080", model="m", slot_affinity_slots=4
            )
        ],
        profiles=[FallbackChain(name="default", providers=["l"])],
    )
    fake = FakeAnthropicAdapter(cfg.providers[0])
    engine = _engine_with_adapters(cfg, {"l": fake})
    req = AnthropicRequest.model_validate(
        {
            "max_tokens": 8,
            "messages": [{"role": "user", "content": "hi"}],
            "thinking": {"type": "enabled", "budget_tokens": 1024},
        }
    )
    req.session_key = "sess-z"
    asyncio.run(engine.generate_anthropic(req))
    sent = fake.generate_calls[0]
    assert "thinking" not in sent.model_dump(exclude_none=True)  # went through strip_thinking
    assert sent.session_key == "sess-z"
    assert fake._payload(sent, stream=False)["id_slot"] == slot_for("sess-z", 4)


@pytest.mark.parametrize(
    ("hints", "expected"),
    [
        ({}, None),
        ({"request_class": "main"}, None),
        ({"session_id": "s"}, "s"),
        ({"session_id": "s", "request_class": "main"}, "s"),
        ({"session_id": "s", "request_class": "Compaction"}, "s"),
        ({"session_id": "s", "request_class": "subagent", "agent_type": "Explore"}, "s:subagent:explore"),
        ({"session_id": "s", "request_class": "auxiliary"}, "s:auxiliary:"),
    ],
)
def test_affinity_key(hints: dict[str, str], expected: str | None) -> None:
    from coderouter.adapters.slot_affinity import affinity_key

    assert affinity_key(hints) == expected
