from __future__ import annotations

import json
import re

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from starlette.responses import Response

from videasy.proxy.middleware import encode_dash_token
from videasy.proxy.stream import do_proxy_stream

router = APIRouter(tags=["Proxy"])


@router.get(
    "/proxy",
    summary="HLS and MP4 stream proxy",
    description="Proxy HLS manifests, MP4 videos, and video segments with Range request forwarding, Sec-Fetch-Dest headers, CORS bypass, and auto-refresh for expired CDN tokens (t=).",
    response_class=StreamingResponse,
)
async def proxy_hls(
    request: Request,
    url: str = Query(..., description="Full URL of the resource to proxy"),
    headers: str = Query(default="", description="Optional custom JSON headers to forward"),
    imdbId: str | None = Query(default=None, description="Optional IMDb ID for automatic token refresh if CDN URL expires"),
    subjectId: str | None = Query(default=None, description="Optional Moviebox Subject ID for automatic token refresh if CDN URL expires"),
    season: int | None = Query(default=None, description="Optional Season number"),
    episode: int | None = Query(default=None, description="Optional Episode number"),
) -> Response:
    """Stream any URL through the backend with Range support and domain-specific headers."""
    client: httpx.AsyncClient = request.app.state.proxy_client
    u_lower = url.lower()

    # Build request headers
    from videasy.proxy.headers import get_domain_headers
    from videasy.core.http_client import build_default_headers

    proxy_headers = get_domain_headers(url) or build_default_headers()

    range_header = request.headers.get("range")
    if range_header:
        proxy_headers["Range"] = range_header

    if headers.strip():
        try:
            extra = json.loads(headers)
            if isinstance(extra, dict):
                proxy_headers.update(extra)
        except Exception:
            pass

    req = client.build_request("GET", url, headers=proxy_headers)
    try:
        resp = await client.send(req, stream=True, follow_redirects=True)
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Upstream timeout connecting to stream")
    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=f"Upstream connection error: {e}")

    if resp.status_code == 403:
        await resp.aclose()
        raise HTTPException(status_code=403, detail=f"CDN rejected request for: {url}")
    if resp.status_code == 404:
        await resp.aclose()
        raise HTTPException(status_code=404, detail=f"Resource not found: {url}")

    # ── DASH MPD: rewrite init/media URLs to /dash/{token}/... (stateless) ──
    if ".mpd" in u_lower or "application/dash+xml" in resp.headers.get("content-type", ""):
        raw_xml = (await resp.aread()).decode("utf-8", errors="ignore")
        await resp.aclose()

        base_dir = url.rsplit("/", 1)[0] + "/"
        token = encode_dash_token(base_dir, headers.strip())

        def _rewrite_url(m: re.Match) -> str:
            attr_val = m.group(1)
            if attr_val.startswith(("http://", "https://")):
                return m.group(0)
            # Convert relative path to /dash/{token}/...
            clean = attr_val.lstrip("/")
            return f'{m.group(0).split("=")[0]}="/dash/{token}/{clean}"'

        rewritten = re.sub(r'(?:initialization|media)=["\']([^"\']+)["\']', _rewrite_url, raw_xml)

        async def xml_gen():
            yield rewritten.encode("utf-8")

        return StreamingResponse(
            xml_gen(),
            status_code=200,
            media_type="application/dash+xml",
            headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Headers": "*",
                "Cache-Control": "no-cache",
            },
        )

    # ── HLS M3U8: rewrite relative segment/variant URLs to absolute ──
    is_m3u8 = ".m3u8" in u_lower or "application/vnd.apple.mpegurl" in resp.headers.get("content-type", "")
    if is_m3u8:
        raw = (await resp.aread()).decode("utf-8", errors="ignore")
        await resp.aclose()

        base_url = url.rsplit("/", 1)[0] + "/"

        rewritten_lines = []
        for line in raw.splitlines(keepends=True):
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and not stripped.startswith(("http://", "https://")):
                line = line.replace(stripped, base_url + stripped.lstrip("/"))
            rewritten_lines.append(line)

        async def m3u8_gen():
            yield "".join(rewritten_lines).encode("utf-8")

        return StreamingResponse(
            m3u8_gen(),
            status_code=200,
            media_type="application/vnd.apple.mpegurl",
            headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Headers": "*",
                "Cache-Control": "no-cache",
            },
        )

    # ── Non-MPD: stream directly ──
    return await do_proxy_stream(
        request,
        url,
        headers,
        imdb_id=imdbId,
        subject_id=subjectId,
        season=season,
        episode=episode,
    )
