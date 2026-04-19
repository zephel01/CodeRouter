"""OpenAI-compatible routes: POST /v1/chat/completions (+ minimal /v1/models).

Profile selection precedence (first hit wins):
    1. JSON body field:  {"profile": "fast", ...}
    2. HTTP header:       X-CodeRouter-Profile: fast
    3. config.default_profile

Body wins over header so that a caller who can embed the field has final say
(useful when a single client talks to multiple routers behind a proxy that
rewrites headers).
"""

from __future__ import annotations

import json
import time

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import StreamingResponse

from coderouter.adapters.base import ChatRequest
from coderouter.routing import FallbackEngine, NoProvidersAvailableError

router = APIRouter()

_PROFILE_HEADER = "x-coderouter-profile"


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


@router.post("/chat/completions")
async def chat_completions(  # noqa: ANN201
    payload: dict,
    request: Request,
    x_coderouter_profile: str | None = Header(default=None, alias=_PROFILE_HEADER),
):
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

    # Validate profile exists before we kick off any upstream call
    if chat_req.profile is not None:
        try:
            config.profile_by_name(chat_req.profile)
        except KeyError as exc:
            available = [p.name for p in config.profiles]
            raise HTTPException(
                status_code=400,
                detail=(
                    f"unknown profile {chat_req.profile!r}. "
                    f"available: {available}"
                ),
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


async def _sse_iterator(engine: FallbackEngine, chat_req: ChatRequest):
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
