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


@pytest.mark.anyio
async def test_player_page_with_sources(client):
    sources_param = '[{"quality":"1080p (1042MB)","url":"https://bcdnxw2.hakunaymatata.com/resource/test.mp4","source":"play","type":"mp4"}]'
    r = await client.get(f"/player?sources={sources_param}")
    assert r.status_code == 200
    assert "sources-input" in r.text
    assert "parseJsonSafely" in r.text
