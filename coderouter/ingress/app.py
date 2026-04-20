"""FastAPI app factory."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from coderouter import __version__
from coderouter.config import load_config
from coderouter.ingress.anthropic_routes import router as anthropic_router
from coderouter.ingress.openai_routes import router as openai_router
from coderouter.logging import configure_logging, get_logger
from coderouter.routing import FallbackEngine

logger = get_logger(__name__)


def create_app(config_path: str | None = None) -> FastAPI:
    configure_logging()
    config = load_config(config_path)
    engine = FallbackEngine(config)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
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
        yield
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

    @app.get("/healthz")
    async def healthz() -> dict[str, object]:
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
        return {"service": "coderouter", "version": __version__}

    app.include_router(openai_router, prefix="/v1", tags=["openai-compat"])
    app.include_router(anthropic_router, prefix="/v1", tags=["anthropic-compat"])

    return app


# Lazy module-level `app` attribute so `uvicorn coderouter.ingress.app:app …`
# works, but importing this module in tests does NOT immediately load
# providers.yaml. The FastAPI instance is built on first attribute access.
#
# Config path is resolved then — from $CODEROUTER_CONFIG or ./providers.yaml;
# see coderouter.config.loader._candidate_paths for the full search order.
_lazy_app: FastAPI | None = None


def __getattr__(name: str) -> object:
    global _lazy_app
    if name == "app":
        if _lazy_app is None:
            _lazy_app = create_app()
        return _lazy_app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
