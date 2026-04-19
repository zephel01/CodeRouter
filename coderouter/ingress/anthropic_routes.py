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
from coderouter.routing import FallbackEngine, NoProvidersAvailableError
from coderouter.translation import (
    AnthropicRequest,
    AnthropicStreamEvent,
    stream_chat_to_anthropic_events,
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

    if anth_req.stream:
        chat_req.stream = True
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

    anth_resp = to_anthropic_response(chat_resp)
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


def _format_anthropic_sse(ev: AnthropicStreamEvent) -> str:
    payload = json.dumps(ev.data, ensure_ascii=False)
    return f"event: {ev.type}\ndata: {payload}\n\n"
