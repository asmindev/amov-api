from __future__ import annotations

import json
import logging

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import StreamingResponse

from videasy.core.http_client import build_default_headers
from videasy.proxy.headers import get_domain_headers

logger = logging.getLogger("videasy")


async def do_proxy_stream(
    request: Request,
    target_url: str,
    extra_headers_json: str = "",
) -> StreamingResponse:
    """Core proxy logic shared by /proxy and DASH segment middleware."""
    proxy_headers = get_domain_headers(target_url) or build_default_headers()

    u_lower = target_url.lower()
    is_subtitle_or_text = any(
        ext in u_lower for ext in (".srt", ".vtt", ".txt", ".json", ".xml", "subtitle", "subt")
    )

    range_header = request.headers.get("range")
    if range_header and not is_subtitle_or_text:
        proxy_headers["Range"] = range_header

    if extra_headers_json.strip():
        try:
            extra = json.loads(extra_headers_json)
            if isinstance(extra, dict):
                proxy_headers.update(extra)
        except Exception:
            pass

    client: httpx.AsyncClient = request.app.state.client
    req = client.build_request("GET", target_url, headers=proxy_headers)
    try:
        resp = await client.send(req, stream=True, follow_redirects=True)
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Upstream timeout connecting to stream")
    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=f"Upstream connection error: {e}")

    # Fallback: if upstream CDN rejected Range header for subtitles/text files with 416
    if resp.status_code == 416 and "Range" in proxy_headers:
        await resp.aclose()
        proxy_headers.pop("Range", None)
        req = client.build_request("GET", target_url, headers=proxy_headers)
        try:
            resp = await client.send(req, stream=True, follow_redirects=True)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Upstream error retry: {e}")

    if resp.status_code == 403:
        await resp.aclose()
        raise HTTPException(status_code=403, detail=f"CDN rejected request for: {target_url}")
    if resp.status_code == 404:
        await resp.aclose()
        raise HTTPException(status_code=404, detail=f"Resource not found: {target_url}")

    async def stream_generator():
        try:
            async for chunk in resp.aiter_bytes(chunk_size=64 * 1024):
                yield chunk
        except (httpx.TimeoutException, httpx.RequestError, Exception) as e:
            logger.debug("Proxy stream ended for %s: %s", target_url, e)
        finally:
            await resp.aclose()

    content_type = resp.headers.get("content-type", "application/octet-stream")
    res_headers: dict[str, str] = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Headers": "*",
        "Accept-Ranges": "bytes",
        "Cache-Control": "public, max-age=3600",
    }
    # NEVER set Content-Length on StreamingResponse to prevent Uvicorn "Response content shorter than Content-Length"
    # when chunked streaming ends early, is paused, or is cancelled by client.
    if resp.headers.get("content-range"):
        res_headers["Content-Range"] = resp.headers["content-range"]
    if resp.headers.get("content-disposition"):
        res_headers["Content-Disposition"] = resp.headers["content-disposition"]

    return StreamingResponse(
        stream_generator(),
        status_code=resp.status_code,
        media_type=content_type,
        headers=res_headers,
    )
