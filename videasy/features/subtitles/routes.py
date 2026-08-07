from __future__ import annotations

from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Path, Query, Request

from videasy.features.sources.providers import AVAILABLE, PROVIDER_MAP
from videasy.features.sources.service import fetch_sources
from videasy.integrations.wyzie import fetch_wyzie, fetch_wyzie_grouped
from videasy.integrations.subsource import fetch_subsource_grouped, extract_subsource_vtt
from videasy.models.source import DecryptedData, SourceParams
from videasy.config import settings
from fastapi.responses import PlainTextResponse

router = APIRouter(tags=["Subtitles"])


@router.get(
    "/subtitles",
    summary="List available subtitle sources",
)
async def list_subtitle_sources() -> dict[str, list[str]]:
    providers = [p.name.lower() for p in AVAILABLE]
    providers.append("wyzie")
    providers.append("subsource")
    return {"sources": providers}


@router.get(
    "/subtitles/{provider_name}",
    summary="Fetch subtitles from a specific provider",
    description="Fetch subtitles from video providers or Wyzie.",
)
async def get_provider_subtitles(
    request: Request,
    provider_name: str = Path(..., description="Provider name (e.g. yoru, neon, wyzie)"),
    title: str = Query(default="", description="Media title (e.g. Interstellar)"),
    mediaType: str = Query(default="movie", pattern=r"^(movie|tv)$", description="Media type"),
    tmdbId: str = Query(default="", pattern=r"^\d*$", description="TMDB numerical ID"),
    year: str = Query(default="", pattern=r"^\d{4}$|^$", description="Release year (optional)"),
    episodeId: str = Query(default="1", pattern=r"^\d+$", description="Episode number"),
    seasonId: str = Query(default="1", pattern=r"^\d+$", description="Season number"),
    imdbId: str = Query(default="", pattern=r"^tt\d+$|^$", description="IMDB ID"),
) -> dict[str, Any]:
    provider_lower = provider_name.lower()

    if provider_lower == "wyzie":
        if not tmdbId and not imdbId:
            raise HTTPException(status_code=400, detail="tmdbId or imdbId is required for Wyzie")
        season_num = int(seasonId) if seasonId else None
        episode_num = int(episodeId) if episodeId else None
        groups = await fetch_wyzie_grouped(
            request.app.state.api_client,
            tmdb_id=tmdbId,
            imdb_id=imdbId,
            season=season_num if mediaType == "tv" else None,
            episode=episode_num if mediaType == "tv" else None,
        )
        return {"subtitles": [g.model_dump() for g in groups]}

    if provider_lower == "subsource":
        missing = [p for p in ("title", "year", "tmdbId", "imdbId") if not locals().get(p)]
        if missing:
            raise HTTPException(status_code=400, detail=f"Missing required SubSource parameter(s): {', '.join(missing)}")
        if not settings.subsource_api_key:
            raise HTTPException(status_code=400, detail="SubSource API key not configured. Set VIDEASY_SUBSOURCE_API_KEY")
        season_num = int(seasonId) if seasonId else None
        episode_num = int(episodeId) if episodeId else None
        groups = await fetch_subsource_grouped(
            request.app.state.api_client,
            settings.subsource_api_key,
            title=title,
            year=year,
            media_type=mediaType,
            imdb_id=imdbId,
            tmdb_id=tmdbId,
            season=season_num if mediaType == "tv" else None,
            episode=episode_num if mediaType == "tv" else None,
        )
        return {"subtitles": [g.model_dump() for g in groups]}

    prov = PROVIDER_MAP.get(provider_lower)
    if not prov:
        available = ", ".join([p.name.lower() for p in AVAILABLE] + ["wyzie"])
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
    "/wyzie",
    summary="Fetch Wyzie subtitles",
    description="Fetch subtitles from Wyzie Subs grouped by language.",
)
async def get_wyzie(
    request: Request,
    tmdbId: str = Query(default="", description="TMDB numerical ID"),
    imdbId: str = Query(default="", description="IMDB ID (e.g. tt1234567)"),
    language: str = Query(default="", description="Language filter (ISO 639-1, comma-separated)"),
    season: int | None = Query(default=None, description="Season number (for TV)"),
    episode: int | None = Query(default=None, description="Episode number (for TV)"),
) -> list[dict[str, Any]]:
    if not tmdbId and not imdbId:
        raise HTTPException(status_code=400, detail="tmdbId or imdbId is required")
    
    # 1. Fetch Wyzie
    groups = await fetch_wyzie_grouped(
        request.app.state.api_client,
        tmdb_id=tmdbId,
        imdb_id=imdbId,
        language=language,
        season=season,
        episode=episode,
    )
            
    return [g.model_dump() for g in groups]

@router.get(
    "/subsource",
    summary="Fetch SubSource subtitles",
    description="Fetch subtitles from SubSource via the official REST API, grouped by language.",
)
async def get_subsource(
    request: Request,
    title: str = Query(..., description="Media title (e.g. Interstellar)"),
    year: str = Query(..., pattern=r"^\d{4}$", description="Release year (e.g. 2014)"),
    tmdbId: str = Query(..., pattern=r"^\d+$", description="TMDB numerical ID"),
    imdbId: str = Query(..., pattern=r"^tt\d+$", description="IMDB ID (e.g. tt0816692)"),
    season: int | None = Query(default=None, description="Season number (for TV)"),
    episode: int | None = Query(default=None, description="Episode number (for TV, used to pick the right SRT from season packs)"),
    language: str = Query(default="", description="Language filter (e.g. indonesian, english)"),
) -> list[dict[str, Any]]:
    if not settings.subsource_api_key:
        raise HTTPException(status_code=400, detail="SubSource API key not configured. Set VIDEASY_SUBSOURCE_API_KEY")
    try:
        groups = await fetch_subsource_grouped(
            request.app.state.api_client,
            settings.subsource_api_key,
            title=title,
            year=year,
            media_type="tv" if season else "movie",
            imdb_id=imdbId,
            tmdb_id=tmdbId,
            language=language,
            season=season,
            episode=episode,
        )
        return [g.model_dump() for g in groups]
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"SubSource subtitle fetch error: {str(e)}")

@router.get(
    "/subsource/download",
    summary="Download SubSource subtitle and convert to VTT",
    description="Downloads a SubSource subtitle by its subtitleId via the REST API and returns VTT text.",
)
async def download_subsource(
    request: Request,
    subtitleId: int = Query(..., description="SubSource subtitleId (from /subsource response)"),
    episode: int | None = Query(default=None, description="Episode number — selects the right SRT from season packs"),
) -> PlainTextResponse:
    if not settings.subsource_api_key:
        raise HTTPException(status_code=400, detail="SubSource API key not configured. Set VIDEASY_SUBSOURCE_API_KEY")
    vtt_content = await extract_subsource_vtt(
        request.app.state.api_client, settings.subsource_api_key, subtitleId, episode
    )
    if not vtt_content:
        raise HTTPException(status_code=404, detail="Failed to download or extract SubSource subtitle")
    return PlainTextResponse(vtt_content, media_type="text/vtt")
