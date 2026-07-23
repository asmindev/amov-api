from __future__ import annotations

import pytest
import httpx
from httpx import ASGITransport

from videasy.app import create_app


@pytest.fixture
def app():
    return create_app()


@pytest.fixture
async def client(app):
    from videasy.core.http_client import create_api_client, create_proxy_client
    from videasy.core.cache import TTLCache

    app.state.api_client = create_api_client()
    app.state.proxy_client = create_proxy_client()
    app.state.client = app.state.api_client
    app.state.cache = TTLCache()

    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c

    await app.state.api_client.aclose()
    await app.state.proxy_client.aclose()
