from __future__ import annotations

from typing import AsyncIterator

import httpx
from fastapi import Request

from videasy.config import Settings, settings
from videasy.core.cache import TTLCache


def get_settings() -> Settings:
    return settings


async def get_http_client(request: Request) -> AsyncIterator[httpx.AsyncClient]:
    yield request.app.state.client


def get_cache(request: Request) -> TTLCache:
    return request.app.state.cache
