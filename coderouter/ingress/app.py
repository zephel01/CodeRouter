"""FastAPI app factory."""

from __future__ import annotations

import contextlib
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from coderouter import __version__
from coderouter.config import load_config
from coderouter.ingress.anthropic_routes import router as anthropic_router
from coderouter.ingress.dashboard_routes import router as dashboard_router
from coderouter.ingress.launcher_routes import router as launcher_router
from coderouter.ingress.metrics_routes import router as metrics_router
from coderouter.ingress.openai_routes import router as openai_router
from coderouter.logging import configure_logging, get_logger
from coderouter.messages import tr
from coderouter.metrics import install_collector
from coderouter.plugins import discover_and_load
from coderouter.routing import FallbackEngine
from coderouter.routing.capability import check_claude_code_chain_suitability

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# H8: DNS-rebinding protection via Host-header validation
# ---------------------------------------------------------------------------

# Loopback hosts that are always allowed. ``testserver`` is the default Host
# that Starlette's TestClient sends, so it is whitelisted to keep the existing
# suite (and any local integration tests) working without extra configuration.
_DEFAULT_ALLOWED_HOSTS: frozenset[str] = frozenset(
    {"localhost", "127.0.0.1", "[::1]", "::1", "testserver"}
)


def _parse_allowed_hosts(raw: str | None) -> frozenset[str]:
    """Merge the built-in loopback set with ``CODEROUTER_ALLOWED_HOSTS``.

    The env var is a comma-separated list of extra hostnames (no port) that
    should be accepted — the escape hatch for deliberate external exposure.
    """
    extra = {
        part.strip().lower()
        for part in (raw or "").split(",")
        if part.strip()
    }
    return _DEFAULT_ALLOWED_HOSTS | frozenset(extra)


def _host_without_port(host_header: str) -> str:
    """Strip the optional ``:port`` suffix from a Host header value.

    Handles bracketed IPv6 literals (``[::1]:8080`` → ``[::1]``) as well as
    the common ``host:port`` form. A bare ``[::1]`` or ``host`` is returned
    unchanged.
    """
    value = host_header.strip().lower()
    if value.startswith("["):
        # IPv6 literal: keep everything up to and including the closing bracket.
        end = value.find("]")
        if end != -1:
            return value[: end + 1]
        return value
    # IPv4 / hostname: split off a trailing :port if present.
    if ":" in value:
        return value.rsplit(":", 1)[0]
    return value


# ---------------------------------------------------------------------------
# M14: request body size limit (DoS protection)
# ---------------------------------------------------------------------------

# Default cap for incoming request bodies. Anthropic/OpenAI chat payloads are
# comfortably below this even with large system prompts; the ceiling exists to
# stop a client from streaming an unbounded body and exhausting memory.
_DEFAULT_MAX_BODY_BYTES = 64 * 1024 * 1024  # 64 MB
_MAX_BODY_BYTES_ENV = "CODEROUTER_MAX_BODY_BYTES"


def _parse_max_body_bytes(raw: str | None) -> int:
    """Resolve the body-size cap from ``CODEROUTER_MAX_BODY_BYTES``.

    Falls back to :data:`_DEFAULT_MAX_BODY_BYTES` when the env var is unset,
    empty, non-numeric, or non-positive — a misconfigured value must never
    silently disable the guard.
    """
    if raw is None or not raw.strip():
        return _DEFAULT_MAX_BODY_BYTES
    try:
        value = int(raw.strip())
    except ValueError:
        return _DEFAULT_MAX_BODY_BYTES
    return value if value > 0 else _DEFAULT_MAX_BODY_BYTES


class _BodyTooLarge(Exception):
    pass


class BodySizeLimitMiddleware:
    """Reject requests whose body exceeds the configured cap.

    M-3: this is a **pure ASGI** middleware, not a ``BaseHTTPMiddleware``.
    The old dispatch only checked the ``Content-Length`` header, so a chunked
    request (no Content-Length) bypassed the cap entirely. We now also count
    the bytes actually received and fail closed with 413 the moment the total
    crosses the limit. It must be pure ASGI because a ``BaseHTTPMiddleware``
    that consumes ``request.stream()`` to measure the body would hand a
    drained (empty) body to the downstream route.

    Streaming *responses* (SSE) are unaffected — this only inspects the
    request side.
    """

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self._max_bytes = max_bytes

    def _too_large(self, observed: int) -> JSONResponse:
        return JSONResponse(
            status_code=413,
            content={
                "detail": tr(
                    "E1202_BODY_TOO_LARGE",
                    observed=observed,
                    limit=self._max_bytes,
                )
            },
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        for key, value in scope.get("headers", []):
            if key == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    break
                if declared > self._max_bytes:
                    await self._too_large(declared)(scope, receive, send)
                    return
                break
        received = 0
        response_started = False

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self._max_bytes:
                    raise _BodyTooLarge(received)
            return message

        async def guarded_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, guarded_send)
        except _BodyTooLarge as exc:
            if response_started:
                raise
            await self._too_large(int(exc.args[0]))(scope, receive, send)


class HostValidationMiddleware(BaseHTTPMiddleware):
    """Reject requests whose Host header is not an allow-listed hostname.

    This blocks DNS-rebinding attacks: even when CodeRouter is bound to
    ``127.0.0.1``, a malicious page can point a hostname it controls at the
    loopback address and drive the local API from the victim's browser. By
    pinning the accepted Host values we make such cross-origin requests fail
    closed with 403.
    """

    def __init__(self, app: ASGIApp, allowed_hosts: frozenset[str]) -> None:
        super().__init__(app)
        self._allowed = allowed_hosts

    async def dispatch(self, request: Request, call_next):  # type: ignore[no-untyped-def]
        host_header = request.headers.get("host", "")
        host = _host_without_port(host_header)
        if host not in self._allowed:
            return JSONResponse(
                status_code=403,
                content={"detail": tr("E1201_HOST_NOT_ALLOWED", host=host_header)},
            )
        return await call_next(request)


def create_app(config_path: str | None = None) -> FastAPI:
    """Build a FastAPI app with routes, engine, and lifespan installed.

    ``config_path`` (optional) is passed through to
    :func:`coderouter.config.load_config`; when ``None`` the loader
    falls through to ``$CODEROUTER_CONFIG`` / ``./providers.yaml``. The
    engine and config are attached to ``app.state`` so route handlers
    can reach them without re-parsing YAML per request.
    """
    configure_logging()
    # v1.5-A: attach the MetricsCollector before the first log line so the
    # startup ``coderouter-startup`` record is already counted. Idempotent,
    # so multiple create_app() calls (tests) don't stack handlers.
    install_collector()
    config = load_config(config_path)
    # v2.3.0: discover plugins from importlib.metadata entry points and
    # apply the user's explicit ``plugins.enabled`` allowlist. When the
    # ``plugins`` block is absent or empty, the loader returns an empty
    # registry and the engine's hook loops short-circuit — the request
    # flow is bit-identical to v2.2.0 in that default case.
    plugin_registry = discover_and_load(config)
    engine = FallbackEngine(config, plugins=plugin_registry)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """Log a structured startup line, yield to serve, log shutdown.

        The startup payload captures the effective default profile and
        whether it came from the YAML file or from ``$CODEROUTER_MODE``
        — useful when a shell env is unknowingly overriding the
        committed config.
        """
        # v0.6-A: surface the effective default_profile + where it came from,
        # so operators can tell at a glance whether a shell env is driving the
        # server ("oh, my .envrc set CODEROUTER_MODE") vs the YAML file
        # ("default_profile: coding was committed").
        mode_source = "env" if os.environ.get("CODEROUTER_MODE", "").strip() else "config"
        logger.info(
            "coderouter-startup",
            extra={
                "version": __version__,
                "providers": [p.name for p in config.providers],
                "profiles": [pr.name for pr in config.profiles],
                "allow_paid": config.allow_paid,
                "default_profile": config.default_profile,
                "mode_source": mode_source,
            },
        )
        # v1.7-B: scan ``claude-code-*`` profiles for providers whose
        # registry-resolved ``claude_code_suitability`` is ``degraded``,
        # emitting a structured warn per affected profile. Runs after the
        # startup line so the operator's eye finds startup → warn in
        # chronological order. Non-fatal — the chain still works, just
        # potentially sub-optimally for the agentic harness.
        check_claude_code_chain_suitability(config, logger=logger)

        # Translation layer: initialize TranslatorManager when enabled (CPU-only, resident)
        # Stored on app.state so ingress and fallback can reach it via request.app.state
        translator_manager = None
        tcfg = getattr(config, "translation", None)
        if tcfg is not None and getattr(tcfg, "enabled", False):
            try:
                import os as _os
                import time as _time

                _os.environ["ARGOS_DEVICE_TYPE"] = "cpu"
                from coderouter.jp_translation.manager import TranslatorManager

                translator_manager = TranslatorManager(model_dir=getattr(tcfg, "model_dir", None))
                _tr_start = _time.monotonic()
                # load() is sync blocking (Argos/CTranslate2 init). In lifespan async context
                # this blocks startup; manager.load() now logs elapsed_ms and slow-startup warning.
                translator_manager.load()
                _tr_elapsed_ms = (_time.monotonic() - _tr_start) * 1000
                logger.info(
                    "translation-manager-started",
                    extra={
                        "model_dir": getattr(tcfg, "model_dir", None) or "argos-cache",
                        "elapsed_ms": round(_tr_elapsed_ms, 1),
                    },
                )
                if _tr_elapsed_ms > 5000:
                    logger.warning(
                        "translation-manager-slow-startup-app",
                        extra={"elapsed_ms": round(_tr_elapsed_ms, 1)},
                    )
            except Exception as exc:
                logger.warning(
                    "translation-manager-failed",
                    extra={"error": str(exc), "hint": "translation layer disabled, falling back to passthrough"},
                )
                translator_manager = None
        # Expose on app.state and engine
        app.state.translator_manager = translator_manager
        # Attach to engine so routing/fallback can translate responses without request.app
        with contextlib.suppress(Exception):
            engine.attach_translator_manager(translator_manager)

        # v2.0-K: attach persistent state store + audit/request log if configured.
        state_store = None
        audit_handler = None
        request_log_handler = None
        if config.state_dir:
            import logging as _logging
            from pathlib import Path

            from coderouter.state.audit_log import AuditLogHandler
            from coderouter.state.store import StateStore

            state_path = Path(config.state_dir).expanduser()
            state_store = StateStore(state_path / "coderouter.db")
            engine.attach_state_store(state_store)

            # Restore MetricsCollector state from the store.
            from coderouter.metrics import get_collector

            collector = get_collector()
            if collector is not None:
                metrics_state = state_store.get("metrics", "state")
                if metrics_state is not None:
                    with contextlib.suppress(Exception):
                        collector.load_state(metrics_state)  # type: ignore[arg-type]

            logger.info(
                "state-store-attached",
                extra={"state_dir": str(state_path)},
            )

            if config.audit_log == "active":
                audit_handler = AuditLogHandler(
                    state_path / "audit.jsonl",
                    max_bytes=config.audit_log_max_bytes,
                )
                _logging.getLogger().addHandler(audit_handler)
                logger.info(
                    "audit-log-started",
                    extra={
                        "path": str(state_path / "audit.jsonl"),
                        "max_bytes": config.audit_log_max_bytes,
                    },
                )

            if config.request_log == "active":
                from coderouter.state.request_log import RequestLogHandler

                request_log_handler = RequestLogHandler(
                    state_path / "requests.jsonl",
                    max_bytes=config.request_log_max_bytes,
                )
                _logging.getLogger().addHandler(request_log_handler)
                logger.info(
                    "request-log-started",
                    extra={
                        "path": str(state_path / "requests.jsonl"),
                        "max_bytes": config.request_log_max_bytes,
                    },
                )

        # v2.0-I: launch continuous probe background task if configured.
        probe_task = None
        shutdown_event = None
        if config.continuous_probe == "active":
            import asyncio

            from coderouter.guards.continuous_probe import probe_loop
            from coderouter.routing.capability import get_default_registry

            shutdown_event = asyncio.Event()
            probe_task = asyncio.create_task(
                probe_loop(
                    config.providers,
                    record_fn=engine.backend_health.record_attempt,
                    interval_s=config.probe_interval_s,
                    timeout_s=config.probe_timeout_s,
                    probe_paid=config.probe_paid,
                    shutdown_event=shutdown_event,
                    registry=get_default_registry(),
                )
            )
            logger.info(
                "continuous-probe-started",
                extra={
                    "interval_s": config.probe_interval_s,
                    "probe_paid": config.probe_paid,
                    "providers": len(config.providers),
                },
            )

        # launcher-model-swap.md §6.6 known-trap #9: the TTL sweeper is a
        # background task like continuous-probe above — start it once the
        # event loop is actually running, not at app-construction time.
        swap_manager = getattr(app.state, "swap", None)
        if swap_manager is not None:
            await swap_manager.start()

        yield

        # Graceful shutdown of probe task
        if probe_task is not None and shutdown_event is not None:
            shutdown_event.set()
            with contextlib.suppress(Exception):
                await probe_task

        # Cancel the swap TTL sweeper *before* shutdown_launcher tears down
        # every ManagedProcess below — shutdown_launcher already stops
        # swap-spawned processes too (they're in the same registry), so
        # this only needs to stop the sweeper loop itself, not race it.
        if swap_manager is not None:
            with contextlib.suppress(Exception):
                await swap_manager.stop()

        # Launcher: stop child llama.cpp / vllm processes so they don't orphan.
        from coderouter.ingress.launcher_routes import shutdown_launcher

        with contextlib.suppress(Exception):
            await shutdown_launcher(app)

        # v2.0-J: graceful shutdown of recovery probe tasks.
        with contextlib.suppress(Exception):
            await engine.shutdown_recovery_probes()

        # H3: close each adapter's shared httpx.AsyncClient so pooled
        # connections / keep-alive sockets are released cleanly instead of
        # being left to garbage collection. Adapters are cached on the
        # engine (one per provider); ``aclose`` is idempotent and a no-op
        # when the adapter never issued a request.
        for _adapter in engine._adapters.values():
            with contextlib.suppress(Exception):
                await _adapter.aclose()

        # v2.0-K: persist state and close audit log on shutdown.
        if state_store is not None:
            with contextlib.suppress(Exception):
                engine.save_all_state()
            # Save MetricsCollector state.
            from coderouter.metrics import get_collector

            collector = get_collector()
            if collector is not None:
                with contextlib.suppress(Exception):
                    state_store.put("metrics", "state", collector.save_state())
            with contextlib.suppress(Exception):
                state_store.close()
        if audit_handler is not None:
            import logging as _logging

            with contextlib.suppress(Exception):
                _logging.getLogger().removeHandler(audit_handler)
                audit_handler.close()
        if request_log_handler is not None:
            import logging as _logging

            with contextlib.suppress(Exception):
                _logging.getLogger().removeHandler(request_log_handler)
                request_log_handler.close()

        # Translation layer: release Argos resources (B-1 fix)
        # Use app.state (robust against closure variable rename) + fallback to locals
        with contextlib.suppress(Exception):
            tm = getattr(app.state, "translator_manager", None)
            if tm is None:
                tm = locals().get("translator_manager")
            if tm is not None and hasattr(tm, "close"):
                tm.close()
                logger.info("translation-manager-closed")
            # ensure state is cleared for test isolation (also covered by conftest autouse)
            with contextlib.suppress(Exception):
                app.state.translator_manager = None
            with contextlib.suppress(Exception):
                if hasattr(engine, "_translator_manager"):
                    engine._translator_manager = None  # type: ignore[attr-defined]

        logger.info("coderouter-shutdown")

    app = FastAPI(
        title="CodeRouter",
        version=__version__,
        description="Local-first, free-first, fallback-built-in LLM router.",
        lifespan=lifespan,
    )

    # Inject engine + config so route handlers can reach them via app.state
    app.state.engine = engine
    app.state.config = config
    # Translation manager placeholder (lifespan will populate when enabled)
    # Ensures request.app.state.translator_manager is always defined
    if not hasattr(app.state, "translator_manager"):
        app.state.translator_manager = None

    # Phase 1 on-demand model swap (docs/designs/launcher-model-swap.md).
    # None (default) leaves the engine's swap dispatch hooks as cheap
    # no-ops — zero behavior change for deployments that don't opt in.
    # Constructed here (needs the real ``app`` for app.state.launcher /
    # app.state.engine) but the TTL sweeper task itself is only started
    # from the lifespan below, once the event loop is actually running.
    swap_cfg = config.launcher.swap if config.launcher is not None else None
    if swap_cfg is not None and swap_cfg.enabled:
        from coderouter.launcher_swap import SwapManager

        swap_manager = SwapManager(app, swap_cfg, config.launcher)
        app.state.swap = swap_manager
        engine.attach_swap_manager(swap_manager)

    # H8: DNS-rebinding protection. Applied to every route so a hostname an
    # attacker controls cannot be pointed at loopback and used to drive the
    # local API from a victim's browser. Extra hostnames for deliberate
    # external exposure come from CODEROUTER_ALLOWED_HOSTS (comma-separated).
    allowed_hosts = _parse_allowed_hosts(
        os.environ.get("CODEROUTER_ALLOWED_HOSTS")
    )
    app.add_middleware(HostValidationMiddleware, allowed_hosts=allowed_hosts)

    # M14: request body size limit (DoS protection). ``add_middleware`` wraps
    # outermost-last, so the middleware added *after* HostValidation ends up
    # the outermost layer — i.e. the size guard runs FIRST, before Host
    # validation. That ordering is harmless: an oversized/chunked body is
    # rejected as early as possible. SSE streaming responses are unaffected;
    # this only inspects the request side.
    max_body_bytes = _parse_max_body_bytes(
        os.environ.get(_MAX_BODY_BYTES_ENV)
    )
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=max_body_bytes)

    @app.get("/healthz")
    async def healthz() -> dict[str, object]:
        """Lightweight liveness / config snapshot endpoint.

        Reports the running version plus the effective provider names
        and paid-gate state. Intended for readiness probes and for
        quick operator inspection — does NOT touch upstream providers.
        """
        return {
            "status": "ok",
            "version": __version__,
            "providers": [p.name for p in config.providers],
            "allow_paid": config.allow_paid,
        }

    # Claude Code and similar SDKs probe the base URL with HEAD / or GET /
    # at startup. Return a tiny identifier instead of 404 so those probes
    # succeed cleanly. Non-functional beyond that.
    @app.api_route("/", methods=["GET", "HEAD"])
    async def root() -> dict[str, str]:
        """Minimal identifier for SDK base-URL probes (GET / and HEAD /).

        Claude Code and similar SDKs HEAD/GET the base URL at startup
        to verify reachability. Returning a tiny JSON payload instead
        of 404 keeps those probes from logging scary warnings.
        """
        return {"service": "coderouter", "version": __version__}

    app.include_router(openai_router, prefix="/v1", tags=["openai-compat"])
    app.include_router(anthropic_router, prefix="/v1", tags=["anthropic-compat"])
    # v1.5-A: /metrics.json sits at the root (no /v1 prefix) — metrics are not
    # part of the OpenAI / Anthropic API surface, and Prometheus-style
    # endpoints conventionally live at the root in v1.5-B.
    app.include_router(metrics_router, tags=["metrics"])
    # v1.5-D: single-page HTML view over the same collector snapshot.
    # Same root-level mount as /metrics.json — the dashboard is a UI
    # concern and doesn't belong under the /v1 API surface.
    app.include_router(dashboard_router, tags=["dashboard"])
    # Launcher UI + process management API.
    # /launcher       → single-page HTML UI
    # /api/launcher/* → model scan, process start/stop/logs
    app.include_router(launcher_router, tags=["launcher"])

    return app


# Lazy module-level `app` attribute so `uvicorn coderouter.ingress.app:app …`
# works, but importing this module in tests does NOT immediately load
# providers.yaml. The FastAPI instance is built on first attribute access.
#
# Config path is resolved then — from $CODEROUTER_CONFIG or ./providers.yaml;
# see coderouter.config.loader._candidate_paths for the full search order.
_lazy_app: FastAPI | None = None


def __getattr__(name: str) -> object:
    """PEP 562 module ``__getattr__`` — lazy FastAPI instance on first access.

    Makes ``uvicorn coderouter.ingress.app:app …`` work without having
    ``import coderouter.ingress.app`` load ``providers.yaml`` at import
    time. Tests can import the module without side effects and call
    :func:`create_app` explicitly with a temp config.
    """
    global _lazy_app
    if name == "app":
        if _lazy_app is None:
            _lazy_app = create_app()
        return _lazy_app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
