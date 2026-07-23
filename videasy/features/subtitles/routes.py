from __future__ import annotations

from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Path, Query, Request

from videasy.features.sources.providers import AVAILABLE, PROVIDER_MAP
from videasy.features.sources.service import fetch_sources
from videasy.integrations.opensubtitles import fetch_opensubtitles
from videasy.models.source import DecryptedData, SourceParams

router = APIRouter(tags=["Subtitles"])


@router.get(
    "/subtitles",
    summary="List available subtitle sources",
)
async def list_subtitle_sources() -> dict[str, list[str]]:
    providers = [p.name.lower() for p in AVAILABLE]
    providers.append("opensubtitles")
    return {"sources": providers}


@router.get(
    "/subtitles/{provider_name}",
    summary="Fetch subtitles from a specific provider",
    description="Fetch subtitles from video providers or OpenSubtitles.",
)
async def get_provider_subtitles(
    request: Request,
    provider_name: str = Path(..., description="Provider name (e.g. yoru, neon, opensubtitles)"),
    title: str = Query(default="", description="Media title (e.g. Interstellar)"),
    mediaType: str = Query(default="movie", pattern=r"^(movie|tv)$", description="Media type"),
    tmdbId: str = Query(default="", pattern=r"^\d*$", description="TMDB numerical ID"),
    year: str = Query(default="", pattern=r"^\d{4}$|^$", description="Release year (optional)"),
    episodeId: str = Query(default="1", pattern=r"^\d+$", description="Episode number"),
    seasonId: str = Query(default="1", pattern=r"^\d+$", description="Season number"),
    imdbId: str = Query(default="", pattern=r"^tt\d+$|^$", description="IMDB ID"),
) -> dict[str, Any]:
    provider_lower = provider_name.lower()

    if provider_lower == "opensubtitles":
        if not imdbId:
            raise HTTPException(status_code=400, detail="imdbId is required for OpenSubtitles")
        subs = await fetch_opensubtitles(request.app.state.api_client, imdbId)
        return {"subtitles": [sub.model_dump() for sub in subs]}

    prov = PROVIDER_MAP.get(provider_lower)
    if not prov:
        available = ", ".join([p.name.lower() for p in AVAILABLE] + ["opensubtitles"])
        raise HTTPException(status_code=400, detail=f"Unknown subtitle provider '{provider_name}'. Available: {available}")

    if not title or not tmdbId:
        raise HTTPException(status_code=400, detail="title and tmdbId are required for video providers")

    params = SourceParams(
        title=title, mediaType=mediaType, tmdbId=tmdbId, provider=prov.name,
        year=year, episodeId=episodeId, seasonId=seasonId, imdbId=imdbId,
    )

    try:
        _, raw = await fetch_sources(request.app.state.api_client, request.app.state.cache, prov, params)
        decrypted_data = DecryptedData.from_raw(raw)
        subs = decrypted_data.subtitles
        for sub in subs:
            sub.language = f"{prov.name} - {sub.language}"
        return {"subtitles": [sub.model_dump() for sub in subs]}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"{prov.name} subtitle fetch error: {str(e)}")


@router.get(
    "/opensubtitles",
    summary="Fetch OpenSubtitles manually",
    description="Fetch subtitles from OpenSubtitles using an IMDB ID.",
)
async def get_opensubtitles(
    request: Request,
    imdbId: str = Query(..., description="IMDB ID (e.g. tt1234567)"),
) -> list[dict[str, Any]]:
    from videasy.models.subtitle import SubtitleItem
    subs = await fetch_opensubtitles(request.app.state.api_client, imdbId)
    return [sub.model_dump() for sub in subs]
