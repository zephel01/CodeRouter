"""Ingress profile-selection tests.

Validates that /v1/chat/completions correctly routes the `profile` selector
from either the JSON body or the X-CodeRouter-Profile header into
FallbackEngine, and that unknown profile names fail fast with 400.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from fastapi.testclient import TestClient

from coderouter.adapters.base import ChatRequest, ChatResponse, StreamChunk
from coderouter.config.schemas import CodeRouterConfig, FallbackChain, ProviderConfig
from coderouter.ingress.app import create_app


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
    """Drop-in replacement for FallbackEngine that records the profile seen."""

    def __init__(self) -> None:
        self.seen_profiles: list[str | None] = []

    async def generate(self, request: ChatRequest) -> ChatResponse:
        self.seen_profiles.append(request.profile)
        return ChatResponse(
            id="chatcmpl-test",
            object="chat.completion",
            created=0,
            model="unused-upstream-model",
            choices=[
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                }
            ],
            coderouter_provider="local",
        )

    async def stream(
        self, request: ChatRequest
    ) -> AsyncIterator[StreamChunk]:  # pragma: no cover - unused in these tests
        self.seen_profiles.append(request.profile)
        yield StreamChunk(
            id="x",
            object="chat.completion.chunk",
            created=0,
            model="unused",
            choices=[{"index": 0, "delta": {"content": "ok"}, "finish_reason": "stop"}],
        )


@pytest.fixture
def client_and_engine(
    two_profile_config: CodeRouterConfig, monkeypatch: pytest.MonkeyPatch
) -> tuple[TestClient, _RecordingEngine]:
    """Spin up a FastAPI app with the recording engine swapped in."""
    # Prevent create_app() from loading the real providers.yaml
    monkeypatch.setattr(
        "coderouter.ingress.app.load_config",
        lambda path=None: two_profile_config,
    )
    app = create_app()
    engine = _RecordingEngine()
    app.state.engine = engine
    app.state.config = two_profile_config
    return TestClient(app), engine


_MINIMAL_BODY = {"messages": [{"role": "user", "content": "hi"}]}


def test_profile_from_body_reaches_engine(
    client_and_engine: tuple[TestClient, _RecordingEngine],
) -> None:
    client, engine = client_and_engine
    resp = client.post(
        "/v1/chat/completions",
        json={**_MINIMAL_BODY, "profile": "fast"},
    )
    assert resp.status_code == 200, resp.text
    assert engine.seen_profiles == ["fast"]


def test_profile_from_header_reaches_engine(
    client_and_engine: tuple[TestClient, _RecordingEngine],
) -> None:
    client, engine = client_and_engine
    resp = client.post(
        "/v1/chat/completions",
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
        "/v1/chat/completions",
        json={**_MINIMAL_BODY, "profile": "fast"},
        headers={"X-CodeRouter-Profile": "default"},
    )
    assert resp.status_code == 200, resp.text
    assert engine.seen_profiles == ["fast"]


def test_no_profile_yields_none_for_default(
    client_and_engine: tuple[TestClient, _RecordingEngine],
) -> None:
    """When neither body nor header specify, engine sees None and uses default."""
    client, engine = client_and_engine
    resp = client.post("/v1/chat/completions", json=_MINIMAL_BODY)
    assert resp.status_code == 200, resp.text
    assert engine.seen_profiles == [None]


def test_unknown_profile_is_400(
    client_and_engine: tuple[TestClient, _RecordingEngine],
) -> None:
    """Unknown profile should fail fast with 400 and not touch the engine."""
    client, engine = client_and_engine
    resp = client.post(
        "/v1/chat/completions",
        json={**_MINIMAL_BODY, "profile": "nope"},
    )
    assert resp.status_code == 400, resp.text
    assert "unknown profile" in resp.text
    assert engine.seen_profiles == []


def test_unknown_profile_from_header_is_400(
    client_and_engine: tuple[TestClient, _RecordingEngine],
) -> None:
    client, engine = client_and_engine
    resp = client.post(
        "/v1/chat/completions",
        json=_MINIMAL_BODY,
        headers={"X-CodeRouter-Profile": "nope"},
    )
    assert resp.status_code == 400, resp.text
    assert engine.seen_profiles == []
