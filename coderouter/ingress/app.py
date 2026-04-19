"""FastAPI app factory."""

from __future__ import annotations

from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from fastapi import FastAPI

from coderouter import __version__
from coderouter.config import load_config
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
        logger.info(
            "coderouter-startup",
            extra={
                "version": __version__,
                "providers": [p.name for p in config.providers],
                "profiles": [pr.name for pr in config.profiles],
                "allow_paid": config.allow_paid,
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

    app.include_router(openai_router, prefix="/v1", tags=["openai-compat"])

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
