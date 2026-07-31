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
    """
    return httpx.AsyncClient(
        http2=True,
        timeout=httpx.Timeout(connect=6.0, read=settings.request_timeout, write=6.0, pool=6.0),
        headers=build_default_headers(),
        limits=httpx.Limits(
            max_connections=20,
            max_keepalive_connections=10,
            keepalive_expiry=10,
        ),
    )


def create_proxy_client() -> httpx.AsyncClient:
    """Client for proxy streaming (HLS, MP4, DASH segments).

    Uses HTTP/2 for multiplexed CDN connections (matching browser behavior for Aliyun Tengine CDN).
    No read timeout — streams must run for as long as the video plays.
    Connect timeout is 5s so dead/stalled CDNs fail fast.
    """
    return httpx.AsyncClient(
        http2=False,
        timeout=httpx.Timeout(connect=5.0, read=None, write=None, pool=5.0),
        headers=build_default_headers(),
        limits=httpx.Limits(
            max_connections=40,
            max_keepalive_connections=10,
            keepalive_expiry=20,
        ),
    )
