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
from coderouter.guards.tool_loop import ToolLoopBreakError, ToolLoopDetection
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
        self.last_drift_severity: str | None = None

    def apply_context_budget(
        self, request: AnthropicRequest
    ) -> tuple[AnthropicRequest, str | None]:
        return request, None

    async def generate_anthropic(self, request: AnthropicRequest) -> AnthropicResponse:
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
        self.last_drift_severity: str | None = None

    def apply_context_budget(
        self, request: AnthropicRequest
    ) -> tuple[AnthropicRequest, str | None]:
        return request, None

    async def generate_anthropic(self, request: AnthropicRequest) -> AnthropicResponse:
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

    class _StubConfig:
        """Minimal config stub for v2.0-H partial stitch resolution."""

        default_profile = "default"

        class _ProfileCfg:
            partial_stitch_action = "off"

        def profile_by_name(self, name: str) -> _MidStreamFailingEngine._StubConfig._ProfileCfg:
            return self._ProfileCfg()

    def __init__(self, provider: str = "local") -> None:
        self.provider = provider
        self.stream_calls = 0
        self.last_drift_severity: str | None = None
        self.config = self._StubConfig()

    def apply_context_budget(
        self, request: AnthropicRequest
    ) -> tuple[AnthropicRequest, str | None]:
        return request, None

    async def generate_anthropic(self, request: AnthropicRequest) -> AnthropicResponse:
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


class _LoopBreakingEngine:
    """Engine that always short-circuits with :class:`ToolLoopBreakError`.

    Models the v1.9-E ``break`` action's behavior at the ingress
    boundary: ``_apply_tool_loop_guard`` raises before the chain is
    consulted, so neither path ever touches a provider. The detection
    fields are fixed so tests can assert on exact values.
    """

    _DETECTION = ToolLoopDetection(
        tool_name="Read",
        repeat_count=3,
        args_canonical='{"path": "a.py"}',
    )

    def __init__(self, profile: str = "default") -> None:
        self.profile = profile
        self.last_drift_severity: str | None = None

    def apply_context_budget(
        self, request: AnthropicRequest
    ) -> tuple[AnthropicRequest, str | None]:
        return request, None

    def _make_error(self) -> ToolLoopBreakError:
        return ToolLoopBreakError(
            self._DETECTION,
            self.profile,
            threshold=3,
            window=5,
        )

    async def generate_anthropic(self, request: AnthropicRequest) -> AnthropicResponse:
        raise self._make_error()

    async def stream_anthropic(
        self, request: AnthropicRequest
    ) -> AsyncIterator[AnthropicStreamEvent]:
        raise self._make_error()
        yield  # pragma: no cover  # generator protocol


class _HangingStreamEngine:
    """Engine whose stream never yields a first event (sleeps forever).

    Exercises the v2.x pre-stream peek timeout: the ingress must give up
    after the resolved first-event budget and return HTTP 504 rather than
    committing a streaming response that would hang the client.
    """

    def __init__(self) -> None:
        self.last_drift_severity: str | None = None
        self.closed = False

    def apply_context_budget(
        self, request: AnthropicRequest
    ) -> tuple[AnthropicRequest, str | None]:
        return request, None

    async def stream_anthropic(
        self, request: AnthropicRequest
    ) -> AsyncIterator[AnthropicStreamEvent]:
        try:
            import asyncio

            await asyncio.sleep(3600)
        finally:
            self.closed = True
        yield  # pragma: no cover  # generator protocol


@pytest.fixture
def client_and_hanging_engine(
    two_profile_config: CodeRouterConfig, monkeypatch: pytest.MonkeyPatch
) -> tuple[TestClient, _HangingStreamEngine]:
    monkeypatch.setattr(
        "coderouter.ingress.app.load_config",
        lambda path=None: two_profile_config,
    )
    # Force a tiny peek budget so the test doesn't actually wait 60s.
    monkeypatch.setattr(
        "coderouter.ingress.anthropic_routes._resolve_first_event_timeout_s",
        lambda engine, profile: 0.05,
    )
    app = create_app()
    engine = _HangingStreamEngine()
    app.state.engine = engine
    app.state.config = two_profile_config
    return TestClient(app), engine


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


@pytest.fixture
def client_and_loop_breaking_engine(
    two_profile_config: CodeRouterConfig, monkeypatch: pytest.MonkeyPatch
) -> tuple[TestClient, _LoopBreakingEngine]:
    monkeypatch.setattr(
        "coderouter.ingress.app.load_config",
        lambda path=None: two_profile_config,
    )
    app = create_app()
    engine = _LoopBreakingEngine(profile="default")
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


def test_anthropic_beta_header_threads_through_to_request(
    client_and_engine: tuple[TestClient, _RecordingEngine],
) -> None:
    """v0.4-D: `anthropic-beta` header lands on AnthropicRequest.anthropic_beta.

    The engine sees the beta flag so the native adapter can forward it to
    api.anthropic.com. Without this, body fields like `context_management`
    that Claude Code relies on 400 with "Extra inputs are not permitted".
    """
    client, engine = client_and_engine
    resp = client.post(
        "/v1/messages",
        json=_MINIMAL_BODY,
        headers={"anthropic-beta": "context-management-2025-06-27,fake-beta"},
    )
    assert resp.status_code == 200, resp.text
    assert len(engine.seen_requests) == 1
    assert engine.seen_requests[0].anthropic_beta == "context-management-2025-06-27,fake-beta"


def test_missing_anthropic_beta_header_leaves_field_none(
    client_and_engine: tuple[TestClient, _RecordingEngine],
) -> None:
    """No header → no beta flag — the adapter won't add one either."""
    client, engine = client_and_engine
    resp = client.post("/v1/messages", json=_MINIMAL_BODY)
    assert resp.status_code == 200, resp.text
    assert engine.seen_requests[0].anthropic_beta is None


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
# v0.6-D: mode_aliases (X-CodeRouter-Mode → profile) — Anthropic ingress
# ----------------------------------------------------------------------


@pytest.fixture
def mode_aliased_config() -> CodeRouterConfig:
    """Config with ``mode_aliases`` declared, parallel to ``two_profile_config``."""
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
        mode_aliases={"coding": "default", "quick": "fast"},
    )


@pytest.fixture
def mode_client_and_engine(
    mode_aliased_config: CodeRouterConfig, monkeypatch: pytest.MonkeyPatch
) -> tuple[TestClient, _RecordingEngine]:
    monkeypatch.setattr(
        "coderouter.ingress.app.load_config",
        lambda path=None: mode_aliased_config,
    )
    app = create_app()
    engine = _RecordingEngine()
    app.state.engine = engine
    app.state.config = mode_aliased_config
    return TestClient(app), engine


def test_mode_header_resolves_to_aliased_profile(
    mode_client_and_engine: tuple[TestClient, _RecordingEngine],
) -> None:
    """Anthropic ingress respects ``X-CodeRouter-Mode`` the same as OpenAI."""
    client, engine = mode_client_and_engine
    resp = client.post(
        "/v1/messages",
        json=_MINIMAL_BODY,
        headers={"X-CodeRouter-Mode": "quick"},
    )
    assert resp.status_code == 200, resp.text
    assert engine.seen_profiles == ["fast"]


def test_profile_header_wins_over_mode_header(
    mode_client_and_engine: tuple[TestClient, _RecordingEngine],
) -> None:
    client, engine = mode_client_and_engine
    resp = client.post(
        "/v1/messages",
        json=_MINIMAL_BODY,
        headers={
            "X-CodeRouter-Profile": "default",
            "X-CodeRouter-Mode": "quick",
        },
    )
    assert resp.status_code == 200, resp.text
    assert engine.seen_profiles == ["default"]


def test_body_profile_wins_over_mode_header(
    mode_client_and_engine: tuple[TestClient, _RecordingEngine],
) -> None:
    client, engine = mode_client_and_engine
    resp = client.post(
        "/v1/messages",
        json={**_MINIMAL_BODY, "profile": "default"},
        headers={"X-CodeRouter-Mode": "quick"},
    )
    assert resp.status_code == 200, resp.text
    assert engine.seen_profiles == ["default"]


def test_unknown_mode_is_400_with_available_list(
    mode_client_and_engine: tuple[TestClient, _RecordingEngine],
) -> None:
    client, engine = mode_client_and_engine
    resp = client.post(
        "/v1/messages",
        json=_MINIMAL_BODY,
        headers={"X-CodeRouter-Mode": "nope"},
    )
    assert resp.status_code == 400, resp.text
    assert "unknown mode" in resp.text
    assert "coding" in resp.text and "quick" in resp.text
    assert engine.seen_profiles == []


def test_mode_header_when_mode_aliases_empty_is_400(
    client_and_engine: tuple[TestClient, _RecordingEngine],
) -> None:
    """Base fixture has no ``mode_aliases`` — mode header must 400, not fall through."""
    client, engine = client_and_engine
    resp = client.post(
        "/v1/messages",
        json=_MINIMAL_BODY,
        headers={"X-CodeRouter-Mode": "coding"},
    )
    assert resp.status_code == 400, resp.text
    assert "unknown mode" in resp.text
    assert engine.seen_profiles == []


def test_streaming_path_also_respects_mode_header(
    mode_client_and_engine: tuple[TestClient, _RecordingEngine],
) -> None:
    """Mode resolution happens before the stream flag is honored, so SSE works too."""
    client, engine = mode_client_and_engine
    body = {**_MINIMAL_BODY, "stream": True}
    with client.stream(
        "POST",
        "/v1/messages",
        json=body,
        headers={"X-CodeRouter-Mode": "quick"},
    ) as resp:
        assert resp.status_code == 200
        # Drain so the generator runs to completion.
        b"".join(resp.iter_bytes())
    assert engine.seen_profiles == ["fast"]


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


def test_streaming_total_failure_returns_502(
    client_and_failing_engine: tuple[TestClient, _FailingEngine],
) -> None:
    """v2.x fail-fast: when the engine raises NoProvidersAvailableError
    before any event ships, the ingress peeks the first event *before*
    committing the streaming response, so a total pre-stream failure now
    surfaces as a real HTTP 502 (same as the non-streaming path) instead
    of a 200 carrying an in-band ``event: error`` frame.
    """
    client, _ = client_and_failing_engine
    body = {**_MINIMAL_BODY, "stream": True}
    resp = client.post("/v1/messages", json=body)
    assert resp.status_code == 502, resp.text
    # NoProvidersAvailableError message embeds the profile name.
    assert "all providers failed" in resp.text


def test_streaming_peek_timeout_returns_504(
    client_and_hanging_engine: tuple[TestClient, _HangingStreamEngine],
) -> None:
    """v2.x fail-fast: if no provider emits a first event within the peek
    budget, the ingress returns HTTP 504 (and closes the engine generator)
    rather than committing a streaming response that hangs the client.
    """
    client, _engine = client_and_hanging_engine
    body = {**_MINIMAL_BODY, "stream": True}
    resp = client.post("/v1/messages", json=body)
    assert resp.status_code == 504, resp.text
    assert "before the first token" in resp.text


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


# ----------------------------------------------------------------------
# v1.9-E (L3): tool-loop ``break`` action — ingress translation
#
# The guard itself (warn/inject/break dispatch) is covered in
# tests/test_guards_tool_loop.py. These two tests cover ONLY the
# ingress-layer translation: ToolLoopBreakError → 400 (non-streaming)
# and ToolLoopBreakError → SSE error event (streaming).
# ----------------------------------------------------------------------


def test_break_action_non_streaming_returns_400_with_structured_detail(
    client_and_loop_breaking_engine: tuple[TestClient, _LoopBreakingEngine],
) -> None:
    """``break`` must surface as 400 with a structured ``detail`` dict.

    Programmatic clients branch on ``detail.error == "tool_loop_detected"``
    and read ``tool_name`` / ``repeat_count`` / ``profile`` to react
    (stop, switch approach, notify the user). The shape is stable —
    new fields may be added but existing ones must not be renamed
    without a version bump.
    """
    client, _ = client_and_loop_breaking_engine
    resp = client.post("/v1/messages", json=_MINIMAL_BODY)
    assert resp.status_code == 400, resp.text

    body = resp.json()
    detail = body["detail"]
    assert isinstance(detail, dict), detail
    # Discriminator clients branch on.
    assert detail["error"] == "tool_loop_detected"
    # Detection fields — straight from the exception.
    assert detail["profile"] == "default"
    assert detail["tool_name"] == "Read"
    assert detail["repeat_count"] == 3
    assert detail["threshold"] == 3
    assert detail["window"] == 5
    # Human-readable message mirrors str(exc) for log-grep parity.
    assert "tool loop detected" in detail["message"]
    assert "'Read'" in detail["message"]
    # ``args_canonical`` must NOT leak into the response — it can
    # contain user data (file paths, shell commands, etc.) and the
    # client doesn't need it to react.
    assert "args_canonical" not in detail


def test_break_action_streaming_returns_400_with_structured_detail(
    client_and_loop_breaking_engine: tuple[TestClient, _LoopBreakingEngine],
) -> None:
    """v2.x fail-fast: streaming ``break`` now returns a real HTTP 400.

    The tool-loop ``break`` guard fires before the first stream event, so
    the ingress peek catches ``ToolLoopBreakError`` *before* the
    StreamingResponse commits HTTP 200. The response is therefore an
    ordinary 400 with the same structured ``detail`` dict as the
    non-streaming path — no in-band SSE error frame, no committed 200.
    """
    client, _ = client_and_loop_breaking_engine
    body = {**_MINIMAL_BODY, "stream": True}
    resp = client.post("/v1/messages", json=body)
    assert resp.status_code == 400, resp.text

    detail = resp.json()["detail"]
    assert isinstance(detail, dict), detail
    # Discriminator clients branch on — identical to the non-streaming 400.
    assert detail["error"] == "tool_loop_detected"
    assert detail["profile"] == "default"
    assert detail["tool_name"] == "Read"
    assert detail["repeat_count"] == 3
    assert detail["threshold"] == 3
    assert detail["window"] == 5
    assert "tool loop detected" in detail["message"]
    # ``args_canonical`` must NOT leak into the response.
    assert "args_canonical" not in detail


# ----------------------------------------------------------------------
# v2.0-F (L1): X-CodeRouter-Context-Budget response header
#
# The ingress calls engine.apply_context_budget() before dispatching to
# generate/stream and attaches the header when the guard fires.
# ----------------------------------------------------------------------


class _ContextBudgetEngine(_RecordingEngine):
    """Engine that reports a context-budget status.

    Simulates the ``apply_context_budget`` call returning "warning" or
    "trimmed" so the ingress can attach the response header.
    """

    def __init__(self, budget_status: str | None = None) -> None:
        super().__init__()
        self._budget_status = budget_status

    def apply_context_budget(
        self, request: AnthropicRequest
    ) -> tuple[AnthropicRequest, str | None]:
        return request, self._budget_status


@pytest.fixture
def client_and_budget_engine_warning(
    two_profile_config: CodeRouterConfig, monkeypatch: pytest.MonkeyPatch
) -> tuple[TestClient, _ContextBudgetEngine]:
    monkeypatch.setattr(
        "coderouter.ingress.app.load_config",
        lambda path=None: two_profile_config,
    )
    app = create_app()
    engine = _ContextBudgetEngine(budget_status="warning")
    app.state.engine = engine
    app.state.config = two_profile_config
    return TestClient(app), engine


@pytest.fixture
def client_and_budget_engine_trimmed(
    two_profile_config: CodeRouterConfig, monkeypatch: pytest.MonkeyPatch
) -> tuple[TestClient, _ContextBudgetEngine]:
    monkeypatch.setattr(
        "coderouter.ingress.app.load_config",
        lambda path=None: two_profile_config,
    )
    app = create_app()
    engine = _ContextBudgetEngine(budget_status="trimmed")
    app.state.engine = engine
    app.state.config = two_profile_config
    return TestClient(app), engine


class TestContextBudgetHeader:
    """v2.0-F: X-CodeRouter-Context-Budget response header."""

    def test_no_header_when_guard_inactive(
        self, client_and_engine: tuple[TestClient, _RecordingEngine]
    ) -> None:
        """No header when the guard returns None (inactive / below threshold)."""
        client, _ = client_and_engine
        resp = client.post("/v1/messages", json=_MINIMAL_BODY)
        assert resp.status_code == 200
        assert "x-coderouter-context-budget" not in resp.headers

    def test_warning_header_non_streaming(
        self,
        client_and_budget_engine_warning: tuple[TestClient, _ContextBudgetEngine],
    ) -> None:
        """Non-streaming: header value is 'warning' when over warn threshold."""
        client, _ = client_and_budget_engine_warning
        resp = client.post("/v1/messages", json=_MINIMAL_BODY)
        assert resp.status_code == 200
        assert resp.headers.get("x-coderouter-context-budget") == "warning"

    def test_trimmed_header_non_streaming(
        self,
        client_and_budget_engine_trimmed: tuple[TestClient, _ContextBudgetEngine],
    ) -> None:
        """Non-streaming: header value is 'trimmed' when messages removed."""
        client, _ = client_and_budget_engine_trimmed
        resp = client.post("/v1/messages", json=_MINIMAL_BODY)
        assert resp.status_code == 200
        assert resp.headers.get("x-coderouter-context-budget") == "trimmed"

    def test_warning_header_streaming(
        self,
        client_and_budget_engine_warning: tuple[TestClient, _ContextBudgetEngine],
    ) -> None:
        """Streaming: header is present on the SSE response."""
        client, _ = client_and_budget_engine_warning
        body = {**_MINIMAL_BODY, "stream": True}
        with client.stream("POST", "/v1/messages", json=body) as resp:
            assert resp.status_code == 200
            assert resp.headers.get("x-coderouter-context-budget") == "warning"
            # Consume stream to avoid ResourceWarning
            _ = b"".join(resp.iter_bytes())

    def test_trimmed_header_streaming(
        self,
        client_and_budget_engine_trimmed: tuple[TestClient, _ContextBudgetEngine],
    ) -> None:
        """Streaming: header value is 'trimmed' when messages removed."""
        client, _ = client_and_budget_engine_trimmed
        body = {**_MINIMAL_BODY, "stream": True}
        with client.stream("POST", "/v1/messages", json=body) as resp:
            assert resp.status_code == 200
            assert resp.headers.get("x-coderouter-context-budget") == "trimmed"
            _ = b"".join(resp.iter_bytes())
