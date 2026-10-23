"""OpenAI Responses API route: ``POST /v1/responses`` (v2.19.0).

Lets the OpenAI Codex CLI (Responses-only since 2026-02) use CodeRouter as a
custom provider::

    [model_providers.coderouter]
    name = "CodeRouter"
    base_url = "http://127.0.0.1:8088/v1"
    wire_api = "responses"

The request is translated to an Anthropic Messages request
(:mod:`coderouter.translation.responses`) and run through the same fallback
engine as ``/v1/messages`` — tool-call repair, guards, fallback and the
v2.16.0 error mapping all apply. Profile selection mirrors the other
routes: body ``profile`` > ``X-CodeRouter-Profile`` > ``X-CodeRouter-Mode``
> auto router > default.

Failures are reported the way Codex reads them:

- before the response commits: an OpenAI-style error body with a real
  HTTP status (400 non-retryable / 429 with ``Retry-After`` / 502);
- a context overflow on a streaming request: ``response.failed`` with code
  ``context_length_exceeded`` — the only overflow signal Codex recognizes
  (an HTTP 400 is treated as a plain invalid request);
- after the stream started: ``response.failed`` (``server_error``), then end
  of stream, which Codex retries.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from coderouter.adapters.slot_affinity import affinity_key
from coderouter.guards.context_budget import ContextBudgetRejectError
from coderouter.guards.tool_loop import ToolCountExceededError, ToolLoopBreakError
from coderouter.ingress.anthropic_errors import (
    PROMPT_TOO_LONG_PREFIX,
    AnthropicErrorResponse,
    map_no_providers_error,
)
from coderouter.ingress.anthropic_routes import (
    _DisconnectWatcher,
    _resolve_stream_timeout_s,
)
from coderouter.logging import get_logger
from coderouter.routing import FallbackEngine, MidStreamError, NoProvidersAvailableError
from coderouter.routing.auto_router import (
    RESERVED_PROFILE_NAME,
    classify,
    extract_gateway_hints,
)
from coderouter.translation import AnthropicStreamEvent
from coderouter.translation.responses import (
    ResponsesStreamTranslator,
    anthropic_response_to_responses,
    format_sse,
    responses_to_anthropic,
)

router = APIRouter()
logger = get_logger(__name__)

_PROFILE_HEADER = "x-coderouter-profile"
_MODE_HEADER = "x-coderouter-mode"

_OPENAI_TYPE = {
    "invalid_request_error": "invalid_request_error",
    "request_too_large": "invalid_request_error",
    "rate_limit_error": "rate_limit_error",
    "timeout_error": "server_error",
    "api_error": "server_error",
}


def _openai_error(
    status: int,
    message: str,
    *,
    etype: str,
    code: str | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"message": message, "type": etype, "param": None, "code": code}},
        headers=headers or None,
    )


def _is_overflow(mapped: AnthropicErrorResponse) -> bool:
    return mapped.status_code == 400 and mapped.message.lower().startswith(PROMPT_TOO_LONG_PREFIX)


def _mapped_to_http(mapped: AnthropicErrorResponse) -> JSONResponse:
    code = None
    if _is_overflow(mapped):
        code = "context_length_exceeded"
    elif mapped.status_code == 429:
        code = "rate_limit_exceeded"
    return _openai_error(
        mapped.status_code,
        mapped.message,
        etype=_OPENAI_TYPE.get(mapped.error_type, "server_error"),
        code=code,
        headers=dict(mapped.headers),
    )


def _failed_stream(translator: ResponsesStreamTranslator, code: str, message: str) -> StreamingResponse:
    async def gen() -> AsyncIterator[str]:
        for ev in translator.start():
            yield format_sse(ev)
        for ev in translator.failed(code, message):
            yield format_sse(ev)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


@router.post("/responses", response_model=None)
async def responses(
    request: Request,
    x_coderouter_profile: str | None = Header(default=None, alias=_PROFILE_HEADER),
    x_coderouter_mode: str | None = Header(default=None, alias=_MODE_HEADER),
) -> Response:
    """OpenAI Responses API endpoint (streaming and non-streaming)."""
    engine: FallbackEngine = request.app.state.engine
    config = request.app.state.config

    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return _openai_error(400, "request body must be JSON", etype="invalid_request_error")
    if not isinstance(body, dict):
        return _openai_error(400, "request body must be a JSON object", etype="invalid_request_error")

    try:
        anth_req, tool_map = responses_to_anthropic(body)
    except Exception as exc:  # pydantic ValidationError and shape errors
        return _openai_error(400, f"could not translate request: {exc}", etype="invalid_request_error")

    model = body.get("model") if isinstance(body.get("model"), str) else "coderouter"

    # Conversation key for llama-server slot affinity: Claude-style hint
    # headers when present, else Codex's own ``session-id`` header.
    hints = extract_gateway_hints(request.headers)
    anth_req.session_key = affinity_key(hints) or request.headers.get("session-id") or None

    if anth_req.profile is None and x_coderouter_profile:
        anth_req.profile = x_coderouter_profile
    if anth_req.profile is None and x_coderouter_mode:
        try:
            anth_req.profile = config.resolve_mode(x_coderouter_mode)
        except KeyError:
            return _openai_error(
                400, f"unknown mode {x_coderouter_mode!r}", etype="invalid_request_error"
            )
    if anth_req.profile is None and config.default_profile == RESERVED_PROFILE_NAME:
        anth_req.profile = classify(anth_req.model_dump(), config, hints)
    if anth_req.profile is not None:
        try:
            config.profile_by_name(anth_req.profile)
        except KeyError:
            return _openai_error(
                400, f"unknown profile {anth_req.profile!r}", etype="invalid_request_error"
            )

    translator = ResponsesStreamTranslator(tool_map, model=model)

    try:
        anth_req, _ctx = engine.apply_context_budget(anth_req)
    except ContextBudgetRejectError as exc:
        if anth_req.stream:
            return _failed_stream(translator, "context_length_exceeded", str(exc))
        return _openai_error(400, str(exc), etype="invalid_request_error", code="context_length_exceeded")

    if not anth_req.stream:
        try:
            resp = await engine.generate_anthropic(anth_req)
        except NoProvidersAvailableError as exc:
            return _mapped_to_http(map_no_providers_error(exc))
        except ContextBudgetRejectError as exc:
            return _openai_error(400, str(exc), etype="invalid_request_error", code="context_length_exceeded")
        except (ToolLoopBreakError, ToolCountExceededError) as exc:
            return _openai_error(400, str(exc), etype="invalid_request_error")
        return JSONResponse(
            anthropic_response_to_responses(resp.model_dump(exclude_none=True), tool_map, model=model)
        )

    # Streaming: pull the first engine event before committing, like
    # /v1/messages (v2.16.0), so pre-content failures get a real status.
    timeout_s = _resolve_stream_timeout_s(engine, anth_req.profile)
    deadline = asyncio.get_running_loop().time() + timeout_s
    source = engine.stream_anthropic(anth_req)
    first: AnthropicStreamEvent | None = None
    watcher = _DisconnectWatcher(request)
    watcher.start()

    async def _close() -> None:
        with contextlib.suppress(Exception):
            await source.aclose()  # type: ignore[attr-defined]

    try:
        async with asyncio.timeout_at(deadline) as scope:
            first = await anext(source)
    except StopAsyncIteration:
        first = None
    except NoProvidersAvailableError as exc:
        await _close()
        mapped = map_no_providers_error(exc)
        if _is_overflow(mapped):
            return _failed_stream(translator, "context_length_exceeded", mapped.message)
        return _mapped_to_http(mapped)
    except ContextBudgetRejectError as exc:
        await _close()
        return _failed_stream(translator, "context_length_exceeded", str(exc))
    except (ToolLoopBreakError, ToolCountExceededError) as exc:
        await _close()
        return _openai_error(400, str(exc), etype="invalid_request_error")
    except MidStreamError as exc:
        await _close()
        return _failed_stream(translator, "server_error", str(exc))
    except TimeoutError:
        await _close()
        if not scope.expired():
            raise
        return _openai_error(504, f"no upstream event within {timeout_s:.0f}s", etype="server_error")
    except asyncio.CancelledError:
        await _close()
        if watcher.fired:
            watcher.uncancel()
            return Response(status_code=499)
        raise
    except BaseException:
        await _close()
        raise
    finally:
        await watcher.stop()

    async def gen() -> AsyncIterator[str]:
        try:
            async with asyncio.timeout_at(deadline):
                for ev in translator.start():
                    yield format_sse(ev)
                if first is not None:
                    for out in translator.feed(first):
                        yield format_sse(out)
                async for ev in source:
                    for out in translator.feed(ev):
                        yield format_sse(out)
            for out in translator.finish():
                yield format_sse(out)
        except TimeoutError:
            logger.warning("responses-stream-timeout", extra={"timeout_s": timeout_s})
            for out in translator.failed("server_error", f"stream exceeded {timeout_s:.0f}s ceiling"):
                yield format_sse(out)
        except (MidStreamError, NoProvidersAvailableError) as exc:
            logger.warning("responses-stream-failed", extra={"error": str(exc)[:300]})
            for out in translator.failed("server_error", str(exc)):
                yield format_sse(out)
        finally:
            await _close()

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})
