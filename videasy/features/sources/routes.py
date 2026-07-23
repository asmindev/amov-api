from __future__ import annotations

from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from videasy.features.sources.providers import AVAILABLE, PROVIDER_MAP
from videasy.features.sources.service import fetch_sources
from videasy.models.common import ErrorDetail
from videasy.models.source import DecryptedData, SourceParams, SourceResponse

router = APIRouter(tags=["Sources"])


@router.get(
    "/sources",
    response_model=SourceResponse,
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
    title: str = Query(..., min_length=1, description="Media title (e.g. Interstellar)"),
    mediaType: str = Query(..., pattern=r"^(movie|tv)$", description="Media type: movie or tv"),
    tmdbId: str = Query(..., pattern=r"^\d+$", description="TMDB numerical ID"),
    provider: str = Query(..., min_length=1, description="Provider name: Yoru, Neon, Cypher, or Breach"),
    year: str = Query(default="", pattern=r"^\d{4}$|^$", description="Release year (optional)"),
    episodeId: str = Query(default="1", pattern=r"^\d+$", description="Episode number — TV only (default: 1)"),
    seasonId: str = Query(default="1", pattern=r"^\d+$", description="Season number — TV only (default: 1)"),
    imdbId: str = Query(default="", pattern=r"^tt\d+$|^$", description="IMDB ID (e.g. tt0816692, optional)"),
) -> SourceResponse | JSONResponse:
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

    decrypted_data = DecryptedData.from_raw(raw)
    for sub in decrypted_data.subtitles:
        sub.language = f"{provider_name} - {sub.language}"

    return SourceResponse(tmdbId=tmdbId, provider=provider_name, data=decrypted_data)
