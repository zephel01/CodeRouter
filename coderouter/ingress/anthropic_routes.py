"""Anthropic-compatible route: POST /v1/messages.

Accepts Anthropic Messages API requests, translates to the internal
ChatRequest/ChatResponse format, routes through FallbackEngine, and
translates back. SSE streaming events follow the Anthropic wire protocol
(message_start / content_block_* / message_delta / message_stop).

Profile selection mirrors the OpenAI route (see openai_routes.py):
    Body field `profile` > `X-CodeRouter-Profile` header > config default.

`anthropic-version` header is accepted but not enforced — Claude Code and
SDKs send values like "2023-06-01"; we log it for diagnostics only.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import StreamingResponse

from coderouter.logging import get_logger
from coderouter.routing import (
    FallbackEngine,
    MidStreamError,
    NoProvidersAvailableError,
)
from coderouter.translation import (
    AnthropicRequest,
    AnthropicStreamEvent,
    stream_chat_to_anthropic_events,
    synthesize_anthropic_stream_from_response,
    to_anthropic_response,
    to_chat_request,
)

router = APIRouter()
logger = get_logger(__name__)

_PROFILE_HEADER = "x-coderouter-profile"
_ANTHROPIC_VERSION_HEADER = "anthropic-version"


@router.post("/messages")
async def messages(  # noqa: ANN201
    payload: dict,
    request: Request,
    x_coderouter_profile: str | None = Header(default=None, alias=_PROFILE_HEADER),
    anthropic_version: str | None = Header(
        default=None, alias=_ANTHROPIC_VERSION_HEADER
    ),
):
    engine: FallbackEngine = request.app.state.engine
    config = request.app.state.config

    if anthropic_version:
        # Don't enforce — just trace. Future: match against a known list.
        logger.debug(
            "anthropic-version-header",
            extra={"value": anthropic_version},
        )

    try:
        anth_req = AnthropicRequest.model_validate(payload)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # Profile selection — body field wins over header (same policy as OpenAI route).
    if anth_req.profile is None and x_coderouter_profile:
        anth_req.profile = x_coderouter_profile

    if anth_req.profile is not None:
        try:
            config.profile_by_name(anth_req.profile)
        except KeyError as exc:
            available = [p.name for p in config.profiles]
            raise HTTPException(
                status_code=400,
                detail=(
                    f"unknown profile {anth_req.profile!r}. "
                    f"available: {available}"
                ),
            ) from exc

    chat_req = to_chat_request(anth_req)

    # v0.3 tool-call repair: pass the request's declared tool names so the
    # converter can extract tool calls the model wrote as plain text.
    tool_names = (
        [t.name for t in anth_req.tools] if anth_req.tools else None
    )

    if anth_req.stream:
        chat_req.stream = True
        # v0.3-D: when tools are declared, models like qwen2.5-coder:14b
        # sometimes emit the tool call as prose (balanced-brace JSON inside
        # text_deltas). We cannot repair that mid-stream — the partial JSON
        # hasn't been closed yet when each chunk arrives. So we downgrade
        # the request to non-streaming internally, run repair on the full
        # response, then replay it as a compliant SSE event sequence. The
        # client still sees streaming; it just arrives in one burst.
        if anth_req.tools:
            return StreamingResponse(
                _anthropic_downgraded_tool_iterator(
                    engine, chat_req, tool_names
                ),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        return StreamingResponse(
            _anthropic_sse_iterator(engine, chat_req),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    chat_req.stream = False
    try:
        chat_resp = await engine.generate(chat_req)
    except NoProvidersAvailableError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    anth_resp = to_anthropic_response(chat_resp, allowed_tool_names=tool_names)
    return anth_resp.model_dump(exclude_none=True)


async def _anthropic_sse_iterator(
    engine: FallbackEngine, chat_req
):
    """Wrap the engine's stream into Anthropic SSE wire format.

    Each emitted line is `event: <type>\\ndata: <json>\\n\\n` per the
    Anthropic spec (distinct from OpenAI's `data:`-only format).
    """
    try:
        events = stream_chat_to_anthropic_events(engine.stream(chat_req))
        async for ev in events:
            yield _format_anthropic_sse(ev)
    except NoProvidersAvailableError as exc:
        # No provider produced even the first chunk — surface as overloaded.
        err_event = AnthropicStreamEvent(
            type="error",
            data={
                "type": "error",
                "error": {
                    "type": "overloaded_error",
                    "message": str(exc),
                },
            },
        )
        yield _format_anthropic_sse(err_event)
    except MidStreamError as exc:
        # A provider failed AFTER emitting at least one chunk. We cannot
        # fall back (client already received partial content), so the only
        # honest thing to do is close the stream with an explicit error
        # event. Use `api_error` so clients distinguish this from
        # "no provider could start" (overloaded_error).
        logger.warning(
            "sse-midstream-error",
            extra={"provider": exc.provider, "original": str(exc.original)},
        )
        err_event = AnthropicStreamEvent(
            type="error",
            data={
                "type": "error",
                "error": {
                    "type": "api_error",
                    "message": str(exc),
                },
            },
        )
        yield _format_anthropic_sse(err_event)


async def _anthropic_downgraded_tool_iterator(
    engine: FallbackEngine,
    chat_req,
    tool_names: list[str] | None,
):
    """v0.3-D: tool-turn downgrade path.

    Resolve the request as non-streaming (so we see the full assistant
    output), run tool-call repair, and replay the final response as a
    spec-compliant Anthropic SSE event sequence. Errors are surfaced as
    `event: error` in the stream (matching the other streaming paths —
    we never switch an in-flight HTTP response to a 5xx).
    """
    chat_req.stream = False
    try:
        chat_resp = await engine.generate(chat_req)
    except NoProvidersAvailableError as exc:
        err_event = AnthropicStreamEvent(
            type="error",
            data={
                "type": "error",
                "error": {
                    "type": "overloaded_error",
                    "message": str(exc),
                },
            },
        )
        yield _format_anthropic_sse(err_event)
        return

    anth_resp = to_anthropic_response(chat_resp, allowed_tool_names=tool_names)
    async for ev in synthesize_anthropic_stream_from_response(anth_resp):
        yield _format_anthropic_sse(ev)


def _format_anthropic_sse(ev: AnthropicStreamEvent) -> str:
    payload = json.dumps(ev.data, ensure_ascii=False)
    return f"event: {ev.type}\ndata: {payload}\n\n"
