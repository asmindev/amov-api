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
            url = f"{settings.cinemeta_base}/meta/{kind}/{params.imdbId}.json"
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
    try:
        cipher_resp.raise_for_status()
        cipher = cipher_resp.text.strip()
        if not cipher:
            raise HTTPException(status_code=502, detail=f"{provider.name}: empty response from upstream")
        data = await decrypt(client, cipher, params.tmdbId, seed)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404 and provider.name.lower() == "moviebox":
            data = {"sources": [], "subtitles": []}
        else:
            raise

    if provider.name.lower() == "moviebox" and not data.get("sources"):
        try:
            base_url = settings.flikhub_proxy_base.rstrip("/")
            if params.mediaType == "tv" and params.seasonId and params.episodeId:
                flik_url = f"{base_url}/tv?id={params.tmdbId}&season={params.seasonId}&episode={params.episodeId}&mode=json&sources=moviebox&hevc=1"
            else:
                flik_url = f"{base_url}/movie?id={params.tmdbId}&mode=json&sources=moviebox&hevc=1"
            
            headers = {
                "Accept": "application/json",
                "Origin": "https://player.cinezo.live",
                "Referer": "https://player.cinezo.live/",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            }
            flik_resp = await client.get(flik_url, headers=headers, timeout=5.0)
            if flik_resp.status_code == 200:
                flik_data = flik_resp.json()
                if "source" in flik_data and "qualities" in flik_data["source"]:
                    if "sources" not in data:
                        data["sources"] = []
                    for q in flik_data["source"]["qualities"]:
                        data["sources"].append({
                            "quality": f"{q.get('quality', 'Auto')} (Flikhub)",
                            "url": q.get("url", ""),
                            "type": q.get("type", "mp4"),
                            "source": "play"
                        })
                    if data["sources"]:
                        if "meta" not in data:
                            data["meta"] = {}
                        data["meta"]["titleMatched"] = True
                        data["meta"]["yearMismatch"] = False
                        if "meta" in flik_data:
                            fm = flik_data["meta"]
                            data["meta"]["title"] = fm.get("title", data["meta"].get("title"))
                            data["meta"]["imdbId"] = fm.get("imdb_id", data["meta"].get("imdbId"))
                            if fm.get("release_date"):
                                data["meta"]["year"] = fm["release_date"].split("-")[0]
                            if fm.get("poster_path"):
                                data["meta"]["cover"] = f"https://image.tmdb.org/t/p/w500{fm['poster_path']}"
                    if "subtitles" in flik_data:
                        if "subtitles" not in data:
                            data["subtitles"] = []
                        for sub in flik_data["subtitles"]:
                            data["subtitles"].append({
                                "lang": sub.get("lang") or sub.get("language") or sub.get("label") or "Unknown",
                                "language": sub.get("language") or sub.get("label") or "un",
                                "url": sub.get("url") or sub.get("file") or ""
                            })
        except Exception as exc:
            logger.debug("flikhub proxy error: %s", exc)

    return provider.name, data
