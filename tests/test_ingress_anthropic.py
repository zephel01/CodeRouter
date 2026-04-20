"""Ingress tests for POST /v1/messages (Anthropic-compatible route).

These exercise the HTTP boundary: request validation, profile selection
(body > header > default), non-streaming response shape, SSE streaming
wire format, and error → 502 / 400 / 422 mappings. The engine is stubbed
with the `generate_anthropic` / `stream_anthropic` API so no network calls
happen and no translation runs in the ingress layer — the ingress just
marshals HTTP to/from the engine's Anthropic-shaped methods.

Engine-internal concerns (translation round-trip, tool-call repair,
v0.3-D downgrade, mid-stream guard dispatch) are tested separately in
tests/test_fallback_anthropic.py.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from fastapi.testclient import TestClient

from coderouter.adapters.base import AdapterError
from coderouter.config.schemas import CodeRouterConfig, FallbackChain, ProviderConfig
from coderouter.ingress.app import create_app
from coderouter.routing import MidStreamError, NoProvidersAvailableError
from coderouter.translation import (
    AnthropicRequest,
    AnthropicResponse,
    AnthropicStreamEvent,
    AnthropicUsage,
)

# ----------------------------------------------------------------------
# Fixtures: config + scripted engines (Anthropic-shaped API)
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
    """Drop-in replacement for FallbackEngine.

    Records the profile seen and returns a canned AnthropicResponse /
    stream. The scripted stream matches the shape the translator (or a
    native adapter) would produce: message_start → content_block_start
    → content_block_delta+ → content_block_stop → message_delta →
    message_stop.
    """

    def __init__(self) -> None:
        self.seen_profiles: list[str | None] = []
        self.seen_requests: list[AnthropicRequest] = []

    async def generate_anthropic(
        self, request: AnthropicRequest
    ) -> AnthropicResponse:
        self.seen_profiles.append(request.profile)
        self.seen_requests.append(request)
        return AnthropicResponse(
            id="msg_test",
            model="qwen-coder",
            content=[{"type": "text", "text": "hello world"}],
            stop_reason="end_turn",
            usage=AnthropicUsage(input_tokens=4, output_tokens=2),
            coderouter_provider="local",
        )

    async def stream_anthropic(
        self, request: AnthropicRequest
    ) -> AsyncIterator[AnthropicStreamEvent]:
        self.seen_profiles.append(request.profile)
        self.seen_requests.append(request)

        yield AnthropicStreamEvent(
            type="message_start",
            data={
                "type": "message_start",
                "message": {
                    "id": "msg_test",
                    "type": "message",
                    "role": "assistant",
                    "content": [],
                    "model": "qwen-coder",
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                },
            },
        )
        yield AnthropicStreamEvent(
            type="content_block_start",
            data={
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
        )
        for piece in ("hel", "lo ", "world"):
            yield AnthropicStreamEvent(
                type="content_block_delta",
                data={
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": piece},
                },
            )
        yield AnthropicStreamEvent(
            type="content_block_stop",
            data={"type": "content_block_stop", "index": 0},
        )
        yield AnthropicStreamEvent(
            type="message_delta",
            data={
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": 2},
            },
        )
        yield AnthropicStreamEvent(
            type="message_stop",
            data={"type": "message_stop"},
        )


class _FailingEngine:
    """Engine that always fails — used to verify 502 / error-event mapping."""

    def __init__(self, profile: str = "default") -> None:
        self.profile = profile

    async def generate_anthropic(
        self, request: AnthropicRequest
    ) -> AnthropicResponse:
        raise NoProvidersAvailableError(self.profile, [])

    async def stream_anthropic(
        self, request: AnthropicRequest
    ) -> AsyncIterator[AnthropicStreamEvent]:
        raise NoProvidersAvailableError(self.profile, [])
        yield  # pragma: no cover  # generator protocol


class _MidStreamFailingEngine:
    """Engine whose stream starts normally then fails partway through.

    Exercises the v0.3-B guard at the ingress boundary: once the first
    event has shipped, the engine's MidStreamError must surface as a
    single `event: error` with type `api_error` inside the SSE stream.
    """

    def __init__(self, provider: str = "local") -> None:
        self.provider = provider
        self.stream_calls = 0

    async def generate_anthropic(
        self, request: AnthropicRequest
    ) -> AnthropicResponse:
        raise AssertionError("generate_anthropic should not be called in stream tests")

    async def stream_anthropic(
        self, request: AnthropicRequest
    ) -> AsyncIterator[AnthropicStreamEvent]:
        self.stream_calls += 1
        # Emit a couple of events so the client has seen partial content.
        yield AnthropicStreamEvent(
            type="message_start",
            data={
                "type": "message_start",
                "message": {
                    "id": "msg_mid",
                    "type": "message",
                    "role": "assistant",
                    "content": [],
                    "model": "qwen-coder",
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                },
            },
        )
        yield AnthropicStreamEvent(
            type="content_block_start",
            data={
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
        )
        yield AnthropicStreamEvent(
            type="content_block_delta",
            data={
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "partial"},
            },
        )
        # Now simulate mid-stream failure surfaced by the engine.
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
    # Usage propagated
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

    # Must start with message_start and end with message_stop.
    assert event_types[0] == "message_start"
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
    """When the engine raises NoProvidersAvailableError before any event
    ships, the SSE channel should emit a single `error` event (not a 5xx
    HTTP status, since headers have already flushed).
    """
    client, _ = client_and_failing_engine
    body = {**_MINIMAL_BODY, "stream": True}
    with client.stream("POST", "/v1/messages", json=body) as resp:
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
# v0.3-B: Mid-stream guard surfacing over SSE (at the ingress boundary)
# ----------------------------------------------------------------------


def test_streaming_midstream_failure_emits_api_error_event(
    client_and_midstream_engine: tuple[TestClient, _MidStreamFailingEngine],
) -> None:
    """After events have streamed, an engine-level MidStreamError must be
    surfaced as an Anthropic `event: error` with type `api_error`
    (distinct from `overloaded_error`, which means no provider could
    start at all). The emitted prefix is preserved, and the stream is
    truncated (no message_stop after the error).
    """
    client, engine = client_and_midstream_engine
    body = {**_MINIMAL_BODY, "stream": True}
    with client.stream("POST", "/v1/messages", json=body) as resp:
        assert resp.status_code == 200
        raw = b"".join(resp.iter_bytes()).decode("utf-8")

    events = _parse_sse(raw)
    event_types = [t for t, _ in events]

    assert event_types[0] == "message_start"
    assert "content_block_delta" in event_types
    assert event_types[-1] == "error"
    # Crucially: truncated — no message_stop after declaring an error.
    assert "message_stop" not in event_types

    import json as _json

    err = next(d for t, d in events if t == "error")
    err_payload = _json.loads(err)
    assert err_payload["type"] == "error"
    assert err_payload["error"]["type"] == "api_error"
    # The engine was only consulted once — the ingress does not retry.
    assert engine.stream_calls == 1
