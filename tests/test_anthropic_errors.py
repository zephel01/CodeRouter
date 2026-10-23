"""v2.16.0: Anthropic-shaped error responses for a failed chain.

Covers the pure mapper (``coderouter.ingress.anthropic_errors``) and the
``/v1/messages`` wiring, streaming and non-streaming:

- context overflow from any upstream → 400 ``invalid_request_error`` whose
  message starts with ``prompt is too long`` (Claude Code's reactive
  compaction trigger);
- a non-retryable upstream 400 → 400 with the upstream's own wording
  (Claude Code's self-recovery from rejected features reads it);
- 429 → 429 ``rate_limit_error`` with ``retry-after`` passed through;
- upstream 401/403 and everything else → 502 ``api_error``.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import pytest
from fastapi.testclient import TestClient

from coderouter.adapters.base import UPSTREAM_BODY_CAP, AdapterError
from coderouter.config.schemas import CodeRouterConfig, FallbackChain, ProviderConfig
from coderouter.ingress.anthropic_errors import (
    is_context_overflow,
    map_no_providers_error,
    upstream_message,
)
from coderouter.ingress.app import create_app
from coderouter.routing import NoProvidersAvailableError
from coderouter.translation import AnthropicRequest, AnthropicResponse, AnthropicStreamEvent

# Real upstream bodies (shape as of 2026-10).
_LLAMACPP_OVERFLOW = json.dumps(
    {
        "error": {
            "code": 400,
            "message": "the request exceeds the available context size, try increasing it",
            "type": "exceed_context_size_error",
            "n_prompt_tokens": 40210,
            "n_ctx": 32768,
        }
    }
)
_VLLM_OVERFLOW = json.dumps(
    {
        "object": "error",
        "message": (
            "This model's maximum context length is 32768 tokens. However, you "
            "requested 40210 tokens in the messages."
        ),
        "type": "BadRequestError",
        "code": 400,
    }
)
_ANTHROPIC_400 = json.dumps(
    {
        "type": "error",
        "error": {
            "type": "invalid_request_error",
            "message": "tools.3.defer_loading: Extra inputs are not permitted",
        },
    }
)


def _err(
    status: int | None,
    body: str | None = None,
    *,
    retryable: bool | None = None,
    retry_after: str | None = None,
    provider: str = "local",
) -> AdapterError:
    if retryable is None:
        retryable = status in {404, 408, 425, 429, 500, 502, 503, 504} or status is None
    return AdapterError(
        f"{status} from upstream: {(body or '')[:200]}",
        provider=provider,
        status_code=status,
        retryable=retryable,
        upstream_body=body,
        retry_after=retry_after,
    )


# ---------------------------------------------------------------------------
# Pure mapper
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("body", [_LLAMACPP_OVERFLOW, _VLLM_OVERFLOW, "prompt too long; exceeded max context length"])
def test_context_overflow_becomes_prompt_is_too_long(body: str) -> None:
    exc = NoProvidersAvailableError("default", [_err(400, body)])
    mapped = map_no_providers_error(exc)
    assert mapped.status_code == 400
    assert mapped.error_type == "invalid_request_error"
    assert mapped.message.startswith("prompt is too long")


def test_overflow_anywhere_in_chain_wins_over_terminal_error() -> None:
    errors = [_err(400, _LLAMACPP_OVERFLOW, retryable=True), _err(503, "busy")]
    mapped = map_no_providers_error(NoProvidersAvailableError("default", errors))
    assert mapped.status_code == 400
    assert mapped.message.startswith("prompt is too long")


def test_overflow_wording_on_a_5xx_is_not_treated_as_overflow() -> None:
    assert not is_context_overflow(_err(500, "maximum context length exceeded in worker"))


def test_non_retryable_400_keeps_upstream_wording() -> None:
    mapped = map_no_providers_error(
        NoProvidersAvailableError("default", [_err(400, _ANTHROPIC_400, retryable=False)])
    )
    assert mapped.status_code == 400
    assert mapped.error_type == "invalid_request_error"
    assert mapped.message == "tools.3.defer_loading: Extra inputs are not permitted"


def test_413_maps_to_request_too_large() -> None:
    mapped = map_no_providers_error(
        NoProvidersAvailableError("default", [_err(413, "payload too big", retryable=False)])
    )
    assert (mapped.status_code, mapped.error_type) == (413, "request_too_large")


def test_429_maps_to_rate_limit_with_retry_after() -> None:
    mapped = map_no_providers_error(
        NoProvidersAvailableError("default", [_err(429, '{"error":{"message":"slow down"}}', retry_after="7")])
    )
    assert (mapped.status_code, mapped.error_type) == (429, "rate_limit_error")
    assert mapped.headers == {"retry-after": "7", "x-should-retry": "true"}
    assert mapped.message == "slow down"


@pytest.mark.parametrize("status", [401, 403, 500, 503, None])
def test_everything_else_is_502_api_error(status: int | None) -> None:
    mapped = map_no_providers_error(
        NoProvidersAvailableError("default", [_err(status, "nope", retryable=False)])
    )
    assert (mapped.status_code, mapped.error_type) == (502, "api_error")
    assert "all providers failed" in mapped.message


def test_empty_chain_is_502() -> None:
    mapped = map_no_providers_error(NoProvidersAvailableError("default", []))
    assert mapped.status_code == 502
    assert "no providers eligible" in mapped.message


def test_upstream_message_redacts_registered_secrets() -> None:
    from coderouter import secret_redaction

    secret_redaction.register_secret("sk-test-very-secret-value-123456", "test")
    try:
        msg = upstream_message(_err(400, '{"error":{"message":"bad key sk-test-very-secret-value-123456"}}'))
    finally:
        secret_redaction.clear_secrets()
    assert "sk-test-very-secret-value-123456" not in msg


def test_upstream_body_is_capped() -> None:
    e = _err(400, "x" * (UPSTREAM_BODY_CAP * 3))
    assert e.upstream_body is not None
    assert len(e.upstream_body) == UPSTREAM_BODY_CAP


# ---------------------------------------------------------------------------
# /v1/messages wiring
# ---------------------------------------------------------------------------


class _ChainFailEngine:
    def __init__(self, errors: list[AdapterError]) -> None:
        self.errors = errors
        self.last_drift_severity: str | None = None

    def apply_context_budget(self, request: AnthropicRequest) -> tuple[AnthropicRequest, str | None]:
        return request, None

    async def generate_anthropic(self, request: AnthropicRequest) -> AnthropicResponse:
        raise NoProvidersAvailableError("default", self.errors)

    async def stream_anthropic(self, request: AnthropicRequest) -> AsyncIterator[AnthropicStreamEvent]:
        raise NoProvidersAvailableError("default", self.errors)
        yield  # pragma: no cover


@pytest.fixture
def make_client(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    config = CodeRouterConfig(
        allow_paid=False,
        default_profile="default",
        providers=[ProviderConfig(name="local", base_url="http://127.0.0.1:8080/v1", model="m")],
        profiles=[FallbackChain(name="default", providers=["local"])],
    )
    monkeypatch.setattr("coderouter.ingress.app.load_config", lambda path=None: config)

    def _make(errors: list[AdapterError]) -> TestClient:
        app = create_app()
        app.state.engine = _ChainFailEngine(errors)
        app.state.config = config
        return TestClient(app)

    return _make


_BODY = {"model": "claude-sonnet-5", "max_tokens": 64, "messages": [{"role": "user", "content": "hi"}]}


@pytest.mark.parametrize("stream", [False, True])
def test_overflow_reaches_client_as_400_prompt_is_too_long(make_client, stream: bool) -> None:  # type: ignore[no-untyped-def]
    client = make_client([_err(400, _LLAMACPP_OVERFLOW, retryable=False)])
    resp = client.post("/v1/messages", json={**_BODY, "stream": stream})
    assert resp.status_code == 400, resp.text
    payload = resp.json()
    assert payload["type"] == "error"
    assert payload["error"]["type"] == "invalid_request_error"
    assert payload["error"]["message"].startswith("prompt is too long")


@pytest.mark.parametrize("stream", [False, True])
def test_rate_limit_reaches_client_with_retry_after(make_client, stream: bool) -> None:  # type: ignore[no-untyped-def]
    client = make_client([_err(429, "busy", retry_after="12")])
    resp = client.post("/v1/messages", json={**_BODY, "stream": stream})
    assert resp.status_code == 429
    assert resp.headers["retry-after"] == "12"
    assert resp.json()["error"]["type"] == "rate_limit_error"


# ---------------------------------------------------------------------------
# Review fixes (v2.16.0)
# ---------------------------------------------------------------------------


def test_completion_budget_overflow_is_not_prompt_too_long() -> None:
    """max_tokens too large for the window: compaction cannot help."""
    body = json.dumps(
        {
            "object": "error",
            "message": (
                "This model's maximum context length is 32768 tokens. However, you "
                "requested 33000 tokens (1000 in the messages, 32000 in the completion)."
            ),
            "code": 400,
        }
    )
    err = _err(400, body, retryable=False)
    assert not is_context_overflow(err)
    mapped = map_no_providers_error(NoProvidersAvailableError("default", [err]))
    assert mapped.status_code == 400
    assert not mapped.message.startswith("prompt is too long")
    assert "in the completion" in mapped.message


class _RejectInsideEngine(_ChainFailEngine):
    """The engine re-runs the budget guard (input-filter plugin, model swap)."""

    def __init__(self) -> None:
        super().__init__([])

    async def generate_anthropic(self, request: AnthropicRequest) -> AnthropicResponse:
        from coderouter.guards.context_budget import ContextBudgetRejectError

        raise ContextBudgetRejectError(estimated_tokens=500, max_context_tokens=100, profile="default")

    async def stream_anthropic(self, request: AnthropicRequest) -> AsyncIterator[AnthropicStreamEvent]:
        from coderouter.guards.context_budget import ContextBudgetRejectError

        raise ContextBudgetRejectError(estimated_tokens=500, max_context_tokens=100, profile="default")
        yield  # pragma: no cover


@pytest.mark.parametrize("stream", [False, True])
def test_reject_raised_inside_engine_is_400_not_500(monkeypatch: pytest.MonkeyPatch, stream: bool) -> None:
    config = CodeRouterConfig(
        allow_paid=False,
        default_profile="default",
        providers=[ProviderConfig(name="local", base_url="http://127.0.0.1:8080/v1", model="m")],
        profiles=[FallbackChain(name="default", providers=["local"])],
    )
    monkeypatch.setattr("coderouter.ingress.app.load_config", lambda path=None: config)
    app = create_app()
    app.state.engine = _RejectInsideEngine()
    app.state.config = config
    resp = TestClient(app).post("/v1/messages", json={**_BODY, "stream": stream})
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["message"].startswith("prompt is too long")


@pytest.mark.asyncio
async def test_guard_stream_honours_shared_deadline() -> None:
    """The prefetch and the stream share one overall ceiling."""
    import asyncio

    from coderouter.ingress.anthropic_routes import _guard_stream

    async def slow() -> AsyncIterator[str]:
        await asyncio.sleep(5)
        yield "never"

    loop = asyncio.get_running_loop()
    frames = [
        f
        async for f in _guard_stream(
            slow(), timeout_s=60.0, label="t", deadline=loop.time() + 0.05
        )
    ]
    assert len(frames) == 1
    assert "timeout_error" in frames[0]


@pytest.mark.asyncio
async def test_disconnect_watcher_cancels_owner() -> None:
    import asyncio

    from coderouter.ingress.anthropic_routes import _DisconnectWatcher

    class _Req:
        def __init__(self) -> None:
            self.calls = 0

        async def is_disconnected(self) -> bool:
            self.calls += 1
            return self.calls >= 2

    watcher = _DisconnectWatcher(_Req(), interval=0.01)  # type: ignore[arg-type]
    watcher.start()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.sleep(5)
    assert watcher.fired
    watcher.uncancel()
    await watcher.stop()


@pytest.mark.asyncio
async def test_disconnect_watcher_quiet_when_client_stays() -> None:
    import asyncio

    from coderouter.ingress.anthropic_routes import _DisconnectWatcher

    class _Req:
        async def is_disconnected(self) -> bool:
            return False

    watcher = _DisconnectWatcher(_Req(), interval=0.01)  # type: ignore[arg-type]
    watcher.start()
    await asyncio.sleep(0.05)
    assert not watcher.fired
    await watcher.stop()
# v2.17.0: x-should-retry
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("errors", "expected"),
    [
        ([_err(400, _LLAMACPP_OVERFLOW, retryable=False)], "false"),
        ([_err(400, _ANTHROPIC_400, retryable=False)], "false"),
        ([_err(413, "big", retryable=False)], "false"),
        ([_err(429, "slow")], "true"),
        ([_err(503, "busy")], "true"),
        ([_err(401, "bad key", retryable=False), _err(403, "nope", retryable=False)], "false"),
        ([_err(401, "bad key", retryable=False), _err(503, "busy")], "true"),
        ([], "false"),
    ],
)
def test_x_should_retry(errors: list[AdapterError], expected: str) -> None:
    mapped = map_no_providers_error(NoProvidersAvailableError("default", errors))
    assert mapped.headers["x-should-retry"] == expected


@pytest.mark.parametrize("stream", [False, True])
def test_x_should_retry_reaches_client(make_client, stream: bool) -> None:  # type: ignore[no-untyped-def]
    client = make_client([_err(401, "bad key", retryable=False)])
    resp = client.post("/v1/messages", json={**_BODY, "stream": stream})
    assert resp.status_code == 502
    assert resp.headers["x-should-retry"] == "false"
