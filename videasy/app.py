from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from videasy.config import settings
from videasy.core.cache import TTLCache
from videasy.core.exceptions import register_exception_handlers
from videasy.core.http_client import build_default_headers, create_http_client

logger = logging.getLogger("videasy")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    logger.info("starting up — httpx client initialised")
    app.state.client = create_http_client()
    app.state.cache: TTLCache = TTLCache()
    yield
    await app.state.client.aclose()
    logger.info("shut down — client closed")


def create_app() -> FastAPI:
    app = FastAPI(
        title="Videasy Decryptor API",
        description=(
            "Decrypt Videasy.net video streams. Fetch encrypted sources from "
            "`api.wingsdatabase.com`, decrypt via `enc-dec.app`, returns "
            "quality-labelled HLS streams + subtitles."
        ),
        version="3.0.0",
        lifespan=lifespan,
        contact={"name": "Videasy Decryptor", "url": "https://github.com/"},
        license_info={"name": "MIT"},
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

    # Exception handlers
    register_exception_handlers(app)

    # Routers
    from videasy.proxy.routes import router as proxy_router
    from videasy.features.sources.routes import router as sources_router
    from videasy.features.moviebox.routes import router as moviebox_router
    from videasy.features.subtitles.routes import router as subtitles_router
    from videasy.features.info.routes import router as info_router
    from videasy.features.player.routes import router as player_router

    for r in [proxy_router, sources_router, moviebox_router, subtitles_router, info_router, player_router]:
        app.include_router(r)

    return app
