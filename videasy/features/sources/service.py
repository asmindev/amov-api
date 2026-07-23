from __future__ import annotations

import logging
import time
from typing import Any
from urllib.parse import quote, urlencode

import httpx
from fastapi import HTTPException

from videasy.config import settings
from videasy.core.cache import TTLCache
from videasy.features.sources.providers import Provider
from videasy.integrations.decryption import decrypt
from videasy.models.source import SourceParams

logger = logging.getLogger("videasy")

SeedCache = TTLCache | dict[str, Any]


def get_seed(cache: SeedCache, tmdb_id: str) -> str | None:
    if isinstance(cache, TTLCache):
        return cache.get(tmdb_id)
    entry = cache.get(tmdb_id)
    if entry:
        if isinstance(entry, tuple):
            seed, expiry = entry
            if time.monotonic() < expiry:
                return seed
        elif isinstance(entry, str):
            return entry
    return None


def set_seed(cache: SeedCache, tmdb_id: str, seed: str, ttl_ms: int) -> None:
    ttl_seconds = (ttl_ms - settings.cache_ttl_offset) / 1000.0
    if ttl_seconds <= 0:
        ttl_seconds = 30.0
    if isinstance(cache, TTLCache):
        cache.set(tmdb_id, seed, ttl_seconds)
    else:
        expiry = time.monotonic() + ttl_seconds
        cache[tmdb_id] = (seed, expiry)


async def fetch_seed(client: httpx.AsyncClient, cache: SeedCache, tmdb_id: str) -> str:
    cached = get_seed(cache, tmdb_id)
    if cached:
        logger.debug("seed cache hit for tmdbId=%s", tmdb_id)
        return cached

    logger.debug("fetching seed for tmdbId=%s", tmdb_id)
    resp = await client.get(f"{settings.api_base}/seed?mediaId={tmdb_id}")
    if resp.status_code == 429:
        raise HTTPException(status_code=429, detail="rate limited by upstream API — try again later")
    resp.raise_for_status()
    data = resp.json()
    seed = data["seed"]
    ttl = data.get("ttlMs", 30_000)
    set_seed(cache, tmdb_id, seed, ttl)
    return seed


async def resolve_title_if_missing(
    client: httpx.AsyncClient,
    params: SourceParams,
) -> str:
    if params.title and params.title.strip():
        return params.title.strip()

    if params.imdbId and params.imdbId.startswith("tt"):
        try:
            kind = "series" if params.mediaType == "tv" else "movie"
            url = f"https://v3-cinemeta.strem.io/meta/{kind}/{params.imdbId}.json"
            r = await client.get(url, timeout=4.0)
            if r.status_code == 200:
                name = r.json().get("meta", {}).get("name")
                if name:
                    return name
        except Exception:
            pass

    return "Media"


async def fetch_sources(
    client: httpx.AsyncClient,
    cache: SeedCache,
    provider: Provider,
    params: SourceParams,
) -> tuple[str, dict[str, Any]]:
    if not params.title or not params.title.strip():
        params.title = await resolve_title_if_missing(client, params)

    seed = await fetch_seed(client, cache, params.tmdbId)
    enc_title = quote(quote(params.title, safe=""), safe="")

    qs = {
        "title": enc_title,
        "mediaType": params.mediaType,
        "year": params.year,
        "episodeId": params.episodeId,
        "seasonId": params.seasonId,
        "tmdbId": params.tmdbId,
        "imdbId": params.imdbId,
        "enc": "2",
        "seed": seed,
    }
    url = f"{settings.api_base}/{provider.endpoint}/sources-with-title?{urlencode(qs)}"

    logger.info("fetching sources: provider=%s tmdbId=%s", provider.name, params.tmdbId)
    cipher_resp = await client.get(url)
    if cipher_resp.status_code == 429:
        raise HTTPException(status_code=429, detail="rate limited by upstream API — try again later")
    if cipher_resp.status_code == 500:
        raise HTTPException(status_code=502, detail=f"{provider.name}: upstream returned 500")
    cipher_resp.raise_for_status()

    cipher = cipher_resp.text.strip()
    if not cipher:
        raise HTTPException(status_code=502, detail=f"{provider.name}: empty response from upstream")

    data = await decrypt(client, cipher, params.tmdbId, seed)
    return provider.name, data
