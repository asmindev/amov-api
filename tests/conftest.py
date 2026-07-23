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
    # Manually trigger lifespan to set up app.state.client
    from videasy.core.http_client import create_http_client
    from videasy.core.cache import TTLCache

    app.state.client = create_http_client()
    app.state.cache = TTLCache()

    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c

    await app.state.client.aclose()
