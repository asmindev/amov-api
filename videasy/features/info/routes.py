from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from videasy.features.sources.providers import AVAILABLE
from videasy.models.common import ProviderInfo, ProviderList

router = APIRouter(tags=["Info"])


@router.get(
    "/providers",
    response_model=ProviderList,
    summary="List providers",
    description="Returns all active providers with their endpoint slugs.",
)
async def list_providers() -> ProviderList:
    return ProviderList(
        providers=[ProviderInfo(name=p.name, endpoint=p.endpoint) for p in AVAILABLE]
    )


@router.get(
    "/health",
    summary="Health check",
    description="Returns service status and version.",
)
async def health() -> dict[str, Any]:
    return {"status": "ok", "version": "3.0.0"}
