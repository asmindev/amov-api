from __future__ import annotations

import pytest


@pytest.mark.anyio
async def test_landing_page(client):
    r = await client.get("/")
    assert r.status_code == 200
    assert "videasy" in r.text.lower()


@pytest.mark.anyio
async def test_player_page(client):
    r = await client.get("/player")
    assert r.status_code == 200
    assert "Video Player" in r.text
