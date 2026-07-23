from __future__ import annotations

import pytest


@pytest.mark.anyio
async def test_health(client):
    r = await client.get("/health")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ok"
    assert "version" in data


@pytest.mark.anyio
async def test_providers(client):
    r = await client.get("/providers")
    assert r.status_code == 200
    data = r.json()
    assert "providers" in data
    assert len(data["providers"]) > 0
    names = [p["name"] for p in data["providers"]]
    assert "Yoru" in names
    assert "Neon" in names


@pytest.mark.anyio
async def test_subtitles_list(client):
    r = await client.get("/subtitles")
    assert r.status_code == 200
    data = r.json()
    assert "sources" in data
    assert "opensubtitles" in data["sources"]
