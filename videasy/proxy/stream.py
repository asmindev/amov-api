from __future__ import annotations

import asyncio
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
    imdb_id: str | None = None,
    subject_id: str | None = None,
    season: int | None = None,
    episode: int | None = None,
) -> StreamingResponse:
    """Core proxy logic shared by /proxy and DASH segment middleware with auto-refresh for expired CDN tokens."""
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

    client: httpx.AsyncClient = request.app.state.proxy_client
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

    # Automatic Backend Token Refresh if CDN returns 403 or 401 due to expired t=
    if resp.status_code in (403, 401) and (imdb_id or subject_id):
        await resp.aclose()
        logger.warning(
            "CDN token expired (HTTP %s) for %s. Auto-refreshing via Moviebox API...",
            resp.status_code,
            target_url,
        )
        try:
            from videasy.features.moviebox.service import fetch_sources
            api_client = getattr(request.app.state, "api_client", client)
            res = await fetch_sources(
                api_client,
                imdb_id=imdb_id,
                subject_id=subject_id,
                season=season,
                episode=episode,
                force_refresh=True,
            )
            sources = res.get("sources", [])
            if sources:
                fresh_url = sources[0]["url"]
                logger.info("Successfully refreshed CDN URL: %s...", fresh_url[:80])
                proxy_headers = get_domain_headers(fresh_url) or build_default_headers()
                if range_header and not is_subtitle_or_text:
                    proxy_headers["Range"] = range_header
                req = client.build_request("GET", fresh_url, headers=proxy_headers)
                resp = await client.send(req, stream=True, follow_redirects=True)
        except Exception as refresh_err:
            logger.error("Failed backend CDN token refresh: %s", refresh_err)

    if resp.status_code == 403:
        await resp.aclose()
        raise HTTPException(status_code=403, detail=f"CDN rejected request for: {target_url}")
    if resp.status_code == 404:
        await resp.aclose()
        raise HTTPException(status_code=404, detail=f"Resource not found: {target_url}")

    # Idle timeout: if the upstream CDN sends nothing for this long (e.g. the
    # client paused or disconnected), close the stream instead of leaving a
    # pending ASGI task on Passenger shared hosting ("Task was destroyed but it
    # is pending!").
    IDLE_CHUNK_TIMEOUT = 60.0

    async def stream_generator():
        try:
            # Per-chunk idle timeout: a stalled CDN or a client that stopped
            # reading (paused / disconnected) will time out instead of leaving
            # a pending ASGI task that logs "Task was destroyed but it is
            # pending!" on Passenger shared hosting.
            while True:
                try:
                    chunk = await asyncio.wait_for(
                        resp.aiter_bytes(chunk_size=64 * 1024).__anext__(),
                        timeout=IDLE_CHUNK_TIMEOUT,
                    )
                except StopAsyncIteration:
                    break
                yield chunk
        except asyncio.CancelledError:
            logger.debug("Proxy stream cancelled during shutdown for %s", target_url)
        except (asyncio.TimeoutError, httpx.TimeoutException, httpx.RequestError, Exception) as e:
            logger.debug("Proxy stream ended for %s: %s", target_url, e)
        finally:
            try:
                await resp.aclose()
            except Exception:
                pass

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
