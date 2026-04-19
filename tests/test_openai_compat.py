"""OpenAICompatAdapter unit tests — uses pytest-httpx to mock upstream HTTP."""

from __future__ import annotations

import json

import httpx
import pytest
from pytest_httpx import HTTPXMock

from coderouter.adapters.base import ChatRequest, Message
from coderouter.adapters.openai_compat import OpenAICompatAdapter
from coderouter.config.schemas import Capabilities, ProviderConfig


def _provider() -> ProviderConfig:
    return ProviderConfig(
        name="ollama-local",
        base_url="http://localhost:11434/v1",
        model="qwen2.5-coder:14b",
        api_key_env=None,
        capabilities=Capabilities(),
    )


def _request(model_field: str | None = "anything") -> ChatRequest:
    """Build a request mimicking what the user's curl test sent."""
    req = ChatRequest(messages=[Message(role="user", content="hi")])
    if model_field is not None:
        req.model = model_field
    return req


@pytest.mark.asyncio
async def test_payload_uses_provider_model_not_request_model(
    httpx_mock: HTTPXMock,
) -> None:
    """Regression: request.model='anything' must NOT be sent upstream.

    Previously (v0.1.0 day-1), ChatRequest.model overrode the provider's
    configured model, leading to upstream 404 model-not-found. The router
    decides the model via profile/provider — request.model is ignored.
    """
    captured: dict[str, str] = {}

    def _capture(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured["model"] = body["model"]
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-1",
                "object": "chat.completion",
                "created": 0,
                "model": body["model"],
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    httpx_mock.add_callback(
        _capture, url="http://localhost:11434/v1/chat/completions", method="POST"
    )

    adapter = OpenAICompatAdapter(_provider())
    resp = await adapter.generate(_request(model_field="anything"))

    assert captured["model"] == "qwen2.5-coder:14b"
    assert resp.coderouter_provider == "ollama-local"


@pytest.mark.asyncio
async def test_404_is_retryable(httpx_mock: HTTPXMock) -> None:
    """Regression: Ollama returns 404 when model is missing — must be retryable.

    Previously 404 was treated as fatal (retryable=False), so the fallback
    chain aborted on the first provider that lacked the requested model.
    """
    from coderouter.adapters.base import AdapterError

    httpx_mock.add_response(
        url="http://localhost:11434/v1/chat/completions",
        method="POST",
        status_code=404,
        json={"error": {"message": "model not found", "type": "not_found_error"}},
    )

    adapter = OpenAICompatAdapter(_provider())
    with pytest.raises(AdapterError) as info:
        await adapter.generate(_request())
    assert info.value.status_code == 404
    assert info.value.retryable is True


@pytest.mark.asyncio
async def test_400_is_not_retryable(httpx_mock: HTTPXMock) -> None:
    """4xx other than the explicit retry list should abort fallback."""
    from coderouter.adapters.base import AdapterError

    httpx_mock.add_response(
        url="http://localhost:11434/v1/chat/completions",
        method="POST",
        status_code=400,
        json={"error": {"message": "bad request"}},
    )

    adapter = OpenAICompatAdapter(_provider())
    with pytest.raises(AdapterError) as info:
        await adapter.generate(_request())
    assert info.value.status_code == 400
    assert info.value.retryable is False


@pytest.mark.asyncio
async def test_429_is_retryable(httpx_mock: HTTPXMock) -> None:
    """Rate-limited upstreams should fall through to the next provider."""
    from coderouter.adapters.base import AdapterError

    httpx_mock.add_response(
        url="http://localhost:11434/v1/chat/completions",
        method="POST",
        status_code=429,
        json={"error": {"message": "rate limited"}},
    )

    adapter = OpenAICompatAdapter(_provider())
    with pytest.raises(AdapterError) as info:
        await adapter.generate(_request())
    assert info.value.retryable is True


@pytest.mark.asyncio
async def test_append_system_prompt_adds_new_system_message(
    httpx_mock: HTTPXMock,
) -> None:
    """If no system message exists, a new one is prepended with the directive."""
    captured: dict[str, object] = {}

    def _capture(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "x",
                "object": "chat.completion",
                "created": 0,
                "model": "qwen3.5:4b",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    httpx_mock.add_callback(
        _capture, url="http://localhost:11434/v1/chat/completions", method="POST"
    )

    provider = ProviderConfig(
        name="qwen3",
        base_url="http://localhost:11434/v1",
        model="qwen3.5:4b",
        append_system_prompt="/no_think",
    )
    req = ChatRequest(messages=[Message(role="user", content="hi")])

    await OpenAICompatAdapter(provider).generate(req)

    messages = captured["messages"]
    assert isinstance(messages, list)
    assert messages[0] == {"role": "system", "content": "/no_think"}
    assert messages[1]["role"] == "user"


@pytest.mark.asyncio
async def test_append_system_prompt_augments_existing_system_message(
    httpx_mock: HTTPXMock,
) -> None:
    """If a system message already exists, the directive is appended to it."""
    captured: dict[str, object] = {}

    def _capture(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "x", "object": "chat.completion", "created": 0,
                "model": "qwen3.5:4b",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    httpx_mock.add_callback(
        _capture, url="http://localhost:11434/v1/chat/completions", method="POST"
    )

    provider = ProviderConfig(
        name="qwen3",
        base_url="http://localhost:11434/v1",
        model="qwen3.5:4b",
        append_system_prompt="/no_think",
    )
    req = ChatRequest(
        messages=[
            Message(role="system", content="You are a helpful coder."),
            Message(role="user", content="hi"),
        ]
    )

    await OpenAICompatAdapter(provider).generate(req)

    messages = captured["messages"]
    assert isinstance(messages, list)
    assert messages[0]["role"] == "system"
    assert messages[0]["content"] == "You are a helpful coder.\n/no_think"
    assert len(messages) == 2


@pytest.mark.asyncio
async def test_extra_body_is_merged_and_overridable(
    httpx_mock: HTTPXMock,
) -> None:
    """Provider.extra_body lets us inject vendor flags like Ollama `think: false`.

    - extra_body fields appear in the outbound payload
    - request-level fields take precedence over extra_body
    - the required fields (model / messages / stream) are not overridden
    """
    captured: dict[str, object] = {}

    def _capture(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "x",
                "object": "chat.completion",
                "created": 0,
                "model": "qwen2.5-coder:14b",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    httpx_mock.add_callback(
        _capture, url="http://localhost:11434/v1/chat/completions", method="POST"
    )

    provider = ProviderConfig(
        name="ollama-with-think",
        base_url="http://localhost:11434/v1",
        model="qwen2.5-coder:14b",
        extra_body={"think": False, "temperature": 0.9},
    )
    req = ChatRequest(messages=[Message(role="user", content="hi")])
    req.temperature = 0.2  # request overrides extra_body's 0.9

    adapter = OpenAICompatAdapter(provider)
    await adapter.generate(req)

    assert captured["think"] is False  # forwarded from extra_body
    assert captured["temperature"] == 0.2  # request won
    assert captured["model"] == "qwen2.5-coder:14b"  # never overridden
    assert captured["stream"] is False  # never overridden
