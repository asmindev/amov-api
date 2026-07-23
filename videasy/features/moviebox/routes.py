from __future__ import annotations

from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Query, Request

router = APIRouter(tags=["Moviebox"])


@router.get(
    "/moviebox/sources",
    summary="Fetch sources from Moviebox",
    description="Fetch video sources from themoviebox.xyz. Provide subjectId, a full URL, or a slug.",
)
async def moviebox_sources(
    request: Request,
    subjectId: str = Query(default="", min_length=0, description="Moviebox subject ID (numeric)"),
    url: str = Query(default="", min_length=0, description="Full themoviebox.xyz URL"),
    seasonId: str = Query(default="0", pattern=r"^\d+$", description="Season number (TV only, default: 0)"),
    episodeId: str = Query(default="0", pattern=r"^\d+$", description="Episode number (TV only, default: 0)"),
    cookie: str = Query(default="", description="Optional user Cookie header"),
) -> dict[str, Any]:
    from videasy.features.moviebox.service import fetch_sources as mb_fetch

    if not subjectId and not url:
        raise HTTPException(status_code=400, detail="Provide either subjectId or url")
    input_str = subjectId or url
    if not input_str.strip():
        raise HTTPException(status_code=400, detail="Empty input")
    try:
        data = await mb_fetch(
            request.app.state.client, input_str,
            se=int(seasonId), ep=int(episodeId), cookie=cookie,
        )
        sid = data.get("subjectId", input_str)
        return {"subjectId": sid, "status": "ok", "data": data}
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
        results = await mb_search(request.app.state.client, q, page=page)
        return {"query": q, "page": page, "status": "ok", "results": results}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Moviebox search error: {str(e)}")
