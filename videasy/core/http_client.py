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

    No read timeout — streams must run for as long as the video plays.
    Connect timeout is configurable (default 10s). If the CDN is reachable, the
    handshake takes <1s; a full connect timeout almost always means the CDN is
    dropping the host's egress IP (see VIDEASY_PROXY_OUTBOUND to route through a
    non-blocked proxy).
    """
    return httpx.AsyncClient(
        http2=False,
        proxy=settings.proxy_outbound or None,
        timeout=httpx.Timeout(
            connect=settings.proxy_connect_timeout,
            read=None,
            write=None,
            pool=settings.proxy_pool_timeout,
        ),
        headers=build_default_headers(),
        limits=httpx.Limits(
            max_connections=40,
            max_keepalive_connections=10,
            keepalive_expiry=20,
        ),
    )
