from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from videasy.core.cache import TTLCache
from videasy.core.exceptions import register_exception_handlers
from videasy.core.http_client import create_api_client, create_proxy_client
from videasy.core.logging import RequestLoggingMiddleware, setup_logging

logger = logging.getLogger("videasy")

_SHUTDOWN_TIMEOUT = 5


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    logger.info("starting up — httpx clients initialised")
    app.state.api_client = create_api_client()
    app.state.proxy_client = create_proxy_client()
    app.state.client = app.state.api_client  # backward compat alias
    app.state.cache = TTLCache()
    yield
    logger.info("shutting down — closing HTTP client connections (timeout %ss)...", _SHUTDOWN_TIMEOUT)
    try:
        await asyncio.wait_for(
            _close_clients(app),
            timeout=_SHUTDOWN_TIMEOUT,
        )
    except asyncio.TimeoutError:
        logger.warning("shutdown timed out after %ss — forcing stop", _SHUTDOWN_TIMEOUT)
    except Exception as exc:
        logger.warning("error during shutdown: %s", exc)
    if hasattr(app.state, "cache") and hasattr(app.state.cache, "clear"):
        try:
            app.state.cache.clear()
        except Exception:
            pass
    logger.info("application gracefully stopped — all resources released successfully")


async def _close_clients(app: FastAPI) -> None:
    for attr in ("api_client", "proxy_client"):
        client = getattr(app.state, attr, None)
        if client is not None:
            try:
                await client.aclose()
            except Exception:
                pass


def create_app() -> FastAPI:
    setup_logging()

    app = FastAPI(
        title="Videasy Decryptor API",
        description=(
            "Decrypt video streams from Videasy.net (Wingsdatabase providers: "
            "Yoru, Neon, Cypher, Breach, Moviebox) and fetch sources from "
            "TheMovieBox.xyz (MP4, DASH, HLS + CDN subtitles).\n\n"
            "The `/subtitles` endpoint supports Wyzie Subs for subtitle lookup.\n\n"
            "The `/proxy` endpoint streams CDN content with Range support and "
            "CORS bypass. DASH manifests are rewritten to route segments "
            "through `/dash/{token}/...`."
        ),
        version="3.0.0",
        lifespan=lifespan,
        contact={"name": "Videasy Decryptor", "url": "https://github.com/"},
        license_info={"name": "MIT"},
        docs_url="/docs",
        redoc_url="/redoc",
    )

    # Middleware (last added = first executed)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    from videasy.proxy.middleware import DASHSegmentMiddleware
    app.add_middleware(DASHSegmentMiddleware)

    # Request logging (outermost middleware — first to execute)
    app.add_middleware(RequestLoggingMiddleware)

    # Exception handlers
    register_exception_handlers(app)

    # Routers
    from videasy.proxy.routes import router as proxy_router
    from videasy.features.sources.routes import router as sources_router
    from videasy.features.moviebox.routes import router as moviebox_router
    from videasy.features.subtitles.routes import router as subtitles_router
    from videasy.features.info.routes import router as info_router
    from videasy.features.player.routes import router as player_router
    from videasy.features.lk21.routes import router as lk21_router

    for r in [proxy_router, sources_router, moviebox_router, subtitles_router, info_router, player_router, lk21_router]:
        app.include_router(r)

    return app
