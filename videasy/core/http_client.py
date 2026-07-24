from __future__ import annotations

import httpx

from videasy.config import settings


def build_default_headers() -> dict[str, str]:
    return {
        "Accept": "*/*",
        "User-Agent": settings.user_agent,
        "Referer": settings.referer,
        "Origin": settings.origin,
    }


def create_api_client() -> httpx.AsyncClient:
    """Client for API calls (sources, moviebox, subtitles, decryption).

    Has a read timeout to prevent zombie connections when upstreams stall.
    """
    return httpx.AsyncClient(
        timeout=httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0),
        headers=build_default_headers(),
        limits=httpx.Limits(
            max_connections=20,
            max_keepalive_connections=10,
            keepalive_expiry=10,
        ),
    )


def create_proxy_client() -> httpx.AsyncClient:
    """Client for proxy streaming (HLS, MP4, DASH segments).

    No read timeout — streams must run for as long as the video plays.
    Connect timeout is 5s so dead/stalled CDNs fail fast.
    """
    return httpx.AsyncClient(
        timeout=httpx.Timeout(connect=5.0, read=None, write=10.0, pool=10.0),
        headers=build_default_headers(),
        limits=httpx.Limits(
            max_connections=100,
            max_keepalive_connections=20,
            keepalive_expiry=30,
        ),
    )
