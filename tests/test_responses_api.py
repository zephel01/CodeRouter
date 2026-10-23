"""v2.19.0: POST /v1/responses (OpenAI Responses API for the Codex CLI).

Request shapes follow openai/codex 970111f: the system prompt is a
``developer`` message, apply_patch is a ``custom`` grammar tool answered with
``custom_tool_call``, MCP tools arrive inside ``namespace`` tools.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import pytest
from fastapi.testclient import TestClient

from coderouter.adapters.base import AdapterError
from coderouter.config.schemas import CodeRouterConfig, FallbackChain, ProviderConfig
from coderouter.ingress.app import create_app
from coderouter.routing import NoProvidersAvailableError
from coderouter.translation import AnthropicRequest, AnthropicResponse, AnthropicStreamEvent
from coderouter.translation.responses import (
    ResponsesStreamTranslator,
    ToolMap,
    responses_to_anthropic,
)

_TOOLS = [
    {
        "type": "function",
        "name": "exec_command",
        "description": "run",
        "strict": False,
        "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}},
    },
    {
        "type": "custom",
        "name": "apply_patch",
        "description": "patch",
        "format": {"type": "grammar", "syntax": "lark", "definition": "start: /.+/"},
    },
    {
        "type": "namespace",
        "name": "mcp__github",
        "description": "gh",
        "tools": [{"type": "function", "name": "create_issue", "parameters": {"type": "object"}}],
    },
    {"type": "web_search", "external_web_access": False},
]


def _body(input_items: list, **kw) -> dict:  # type: ignore[no-untyped-def]
    return {
        "model": "gpt-5.5",
        "stream": True,
        "tool_choice": "auto",
        "parallel_tool_calls": False,
        "reasoning": {},
        "store": False,
        "include": ["reasoning.encrypted_content"],
        "tools": _TOOLS,
        "input": input_items,
        **kw,
    }


def _dev(text: str) -> dict:
    return {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": text}]}


def _user(text: str) -> dict:
    return {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}


# ---------------------------------------------------------------------------
# Request translation
# ---------------------------------------------------------------------------


def test_developer_messages_become_system_and_hosted_tools_are_dropped() -> None:
    req, tool_map = responses_to_anthropic(_body([_dev("BASE"), _user("hi"), _dev("LATER")]))
    assert req.system == "BASE\n\nLATER"
    assert [m.role for m in req.messages] == ["user"]
    names = [t.name for t in req.tools or []]
    assert names == ["exec_command", "apply_patch", "mcp__github__create_issue"]
    assert tool_map.lookup("mcp__github__create_issue").namespace == "mcp__github"
    assert "lark" in json.dumps(req.tools[1].input_schema)  # grammar carried to the model


def test_tool_history_round_trips_and_pairs() -> None:
    items = [
        _dev("BASE"),
        _user("fix it"),
        {"type": "reasoning", "summary": [], "encrypted_content": "x"},
        {"type": "function_call", "call_id": "call_1", "name": "exec_command", "arguments": '{"cmd":"ls"}'},
        {"type": "custom_tool_call", "call_id": "call_2", "name": "apply_patch", "input": "*** Begin Patch"},
        {"type": "function_call_output", "call_id": "call_1", "output": "a.py"},
        {"type": "custom_tool_call_output", "call_id": "call_2", "output": "Done!"},
        {"type": "function_call_output", "call_id": "orphan", "output": "x"},
        _user("thanks"),
    ]
    req, _ = responses_to_anthropic(_body(items))
    msgs = [m.model_dump() for m in req.messages]
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
    uses = [b for b in msgs[1]["content"] if b["type"] == "tool_use"]
    assert uses[0]["input"] == {"cmd": "ls"}
    assert uses[1]["input"] == {"input": "*** Begin Patch"}
    results = [b for b in msgs[2]["content"] if b["type"] == "tool_result"]
    assert [r["tool_use_id"] for r in results] == ["call_1", "call_2"]  # orphan dropped
    assert msgs[2]["content"][-1] == {"type": "text", "text": "thanks"}


def test_missing_tool_output_is_synthesized() -> None:
    items = [
        _user("go"),
        {"type": "function_call", "call_id": "c9", "name": "exec_command", "arguments": "{}"},
        _user("never mind"),
    ]
    req, _ = responses_to_anthropic(_body(items))
    first_block = req.messages[2].model_dump()["content"][0]
    assert first_block == {"type": "tool_result", "tool_use_id": "c9", "content": "aborted"}


def test_image_data_url() -> None:
    items = [
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_image", "image_url": "data:image/png;base64,AAAA"}],
        }
    ]
    req, _ = responses_to_anthropic(_body(items))
    block = req.messages[0].model_dump()["content"][0]
    assert block["source"] == {"type": "base64", "media_type": "image/png", "data": "AAAA"}


# ---------------------------------------------------------------------------
# Stream translation
# ---------------------------------------------------------------------------


def _anth_events(tool_name: str, tool_input: dict) -> list[AnthropicStreamEvent]:
    def ev(t: str, **d) -> AnthropicStreamEvent:  # type: ignore[no-untyped-def]
        return AnthropicStreamEvent(type=t, data={"type": t, **d})

    return [
        ev("message_start", message={"usage": {"input_tokens": 12, "cache_read_input_tokens": 4}}),
        ev("content_block_start", index=0, content_block={"type": "text", "text": ""}),
        ev("content_block_delta", index=0, delta={"type": "text_delta", "text": "Patching."}),
        ev("content_block_stop", index=0),
        ev("content_block_start", index=1, content_block={"type": "tool_use", "id": "toolu_1", "name": tool_name, "input": {}}),
        ev("content_block_delta", index=1, delta={"type": "input_json_delta", "partial_json": json.dumps(tool_input)}),
        ev("content_block_stop", index=1),
        ev("message_delta", delta={"stop_reason": "tool_use"}, usage={"output_tokens": 7}),
        ev("message_stop"),
    ]


def _translate(tool_map: ToolMap, events: list[AnthropicStreamEvent]) -> list[dict]:
    t = ResponsesStreamTranslator(tool_map, model="gpt-5.5")
    out = t.start()
    for e in events:
        out.extend(t.feed(e))
    out.extend(t.finish())
    return out


def test_stream_sequence_for_text_and_custom_tool() -> None:
    _, tool_map = responses_to_anthropic(_body([_user("x")]))
    out = _translate(tool_map, _anth_events("apply_patch", {"input": "*** Begin Patch\n*** End Patch"}))
    types = [e["type"] for e in out]
    assert types[0] == "response.created"
    # A text delta only ever follows an open item.
    assert types.index("response.output_item.added") < types.index("response.output_text.delta")
    done = [e["item"] for e in out if e["type"] == "response.output_item.done"]
    assert done[0]["type"] == "message"
    assert done[0]["content"][0]["text"] == "Patching."
    assert done[1] == {
        **done[1],
        "type": "custom_tool_call",
        "call_id": "toolu_1",
        "name": "apply_patch",
        "input": "*** Begin Patch\n*** End Patch",
    }
    completed = out[-1]
    assert completed["type"] == "response.completed"
    resp = completed["response"]
    assert resp["id"].startswith("resp_")
    # Anthropic's input_tokens (12) excludes the 4 cached ones; OpenAI's
    # includes them (Codex reads total_tokens as context usage).
    assert resp["usage"] == {
        "input_tokens": 16,
        "input_tokens_details": {"cached_tokens": 4},
        "output_tokens": 7,
        "output_tokens_details": {"reasoning_tokens": 0},
        "total_tokens": 23,
    }


def test_stream_function_call_in_namespace() -> None:
    _, tool_map = responses_to_anthropic(_body([_user("x")]))
    out = _translate(tool_map, _anth_events("mcp__github__create_issue", {"title": "t"}))
    item = [e["item"] for e in out if e["type"] == "response.output_item.done"][1]
    assert item["type"] == "function_call"
    assert item["name"] == "create_issue"
    assert item["namespace"] == "mcp__github"
    assert json.loads(item["arguments"]) == {"title": "t"}


def test_max_tokens_stop_is_incomplete() -> None:
    _, tool_map = responses_to_anthropic(_body([_user("x")]))
    t = ResponsesStreamTranslator(tool_map, model="m")
    t.start()
    t.feed(AnthropicStreamEvent(type="message_delta", data={"delta": {"stop_reason": "max_tokens"}, "usage": {}}))
    t.feed(AnthropicStreamEvent(type="message_stop", data={"type": "message_stop"}))
    out = t.finish()
    assert out[0]["type"] == "response.incomplete"
    assert out[0]["response"]["incomplete_details"] == {"reason": "max_output_tokens"}


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


class _Engine:
    def __init__(self, *, events=None, fail: list[AdapterError] | None = None):  # type: ignore[no-untyped-def]
        self.events = events or []
        self.fail = fail
        self.last_drift_severity = None
        self.seen: list[AnthropicRequest] = []

    def apply_context_budget(self, request):  # type: ignore[no-untyped-def]
        return request, None

    async def stream_anthropic(self, request: AnthropicRequest) -> AsyncIterator[AnthropicStreamEvent]:
        self.seen.append(request)
        if self.fail is not None:
            raise NoProvidersAvailableError("default", self.fail)
        for e in self.events:
            yield e

    async def generate_anthropic(self, request: AnthropicRequest) -> AnthropicResponse:
        self.seen.append(request)
        if self.fail is not None:
            raise NoProvidersAvailableError("default", self.fail)
        return AnthropicResponse(
            id="m", model="x", content=[{"type": "text", "text": "hello"}], stop_reason="end_turn"
        )


@pytest.fixture
def make_client(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    config = CodeRouterConfig(
        allow_paid=False,
        default_profile="default",
        providers=[ProviderConfig(name="local", base_url="http://127.0.0.1:9931/v1", model="m")],
        profiles=[FallbackChain(name="default", providers=["local"])],
    )
    monkeypatch.setattr("coderouter.ingress.app.load_config", lambda path=None: config)

    def _make(engine: _Engine) -> TestClient:
        app = create_app()
        app.state.engine = engine
        app.state.config = config
        return TestClient(app)

    return _make


def _sse(text: str) -> list[dict]:
    return [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: ")]


def test_route_streams_a_codex_turn(make_client) -> None:  # type: ignore[no-untyped-def]
    engine = _Engine(events=_anth_events("exec_command", {"cmd": "ls"}))
    client = make_client(engine)
    resp = client.post("/v1/responses", json=_body([_dev("BASE"), _user("hi")]), headers={"session-id": "s1"})
    assert resp.status_code == 200
    events = _sse(resp.text)
    assert events[0]["type"] == "response.created"
    assert events[-1]["type"] == "response.completed"
    call = [e["item"] for e in events if e["type"] == "response.output_item.done"][1]
    assert call["type"] == "function_call" and call["call_id"] == "toolu_1"
    assert engine.seen[0].session_key == "s1"  # Codex session-id → slot affinity key


def test_route_overflow_is_response_failed_context_length_exceeded(make_client) -> None:  # type: ignore[no-untyped-def]
    overflow = AdapterError(
        "400",
        provider="local",
        status_code=400,
        retryable=False,
        upstream_body='{"error":{"message":"the request exceeds the available context size","type":"exceed_context_size_error"}}',
    )
    client = make_client(_Engine(fail=[overflow]))
    resp = client.post("/v1/responses", json=_body([_user("hi")]))
    assert resp.status_code == 200
    events = _sse(resp.text)
    assert [e["type"] for e in events] == ["response.created", "response.failed"]
    assert events[-1]["response"]["error"]["code"] == "context_length_exceeded"


def test_route_rate_limit_is_http_429_with_retry_after(make_client) -> None:  # type: ignore[no-untyped-def]
    err = AdapterError("429", provider="local", status_code=429, retry_after="3")
    resp = make_client(_Engine(fail=[err])).post("/v1/responses", json=_body([_user("hi")]))
    assert resp.status_code == 429
    assert resp.headers["retry-after"] == "3"
    assert resp.json()["error"]["code"] == "rate_limit_exceeded"


def test_route_non_streaming_json(make_client) -> None:  # type: ignore[no-untyped-def]
    resp = make_client(_Engine()).post("/v1/responses", json=_body([_user("hi")], stream=False))
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["object"] == "response"
    assert payload["output"][0]["content"][0]["text"] == "hello"


def test_route_rejects_non_json(make_client) -> None:  # type: ignore[no-untyped-def]
    resp = make_client(_Engine()).post("/v1/responses", content=b"nope", headers={"content-type": "application/json"})
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Review fixes (v2.19.0)
# ---------------------------------------------------------------------------


def test_tool_output_images_become_image_blocks_not_base64_text() -> None:
    """Codex view_image returns input_image parts in function_call_output."""
    items = [
        _user("look"),
        {"type": "function_call", "call_id": "c1", "name": "exec_command", "arguments": "{}"},
        {
            "type": "function_call_output",
            "call_id": "c1",
            "output": [
                {"type": "input_text", "text": "screenshot:"},
                {"type": "input_image", "image_url": "data:image/png;base64,QUJD"},
            ],
        },
    ]
    req, _ = responses_to_anthropic(_body(items))
    result = req.messages[2].model_dump()["content"][0]
    assert result["type"] == "tool_result"
    assert result["content"][0] == {"type": "text", "text": "screenshot:"}
    assert result["content"][1]["source"]["data"] == "QUJD"
    assert "QUJD" not in json.dumps(result["content"][0])


def test_truncated_stream_is_failed_not_completed() -> None:
    """No message_stop / open block → retryable failure, never 'completed'."""
    _, tool_map = responses_to_anthropic(_body([_user("x")]))
    events = _anth_events("exec_command", {"cmd": "ls"})[:3]  # text block left open
    out = _translate(tool_map, events)
    assert out[-1]["type"] == "response.failed"
    assert out[-1]["response"]["error"]["code"] == "server_error"
    # Closed blocks but no message_stop is also a cut stream.
    out = _translate(tool_map, _anth_events("exec_command", {"cmd": "ls"})[:-1])
    assert out[-1]["type"] == "response.failed"


def test_cache_writes_count_as_input() -> None:
    from coderouter.translation.responses import _usage

    u = _usage(10, 5, cached=100, cache_write=50)
    assert u["input_tokens"] == 160
    assert u["total_tokens"] == 165
    assert u["input_tokens_details"] == {"cached_tokens": 100, "cache_write_tokens": 50}


def test_long_namespaced_names_are_capped_and_reversible() -> None:
    tools = [
        {
            "type": "namespace",
            "name": "mcp__a_very_long_server_name_for_documentation_search",
            "tools": [
                {"type": "function", "name": "search_documents_by_keyword_and_date", "parameters": {"type": "object"}},
                {"type": "function", "name": "search_documents_by_keyword_and_tags", "parameters": {"type": "object"}},
            ],
        }
    ]
    req, tool_map = responses_to_anthropic({"model": "m", "input": "hi", "tools": tools})
    names = [t.name for t in req.tools or []]
    assert all(len(n) <= 64 for n in names)
    assert len(set(names)) == 2
    assert {tool_map.lookup(n).name for n in names} == {
        "search_documents_by_keyword_and_date",
        "search_documents_by_keyword_and_tags",
    }


def test_custom_tool_single_string_field_is_used_as_input() -> None:
    _, tool_map = responses_to_anthropic(_body([_user("x")]))
    out = _translate(tool_map, _anth_events("apply_patch", {"patch": "*** Begin Patch"}))
    item = [e["item"] for e in out if e["type"] == "response.output_item.done"][1]
    assert item["type"] == "custom_tool_call"
    assert item["input"] == "*** Begin Patch"
