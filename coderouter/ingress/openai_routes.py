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

import asyncio
import contextlib
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

# M14: overall SSE stream ceiling — see anthropic_routes for rationale.
_STREAM_TIMEOUT_MULTIPLIER = 20.0
_STREAM_TIMEOUT_DEFAULT_S = 900.0
_STREAM_TIMEOUT_MIN_S = 60.0

# v2.x: peek-timeout for the first streamed chunk — see anthropic_routes for
# rationale. Per-call provider timeout flavor (no stream multiplier).
_FIRST_EVENT_TIMEOUT_DEFAULT_S = 60.0
_FIRST_EVENT_TIMEOUT_MIN_S = 10.0

# v2.x: peek/continuation handshake sentinels for ``_sse_iterator``. ``_UNSET``
# means "no peeked chunk supplied, create + drive the source yourself" (legacy
# call convention used by unit tests); ``_EMPTY_STREAM`` means "the peek saw an
# immediately exhausted stream" (legal — just emit the terminal ``[DONE]``).
_UNSET: Any = object()
_EMPTY_STREAM: Any = object()


async def _aclose_quiet(source: Any) -> None:
    """v2.x: best-effort close an async generator, swallowing any error.

    Finalizes the engine ``stream`` generator when the pre-stream peek fails
    and we raise an :class:`HTTPException` instead of committing a streaming
    response, so the adapter's ``httpx`` streaming context releases the
    upstream connection.
    """
    aclose = getattr(source, "aclose", None)
    if aclose is not None:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await aclose()


async def _anext_or_empty(source: AsyncIterator[Any]) -> Any:
    """v2.x: pull the first chunk, mapping an empty stream to ``_EMPTY_STREAM``.

    Keeps ``StopAsyncIteration`` from crossing the :func:`asyncio.wait_for`
    task boundary. Any other exception (``NoProvidersAvailableError``, ...)
    propagates unchanged for the caller to map to an HTTP status.
    """
    try:
        return await source.__anext__()
    except StopAsyncIteration:
        return _EMPTY_STREAM


def _resolve_first_event_timeout_s(
    engine: FallbackEngine, profile: str | None
) -> float:
    """v2.x: derive the pre-stream peek timeout (seconds) for a profile.

    Mirrors :func:`_resolve_stream_timeout_s`'s config access but WITHOUT the
    stream multiplier — bounds only the wait for the first streamed chunk (the
    engine emits it after any failover completes). Floored at
    ``_FIRST_EVENT_TIMEOUT_MIN_S``; any resolution failure falls back to
    ``_FIRST_EVENT_TIMEOUT_DEFAULT_S``. Does not change the config schema.
    """
    per_call: float | None = None
    try:
        config = engine.config
        chosen = profile or config.default_profile
        chain_cfg = config.profile_by_name(chosen)
        per_call = getattr(chain_cfg, "timeout_s", None)
        if per_call is None:
            for pname in getattr(chain_cfg, "providers", []) or []:
                pconf = next(
                    (p for p in config.providers if p.name == pname), None
                )
                if pconf is not None:
                    per_call = getattr(pconf, "timeout_s", None)
                    break
    except (AttributeError, KeyError, ValueError):
        per_call = None

    if per_call is None:
        return _FIRST_EVENT_TIMEOUT_DEFAULT_S
    return max(_FIRST_EVENT_TIMEOUT_MIN_S, float(per_call))


def _resolve_stream_timeout_s(engine: FallbackEngine, profile: str | None) -> float:
    """M14: derive the overall stream ceiling (seconds) for a profile.

    Mirrors the Anthropic route. Uses the profile's per-call ``timeout_s``
    (or the first provider's, or the default) scaled up. Never below
    ``_STREAM_TIMEOUT_MIN_S``; any resolution failure falls back to
    ``_STREAM_TIMEOUT_DEFAULT_S``. Does not change the config schema.
    """
    per_call: float | None = None
    try:
        config = engine.config
        chosen = profile or config.default_profile
        chain_cfg = config.profile_by_name(chosen)
        per_call = getattr(chain_cfg, "timeout_s", None)
        if per_call is None:
            for pname in getattr(chain_cfg, "providers", []) or []:
                pconf = next(
                    (p for p in config.providers if p.name == pname), None
                )
                if pconf is not None:
                    per_call = getattr(pconf, "timeout_s", None)
                    break
    except (AttributeError, KeyError, ValueError):
        per_call = None

    if per_call is None:
        return _STREAM_TIMEOUT_DEFAULT_S
    return max(_STREAM_TIMEOUT_MIN_S, float(per_call) * _STREAM_TIMEOUT_MULTIPLIER)


# --- /v1/models upstream passthrough -------------------------------------
#
# Providers with an *empty* ``model`` field are passthrough providers: the
# upstream (llama-server, LM Studio, ...) decides which model is loaded and
# CodeRouter never sends a model name. For those providers the historic
# behaviour — returning the provider *name* — makes external benchmarks
# indistinguishable across loaded GGUFs (the launcher_gui setup always
# reported ``llama-cpp-local`` regardless of the selected model). We now ask
# the upstream's own ``/models`` endpoint for its real model id(s) and
# surface those instead.
#
# Guards: only for ``kind == "openai_compat"`` with ``model == ""``; a short
# per-upstream TTL cache keeps repeated SDK probes cheap; ANY failure (refused
# connection, timeout, unexpected shape) falls back to the historic
# provider-name entry, so this is strictly additive. Providers with a
# configured model keep the exact previous behaviour.

_UPSTREAM_MODELS_TTL_S = 30.0
_UPSTREAM_MODELS_TIMEOUT_S = 2.0
# base_url -> (expires_monotonic, ids)
_upstream_models_cache: dict[str, tuple[float, list[str]]] = {}


async def _fetch_upstream_model_ids(base_url: str) -> list[str]:
    """GET {base_url}/models and return the upstream model ids.

    Raises on any transport / shape problem — the caller treats every
    exception as "fall back to the provider-name entry".
    """
    import httpx  # local import keeps module import time flat

    url = base_url.rstrip("/") + "/models"
    async with httpx.AsyncClient(timeout=_UPSTREAM_MODELS_TIMEOUT_S) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        payload = resp.json()
    ids = [
        str(item["id"])
        for item in payload.get("data", [])
        if isinstance(item, dict) and item.get("id")
    ]
    if not ids:
        raise ValueError(f"upstream {url} returned no model ids")
    return ids


async def _upstream_model_ids_cached(base_url: str) -> list[str]:
    """TTL-cached wrapper around :func:`_fetch_upstream_model_ids`."""
    now = time.monotonic()
    hit = _upstream_models_cache.get(base_url)
    if hit is not None and hit[0] > now:
        return hit[1]
    ids = await _fetch_upstream_model_ids(base_url)
    _upstream_models_cache[base_url] = (now + _UPSTREAM_MODELS_TTL_S, ids)
    return ids


@router.get("/models")
async def list_models(request: Request) -> dict[str, object]:
    """/v1/models: provider names, with upstream passthrough for empty-model
    providers (see the passthrough note above)."""
    config = request.app.state.config
    created = int(time.time())
    data: list[dict[str, object]] = []
    for p in config.providers:
        if p.model == "" and p.kind == "openai_compat":
            try:
                ids = await _upstream_model_ids_cached(str(p.base_url))
            except Exception:  # any failure means "no passthrough"
                logger.debug(
                    "models passthrough failed; falling back to provider name",
                    extra={"provider": p.name},
                )
            else:
                data.extend(
                    {
                        "id": model_id,
                        "object": "model",
                        "created": created,
                        "owned_by": f"coderouter/{p.name}",
                    }
                    for model_id in ids
                )
                continue
        data.append(
            {
                "id": p.name,
                "object": "model",
                "created": created,
                "owned_by": "coderouter",
            }
        )
    return {"object": "list", "data": data}


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
        # v2.x: fail-fast peek. The engine finishes its failover before the
        # first stream chunk, so a total pre-stream failure raises
        # NoProvidersAvailableError here — before the StreamingResponse commits
        # HTTP 200 — and can surface as a real 502 instead of a 200 carrying an
        # in-band error frame. (No tool-loop / tool-count guards on this path.)
        source = engine.stream(chat_req)
        peek_timeout_s = _resolve_first_event_timeout_s(engine, chat_req.profile)
        try:
            first_chunk = await asyncio.wait_for(
                _anext_or_empty(source), timeout=peek_timeout_s
            )
        except NoProvidersAvailableError as exc:
            await _aclose_quiet(source)
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except TimeoutError as exc:
            # asyncio.wait_for raises asyncio.TimeoutError (== builtin
            # TimeoutError on 3.11+) when the peek budget elapses.
            await _aclose_quiet(source)
            raise HTTPException(
                status_code=504,
                detail=(
                    f"no stream chunk within {peek_timeout_s:.0f}s "
                    "(all providers timed out before the first token)"
                ),
            ) from exc

        # M14: overall stream timeout + client-disconnect cleanup. The peeked
        # first chunk is replayed by the iterator before it delegates to the
        # live source.
        timeout_s = _resolve_stream_timeout_s(engine, chat_req.profile)
        return StreamingResponse(
            _sse_iterator(
                engine,
                chat_req,
                source=source,
                first_chunk=first_chunk,
                timeout_s=timeout_s,
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    try:
        response = await engine.generate(chat_req)
    except NoProvidersAvailableError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return response.model_dump(exclude_none=True)


async def _sse_iterator(
    engine: FallbackEngine,
    chat_req: ChatRequest,
    *,
    source: AsyncIterator[Any] | None = None,
    first_chunk: Any = _UNSET,
    timeout_s: float = _STREAM_TIMEOUT_DEFAULT_S,
) -> AsyncIterator[str]:
    """Wrap the engine's stream into SSE wire format.

    M14: bounded by an overall ``timeout_s`` ceiling and guarantees the
    upstream engine generator is finalized on client disconnect
    (``CancelledError``) or timeout, so the upstream connection is
    released instead of leaking.

    v2.x: when ``source`` / ``first_chunk`` are supplied by the fail-fast peek
    in :func:`chat_completions`, this iterator replays the already-pulled first
    chunk then continues the *same* live generator. In that mode the
    ``NoProvidersAvailableError`` branch below is defensive dead code (a total
    failure raises during the peek, which maps it to a real 502). It is
    retained for the legacy no-peek call convention (``source is None``) used
    by unit tests, where this iterator drives the generator itself.
    """
    if source is None:
        source = engine.stream(chat_req)
    try:
        async with asyncio.timeout(timeout_s):
            if first_chunk is not _UNSET and first_chunk is not _EMPTY_STREAM:
                data = first_chunk.model_dump(exclude_none=True)
                yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
            async for chunk in source:
                data = chunk.model_dump(exclude_none=True)
                yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
    except NoProvidersAvailableError as exc:
        # Encode the error inside the SSE channel — OpenAI clients handle this
        err = {"error": {"message": str(exc), "type": "no_providers_available"}}
        yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"
    except TimeoutError:
        # M14: overall ceiling hit — surface a terminal error frame.
        logger.warning("sse-stream-timeout", extra={"timeout_s": timeout_s})
        err = {"error": {"message": f"stream exceeded {timeout_s:.0f}s ceiling", "type": "timeout"}}
        yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"
    except asyncio.CancelledError:
        # M14: client disconnected — re-raise after finalizing the source.
        logger.info("sse-client-disconnect")
        raise
    finally:
        # M14: ensure the engine generator's finally blocks run so the
        # adapter's httpx streaming context releases the upstream socket.
        aclose = getattr(source, "aclose", None)
        if aclose is not None:
            # Best-effort cleanup; never mask the original exit reason.
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await aclose()
