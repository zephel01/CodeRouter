"""Ingress tests for POST /v1/messages (Anthropic-compatible route).

These exercise the HTTP boundary: request validation, profile selection
(body > header > default), non-streaming response shape, SSE streaming
wire format, and error → 502 / 400 / 422 mappings. The engine is stubbed
so no network calls happen.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from fastapi.testclient import TestClient

from coderouter.adapters.base import AdapterError, ChatRequest, ChatResponse, StreamChunk
from coderouter.config.schemas import CodeRouterConfig, FallbackChain, ProviderConfig
from coderouter.ingress.app import create_app
from coderouter.routing import MidStreamError, NoProvidersAvailableError

# ----------------------------------------------------------------------
# Fixtures: config + recording / scripted engines
# ----------------------------------------------------------------------


@pytest.fixture
def two_profile_config() -> CodeRouterConfig:
    return CodeRouterConfig(
        allow_paid=False,
        default_profile="default",
        providers=[
            ProviderConfig(
                name="local",
                base_url="http://localhost:8080/v1",
                model="qwen-coder",
            ),
            ProviderConfig(
                name="small",
                base_url="http://localhost:8080/v1",
                model="qwen-small",
            ),
        ],
        profiles=[
            FallbackChain(name="default", providers=["local"]),
            FallbackChain(name="fast", providers=["small"]),
        ],
    )


class _RecordingEngine:
    """Drop-in replacement for FallbackEngine that records the profile seen
    and returns a canned ChatResponse. Streaming returns a scripted sequence
    of StreamChunks that drive the Anthropic translator through a normal
    text-only message lifecycle.
    """

    def __init__(self) -> None:
        self.seen_profiles: list[str | None] = []
        self.seen_requests: list[ChatRequest] = []

    async def generate(self, request: ChatRequest) -> ChatResponse:
        self.seen_profiles.append(request.profile)
        self.seen_requests.append(request)
        return ChatResponse(
            id="chatcmpl-test",
            object="chat.completion",
            created=0,
            model="qwen-coder",
            choices=[
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "hello world"},
                    "finish_reason": "stop",
                }
            ],
            usage={"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6},
            coderouter_provider="local",
        )

    async def stream(self, request: ChatRequest) -> AsyncIterator[StreamChunk]:
        self.seen_profiles.append(request.profile)
        self.seen_requests.append(request)

        # First chunk: role only (no content yet) — translator should still
        # open a text block when the first delta with content arrives.
        yield StreamChunk(
            id="chatcmpl-stream",
            object="chat.completion.chunk",
            created=0,
            model="qwen-coder",
            choices=[{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
        )
        # A few content fragments.
        for piece in ("hel", "lo ", "world"):
            yield StreamChunk(
                id="chatcmpl-stream",
                object="chat.completion.chunk",
                created=0,
                model="qwen-coder",
                choices=[
                    {"index": 0, "delta": {"content": piece}, "finish_reason": None}
                ],
            )
        # Terminal chunk — finish_reason=stop.
        yield StreamChunk(
            id="chatcmpl-stream",
            object="chat.completion.chunk",
            created=0,
            model="qwen-coder",
            choices=[{"index": 0, "delta": {}, "finish_reason": "stop"}],
        )


class _FailingEngine:
    """Engine that always fails — used to verify 502 / error-event mapping."""

    def __init__(self, profile: str = "default") -> None:
        self.profile = profile

    async def generate(self, request: ChatRequest) -> ChatResponse:
        raise NoProvidersAvailableError(self.profile, [])

    async def stream(self, request: ChatRequest) -> AsyncIterator[StreamChunk]:
        raise NoProvidersAvailableError(self.profile, [])
        yield  # pragma: no cover  # generator protocol


class _ToolAsTextEngine:
    """Engine whose non-streaming response emits the tool call as prose
    inside the assistant text — the qwen2.5-coder:14b failure mode that
    v0.3-A / v0.3-D tool-call repair exists to fix.

    stream() is intentionally unimplemented: v0.3-D must route through
    generate() even when the client asked for stream=true (the ingress
    downgrades tool-bearing requests). If stream() were called, it means
    the downgrade logic failed — so we blow up loudly.
    """

    def __init__(self) -> None:
        self.generate_calls = 0
        self.stream_calls = 0

    async def generate(self, request: ChatRequest) -> ChatResponse:
        self.generate_calls += 1
        return ChatResponse(
            id="chatcmpl-toolastext",
            object="chat.completion",
            created=0,
            model="qwen-coder",
            choices=[
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        # No structured tool_calls — the model wrote JSON prose.
                        "content": (
                            "Let me look it up.\n"
                            '{"name": "get_weather", "arguments": '
                            '{"location": "Tokyo"}}'
                        ),
                    },
                    "finish_reason": "stop",
                }
            ],
            usage={"prompt_tokens": 9, "completion_tokens": 7, "total_tokens": 16},
            coderouter_provider="local",
        )

    async def stream(self, request: ChatRequest) -> AsyncIterator[StreamChunk]:
        self.stream_calls += 1
        raise AssertionError(
            "stream() must NOT be called when tools are present — "
            "v0.3-D requires the request to be downgraded to non-streaming."
        )
        yield  # pragma: no cover  # generator protocol


class _MidStreamFailingEngine:
    """Engine that starts a stream successfully, then fails partway through.

    Exercises the v0.3-B guard: once the first chunk has gone out, the
    engine MUST surface a MidStreamError (no silent provider swap).
    """

    def __init__(self, provider: str = "local") -> None:
        self.provider = provider
        self.stream_calls = 0

    async def generate(self, request: ChatRequest) -> ChatResponse:
        raise AssertionError("generate() should not be called in stream tests")

    async def stream(self, request: ChatRequest) -> AsyncIterator[StreamChunk]:
        self.stream_calls += 1
        # Role chunk (so the translator opens a text block before we fail).
        yield StreamChunk(
            id="chatcmpl-midstream",
            object="chat.completion.chunk",
            created=0,
            model="qwen-coder",
            choices=[{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
        )
        # One content fragment so the client visibly receives bytes.
        yield StreamChunk(
            id="chatcmpl-midstream",
            object="chat.completion.chunk",
            created=0,
            model="qwen-coder",
            choices=[
                {"index": 0, "delta": {"content": "partial"}, "finish_reason": None}
            ],
        )
        # Now simulate the adapter blowing up mid-stream.
        raise MidStreamError(
            self.provider,
            AdapterError(
                "connection reset",
                provider=self.provider,
                retryable=True,
            ),
        )


@pytest.fixture
def client_and_engine(
    two_profile_config: CodeRouterConfig, monkeypatch: pytest.MonkeyPatch
) -> tuple[TestClient, _RecordingEngine]:
    monkeypatch.setattr(
        "coderouter.ingress.app.load_config",
        lambda path=None: two_profile_config,
    )
    app = create_app()
    engine = _RecordingEngine()
    app.state.engine = engine
    app.state.config = two_profile_config
    return TestClient(app), engine


@pytest.fixture
def client_and_failing_engine(
    two_profile_config: CodeRouterConfig, monkeypatch: pytest.MonkeyPatch
) -> tuple[TestClient, _FailingEngine]:
    monkeypatch.setattr(
        "coderouter.ingress.app.load_config",
        lambda path=None: two_profile_config,
    )
    app = create_app()
    engine = _FailingEngine()
    app.state.engine = engine
    app.state.config = two_profile_config
    return TestClient(app), engine


@pytest.fixture
def client_and_tool_as_text_engine(
    two_profile_config: CodeRouterConfig, monkeypatch: pytest.MonkeyPatch
) -> tuple[TestClient, _ToolAsTextEngine]:
    monkeypatch.setattr(
        "coderouter.ingress.app.load_config",
        lambda path=None: two_profile_config,
    )
    app = create_app()
    engine = _ToolAsTextEngine()
    app.state.engine = engine
    app.state.config = two_profile_config
    return TestClient(app), engine


@pytest.fixture
def client_and_midstream_engine(
    two_profile_config: CodeRouterConfig, monkeypatch: pytest.MonkeyPatch
) -> tuple[TestClient, _MidStreamFailingEngine]:
    monkeypatch.setattr(
        "coderouter.ingress.app.load_config",
        lambda path=None: two_profile_config,
    )
    app = create_app()
    engine = _MidStreamFailingEngine()
    app.state.engine = engine
    app.state.config = two_profile_config
    return TestClient(app), engine


# ----------------------------------------------------------------------
# Minimal payload helper
# ----------------------------------------------------------------------

_MINIMAL_BODY = {
    "model": "claude-3-5-sonnet",
    "max_tokens": 64,
    "messages": [{"role": "user", "content": "hi"}],
}


# ----------------------------------------------------------------------
# Non-streaming happy path + validation
# ----------------------------------------------------------------------


def test_basic_non_streaming_returns_anthropic_shape(
    client_and_engine: tuple[TestClient, _RecordingEngine],
) -> None:
    client, engine = client_and_engine
    resp = client.post("/v1/messages", json=_MINIMAL_BODY)
    assert resp.status_code == 200, resp.text

    body = resp.json()
    # Anthropic Messages wire shape
    assert body["type"] == "message"
    assert body["role"] == "assistant"
    assert body["id"]  # non-empty
    assert body["content"] == [{"type": "text", "text": "hello world"}]
    assert body["stop_reason"] == "end_turn"
    # Usage propagated (OpenAI prompt_tokens → Anthropic input_tokens)
    assert body["usage"]["input_tokens"] == 4
    assert body["usage"]["output_tokens"] == 2
    # CodeRouter metadata
    assert body["coderouter_provider"] == "local"
    # Engine saw no profile (default path)
    assert engine.seen_profiles == [None]


def test_missing_max_tokens_is_422(
    client_and_engine: tuple[TestClient, _RecordingEngine],
) -> None:
    client, engine = client_and_engine
    bad = {k: v for k, v in _MINIMAL_BODY.items() if k != "max_tokens"}
    resp = client.post("/v1/messages", json=bad)
    assert resp.status_code == 422, resp.text
    # Engine should never have been called
    assert engine.seen_profiles == []


def test_anthropic_version_header_is_accepted(
    client_and_engine: tuple[TestClient, _RecordingEngine],
) -> None:
    client, _ = client_and_engine
    resp = client.post(
        "/v1/messages",
        json=_MINIMAL_BODY,
        headers={"anthropic-version": "2023-06-01"},
    )
    assert resp.status_code == 200, resp.text


# ----------------------------------------------------------------------
# Profile selection
# ----------------------------------------------------------------------


def test_profile_from_body_reaches_engine(
    client_and_engine: tuple[TestClient, _RecordingEngine],
) -> None:
    client, engine = client_and_engine
    resp = client.post("/v1/messages", json={**_MINIMAL_BODY, "profile": "fast"})
    assert resp.status_code == 200, resp.text
    assert engine.seen_profiles == ["fast"]


def test_profile_from_header_reaches_engine(
    client_and_engine: tuple[TestClient, _RecordingEngine],
) -> None:
    client, engine = client_and_engine
    resp = client.post(
        "/v1/messages",
        json=_MINIMAL_BODY,
        headers={"X-CodeRouter-Profile": "fast"},
    )
    assert resp.status_code == 200, resp.text
    assert engine.seen_profiles == ["fast"]


def test_body_profile_wins_over_header(
    client_and_engine: tuple[TestClient, _RecordingEngine],
) -> None:
    client, engine = client_and_engine
    resp = client.post(
        "/v1/messages",
        json={**_MINIMAL_BODY, "profile": "fast"},
        headers={"X-CodeRouter-Profile": "default"},
    )
    assert resp.status_code == 200, resp.text
    assert engine.seen_profiles == ["fast"]


def test_unknown_profile_is_400(
    client_and_engine: tuple[TestClient, _RecordingEngine],
) -> None:
    client, engine = client_and_engine
    resp = client.post("/v1/messages", json={**_MINIMAL_BODY, "profile": "nope"})
    assert resp.status_code == 400, resp.text
    assert "unknown profile" in resp.text
    assert engine.seen_profiles == []


def test_unknown_profile_from_header_is_400(
    client_and_engine: tuple[TestClient, _RecordingEngine],
) -> None:
    client, engine = client_and_engine
    resp = client.post(
        "/v1/messages",
        json=_MINIMAL_BODY,
        headers={"X-CodeRouter-Profile": "nope"},
    )
    assert resp.status_code == 400, resp.text
    assert engine.seen_profiles == []


# ----------------------------------------------------------------------
# Error mapping (non-streaming)
# ----------------------------------------------------------------------


def test_no_providers_available_is_502(
    client_and_failing_engine: tuple[TestClient, _FailingEngine],
) -> None:
    client, _ = client_and_failing_engine
    resp = client.post("/v1/messages", json=_MINIMAL_BODY)
    assert resp.status_code == 502, resp.text
    # NoProvidersAvailableError message embeds the profile name.
    assert "all providers failed" in resp.text


# ----------------------------------------------------------------------
# Streaming SSE wire format
# ----------------------------------------------------------------------


def _parse_sse(stream_text: str) -> list[tuple[str, str]]:
    """Parse raw SSE text into [(event_name, data_json_str), ...]."""
    out: list[tuple[str, str]] = []
    event: str | None = None
    data_lines: list[str] = []
    for line in stream_text.splitlines():
        if line.startswith("event: "):
            event = line[len("event: ") :]
        elif line.startswith("data: "):
            data_lines.append(line[len("data: ") :])
        elif line == "":
            if event is not None and data_lines:
                out.append((event, "\n".join(data_lines)))
            event = None
            data_lines = []
    # Trailing event without blank line
    if event is not None and data_lines:
        out.append((event, "\n".join(data_lines)))
    return out


def test_streaming_emits_anthropic_event_sequence(
    client_and_engine: tuple[TestClient, _RecordingEngine],
) -> None:
    client, engine = client_and_engine
    body = {**_MINIMAL_BODY, "stream": True}
    with client.stream("POST", "/v1/messages", json=body) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        raw = b"".join(resp.iter_bytes()).decode("utf-8")

    events = _parse_sse(raw)
    event_types = [t for t, _ in events]

    # Must start with message_start.
    assert event_types[0] == "message_start"
    # Must end with message_stop.
    assert event_types[-1] == "message_stop"
    # Must open and close a text content block exactly once.
    assert event_types.count("content_block_start") == 1
    assert event_types.count("content_block_stop") == 1
    # Must emit at least one delta for the text fragments.
    assert event_types.count("content_block_delta") >= 1
    # message_delta (carrying stop_reason) must precede message_stop.
    assert "message_delta" in event_types
    assert event_types.index("message_delta") < event_types.index("message_stop")

    # The content_block_start must declare a text block at index 0.
    import json as _json

    start = next(d for t, d in events if t == "content_block_start")
    start_payload = _json.loads(start)
    assert start_payload["index"] == 0
    assert start_payload["content_block"]["type"] == "text"

    # The profile propagates even in streaming mode.
    assert engine.seen_profiles == [None]


def test_streaming_error_emits_error_event(
    client_and_failing_engine: tuple[TestClient, _FailingEngine],
) -> None:
    """When the engine raises NoProvidersAvailableError mid-stream setup, the
    SSE channel should emit a single `error` event (not a 5xx HTTP status).
    """
    client, _ = client_and_failing_engine
    body = {**_MINIMAL_BODY, "stream": True}
    with client.stream("POST", "/v1/messages", json=body) as resp:
        # Streaming response: status is 200 by the time headers flush;
        # the failure surfaces inside the SSE stream.
        assert resp.status_code == 200
        raw = b"".join(resp.iter_bytes()).decode("utf-8")

    events = _parse_sse(raw)
    assert any(t == "error" for t, _ in events), raw

    import json as _json

    err = next(d for t, d in events if t == "error")
    err_payload = _json.loads(err)
    assert err_payload["type"] == "error"
    assert err_payload["error"]["type"] == "overloaded_error"


# ----------------------------------------------------------------------
# v0.3-B: Mid-stream guard surfacing over SSE
# ----------------------------------------------------------------------


def test_streaming_midstream_failure_emits_api_error_event(
    client_and_midstream_engine: tuple[TestClient, _MidStreamFailingEngine],
) -> None:
    """After some chunks have streamed, an engine-level MidStreamError must
    be surfaced as an Anthropic `event: error` with type `api_error`
    (distinct from `overloaded_error`, which means no provider could start
    at all). The stream must include the partial content that already
    shipped, and must NOT include message_stop (the stream is truncated).
    """
    client, engine = client_and_midstream_engine
    body = {**_MINIMAL_BODY, "stream": True}
    with client.stream("POST", "/v1/messages", json=body) as resp:
        assert resp.status_code == 200
        raw = b"".join(resp.iter_bytes()).decode("utf-8")

    events = _parse_sse(raw)
    event_types = [t for t, _ in events]

    # The stream opened normally.
    assert event_types[0] == "message_start"
    # At least one text delta made it to the client before the failure.
    assert "content_block_delta" in event_types
    # And the stream terminates with an explicit error event.
    assert event_types[-1] == "error"
    # Crucially: the stream is NOT a clean close — no message_stop after
    # we've declared an error.
    assert "message_stop" not in event_types

    import json as _json

    err = next(d for t, d in events if t == "error")
    err_payload = _json.loads(err)
    assert err_payload["type"] == "error"
    assert err_payload["error"]["type"] == "api_error"
    # The engine was only consulted once — the ingress does not retry.
    assert engine.stream_calls == 1


# ----------------------------------------------------------------------
# v0.3-D: Streaming tool-call repair (downgrade-to-non-stream strategy)
# ----------------------------------------------------------------------


_TOOL_BODY = {
    "model": "claude-3-5-sonnet",
    "max_tokens": 64,
    "messages": [{"role": "user", "content": "what's the weather in Tokyo?"}],
    "stream": True,
    "tools": [
        {
            "name": "get_weather",
            "description": "Get current weather",
            "input_schema": {
                "type": "object",
                "properties": {"location": {"type": "string"}},
            },
        }
    ],
}


def test_streaming_with_tools_downgrades_and_repairs(
    client_and_tool_as_text_engine: tuple[TestClient, _ToolAsTextEngine],
) -> None:
    """v0.3-D: when the client streams AND declares tools, the ingress must
    resolve the request non-streaming so tool-call repair can run over the
    full assistant text. The emitted SSE must include a structural
    tool_use block — not the raw JSON-as-text that the upstream returned.
    """
    client, engine = client_and_tool_as_text_engine
    with client.stream("POST", "/v1/messages", json=_TOOL_BODY) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        raw = b"".join(resp.iter_bytes()).decode("utf-8")

    events = _parse_sse(raw)
    event_types = [t for t, _ in events]

    # Downgraded path → full sequence ending with message_stop.
    assert event_types[0] == "message_start"
    assert event_types[-1] == "message_stop"
    assert "content_block_start" in event_types
    # Downgrade means generate() was called; stream() must not have run
    # (the _ToolAsTextEngine would raise AssertionError if it had).
    assert engine.generate_calls == 1
    assert engine.stream_calls == 0

    # Repair must have surfaced a tool_use block.
    import json as _json

    tool_starts = [
        _json.loads(d)
        for t, d in events
        if t == "content_block_start"
        and _json.loads(d)["content_block"]["type"] == "tool_use"
    ]
    assert len(tool_starts) == 1
    assert tool_starts[0]["content_block"]["name"] == "get_weather"

    # The input JSON rides on input_json_delta.
    tool_idx = tool_starts[0]["index"]
    input_deltas = [
        _json.loads(d)
        for t, d in events
        if t == "content_block_delta"
        and _json.loads(d).get("index") == tool_idx
    ]
    assert len(input_deltas) == 1
    assert input_deltas[0]["delta"]["type"] == "input_json_delta"
    assert _json.loads(input_deltas[0]["delta"]["partial_json"]) == {
        "location": "Tokyo"
    }

    # Final stop_reason must reflect tool_use, not end_turn.
    md = next(d for t, d in events if t == "message_delta")
    md_payload = _json.loads(md)
    assert md_payload["delta"]["stop_reason"] == "tool_use"


def test_streaming_without_tools_stays_on_stream_path(
    client_and_engine: tuple[TestClient, _RecordingEngine],
) -> None:
    """Sanity check: requests WITHOUT tools must still use real streaming
    (engine.stream), not the downgrade path. Otherwise every streaming
    turn would pay the full-response latency penalty.
    """
    client, engine = client_and_engine
    body = {**_MINIMAL_BODY, "stream": True}  # no "tools" key
    with client.stream("POST", "/v1/messages", json=body) as resp:
        assert resp.status_code == 200
        _ = b"".join(resp.iter_bytes())

    # _RecordingEngine tracks both paths via seen_profiles; stream() is the
    # one that got called because there are no tools.
    # (seen_profiles has a single None entry for the stream call.)
    assert engine.seen_profiles == [None]


def test_streaming_with_tools_502_surfaces_error_event(
    two_profile_config: CodeRouterConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even in the downgraded path, engine failure must surface as an SSE
    error event (not an HTTP 5xx) because the response headers have
    already shipped by the time we start resolving.
    """
    monkeypatch.setattr(
        "coderouter.ingress.app.load_config",
        lambda path=None: two_profile_config,
    )
    app = create_app()
    app.state.engine = _FailingEngine()
    app.state.config = two_profile_config
    client = TestClient(app)

    with client.stream("POST", "/v1/messages", json=_TOOL_BODY) as resp:
        assert resp.status_code == 200
        raw = b"".join(resp.iter_bytes()).decode("utf-8")

    events = _parse_sse(raw)
    assert any(t == "error" for t, _ in events), raw
    import json as _json

    err = next(d for t, d in events if t == "error")
    err_payload = _json.loads(err)
    assert err_payload["error"]["type"] == "overloaded_error"
