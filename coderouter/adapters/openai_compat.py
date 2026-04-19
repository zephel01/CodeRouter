"""OpenAI-compatible HTTP adapter.

Single adapter that covers:
    - Local llama.cpp server (--api-server mode)
    - Local Ollama (/v1 endpoint)
    - LM Studio
    - OpenRouter (free + paid)
    - Together / Fireworks / Groq / DeepInfra
    - Any OpenAI-shaped /v1/chat/completions endpoint

We deliberately do NOT use the openai SDK — see plan.md §5.4 (dependency
minimalism). All upstream calls are plain httpx.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from coderouter.adapters.base import (
    AdapterError,
    BaseAdapter,
    ChatRequest,
    ChatResponse,
    StreamChunk,
)
from coderouter.config.loader import resolve_api_key

# httpx status codes that mean "fall through to next provider"
# - 404: upstream doesn't have the requested model — next provider has a
#   different model so try it
# - 408 / 504: timeouts
# - 425: too early
# - 429: rate limit
# - 5xx: upstream errors
_RETRYABLE_STATUSES = {404, 408, 425, 429, 500, 502, 503, 504}


class OpenAICompatAdapter(BaseAdapter):
    """Talks the OpenAI Chat Completions wire format over httpx."""

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "CodeRouter/0.1",
        }
        api_key = resolve_api_key(self.config.api_key_env)
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        return headers

    def _prepare_messages(self, request: ChatRequest) -> list[dict[str, Any]]:
        """Serialize messages and inject append_system_prompt if configured."""
        messages = [m.model_dump(exclude_none=True) for m in request.messages]
        directive = self.config.append_system_prompt
        if not directive:
            return messages

        # Augment an existing system message, or add a new one at the front.
        for msg in messages:
            if msg.get("role") == "system":
                existing = msg.get("content", "")
                if isinstance(existing, str):
                    msg["content"] = f"{existing}\n{directive}".strip()
                elif isinstance(existing, list):
                    # multimodal content — append a text block
                    msg["content"] = [*existing, {"type": "text", "text": directive}]
                else:
                    msg["content"] = directive
                return messages

        return [{"role": "system", "content": directive}, *messages]

    def _payload(self, request: ChatRequest, *, stream: bool) -> dict[str, Any]:
        # CodeRouter routing is decided by `profile`, NOT by `request.model`.
        # The OpenAI API requires a `model` field in the body, but here it's
        # always set from the provider config — clients that pass arbitrary
        # placeholder strings (e.g. "anything") would otherwise blow up the
        # upstream with 404 model-not-found.
        #
        # Start from provider's extra_body (e.g. `think: false` for Ollama
        # thinking models) so that fields from the request can override them.
        body: dict[str, Any] = dict(self.config.extra_body)
        body.update(
            {
                "model": self.config.model,
                "messages": self._prepare_messages(request),
                "stream": stream,
            }
        )
        for field in ("temperature", "max_tokens", "top_p", "stop", "tools", "tool_choice"):
            value = getattr(request, field, None)
            if value is not None:
                body[field] = value
        if stream:
            # Request a terminal usage chunk. Providers that honor this
            # (OpenAI, OpenRouter, Ollama >=0.x) will send one extra chunk
            # with `choices: []` and `usage: {prompt_tokens, completion_tokens, ...}`
            # at the end of the stream. Providers that don't understand the
            # flag silently ignore it — so it's safe to always send.
            body.setdefault("stream_options", {"include_usage": True})
        return body

    def _url(self) -> str:
        # base_url is normalized to OpenAI shape: it should already include /v1
        # We just append /chat/completions.
        base = str(self.config.base_url).rstrip("/")
        return f"{base}/chat/completions"

    async def healthcheck(self) -> bool:
        """GET base_url/models — most OpenAI-compat servers expose this cheaply."""
        base = str(self.config.base_url).rstrip("/")
        url = f"{base}/models"
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(url, headers=self._headers())
                return resp.status_code < 500
        except httpx.HTTPError:
            return False

    async def generate(self, request: ChatRequest) -> ChatResponse:
        url = self._url()
        payload = self._payload(request, stream=False)
        try:
            async with httpx.AsyncClient(timeout=self.config.timeout_s) as client:
                resp = await client.post(url, json=payload, headers=self._headers())
        except httpx.TimeoutException as exc:
            raise AdapterError(
                f"timeout contacting {url}", provider=self.name, retryable=True
            ) from exc
        except httpx.HTTPError as exc:
            raise AdapterError(
                f"transport error: {exc}", provider=self.name, retryable=True
            ) from exc

        if resp.status_code >= 400:
            raise AdapterError(
                f"{resp.status_code} from upstream: {resp.text[:200]}",
                provider=self.name,
                status_code=resp.status_code,
                retryable=resp.status_code in _RETRYABLE_STATUSES,
            )

        try:
            data = resp.json()
        except json.JSONDecodeError as exc:
            raise AdapterError(
                f"invalid JSON from upstream: {exc}",
                provider=self.name,
                retryable=False,
            ) from exc

        # Tag the response with which provider answered
        data.setdefault("object", "chat.completion")
        return ChatResponse(coderouter_provider=self.name, **data)

    async def stream(self, request: ChatRequest) -> AsyncIterator[StreamChunk]:
        url = self._url()
        payload = self._payload(request, stream=True)
        try:
            async with httpx.AsyncClient(timeout=self.config.timeout_s) as client:
                async with client.stream(
                    "POST", url, json=payload, headers=self._headers()
                ) as resp:
                    if resp.status_code >= 400:
                        body = await resp.aread()
                        raise AdapterError(
                            f"{resp.status_code} from upstream: {body[:200]!r}",
                            provider=self.name,
                            status_code=resp.status_code,
                            retryable=resp.status_code in _RETRYABLE_STATUSES,
                        )
                    async for line in resp.aiter_lines():
                        if not line:
                            continue
                        # SSE format: lines start with "data: "
                        if line.startswith(":"):
                            continue  # comment / heartbeat
                        if not line.startswith("data:"):
                            continue
                        data_str = line[len("data:"):].strip()
                        if data_str == "[DONE]":
                            return
                        try:
                            payload_obj = json.loads(data_str)
                        except json.JSONDecodeError:
                            continue  # skip malformed chunks rather than abort
                        yield StreamChunk(**payload_obj)
        except httpx.TimeoutException as exc:
            raise AdapterError(
                f"timeout streaming from {url}", provider=self.name, retryable=True
            ) from exc
        except httpx.HTTPError as exc:
            raise AdapterError(
                f"transport error: {exc}", provider=self.name, retryable=True
            ) from exc
