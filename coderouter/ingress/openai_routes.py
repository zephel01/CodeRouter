"""OpenAI-compatible routes: POST /v1/chat/completions (+ minimal /v1/models).

Profile selection precedence (first hit wins):
    1. JSON body field:  {"profile": "fast", ...}
    2. HTTP header:       X-CodeRouter-Profile: fast
    3. HTTP header:       X-CodeRouter-Mode: coding  (v0.6-D, via mode_aliases)
    4. auto_router       (v1.6-A, fires only when default_profile == "auto")
    5. config.default_profile

Body wins over header so that a caller who can embed the field has final say
(useful when a single client talks to multiple routers behind a proxy that
rewrites headers). Mode sits below Profile because Mode is an INTENT
(``coding`` / ``long`` / ``fast``) and Profile is the concrete
implementation — when a caller specifies the concrete profile, respect it.

The auto router slot is intentionally narrow: it only fires when the operator
opts in via ``default_profile: auto`` (the reserved sentinel). For every other
configuration the chain behaves exactly as in v0.6-D — unresolved requests fall
through to the engine, which applies ``config.default_profile``.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import StreamingResponse

from coderouter.adapters.base import ChatRequest
from coderouter.logging import get_logger
from coderouter.routing import FallbackEngine, NoProvidersAvailableError
from coderouter.routing.auto_router import RESERVED_PROFILE_NAME, classify

router = APIRouter()
logger = get_logger(__name__)

_PROFILE_HEADER = "x-coderouter-profile"
_MODE_HEADER = "x-coderouter-mode"


@router.get("/models")
async def list_models(request: Request) -> dict[str, object]:
    """Minimal /v1/models so OpenAI SDKs that probe it don't choke."""
    config = request.app.state.config
    return {
        "object": "list",
        "data": [
            {
                "id": p.name,
                "object": "model",
                "created": int(time.time()),
                "owned_by": "coderouter",
            }
            for p in config.providers
        ],
    }


@router.post("/chat/completions", response_model=None)
async def chat_completions(
    payload: dict[str, Any],
    request: Request,
    x_coderouter_profile: str | None = Header(default=None, alias=_PROFILE_HEADER),
    x_coderouter_mode: str | None = Header(default=None, alias=_MODE_HEADER),
) -> StreamingResponse | dict[str, Any]:
    """OpenAI Chat Completions endpoint.

    Validates the body into :class:`ChatRequest`, resolves the profile
    per the precedence described in the module docstring, and dispatches
    to the engine. Streaming requests return a :class:`StreamingResponse`
    that serializes chunks onto the OpenAI SSE wire (``data: {json}`` +
    trailing ``data: [DONE]``); non-streaming requests return the JSON
    response body.
    """
    engine: FallbackEngine = request.app.state.engine
    config = request.app.state.config

    # Accept extension fields (e.g. "profile") without rejecting
    try:
        chat_req = ChatRequest.model_validate(payload)
    except Exception as exc:  # pydantic.ValidationError, etc.
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # Header-based override (body wins if both are set — see module docstring)
    if chat_req.profile is None and x_coderouter_profile:
        chat_req.profile = x_coderouter_profile

    # v0.6-D: ``X-CodeRouter-Mode`` → mode_aliases → profile. Only kicks
    # in when neither body nor X-CodeRouter-Profile already nailed down
    # the profile (profile > mode precedence).
    if chat_req.profile is None and x_coderouter_mode:
        try:
            chat_req.profile = config.resolve_mode(x_coderouter_mode)
        except KeyError as exc:
            available = sorted(config.mode_aliases.keys())
            raise HTTPException(
                status_code=400,
                detail=(f"unknown mode {x_coderouter_mode!r}. available modes: {available}"),
            ) from exc
        logger.info(
            "mode-alias-resolved",
            extra={"mode": x_coderouter_mode, "profile": chat_req.profile},
        )

    # v1.6-A: auto router slot. Only fires when the operator opted in by
    # setting ``default_profile: auto`` and no higher-priority caller signal
    # (body / profile header / mode header) already nailed down a profile.
    # When inactive, the engine still falls through to
    # ``config.default_profile`` on its own — same semantics as pre-v1.6.
    if chat_req.profile is None and config.default_profile == RESERVED_PROFILE_NAME:
        chat_req.profile = classify(payload, config)

    # Validate profile exists before we kick off any upstream call
    if chat_req.profile is not None:
        try:
            config.profile_by_name(chat_req.profile)
        except KeyError as exc:
            available = [p.name for p in config.profiles]
            raise HTTPException(
                status_code=400,
                detail=(f"unknown profile {chat_req.profile!r}. available: {available}"),
            ) from exc

    if chat_req.stream:
        return StreamingResponse(
            _sse_iterator(engine, chat_req),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    try:
        response = await engine.generate(chat_req)
    except NoProvidersAvailableError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return response.model_dump(exclude_none=True)


async def _sse_iterator(engine: FallbackEngine, chat_req: ChatRequest) -> AsyncIterator[str]:
    """Wrap the engine's stream into SSE wire format."""
    try:
        async for chunk in engine.stream(chat_req):
            data = chunk.model_dump(exclude_none=True)
            yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"
    except NoProvidersAvailableError as exc:
        # Encode the error inside the SSE channel — OpenAI clients handle this
        err = {"error": {"message": str(exc), "type": "no_providers_available"}}
        yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"
