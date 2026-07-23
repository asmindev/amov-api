from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
import httpx

from videasy.config import settings
from videasy.core.cache import TTLCache
from videasy.features.sources.service import fetch_sources, fetch_seed
from videasy.features.sources.providers import PROVIDER_MAP
from videasy.models.source import SourceParams


def test_ttl_cache_operations():
    cache = TTLCache()
    cache.set("key1", "val1", 10.0)
    assert cache.get("key1") == "val1"
    assert "key1" in cache

    # Item assignment and item retrieval test
    cache["key2"] = "val2"
    assert cache["key2"] == "val2"

    cache["key3"] = ("val3", 60.0)
    assert cache["key3"] == "val3"


@pytest.mark.anyio
async def test_fetch_seed_with_ttl_cache():
    cache = TTLCache()

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"seed": "myseed123", "ttlMs": 30000}
    mock_resp.raise_for_status = MagicMock()

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.get.return_value = mock_resp

    seed = await fetch_seed(mock_client, cache, "157336")
    assert seed == "myseed123"
    assert cache.get("157336") == "myseed123"

    # Second call should hit cache without calling client.get
    mock_client.get.reset_mock()
    seed_cached = await fetch_seed(mock_client, cache, "157336")
    assert seed_cached == "myseed123"
    mock_client.get.assert_not_called()


@pytest.mark.anyio
async def test_get_sources_route(client):
    with patch("videasy.features.sources.routes.fetch_sources") as mock_fetch:
        mock_fetch.return_value = (
            "Yoru",
            {
                "sources": [{"quality": "1080p", "url": "https://cdn.example.com/stream.m3u8"}],
                "subtitles": [{"lang": "En", "language": "English", "url": "https://subs.example.com/en.vtt"}],
            },
        )

        response = await client.get(
            "/sources?tmdbId=157336&mediaType=movie&title=Interstellar&provider=Yoru&year=2014&imdbId=tt0816692"
        )
        assert response.status_code == 200
        data = response.json()
        assert data["tmdbId"] == "157336"
        assert data["provider"] == "Yoru"
        assert len(data["data"]["sources"]) == 1
        assert data["data"]["sources"][0]["quality"] == "1080p"
