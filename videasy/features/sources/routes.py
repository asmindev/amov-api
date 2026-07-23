from __future__ import annotations

from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from videasy.features.sources.providers import AVAILABLE, PROVIDER_MAP
from videasy.features.sources.service import fetch_sources
from videasy.models.common import ErrorDetail
from videasy.models.media import (
    EpisodeInfo,
    MediaMeta,
    MediaSourceItem,
    MediaSubtitleItem,
    UnifiedMediaResponse,
)
from videasy.models.source import SourceParams

router = APIRouter(tags=["Sources"])


@router.get(
    "/sources",
    response_model=UnifiedMediaResponse,
    responses={
        400: {"model": ErrorDetail, "description": "Invalid parameters or unknown provider"},
        429: {"model": ErrorDetail, "description": "Rate limited by upstream API"},
        502: {"model": ErrorDetail, "description": "Upstream API failure"},
        504: {"description": "Upstream request timed out"},
    },
    summary="Fetch decrypted sources",
    description="Get decrypted HLS streams + subtitles for a movie or TV show.",
)
async def get_sources(
    request: Request,
    title: str = Query(default="", description="Media title (e.g. Interstellar)"),
    mediaType: str = Query(..., pattern=r"^(movie|tv)$", description="Media type: movie or tv"),
    tmdbId: str = Query(..., pattern=r"^\d+$", description="TMDB numerical ID"),
    provider: str = Query(..., min_length=1, description="Provider name: Yoru, Neon, Cypher, or Breach"),
    year: str = Query(default="", pattern=r"^\d{4}$|^$", description="Release year (optional)"),
    episodeId: str = Query(default="1", pattern=r"^\d+$", description="Episode number — TV only (default: 1)"),
    seasonId: str = Query(default="1", pattern=r"^\d+$", description="Season number — TV only (default: 1)"),
    imdbId: str = Query(default="", pattern=r"^tt\d+$|^$", description="IMDB ID (e.g. tt0816692, optional)"),
) -> UnifiedMediaResponse | JSONResponse:
    prov = PROVIDER_MAP.get(provider.lower())
    if not prov:
        available = ", ".join(p.name for p in AVAILABLE)
        return JSONResponse(
            status_code=400,
            content=ErrorDetail(error="unknown_provider", detail=f"Unknown provider '{provider}'. Available: {available}").model_dump(),
        )

    params = SourceParams(
        title=title, mediaType=mediaType, tmdbId=tmdbId, provider=provider,
        year=year, episodeId=episodeId, seasonId=seasonId, imdbId=imdbId,
    )

    try:
        provider_name, raw = await fetch_sources(
            client=request.app.state.client,
            cache=request.app.state.cache,
            provider=prov,
            params=params,
        )
    except HTTPException:
        raise
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail=f"{prov.name}: upstream request timed out")
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=502, detail=f"{prov.name}: upstream HTTP {e.response.status_code}")
    except (httpx.RequestError, ConnectionError) as e:
        raise HTTPException(status_code=502, detail=f"{prov.name}: connection error — {e}")

    sources_list = [
        MediaSourceItem(
            quality=s.get("quality", "Auto"),
            url=s.get("url", ""),
            type="hls" if ".m3u8" in s.get("url", "").lower() else "mp4",
            headers=s.get("headers"),
            source="play",
        )
        for s in raw.get("sources", [])
    ]

    subtitles_list = [
        MediaSubtitleItem(
            lang=(sub.get("lang") or "en")[:2].lower(),
            language=f"{provider_name} - {sub.get('language') or 'Subtitle'}",
            url=sub.get("url", ""),
        )
        for sub in raw.get("subtitles", [])
    ]

    episode_info = (
        EpisodeInfo(
            season=int(seasonId) if seasonId.isdigit() else 1,
            episode=int(episodeId) if episodeId.isdigit() else 1,
        )
        if mediaType == "tv"
        else None
    )

    meta = MediaMeta(
        title=title,
        provider=provider_name,
        mediaType=mediaType,
        tmdbId=tmdbId or None,
        imdbId=imdbId or None,
        year=year or None,
        cover=None,
    )

    return UnifiedMediaResponse(
        meta=meta,
        episode=episode_info,
        sources=sources_list,
        subtitles=subtitles_list,
    )
