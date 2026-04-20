"""Anthropic-compatible route: POST /v1/messages.

Accepts Anthropic Messages API requests and routes them through the
engine's Anthropic-shaped entry points (`generate_anthropic` /
`stream_anthropic`). For `kind: "anthropic"` providers the engine does
direct passthrough; for `kind: "openai_compat"` providers it handles
translation, tool-call repair, and the v0.3-D tool-turn downgrade.

SSE streaming events follow the Anthropic wire protocol
(`message_start` / `content_block_*` / `message_delta` / `message_stop`).

Profile selection mirrors the OpenAI route (see openai_routes.py):
    Body field `profile` > `X-CodeRouter-Profile` header >
    `X-CodeRouter-Mode` header (v0.6-D, via mode_aliases) >
    config default.

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
)

router = APIRouter()
logger = get_logger(__name__)

_PROFILE_HEADER = "x-coderouter-profile"
_MODE_HEADER = "x-coderouter-mode"
_ANTHROPIC_VERSION_HEADER = "anthropic-version"
_ANTHROPIC_BETA_HEADER = "anthropic-beta"


@router.post("/messages")
async def messages(
    payload: dict,
    request: Request,
    x_coderouter_profile: str | None = Header(default=None, alias=_PROFILE_HEADER),
    x_coderouter_mode: str | None = Header(default=None, alias=_MODE_HEADER),
    anthropic_version: str | None = Header(
        default=None, alias=_ANTHROPIC_VERSION_HEADER
    ),
    anthropic_beta: str | None = Header(
        default=None, alias=_ANTHROPIC_BETA_HEADER
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

    # v0.4-D: forward the `anthropic-beta` header through to the native
    # adapter. Without this, any body field gated behind a beta header
    # (`context_management`, newer cache_control/thinking variants, etc.)
    # is rejected by api.anthropic.com with 400 "Extra inputs are not
    # permitted". We stash it on the request model with exclude=True so
    # the adapter can reach it without leaking into the wire body.
    if anthropic_beta:
        anth_req.anthropic_beta = anthropic_beta

    # Profile selection — body field wins over header (same policy as OpenAI route).
    if anth_req.profile is None and x_coderouter_profile:
        anth_req.profile = x_coderouter_profile

    # v0.6-D: X-CodeRouter-Mode → mode_aliases → profile. Mode sits below
    # Profile because Mode is intent / Profile is the implementation.
    if anth_req.profile is None and x_coderouter_mode:
        try:
            anth_req.profile = config.resolve_mode(x_coderouter_mode)
        except KeyError as exc:
            available = sorted(config.mode_aliases.keys())
            raise HTTPException(
                status_code=400,
                detail=(
                    f"unknown mode {x_coderouter_mode!r}. "
                    f"available modes: {available}"
                ),
            ) from exc
        logger.info(
            "mode-alias-resolved",
            extra={"mode": x_coderouter_mode, "profile": anth_req.profile},
        )

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

    if anth_req.stream:
        return StreamingResponse(
            _anthropic_sse_iterator(engine, anth_req),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    try:
        anth_resp = await engine.generate_anthropic(anth_req)
    except NoProvidersAvailableError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return anth_resp.model_dump(exclude_none=True)


async def _anthropic_sse_iterator(
    engine: FallbackEngine, anth_req: AnthropicRequest
):
    """Serialize engine.stream_anthropic() onto the Anthropic SSE wire.

    Each emitted block is `event: <type>\\ndata: <json>\\n\\n` per the
    Anthropic spec (distinct from OpenAI's `data:`-only format).
    Errors map to in-stream `event: error` events — we never switch an
    in-flight HTTP response to a 5xx once headers have shipped.
    """
    try:
        async for ev in engine.stream_anthropic(anth_req):
            yield _format_anthropic_sse(ev)
    except NoProvidersAvailableError as exc:
        # No provider produced even the first event — surface as overloaded.
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
        # v0.3-B: a provider failed AFTER emitting at least one event. We
        # cannot fall back (client already received partial content), so
        # close the stream with an explicit error event. `api_error`
        # distinguishes this from "no provider could start" (overloaded).
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


def _format_anthropic_sse(ev: AnthropicStreamEvent) -> str:
    payload = json.dumps(ev.data, ensure_ascii=False)
    return f"event: {ev.type}\ndata: {payload}\n\n"
