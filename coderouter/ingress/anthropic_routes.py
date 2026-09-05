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
    auto_router (v1.6-A, when ``default_profile: auto``) >
    config default.

`anthropic-version` header is accepted but not enforced — Claude Code and
SDKs send values like "2023-06-01"; we log it for diagnostics only.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from coderouter.adapters.base import StreamTruncatedError
from coderouter.guards.tool_loop import ToolCountExceededError, ToolLoopBreakError
from coderouter.logging import get_logger
from coderouter.routing import (
    FallbackEngine,
    MidStreamError,
    NoProvidersAvailableError,
)
from coderouter.routing.auto_router import RESERVED_PROFILE_NAME, classify
from coderouter.routing.fallback_trace import (
    SSE_FALLBACK_EVENT,
    current_fallback_trace,
)
from coderouter.token_estimation import extract_text_from_anthropic_request
from coderouter.token_estimation_accurate import count_tokens, is_accuracy_available
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
_CTX_BUDGET_HEADER = "X-CodeRouter-Context-Budget"
_DRIFT_HEADER = "X-CodeRouter-Drift"
# v2.15.0: the ``X-CodeRouter-Fallback-*`` family is built by
# ``FallbackTrace.header_values()`` rather than named here, because the set
# of headers varies with the trace (``-To`` is absent when the whole chain
# was exhausted). The names themselves live next to the trace so the
# engine, the ingress and the tests all read one definition.

# M14: overall SSE stream ceiling. A single streamed request can legitimately
# run much longer than one provider call (long generations, tool turns), so we
# derive the ceiling from the profile's per-call ``timeout_s`` scaled up, and
# fall back to a large default when no profile timeout is configured. This is a
# safety net against a wedged upstream holding a client (and the upstream
# socket) open forever — NOT a tight per-token deadline.
_STREAM_TIMEOUT_MULTIPLIER = 20.0
_STREAM_TIMEOUT_DEFAULT_S = 900.0
_STREAM_TIMEOUT_MIN_S = 60.0


def _resolve_stream_timeout_s(engine: FallbackEngine, profile: str | None) -> float:
    """M14: derive the overall stream ceiling (seconds) for a profile.

    Uses the profile's per-call ``timeout_s`` (or the first provider's, or
    the default) scaled by ``_STREAM_TIMEOUT_MULTIPLIER`` so a whole stream
    gets a generous but bounded budget. Never returns below
    ``_STREAM_TIMEOUT_MIN_S``. Any resolution failure (stub config in tests,
    missing profile) falls back to ``_STREAM_TIMEOUT_DEFAULT_S`` — the guard
    stays active rather than disabling itself. Does NOT change the config
    schema.
    """
    per_call: float | None = None
    try:
        config = engine.config
        chosen = profile or config.default_profile
        chain_cfg = config.profile_by_name(chosen)
        per_call = getattr(chain_cfg, "timeout_s", None)
        if per_call is None:
            # Fall back to the first provider's timeout.
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


async def _guard_stream(
    source: AsyncIterator[str],
    *,
    timeout_s: float,
    label: str,
) -> AsyncIterator[str]:
    """M14: enforce an overall timeout + clean cancellation on an SSE stream.

    Wraps ``source`` (an iterator of already-formatted SSE frames) so that:

    * The total wall-clock time is bounded by ``timeout_s`` via
      ``asyncio.timeout`` — a wedged upstream can no longer hold the client
      (and the upstream socket) open indefinitely.
    * On client disconnect (the ASGI server throws ``CancelledError`` into
      the generator) or on timeout, the underlying ``source`` generator is
      explicitly closed via ``aclose()``. Closing the engine generator runs
      its ``finally`` blocks, which lets the adapter's ``httpx`` streaming
      context manager exit and release the upstream connection instead of
      leaking it until GC.

    Errors raised by ``source`` itself (in-stream ``event: error`` framing)
    are produced by the caller's iterator before it reaches here, so this
    wrapper only has to deal with timeout / cancellation / normal completion.
    """
    try:
        async with asyncio.timeout(timeout_s):
            async for frame in source:
                yield frame
    except TimeoutError:
        # Overall ceiling hit. Emit a terminal error frame so a
        # spec-compliant client sees a clean stream end rather than a
        # silently truncated body, then stop.
        logger.warning("sse-stream-timeout", extra={"label": label, "timeout_s": timeout_s})
        err_event = AnthropicStreamEvent(
            type="error",
            data={
                "type": "error",
                "error": {
                    "type": "timeout_error",
                    "message": f"stream exceeded {timeout_s:.0f}s ceiling",
                },
            },
        )
        yield _format_anthropic_sse(err_event)
    except asyncio.CancelledError:
        # Client disconnected. Re-raise after ensuring the source is closed
        # in the finally block so the upstream connection is released.
        logger.info("sse-client-disconnect", extra={"label": label})
        raise
    finally:
        # Guarantee the upstream generator is finalized on every exit path
        # (normal completion, timeout, or cancellation).
        aclose = getattr(source, "aclose", None)
        if aclose is not None:
            # Best-effort cleanup; never mask the original exit reason.
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await aclose()


@router.post("/messages", response_model=None)
async def messages(
    payload: dict[str, Any],
    request: Request,
    x_coderouter_profile: str | None = Header(default=None, alias=_PROFILE_HEADER),
    x_coderouter_mode: str | None = Header(default=None, alias=_MODE_HEADER),
    anthropic_version: str | None = Header(default=None, alias=_ANTHROPIC_VERSION_HEADER),
    anthropic_beta: str | None = Header(default=None, alias=_ANTHROPIC_BETA_HEADER),
) -> StreamingResponse | dict[str, Any]:
    """Anthropic Messages API endpoint.

    Validates the body into :class:`AnthropicRequest`, resolves the
    profile (body > profile header > mode header > config default),
    then dispatches to the engine's Anthropic-shaped entry points. For
    streaming requests, returns a :class:`StreamingResponse` that
    serializes engine events onto the Anthropic SSE wire; otherwise
    returns the JSON response body.
    """
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

    # Translation layer: Request JA→EN (design §7.1, before profile resolution)
    # Must not translate system/tool_use/tool_result; only user text with is_japanese
    # A-1 fix: outer fail-open now logs at debug so programming errors (AttributeError etc.)
    # are not silently swallowed; inner warning remains for translation-specific failures.
    try:
        tcfg = getattr(config, "translation", None)
        if tcfg is not None and getattr(tcfg, "enabled", False):
            manager = getattr(request.app.state, "translator_manager", None)
            if manager is not None and getattr(manager, "is_available", lambda: False)():
                try:
                    from coderouter.jp_translation.translator import (
                        translate_anthropic_request_ja_to_en,
                    )

                    # Sync API → to_thread + 5s timeout (design §3.2.3)
                    anth_req = await asyncio.wait_for(
                        asyncio.to_thread(
                            translate_anthropic_request_ja_to_en, anth_req, manager
                        ),
                        timeout=5.0,
                    )
                    if getattr(tcfg, "log_translations", False):
                        logger.info(
                            "translation-ja-en-applied",
                            extra={"messages": len(anth_req.messages)},
                        )
                except Exception as exc:
                    logger.warning(
                        "translation-ja-en-failed",
                        extra={"error": str(exc)},
                    )
    except Exception as exc:
        # Translation must never break request path (design §3.5) — debug so silent swallowing is observable.
        logger.debug("translation-ja-en-skip", extra={"error": str(exc)})

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
                detail=(f"unknown mode {x_coderouter_mode!r}. available modes: {available}"),
            ) from exc
        logger.info(
            "mode-alias-resolved",
            extra={"mode": x_coderouter_mode, "profile": anth_req.profile},
        )

    # Resolve profile from request model field if profile is not explicitly specified
    if anth_req.profile is None and anth_req.model:
        resolved = config.resolve_model_to_profile(anth_req.model)
        if resolved:
            anth_req.profile = resolved
            logger.info(
                "model-resolved-to-profile",
                extra={"model": anth_req.model, "profile": anth_req.profile},
            )

    # v1.6-A: auto router slot. Symmetric with the OpenAI route — fires only

    # when ``default_profile: auto`` is set and no explicit profile signal won
    # above. When inactive the engine falls through to ``default_profile`` on
    # its own. ``classify`` inspects the raw ``payload`` dict (not the
    # AnthropicRequest), so both OpenAI and Anthropic ingress use the same
    # classifier without a shared request shim.
    if anth_req.profile is None and config.default_profile == RESERVED_PROFILE_NAME:
        anth_req.profile = classify(payload, config)

    if anth_req.profile is not None:
        try:
            config.profile_by_name(anth_req.profile)
        except KeyError as exc:
            available = [p.name for p in config.profiles]
            raise HTTPException(
                status_code=400,
                detail=(f"unknown profile {anth_req.profile!r}. available: {available}"),
            ) from exc

    # v2.0-F (L1): run context budget guard before dispatch so the
    # response header can be set for both streaming and non-streaming.
    # The engine's internal guard re-check is a cheap no-op.
    anth_req, ctx_budget_status = engine.apply_context_budget(anth_req)

    if anth_req.stream:
        stream_headers: dict[str, str] = {
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        }
        if ctx_budget_status:
            stream_headers[_CTX_BUDGET_HEADER] = ctx_budget_status
        # v2.0-G: drift header is set post-stream via a trailer-like
        # mechanism — for streaming we cannot know the verdict before
        # the first chunk ships. Instead, check pre-existing drift state.
        drift_severity = engine.last_drift_severity
        if drift_severity:
            stream_headers[_DRIFT_HEADER] = drift_severity
        # v2.15.0: fallback headers on the streaming path carry what is
        # already known when the response commits — i.e. the pre-attempt
        # hops (paid gate / budget / backend health / self-healing) that
        # ``apply_context_budget`` recorded while resolving the chain just
        # above. Runtime attempt failures happen *after* the HTTP headers
        # have shipped and are therefore physically unreachable here; they
        # are delivered instead as the trailing ``coderouter_fallback`` SSE
        # metadata event emitted by ``_anthropic_sse_iterator`` (the same
        # trailer mechanism v2.0-H uses for ``coderouter_partial``), and as
        # ``fallback-occurred`` log lines. Same commit-order constraint the
        # drift header comment above describes.
        pre_dispatch_trace = current_fallback_trace()
        if pre_dispatch_trace is not None:
            stream_headers.update(pre_dispatch_trace.header_values())
        # M14: wrap the SSE iterator with an overall timeout + client
        # disconnect cleanup so a wedged upstream cannot pin the client
        # (or the upstream socket) open forever.
        timeout_s = _resolve_stream_timeout_s(engine, anth_req.profile)
        guarded = _guard_stream(
            _anthropic_sse_iterator(engine, anth_req),
            timeout_s=timeout_s,
            label="anthropic",
        )
        return StreamingResponse(
            guarded,
            media_type="text/event-stream",
            headers=stream_headers,
        )

    try:
        anth_resp = await engine.generate_anthropic(anth_req)
    except NoProvidersAvailableError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except ToolLoopBreakError as exc:
        # v1.9-E (L3): the ``break`` action short-circuits the request
        # before any provider is called. Surface as a structured 400 so
        # programmatic clients can branch on ``error == "tool_loop_detected"``
        # and read ``tool_name`` / ``repeat_count`` without regex-parsing
        # the message string. (NoProvidersAvailableError → 502 stays as
        # a plain string because it's a runtime / chain-failure event;
        # break is policy and meant to be machine-readable.)
        raise HTTPException(
            status_code=400,
            detail=_tool_loop_break_detail(exc),
        ) from exc
    except ToolCountExceededError as exc:
        # v2.2: total tool-call count exceeded — surface as a 400.
        raise HTTPException(
            status_code=400,
            detail={
                "error": "tool_count_exceeded",
                "message": str(exc),
                "total_count": exc.exceeded.total_count,
                "max_allowed": exc.exceeded.max_allowed,
                "profile": exc.profile,
            },
        ) from exc

    # v2.0-G: collect drift header after engine dispatch.
    drift_severity = engine.last_drift_severity
    resp_headers: dict[str, str] = {}
    if ctx_budget_status:
        resp_headers[_CTX_BUDGET_HEADER] = ctx_budget_status
    if drift_severity:
        resp_headers[_DRIFT_HEADER] = drift_severity
    # v2.15.0: fallback reason headers. The engine wrote its hops onto the
    # request-scoped trace while ``generate_anthropic`` was awaited above,
    # so by here the trail is complete — ``header_values()`` returns an
    # empty dict (and this whole block is inert) when the first provider
    # served the request, which keeps the healthy path byte-identical to
    # v2.14.0.
    fallback_trace = current_fallback_trace()
    if fallback_trace is not None:
        resp_headers.update(fallback_trace.header_values())

    if resp_headers:
        return JSONResponse(
            content=anth_resp.model_dump(exclude_none=True),
            headers=resp_headers,
        )
    return anth_resp.model_dump(exclude_none=True)


def _resolve_count_tokens_tokenizer_path(
    config: Any, profile: str | None
) -> str | None:
    """S1 (shim): best-effort resolve a local tokenizer.json for counting.

    Routing for ``count_tokens`` is not a full chain resolution — we only
    need *a* representative tokenizer for the profile the request would use.
    We take the first provider of the resolved profile (or the default
    profile) and return its ``tokenizer_path`` when declared. Any resolution
    hiccup (unknown profile, empty chain, stub config in tests) returns
    ``None``, which makes :func:`count_tokens` fall back to the char/4
    heuristic — a graceful degrade rather than an error.
    """
    try:
        chosen = profile or config.default_profile
        chain_cfg = config.profile_by_name(chosen)
        for pname in getattr(chain_cfg, "providers", []) or []:
            pconf = next((p for p in config.providers if p.name == pname), None)
            if pconf is not None:
                tok = getattr(pconf, "tokenizer_path", None)
                if tok:
                    return tok
                # First provider resolved but declares no tokenizer — stop
                # here (don't scan the whole chain for an unrelated one).
                return None
    except (AttributeError, KeyError, ValueError, TypeError):
        return None
    return None


@router.post("/messages/count_tokens", response_model=None)
async def count_tokens_route(
    payload: dict[str, Any],
    request: Request,
    x_coderouter_profile: str | None = Header(default=None, alias=_PROFILE_HEADER),
    x_coderouter_mode: str | None = Header(default=None, alias=_MODE_HEADER),
) -> dict[str, Any]:
    """Anthropic ``POST /v1/messages/count_tokens`` — local token estimate.

    Mirrors Anthropic's count_tokens endpoint: the request body has the
    same shape as ``/v1/messages`` (minus the ``max_tokens`` requirement)
    and the response is ``{"input_tokens": N}``. CodeRouter answers this
    entirely locally — there is no upstream round-trip — using the same
    text-extraction the language-tax / context-budget guards use, feeding
    an accurate local tokenizer when the routed provider declares one and
    the ``accuracy`` extra is installed, otherwise the char/4 heuristic.

    Validation is deliberately looser than :class:`AnthropicRequest` (no
    ``max_tokens`` needed): ``model`` and a non-empty ``messages`` list are
    required, everything else optional. Profile selection follows the same
    body > profile-header > mode-header precedence as ``/messages`` so the
    tokenizer resolves against the provider the real request would hit.
    """
    config = request.app.state.config

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="request body must be a JSON object")

    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        raise HTTPException(
            status_code=400,
            detail="'messages' is required and must be a non-empty list",
        )
    if "model" not in payload:
        raise HTTPException(status_code=400, detail="'model' is required")

    # Profile selection — body field wins over header, mode header last
    # (same policy as /messages). Kept minimal: we only need the profile to
    # pick a representative tokenizer, so an unknown mode/profile degrades
    # to the default rather than 400'ing the count.
    profile = payload.get("profile") or x_coderouter_profile
    if profile is None and x_coderouter_mode:
        try:
            profile = config.resolve_mode(x_coderouter_mode)
        except (KeyError, AttributeError):
            profile = None
    if profile is None and payload.get("model"):
        profile = config.resolve_model_to_profile(payload.get("model"))


    # Combine system + messages (+ tool JSON length) into one text blob and
    # count. tools contribute their JSON length as a coarse proxy for the
    # schema tokens Anthropic would bill.
    text = extract_text_from_anthropic_request(
        system=payload.get("system"),
        messages=messages,
    )
    tools = payload.get("tools")
    if isinstance(tools, list) and tools:
        with contextlib.suppress(TypeError, ValueError):
            text = f"{text}\n{json.dumps(tools, ensure_ascii=False)}"

    tokenizer_path = _resolve_count_tokens_tokenizer_path(config, profile)
    input_tokens = count_tokens(text, tokenizer_path=tokenizer_path)
    # Report the method count_tokens *actually* used: the accurate backend
    # only engages when a path is declared AND the optional ``tokenizers``
    # dependency is importable (see token_estimation_accurate). Otherwise
    # count_tokens transparently falls back to char/4 — so we must too, or
    # the log would misreport a heuristic count as tokenizer-accurate.
    method = "tokenizer" if (tokenizer_path and is_accuracy_available()) else "heuristic"

    logger.info(
        "count-tokens-served",
        extra={"method": method, "input_tokens": input_tokens},
    )
    return {"input_tokens": input_tokens}


async def _anthropic_sse_iterator(
    engine: FallbackEngine, anth_req: AnthropicRequest
) -> AsyncIterator[str]:
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
    except ToolLoopBreakError as exc:
        # v1.9-E (L3) streaming counterpart of the non-streaming 400. The
        # guard runs at the top of stream_anthropic — before any event
        # has been yielded — so this is the "no bytes yet" case in
        # principle. We still emit the error inside the SSE stream
        # (rather than a 400) because StreamingResponse has already
        # committed HTTP 200 + text/event-stream headers by the time
        # we iterate the generator. Mirrors the NoProvidersAvailableError
        # branch above. The error body uses the Anthropic-shaped
        # ``invalid_request_error`` type with a ``tool_loop`` extension
        # block that carries the structured detection fields.
        err_event = AnthropicStreamEvent(
            type="error",
            data={
                "type": "error",
                "error": {
                    "type": "invalid_request_error",
                    "message": str(exc),
                    "tool_loop": _tool_loop_break_extension(exc),
                },
            },
        )
        yield _format_anthropic_sse(err_event)
    except ToolCountExceededError as exc:
        # v2.2: streaming counterpart of the tool-count-exceeded 400.
        err_event = AnthropicStreamEvent(
            type="error",
            data={
                "type": "error",
                "error": {
                    "type": "invalid_request_error",
                    "message": str(exc),
                    "tool_count": {
                        "total_count": exc.exceeded.total_count,
                        "max_allowed": exc.exceeded.max_allowed,
                        "profile": exc.profile,
                    },
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

        # v2.0-H (L6): partial stitch surface mode — synthesize a graceful
        # stream termination that delivers accumulated text to the client.
        profile_name = anth_req.profile or engine.config.default_profile
        partial_action = "off"
        try:
            chain_cfg = engine.config.profile_by_name(profile_name)
            partial_action = chain_cfg.partial_stitch_action
        except (KeyError, ValueError):
            pass

        # v2.15.0 (stream-truncation): name the specific failure on the
        # metadata event. ``mid_stream_failure`` stays the default so every
        # pre-v2.15.0 case is byte-identical; ``stream_truncated`` tells the
        # client the difference between "the provider errored out" and "the
        # provider went quiet with the message still open".
        partial_reason = (
            "stream_truncated"
            if isinstance(exc.original, StreamTruncatedError)
            else "mid_stream_failure"
        )

        if partial_action == "surface" and exc.partial_content:
            # Emit message_delta with accumulated usage (signals stream end).
            yield _format_anthropic_sse(AnthropicStreamEvent(
                type="message_delta",
                data={
                    "type": "message_delta",
                    "delta": {"stop_reason": None, "stop_sequence": None},
                    "usage": {"output_tokens": 0},
                },
            ))
            # Emit message_stop so the client sees a complete stream.
            yield _format_anthropic_sse(AnthropicStreamEvent(
                type="message_stop",
                data={"type": "message_stop"},
            ))
            # Emit coderouter_partial metadata event (client-optional).
            yield _format_anthropic_sse(AnthropicStreamEvent(
                type="coderouter_partial",
                data={
                    "type": "coderouter_partial",
                    "partial_content": exc.partial_content,
                    "provider": exc.provider,
                    "reason": partial_reason,
                    "original_error": str(exc.original)[:200],
                },
            ))
            # v2.15.0: keep the pre-v2.15.0 key set (and order) intact for
            # every failure that is not a truncation. The JSON formatter
            # walks ``record.__dict__`` and emits whatever ``extra`` carries,
            # so an unconditional ``reason`` would change the shape of this
            # log line even under the default ``stream_truncation_action:
            # off`` — where no truncation can be detected in the first place
            # and the release claims byte-for-byte compatibility with
            # v2.14.0. The key is therefore appended only when it says
            # something new.
            _stitch_extra: dict[str, Any] = {
                "provider": exc.provider,
                "profile": profile_name,
                "text_blocks": len(exc.partial_content),
                "text_length": sum(
                    len(b.get("text", "")) for b in exc.partial_content
                ),
            }
            if partial_reason != "mid_stream_failure":
                # Lets a dashboard split "provider errored" from "provider
                # went quiet" without re-parsing the SSE.
                _stitch_extra["reason"] = partial_reason
            logger.info("partial-stitch-surfaced", extra=_stitch_extra)
        else:
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

    # v2.15.0: trailing fallback metadata. Emitted after the stream has
    # terminated (normally or with an in-stream error frame), so it never
    # interleaves with the Anthropic event sequence a client is parsing —
    # exactly the position ``coderouter_partial`` occupies. Client-optional:
    # spec-compliant Anthropic SDKs ignore unknown event types, and the
    # frame is only produced when a fallback actually happened.
    trace = current_fallback_trace()
    if trace is not None and trace.occurred:
        yield _format_anthropic_sse(
            AnthropicStreamEvent(
                type=SSE_FALLBACK_EVENT,
                data=trace.as_event_payload(),
            )
        )


def _format_anthropic_sse(ev: AnthropicStreamEvent) -> str:
    """Serialize an :class:`AnthropicStreamEvent` onto the SSE wire.

    Anthropic's SSE format requires both an ``event:`` and a ``data:``
    line per frame (unlike OpenAI's ``data:``-only chunks). The event
    name carries the type (``message_start`` / ``content_block_delta``
    / ...) and the data line carries the JSON payload.
    """
    payload = json.dumps(ev.data, ensure_ascii=False)
    return f"event: {ev.type}\ndata: {payload}\n\n"


# ---------------------------------------------------------------------------
# v1.9-E (L3): structured payloads for the ``break`` action
#
# Both shapes carry the same underlying detection fields. Differences:
#
#   * The non-streaming 400 ``detail`` is a flat dict whose ``error``
#     field is the discriminator — clients branch on that. ``message``
#     duplicates the str(exc) for log-grep friendliness.
#   * The streaming SSE event nests the detection fields under a
#     ``tool_loop`` key inside Anthropic's standard
#     ``{"type":"error","error":{"type":...,"message":...}}`` envelope,
#     so existing Anthropic SDKs that read ``error.type`` /
#     ``error.message`` keep working and CodeRouter-aware clients can
#     also look at ``error.tool_loop`` for the structured fields.
# ---------------------------------------------------------------------------


def _tool_loop_break_extension(exc: ToolLoopBreakError) -> dict[str, object]:
    """Build the structured detection payload (shared by both shapes).

    Carries only fields the client can act on — ``args_canonical`` is
    intentionally omitted because tool input often contains user data
    we don't want to leak into a 400 detail or an SSE error event.
    """
    return {
        "profile": exc.profile,
        "tool_name": exc.detection.tool_name,
        "repeat_count": exc.detection.repeat_count,
        "threshold": exc.threshold,
        "window": exc.window,
    }


def _tool_loop_break_detail(exc: ToolLoopBreakError) -> dict[str, object]:
    """Build the flat ``detail`` dict for the non-streaming 400.

    ``error: "tool_loop_detected"`` is the stable string clients should
    branch on; ``message`` mirrors ``str(exc)`` so a human reading the
    log gets the same line whether they look at the response or the
    server log. The remaining fields come straight from
    :func:`_tool_loop_break_extension`.
    """
    detail: dict[str, object] = {
        "error": "tool_loop_detected",
        "message": str(exc),
    }
    detail.update(_tool_loop_break_extension(exc))
    return detail
