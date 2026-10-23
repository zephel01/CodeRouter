"""v2.16.0: Claude Code tool search on non-Anthropic upstreams."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from fastapi.testclient import TestClient

from coderouter.config.schemas import CodeRouterConfig, FallbackChain, ProviderConfig
from coderouter.ingress.app import create_app
from coderouter.ingress.tool_search import expand_tool_search, resolve_action, uses_tool_search
from coderouter.translation import AnthropicRequest, AnthropicResponse, AnthropicStreamEvent

_SEARCH_TOOL = {"type": "tool_search_tool_regex_20251119", "name": "tool_search_tool_regex"}
_DEFERRED = {
    "name": "mcp__github__create_issue",
    "description": "Create an issue",
    "input_schema": {"type": "object", "properties": {}},
    "defer_loading": True,
}
_PLAIN = {"name": "Read", "description": "Read a file", "input_schema": {"type": "object"}}


def _req(tools: list[dict]) -> AnthropicRequest:
    return AnthropicRequest.model_validate(
        {"max_tokens": 16, "messages": [{"role": "user", "content": "hi"}], "tools": tools}
    )


def _config(*providers: ProviderConfig, action: str = "auto") -> CodeRouterConfig:
    return CodeRouterConfig(
        allow_paid=True,
        default_profile="default",
        providers=list(providers),
        profiles=[
            FallbackChain(
                name="default", providers=[p.name for p in providers], deferred_tools_action=action
            )
        ],
    )


_LOCAL = ProviderConfig(name="local", base_url="http://127.0.0.1:9931/v1", model="m")
_OLLAMA_NATIVE = ProviderConfig(name="ollama", kind="anthropic", base_url="http://127.0.0.1:11434", model="m")
_ANTHROPIC = ProviderConfig(
    name="claude", kind="anthropic", base_url="https://api.anthropic.com", model="claude-sonnet-5", paid=True
)


def test_detection() -> None:
    assert uses_tool_search(_req([_SEARCH_TOOL, _PLAIN]))
    assert uses_tool_search(_req([_DEFERRED]))
    assert not uses_tool_search(_req([_PLAIN]))
    assert not uses_tool_search(_req([]))


@pytest.mark.parametrize(
    ("providers", "expected"),
    [
        ((_LOCAL,), "reject"),
        ((_OLLAMA_NATIVE,), "reject"),  # native /v1/messages, but not api.anthropic.com
        ((_LOCAL, _ANTHROPIC), "reject"),  # one non-Anthropic hop is enough
        ((_ANTHROPIC,), "passthrough"),
    ],
)
def test_auto_resolution(providers: tuple[ProviderConfig, ...], expected: str) -> None:
    assert resolve_action(_config(*providers), None) == expected


@pytest.mark.parametrize("action", ["reject", "expand", "passthrough"])
def test_explicit_action_wins(action: str) -> None:
    assert resolve_action(_config(_LOCAL, action=action), "default") == action


def test_expand_drops_search_tool_and_flags() -> None:
    out = expand_tool_search(_req([_SEARCH_TOOL, _DEFERRED, _PLAIN]))
    dumped = [t.model_dump() for t in out.tools or []]
    assert [t["name"] for t in dumped] == ["mcp__github__create_issue", "Read"]
    assert all("defer_loading" not in t for t in dumped)


class _RecordingEngine:
    def __init__(self) -> None:
        self.seen: list[AnthropicRequest] = []
        self.last_drift_severity: str | None = None

    def apply_context_budget(self, request: AnthropicRequest) -> tuple[AnthropicRequest, str | None]:
        return request, None

    async def generate_anthropic(self, request: AnthropicRequest) -> AnthropicResponse:
        self.seen.append(request)
        return AnthropicResponse(
            id="msg_1", model="m", content=[{"type": "text", "text": "ok"}], stop_reason="end_turn"
        )

    async def stream_anthropic(self, request: AnthropicRequest) -> AsyncIterator[AnthropicStreamEvent]:
        self.seen.append(request)
        yield AnthropicStreamEvent(type="message_stop", data={"type": "message_stop"})


def _client(monkeypatch: pytest.MonkeyPatch, config: CodeRouterConfig) -> tuple[TestClient, _RecordingEngine]:
    monkeypatch.setattr("coderouter.ingress.app.load_config", lambda path=None: config)
    app = create_app()
    engine = _RecordingEngine()
    app.state.engine = engine
    app.state.config = config
    return TestClient(app), engine


_BODY = {"model": "claude-sonnet-5", "max_tokens": 16, "messages": [{"role": "user", "content": "hi"}]}


@pytest.mark.parametrize("stream", [False, True])
def test_route_rejects_tool_search_for_local_chain(monkeypatch: pytest.MonkeyPatch, stream: bool) -> None:
    client, engine = _client(monkeypatch, _config(_LOCAL))
    resp = client.post("/v1/messages", json={**_BODY, "stream": stream, "tools": [_SEARCH_TOOL, _DEFERRED]})
    assert resp.status_code == 400
    assert resp.json()["error"]["type"] == "invalid_request_error"
    assert "tool search" in resp.json()["error"]["message"]
    assert resp.headers["x-should-retry"] == "false"
    assert engine.seen == []  # refused before dispatch


def test_route_expand_forwards_plain_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    client, engine = _client(monkeypatch, _config(_LOCAL, action="expand"))
    resp = client.post("/v1/messages", json={**_BODY, "tools": [_SEARCH_TOOL, _DEFERRED, _PLAIN]})
    assert resp.status_code == 200, resp.text
    tools = [t.model_dump() for t in engine.seen[0].tools or []]
    assert [t["name"] for t in tools] == ["mcp__github__create_issue", "Read"]


def test_route_without_tool_search_is_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    client, engine = _client(monkeypatch, _config(_LOCAL))
    resp = client.post("/v1/messages", json={**_BODY, "tools": [_PLAIN]})
    assert resp.status_code == 200
    assert len(engine.seen) == 1
