from fastapi import APIRouter, Depends, HTTPException
import httpx

from videasy.deps import get_http_client
from videasy.integrations.lk21 import fetch_lk21_source

router = APIRouter(prefix="/lk21", tags=["lk21"])

@router.get("/sources")
async def get_lk21_sources(
    title: str,
    year: str = "",
    client: httpx.AsyncClient = Depends(get_http_client),
):
    """Fetch movie sources from LK21 using title."""
    result = await fetch_lk21_source(client, title, year)
    if not result:
        raise HTTPException(status_code=404, detail="Movie not found on LK21")
    return result
