from __future__ import annotations

from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Query, Request

from videasy.models.media import (
    EpisodeInfo,
    MediaMeta,
    MediaSourceItem,
    MediaSubtitleItem,
    UnifiedMediaResponse,
)

router = APIRouter(tags=["Moviebox"])


@router.get(
    "/moviebox/sources",
    response_model=UnifiedMediaResponse,
    summary="Fetch sources from Moviebox",
    description="Fetch video sources from themoviebox.xyz. Provide subjectId, imdbId, a full URL, or a slug.",
)
async def moviebox_sources(
    request: Request,
    subjectId: str = Query(default="", min_length=0, description="Moviebox subject ID (numeric)"),
    imdbId: str = Query(default="", pattern=r"^tt\d+$|^$", description="IMDB ID (e.g. tt9018736)"),
    originalTitle: str = Query(default="", min_length=0, description="Original title (required when using imdbId)"),
    englishTitle: str = Query(default="", min_length=0, description="English title (optional; used with originalTitle for title matching)"),
    mediaType: str = Query(default="movie", pattern=r"^(movie|tv)$", description="Media type: movie or tv"),
    year: str = Query(default="", pattern=r"^\d{4}$|^$", description="Release year (e.g. 2024)"),
    url: str = Query(default="", min_length=0, description="Full themoviebox.xyz URL"),
    seasonId: str = Query(default="0", pattern=r"^\d+$", description="Season number (TV only, default: 0)"),
    episodeId: str = Query(default="0", pattern=r"^\d+$", description="Episode number (TV only, default: 0)"),
    cookie: str = Query(default="", description="Optional user Cookie header"),
) -> UnifiedMediaResponse:
    from videasy.features.moviebox.service import fetch_sources as mb_fetch

    if not subjectId and not url and not imdbId:
        raise HTTPException(status_code=400, detail="Provide subjectId, imdbId, or url")
    if imdbId and not originalTitle.strip():
        raise HTTPException(status_code=400, detail="originalTitle is required when using imdbId")
    input_str = subjectId or imdbId or url
    if not input_str.strip():
        raise HTTPException(status_code=400, detail="Empty input")
    try:
        data = await mb_fetch(
            request.app.state.api_client, input_str,
            se=int(seasonId), ep=int(episodeId), cookie=cookie,
            original_title=originalTitle.strip(),
            english_title=englishTitle.strip(),
            media_type=mediaType,
            year=year.strip(),
        )
        subject_type = data.get("subjectType", 1)
        media_type = "tv" if subject_type == 2 else "movie"

        from videasy.utils.quality import parse_quality_and_size

        sources_list = []
        for s in data.get("sources", []):
            q, sz = parse_quality_and_size(s.get("quality", "Auto"))
            sources_list.append(
                MediaSourceItem(
                    quality=q,
                    size=sz or s.get("size"),
                    url=s.get("url", ""),
                    type=s.get("type", "mp4"),
                    headers=s.get("headers"),
                    source=s.get("source", "play"),
                )
            )

        subtitles_list = [
            MediaSubtitleItem(
                lang=(sub.get("lang") or "en")[:2].lower(),
                language=sub.get("language") or "Subtitle",
                url=sub.get("url", ""),
            )
            for sub in data.get("subtitles", [])
            if sub.get("url")
        ]

        se_num = int(seasonId) if seasonId.isdigit() and int(seasonId) > 0 else (1 if media_type == "tv" else None)
        ep_num = int(episodeId) if episodeId.isdigit() and int(episodeId) > 0 else (1 if media_type == "tv" else None)

        episode_info = EpisodeInfo(season=se_num, episode=ep_num) if media_type == "tv" else None

        meta = MediaMeta(
            title=data.get("title", ""),
            provider="Moviebox",
            mediaType=media_type,
            tmdbId=None,
            imdbId=data.get("imdbId") or None,
            year=data.get("year") or None,
            cover=data.get("cover") or None,
            requestedTitle=data.get("requestedTitle"),
            titleMatched=data.get("titleMatched"),
            yearMismatch=data.get("yearMismatch"),
        )

        if not sources_list:
            raise HTTPException(status_code=404, detail="Moviebox streams not found")

        return UnifiedMediaResponse(
            meta=meta,
            episode=episode_info,
            sources=sources_list,
            subtitles=subtitles_list,
        )
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Moviebox API request timed out")
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=502, detail=f"Moviebox API HTTP {e.response.status_code}")
    except (httpx.RequestError, ConnectionError) as e:
        raise HTTPException(status_code=502, detail=f"Moviebox connection error — {e}")
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get(
    "/moviebox/search",
    summary="Search Moviebox titles",
    description="Search for movies and TV shows on themoviebox.xyz by keyword.",
)
async def moviebox_search_endpoint(
    request: Request,
    q: str = Query(..., min_length=1, description="Search keyword"),
    page: int = Query(default=1, ge=1, description="Page number"),
) -> dict[str, Any]:
    from videasy.features.moviebox.service import search_titles as mb_search

    try:
        results = await mb_search(request.app.state.api_client, q, page=page)
        return {"query": q, "page": page, "status": "ok", "results": results}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Moviebox search error: {str(e)}")
