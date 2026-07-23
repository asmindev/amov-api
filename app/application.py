from __future__ import annotations

import json
import logging
import time
import re
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlencode, quote, unquote

import httpx
from fastapi import FastAPI, HTTPException, Query, Path, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from app.config import settings
from app.decrypt import decrypt as _decrypt
from app.models import DecryptedData, ErrorDetail, ProviderInfo, ProviderList, SourceParams, SourceResponse, SubtitleItem
from app.providers import AVAILABLE, PROVIDER_MAP, Provider
from app.opensubtitles import fetch_opensubtitles
from app.moviebox import fetch_sources as moviebox_fetch_sources, search_titles as moviebox_search

logger = logging.getLogger("videasy")

SeedCache = dict[str, tuple[str, float]]


class DASHSegmentMiddleware:
    """ASGI middleware that intercepts *.m4s requests that didn't match any route
    and proxies them through the stored DASH base URL + headers."""

    def __init__(self, asgi_app: ASGIApp) -> None:
        self.app = asgi_app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        path = scope.get("path", "")
        if not path.endswith(".m4s"):
            return await self.app(scope, receive, send)

        last_base = getattr(app.state, "last_dash_base", None)
        if not last_base:
            return await self.app(scope, receive, send)

        base_dir, headers_json = last_base
        file_name = path.lstrip("/")
        target_url = base_dir + file_name

        from starlette.requests import Request as StarletteRequest
        request = StarletteRequest(scope, receive, send)
        try:
            response = await _do_proxy_stream(request, target_url, headers_json)
        except HTTPException as exc:
            if exc.status_code == 404:
                return await self.app(scope, receive, send)
            raise

        await response(scope, receive, send)


def _build_headers() -> dict[str, str]:
    return {
        "Accept": "*/*",
        "User-Agent": settings.user_agent,
        "Referer": settings.referer,
        "Origin": settings.origin,
    }


def _get_seed(cache: SeedCache, tmdb_id: str) -> str | None:
    entry = cache.get(tmdb_id)
    if entry:
        seed, expiry = entry
        if time.monotonic() < expiry:
            return seed
    return None


def _set_seed(cache: SeedCache, tmdb_id: str, seed: str, ttl_ms: int) -> None:
    expiry = time.monotonic() + (ttl_ms - settings.cache_ttl_offset) / 1000
    cache[tmdb_id] = (seed, expiry)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("starting up — httpx client initialised")
    app.state.client = httpx.AsyncClient(
        timeout=httpx.Timeout(settings.request_timeout),
        headers=_build_headers(),
    )
    app.state.cache: SeedCache = {}
    yield
    await app.state.client.aclose()
    logger.info("shut down — client closed")


app = FastAPI(
    title="Videasy Decryptor API",
    description="Decrypt Videasy.net video streams. Fetch encrypted sources from `api.wingsdatabase.com`, decrypt via `enc-dec.app`, returns quality-labelled HLS streams + subtitles.",
    version="2.0.0",
    lifespan=lifespan,
    contact={"name": "Videasy Decryptor", "url": "https://github.com/"},
    license_info={"name": "MIT"},
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.add_middleware(DASHSegmentMiddleware)


@app.exception_handler(Exception)
async def global_exception(_request, exc: Exception) -> JSONResponse:
    logger.exception("unhandled exception")
    return JSONResponse(
        status_code=500,
        content=ErrorDetail(error="internal_error", detail=str(exc)).model_dump(),
    )


async def _fetch_seed(client: httpx.AsyncClient, cache: SeedCache, tmdb_id: str) -> str:
    cached = _get_seed(cache, tmdb_id)
    if cached:
        logger.debug("seed cache hit for tmdbId=%s", tmdb_id)
        return cached

    logger.debug("fetching seed for tmdbId=%s", tmdb_id)
    resp = await client.get(f"{settings.api_base}/seed?mediaId={tmdb_id}")
    if resp.status_code == 429:
        raise HTTPException(status_code=429, detail="rate limited by upstream API — try again later")
    resp.raise_for_status()
    data = resp.json()
    seed = data["seed"]
    ttl = data.get("ttlMs", 30_000)
    _set_seed(cache, tmdb_id, seed, ttl)
    return seed


async def _get_sources(
    client: httpx.AsyncClient,
    cache: SeedCache,
    provider: Provider,
    params: SourceParams,
) -> tuple[str, dict[str, Any]]:
    seed = await _fetch_seed(client, cache, params.tmdbId)
    enc_title = quote(quote(params.title, safe=""), safe="")

    qs = {
        "title": enc_title,
        "mediaType": params.mediaType,
        "year": params.year,
        "episodeId": params.episodeId,
        "seasonId": params.seasonId,
        "tmdbId": params.tmdbId,
        "imdbId": params.imdbId,
        "enc": "2",
        "seed": seed,
    }
    url = f"{settings.api_base}/{provider.endpoint}/sources-with-title?{urlencode(qs)}"

    logger.info("fetching sources: provider=%s tmdbId=%s", provider.name, params.tmdbId)
    cipher_resp = await client.get(url)
    if cipher_resp.status_code == 429:
        raise HTTPException(status_code=429, detail="rate limited by upstream API — try again later")
    if cipher_resp.status_code == 500:
        raise HTTPException(status_code=502, detail=f"{provider.name}: upstream returned 500")
    cipher_resp.raise_for_status()

    cipher = cipher_resp.text.strip()
    if not cipher:
        raise HTTPException(status_code=502, detail=f"{provider.name}: empty response from upstream")

    data = await _decrypt(client, cipher, params.tmdbId, seed)
    return provider.name, data


@app.get(
    "/sources",
    response_model=SourceResponse,
    responses={
        400: {"model": ErrorDetail, "description": "Invalid parameters or unknown provider"},
        429: {"model": ErrorDetail, "description": "Rate limited by upstream API"},
        502: {"model": ErrorDetail, "description": "Upstream API failure"},
        504: {"description": "Upstream request timed out"},
    },
    summary="Fetch decrypted sources",
    description="Get decrypted HLS streams + subtitles for a movie or TV show. Requires a TMDB ID and a provider name. Try each provider if one fails — availability varies per title.",
    tags=["Sources"],
)
async def get_sources(
    title: str = Query(..., min_length=1, description="Media title (e.g. Interstellar)"),
    mediaType: str = Query(..., pattern=r"^(movie|tv)$", description="Media type: movie or tv"),
    tmdbId: str = Query(..., pattern=r"^\d+$", description="TMDB numerical ID"),
    provider: str = Query(..., min_length=1, description="Provider name: Yoru, Neon, Cypher, or Breach"),
    year: str = Query(default="", pattern=r"^\d{4}$|^$", description="Release year (optional)"),
    episodeId: str = Query(default="1", pattern=r"^\d+$", description="Episode number — TV only (default: 1)"),
    seasonId: str = Query(default="1", pattern=r"^\d+$", description="Season number — TV only (default: 1)"),
    imdbId: str = Query(default="", pattern=r"^tt\d+$|^$", description="IMDB ID (e.g. tt0816692, optional)"),
) -> SourceResponse | JSONResponse:
    prov = PROVIDER_MAP.get(provider.lower())
    if not prov:
        available = ", ".join(p.name for p in AVAILABLE)
        return JSONResponse(
            status_code=400,
            content=ErrorDetail(error="unknown_provider", detail=f"Unknown provider '{provider}'. Available: {available}").model_dump(),
        )

    params = SourceParams(
        title=title,
        mediaType=mediaType,
        tmdbId=tmdbId,
        provider=provider,
        year=year,
        episodeId=episodeId,
        seasonId=seasonId,
        imdbId=imdbId,
    )

    try:
        provider_name, raw = await _get_sources(
            client=app.state.client,
            cache=app.state.cache,
            provider=prov,
            params=params,
        )
    except HTTPException:
        raise
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail=f"{prov.name}: upstream request timed out")
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=502, detail=f"{prov.name}: upstream HTTP {e.response.status_code}")
    except (httpx.RequestError, ConnectionError) as e:
        raise HTTPException(status_code=502, detail=f"{prov.name}: connection error — {e}")
    decrypted_data = DecryptedData.from_raw(raw)
    
    for sub in decrypted_data.subtitles:
        # Menambahkan label nama provider pada subtitle bawaan
        sub.language = f"{provider_name} - {sub.language}"
    
    return SourceResponse(
        tmdbId=tmdbId,
        provider=provider_name,
        data=decrypted_data,
    )


@app.get(
    "/moviebox/sources",
    summary="Fetch sources from Moviebox",
    description="Fetch video sources from themoviebox.xyz. Provide subjectId (numeric), a full themoviebox.xyz URL, or a slug.",
    tags=["Moviebox"],
)
async def moviebox_sources(
    subjectId: str = Query(default="", min_length=0, description="Moviebox subject ID (numeric)"),
    url: str = Query(default="", min_length=0, description="Full themoviebox.xyz URL (alternative to subjectId)"),
    seasonId: str = Query(default="0", pattern=r"^\d+$", description="Season number (TV only, default: 0)"),
    episodeId: str = Query(default="0", pattern=r"^\d+$", description="Episode number (TV only, default: 0)"),
    cookie: str = Query(default="", description="Optional user Cookie header (e.g. mb_token=...; token=...)"),
) -> dict[str, Any]:
    if not subjectId and not url:
        raise HTTPException(status_code=400, detail="Provide either subjectId or url")
    input_str = subjectId or url
    if not input_str.strip():
        raise HTTPException(status_code=400, detail="Empty input")
    try:
        data = await moviebox_fetch_sources(
            app.state.client,
            input_str,
            se=int(seasonId),
            ep=int(episodeId),
            cookie=cookie,
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


@app.get(
    "/moviebox/search",
    summary="Search Moviebox titles",
    description="Search for movies and TV shows on themoviebox.xyz by keyword.",
    tags=["Moviebox"],
)
async def moviebox_search_endpoint(
    q: str = Query(..., min_length=1, description="Search keyword"),
    page: int = Query(default=1, ge=1, description="Page number"),
) -> dict[str, Any]:
    try:
        results = await moviebox_search(app.state.client, q, page=page)
        return {"query": q, "page": page, "status": "ok", "results": results}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Moviebox search error: {str(e)}")


@app.get(
    "/subtitles",
    summary="List available subtitle sources",
    tags=["Subtitles"],
)
async def list_subtitle_sources() -> dict[str, list[str]]:
    providers = [p.name.lower() for p in AVAILABLE]
    providers.append("opensubtitles")
    return {"sources": providers}


@app.get(
    "/subtitles/{provider_name}",
    summary="Fetch subtitles manually from a specific provider",
    description="Fetch subtitles from video providers or OpenSubtitles.",
    tags=["Subtitles"],
)
async def get_provider_subtitles(
    provider_name: str = Path(..., description="Provider name (e.g. yoru, neon, opensubtitles)"),
    title: str = Query(default="", description="Media title (e.g. Interstellar)"),
    mediaType: str = Query(default="movie", pattern=r"^(movie|tv)$", description="Media type"),
    tmdbId: str = Query(default="", pattern=r"^\d*$", description="TMDB numerical ID"),
    year: str = Query(default="", pattern=r"^\d{4}$|^$", description="Release year (optional)"),
    episodeId: str = Query(default="1", pattern=r"^\d+$", description="Episode number"),
    seasonId: str = Query(default="1", pattern=r"^\d+$", description="Season number"),
    imdbId: str = Query(default="", pattern=r"^tt\d+$|^$", description="IMDB ID"),
) -> dict[str, Any]:
    provider_lower = provider_name.lower()
    
    if provider_lower == "opensubtitles":
        if not imdbId:
            raise HTTPException(status_code=400, detail="imdbId is required for OpenSubtitles")
        subs = await fetch_opensubtitles(app.state.client, imdbId)
        return {"subtitles": [sub.model_dump() for sub in subs]}
        
    prov = PROVIDER_MAP.get(provider_lower)
    if not prov:
        available = ", ".join([p.name.lower() for p in AVAILABLE] + ["opensubtitles"])
        raise HTTPException(status_code=400, detail=f"Unknown subtitle provider '{provider_name}'. Available: {available}")
        
    if not title or not tmdbId:
        raise HTTPException(status_code=400, detail="title and tmdbId are required for video providers")

    params = SourceParams(
        title=title, mediaType=mediaType, tmdbId=tmdbId, provider=prov.name,
        year=year, episodeId=episodeId, seasonId=seasonId, imdbId=imdbId
    )

    try:
        _, raw = await _get_sources(app.state.client, app.state.cache, prov, params)
        decrypted_data = DecryptedData.from_raw(raw)
        subs = decrypted_data.subtitles
        for sub in subs:
            sub.language = f"{prov.name} - {sub.language}"
        return {"subtitles": [sub.model_dump() for sub in subs]}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"{prov.name} subtitle fetch error: {str(e)}")


@app.get(
    "/providers",
    response_model=ProviderList,
    summary="List providers",
    description="Returns all active providers with their endpoint slugs.",
    tags=["Info"],
)
async def list_providers() -> ProviderList:
    return ProviderList(
        providers=[ProviderInfo(name=p.name, endpoint=p.endpoint) for p in AVAILABLE]
    )


@app.get(
    "/health",
    summary="Health check",
    description="Returns service status and version.",
    tags=["Info"],
)
async def health() -> dict[str, Any]:
    return {"status": "ok", "version": "2.0.0"}


@app.get(
    "/opensubtitles",
    response_model=list[SubtitleItem],
    summary="Fetch OpenSubtitles manually",
    description="Fetch subtitles from OpenSubtitles using an IMDB ID.",
    tags=["Subtitles"],
)
async def get_opensubtitles(
    imdbId: str = Query(..., description="IMDB ID (e.g. tt1234567)")
) -> list[SubtitleItem]:
    return await fetch_opensubtitles(app.state.client, imdbId)


@app.get(
    "/proxy",
    summary="HLS and MP4 stream proxy",
    description="Proxy HLS manifests, MP4 videos, and video segments with Range request forwarding, Sec-Fetch-Dest headers, and CORS bypass. Used by the player.",
    tags=["Proxy"],
    response_class=StreamingResponse,
)
async def proxy_hls(
    request: Request,
    url: str = Query(..., description="Full URL of the resource to proxy"),
    headers: str = Query(default="", description="Optional custom JSON headers to forward"),
) -> StreamingResponse:
    """Stream any URL through the backend with Range support and domain-specific headers."""
    u_lower = url.lower()
    if any(d in u_lower for d in ("hakunaymatata.com", "aoneroom.com", "themoviebox.xyz")):
        proxy_headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:152.0) Gecko/20100101 Firefox/152.0",
            "Accept": "video/webm,video/ogg,video/*;q=0.9,application/ogg;q=0.7,audio/*;q=0.6,*/*;q=0.5",
            "Origin": "https://themoviebox.xyz",
            "Referer": "https://themoviebox.xyz/",
            "Sec-Fetch-Dest": "video",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "cross-site",
        }
    else:
        proxy_headers = _build_headers()

    # Forward incoming Range header for video seeking/scrubbing
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

    req = app.state.client.build_request("GET", url, headers=proxy_headers)
    try:
        resp = await app.state.client.send(req, stream=True, follow_redirects=True)
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

    # For DASH MPD manifests, store base info so middleware can catch segment requests
    if ".mpd" in u_lower or "application/dash+xml" in resp.headers.get("content-type", ""):
        raw_xml = (await resp.aread()).decode("utf-8", errors="ignore")
        await resp.aclose()

        base_dir = url.rsplit("/", 1)[0] + "/"
        app.state.last_dash_base = (base_dir, headers.strip())

        async def xml_gen():
            yield raw_xml.encode("utf-8")

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

    # Non-MPD content (segments, videos, etc.) — stream directly
    async def stream_generator():
        try:
            async for chunk in resp.aiter_bytes(chunk_size=64 * 1024):
                yield chunk
        except httpx.TimeoutException:
            logger.warning("ReadTimeout while streaming proxy chunk from %s", url)
        except httpx.RequestError as e:
            logger.warning("RequestError while streaming proxy chunk from %s: %s", url, e)
        finally:
            await resp.aclose()

    content_type = resp.headers.get("content-type", "application/octet-stream")
    stream_headers = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Headers": "*",
        "Accept-Ranges": "bytes",
        "Cache-Control": "public, max-age=3600",
    }
    if resp.headers.get("content-length"):
        stream_headers["Content-Length"] = resp.headers["content-length"]
    if resp.headers.get("content-range"):
        stream_headers["Content-Range"] = resp.headers["content-range"]
    if resp.headers.get("content-disposition"):
        stream_headers["Content-Disposition"] = resp.headers["content-disposition"]

    return StreamingResponse(
        stream_generator(),
        status_code=resp.status_code,
        media_type=content_type,
        headers=stream_headers,
    )


async def _do_proxy_stream(
    request: Request,
    target_url: str,
    extra_headers_json: str = "",
) -> StreamingResponse:
    """Core proxy logic shared by /proxy and fallback segment routes."""
    u_lower = target_url.lower()
    if any(d in u_lower for d in ("hakunaymatata.com", "aoneroom.com", "themoviebox.xyz")):
        proxy_headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:152.0) Gecko/20100101 Firefox/152.0",
            "Accept": "video/webm,video/ogg,video/*;q=0.9,application/ogg;q=0.7,audio/*;q=0.6,*/*;q=0.5",
            "Origin": "https://themoviebox.xyz",
            "Referer": "https://themoviebox.xyz/",
            "Sec-Fetch-Dest": "video",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "cross-site",
        }
    else:
        proxy_headers = _build_headers()

    range_header = request.headers.get("range")
    if range_header:
        proxy_headers["Range"] = range_header

    if extra_headers_json.strip():
        try:
            extra = json.loads(extra_headers_json)
            if isinstance(extra, dict):
                proxy_headers.update(extra)
        except Exception:
            pass

    req = app.state.client.build_request("GET", target_url, headers=proxy_headers)
    try:
        resp = await app.state.client.send(req, stream=True, follow_redirects=True)
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Upstream timeout connecting to stream")
    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=f"Upstream connection error: {e}")

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
        except httpx.TimeoutException:
            logger.warning("ReadTimeout while streaming proxy chunk from %s", target_url)
        except httpx.RequestError as e:
            logger.warning("RequestError while streaming proxy chunk from %s: %s", target_url, e)
        finally:
            await resp.aclose()

    content_type = resp.headers.get("content-type", "video/mp4")
    res_headers = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Headers": "*",
        "Accept-Ranges": "bytes",
        "Cache-Control": "public, max-age=3600",
    }
    if resp.headers.get("content-length"):
        res_headers["Content-Length"] = resp.headers["content-length"]
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


@app.get("/player", response_class=HTMLResponse, include_in_schema=False)
async def player_page(
    url: str = Query(default="", description="Video URL to play"),
    sources: str = Query(default="", description="JSON array of sources (quality/url pairs)"),
    subtitles: str = Query(default="", description="JSON array of subtitles (language/url pairs)"),
) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Video Player</title>
  <script src="https://cdn.jsdelivr.net/npm/hls.js@latest"></script>
  <script src="https://cdn.jsdelivr.net/npm/dashjs@latest/dist/dash.all.min.js"></script>
  <style>
    *{{box-sizing:border-box;margin:0;padding:0}}
    body{{font-family:'Inter',sans-serif;background:#0a0a0f;color:#e2e8f0;min-height:100vh;display:flex;flex-direction:column}}
    .nav{{display:flex;align-items:center;justify-content:space-between;padding:14px 28px;border-bottom:1px solid rgba(255,255,255,.06);background:rgba(10,10,15,.8)}}
    .logo{{font-weight:700;font-size:1rem}} .logo span{{color:#60a5fa}}
    .nav-links a{{color:#94a3b8;text-decoration:none;font-size:.85rem;margin-left:20px}} .nav-links a:hover{{color:#e2e8f0}}
    .container{{max-width:960px;margin:0 auto;padding:24px;flex:1;display:flex;flex-direction:column;gap:16px}}
    .card{{background:#111118;border:1px solid rgba(255,255,255,.06);border-radius:12px;padding:20px}}
    .card-title{{font-size:.75rem;text-transform:uppercase;letter-spacing:.08em;color:#64748b;font-weight:600;margin-bottom:12px}}
    video{{width:100%;border-radius:8px;background:#000;display:block}}
    .source-list{{display:flex;gap:8px;flex-wrap:wrap}}
    .source-btn{{padding:8px 16px;border:1px solid rgba(255,255,255,.12);border-radius:8px;background:transparent;color:#94a3b8;cursor:pointer;font-size:.85rem;transition:all .15s}}
    .source-btn:hover{{border-color:#60a5fa;color:#e2e8f0}}
    .source-btn.active{{border-color:#60a5fa;color:#60a5fa;background:rgba(96,165,250,.12)}}
    textarea{{width:100%;padding:12px;background:#1a1a24;border:1px solid rgba(255,255,255,.08);border-radius:8px;color:#e2e8f0;font-family:'JetBrains Mono',monospace;font-size:.82rem;resize:vertical;min-height:80px;outline:none}}
    textarea:focus{{border-color:#60a5fa}}
    .row{{display:flex;gap:12px;align-items:center;flex-wrap:wrap}}
    .btn{{padding:9px 20px;border:none;border-radius:8px;font-weight:600;font-size:.85rem;cursor:pointer;transition:all .15s}}
    .btn-primary{{background:#2563eb;color:#fff}} .btn-primary:hover{{background:#1d4ed8}}
    .status{{font-size:.82rem;color:#64748b;margin-top:8px}}
    .flex{{display:flex;align-items:center;gap:12px;flex-wrap:wrap}}
    .field{{flex:1;min-width:200px}}
    .field input{{width:100%;padding:9px 12px;background:#1a1a24;border:1px solid rgba(255,255,255,.08);border-radius:8px;color:#e2e8f0;font-family:'JetBrains Mono',monospace;font-size:.82rem;outline:none}}
    .field input:focus{{border-color:#60a5fa}}
    label{{display:block;font-size:.72rem;text-transform:uppercase;letter-spacing:.06em;color:#64748b;font-weight:600;margin-bottom:4px}}
  </style>
</head>
<body>
  <nav class="nav">
    <div class="logo">▶ <span>videasy</span> player</div>
    <div class="nav-links">
      <a href="/">Home</a>
      <a href="/docs" target="_blank">API</a>
    </div>
  </nav>
  <div class="container">
    <div class="card">
      <div class="card-title" style="display:flex;justify-content:space-between;align-items:center">
        <span>Player</span>
        <label style="display:inline-flex;align-items:center;gap:6px;cursor:pointer;font-size:.78rem;text-transform:none;color:#60a5fa">
          <input type="checkbox" id="use-proxy" checked style="width:auto"> Use Proxy (/proxy)
        </label>
      </div>
      <video id="video" controls autoplay playsinline></video>
      <div id="sources" class="source-list" style="margin-top:12px"></div>
      <div id="subtitles-list" class="source-list" style="margin-top:8px"></div>
      <div id="status" class="status"></div>
    </div>
    <div class="card">
      <div class="card-title">Load Source</div>
      <div class="row">
        <div class="field">
          <label>Video URL</label>
          <input id="url-input" value='{url}' placeholder="https://example.com/video.mp4 or .m3u8 or .mpd">
        </div>
        <button class="btn btn-primary" onclick="loadUrl()">Load</button>
      </div>
      <div style="margin-top:12px">
        <label>Sources JSON (optional)</label>
        <textarea id="sources-input" placeholder='[{{"quality":"1080p","url":"https://..."}}]'>{sources}</textarea>
        <button class="btn btn-primary" style="margin-top:8px" onclick="loadSources()">Load Sources</button>
      </div>
      <div style="margin-top:12px">
        <label>Quick Links</label>
        <div class="flex">
          <button class="source-btn" onclick="fetchAndPlay('/sources?tmdbId=157336&mediaType=movie&title=Interstellar&year=2014&provider=Yoru')">Interstellar (Yoru)</button>
          <button class="source-btn" onclick="fetchAndPlay('/sources?tmdbId=1396&mediaType=tv&title=Breaking+Bad&year=2008&seasonId=1&episodeId=1&provider=Yoru')">Breaking Bad S1E1</button>
          <button class="source-btn" onclick="fetchAndPlay('/moviebox/sources?subjectId=8313012068559605176')">Moviebox Test</button>
        </div>
      </div>
    </div>
  </div>
  <script>
    let hlsPlayer = null;
    let dashPlayer = null;
    let originalRawSource = null;
    const video = document.getElementById('video');

    function isDirectMedia(url) {{
      if (!url) return false;
      const u = url.toLowerCase();
      if (u.includes('.m3u8') || u.includes('.mp4') || u.includes('.mpd') || u.includes('.webm') || u.includes('aoneroom.com') || u.includes('hakunaymatata.com')) return true;
      if (u.includes('.html') || u.includes('fzmovies') || u.includes('ugc-video')) return false;
      return true;
    }}

    function cleanPlayers() {{
      if (hlsPlayer) {{ hlsPlayer.destroy(); hlsPlayer = null; }}
      if (dashPlayer) {{ dashPlayer.reset(); dashPlayer = null; }}
      video.removeAttribute('src');
      video.load();
    }}

    function play(sourceObj, forceProxy = false) {{
      cleanPlayers();
      const status = document.getElementById('status');
      originalRawSource = sourceObj;

      let url = typeof sourceObj === 'string' ? sourceObj : (sourceObj.url || '');
      let extraHeaders = typeof sourceObj === 'object' ? sourceObj.headers : null;

      if (!isDirectMedia(url)) {{
        status.innerHTML = '🔗 External Web Link: <a href="' + url + '" target="_blank" style="color:#60a5fa;text-decoration:underline">Open in New Tab ↗</a>';
        window.open(url, '_blank');
        return;
      }}

      const useProxy = forceProxy || document.getElementById('use-proxy').checked;
      let finalUrl = url;
      if (useProxy && !url.startsWith('/proxy') && !url.startsWith(window.location.origin + '/proxy')) {{
        let proxyParams = 'url=' + encodeURIComponent(url);
        if (extraHeaders) {{
          proxyParams += '&headers=' + encodeURIComponent(JSON.stringify(extraHeaders));
        }}
        finalUrl = '/proxy?' + proxyParams;
      }}

      const urlLower = url.toLowerCase();

      // 1. DASH Stream (.mpd)
      if (urlLower.includes('.mpd') || (typeof sourceObj === 'object' && sourceObj.type === 'dash')) {{
        if (typeof dashjs !== 'undefined') {{
          dashPlayer = dashjs.MediaPlayer().create();
          if (useProxy) {{
            dashPlayer.extend("RequestModifier", function () {{
              return {{
                modifyRequestURL: function (reqUrl) {{
                  if (!reqUrl) return reqUrl;
                  // Already a proxy path — pass through
                  if (reqUrl.startsWith("/proxy?")) return reqUrl;
                  // Any other URL (relative, absolute CDN, or localhost) — proxy it
                  const baseDir = url.substring(0, url.lastIndexOf("/") + 1);
                  let targetUrl;
                  if (reqUrl.startsWith("http://") || reqUrl.startsWith("https://")) {{
                    targetUrl = reqUrl;
                  }} else {{
                    // Relative URL — resolve against original MPD CDN base
                    targetUrl = baseDir + (reqUrl.startsWith("/") ? reqUrl.slice(1) : reqUrl);
                  }}
                  let pParams = "url=" + encodeURIComponent(targetUrl);
                  if (extraHeaders) {{
                    pParams += "&headers=" + encodeURIComponent(JSON.stringify(extraHeaders));
                  }}
                  return "/proxy?" + pParams;
                }}
              }};
            }});
          }}
          dashPlayer.initialize(video, finalUrl, true);
          status.textContent = (useProxy ? 'Proxy ' : '') + 'DASH • ' + url.split('/').pop().split('?')[0];
        }} else {{
          video.src = finalUrl;
          video.play().catch(e => {{}});
          status.textContent = (useProxy ? 'Proxy ' : '') + 'DASH (native)';
        }}
      }}
      // 2. HLS Stream (.m3u8)
      else if (urlLower.includes('.m3u8') || (typeof sourceObj === 'object' && sourceObj.type === 'hls')) {{
        if (typeof Hls !== 'undefined' && Hls.isSupported()) {{
          hlsPlayer = new Hls();
          hlsPlayer.loadSource(finalUrl);
          hlsPlayer.attachMedia(video);
          hlsPlayer.on(Hls.Events.MANIFEST_PARSED, () => video.play().catch(e => {{}}));
          status.textContent = (useProxy ? 'Proxy ' : '') + 'HLS • ' + url.split('/').pop().split('?')[0];
        }} else if (video.canPlayType('application/vnd.apple.mpegurl')) {{
          video.src = finalUrl;
          video.play().catch(e => {{}});
          status.textContent = (useProxy ? 'Proxy ' : '') + 'HLS (native)';
        }} else {{
          status.textContent = 'HLS not supported in this browser';
        }}
      }}
      // 3. MP4 Direct Video Stream
      else {{
        video.src = finalUrl;
        video.play().catch(e => {{}});
        status.textContent = (useProxy ? 'Proxy ' : '') + 'MP4 • ' + url.split('/').pop().split('?')[0];
      }}
    }}

    video.onerror = () => {{
      const status = document.getElementById('status');
      if (originalRawSource && !document.getElementById('use-proxy').checked) {{
        status.innerHTML = '⚠️ Playback failed. Retrying via Proxy (/proxy)...';
        document.getElementById('use-proxy').checked = true;
        play(originalRawSource, true);
      }} else if (originalRawSource) {{
        const rawUrl = typeof originalRawSource === 'string' ? originalRawSource : originalRawSource.url;
        status.innerHTML = '⚠️ Media playback failed. <a href="' + rawUrl + '" target="_blank" style="color:#60a5fa;text-decoration:underline">Try opening directly ↗</a>';
      }}
    }};

    function loadUrl() {{
      const u = document.getElementById('url-input').value.trim();
      if (!u) return;
      play({{ quality: 'Auto', url: u }});
      document.querySelectorAll('.source-btn').forEach(b => b.classList.remove('active'));
    }}

    function loadSources() {{
      const raw = document.getElementById('sources-input').value.trim();
      if (!raw) return;
      try {{
        const list = JSON.parse(raw);
        if (!Array.isArray(list)) throw new Error('not an array');
        renderSources(list);
        playBestSource(list);
      }} catch (e) {{
        document.getElementById('status').textContent = 'Invalid JSON: ' + e.message;
      }}
    }}

    function playBestSource(list) {{
      if (!list || list.length === 0) return;
      const direct = list.find(s => isDirectMedia(s.url));
      if (direct) {{
        play(direct);
      }} else {{
        document.getElementById('status').innerHTML = '⚠️ All available sources are external web links. Click a source button below to open it in a new tab.';
      }}
    }}

    function renderSources(list) {{
      const container = document.getElementById('sources');
      container.innerHTML = '';
      const firstDirect = list.find(s => isDirectMedia(s.url));
      list.forEach((s, i) => {{
        const btn = document.createElement('button');
        const isDirect = isDirectMedia(s.url);
        const isActive = firstDirect ? (s.url === firstDirect.url) : (i === 0);
        btn.className = 'source-btn' + (isActive ? ' active' : '');
        const label = (s.source ? '[' + s.source + '] ' : '') + (s.quality || 'Auto');
        btn.textContent = (isDirect ? '▶ ' : '🔗 ') + label;
        btn.onclick = () => {{
          document.querySelectorAll('.source-btn').forEach(b => b.classList.remove('active'));
          btn.classList.add('active');
          play(s);
        }};
        container.appendChild(btn);
      }});
    }}

    function renderSubtitles(subs) {{
      const container = document.getElementById('subtitles-list');
      container.innerHTML = '';
      if (!subs || subs.length === 0) return;
      
      // Add track elements to video
      while (video.getElementsByTagName('track').length > 0) {{
        video.removeChild(video.getElementsByTagName('track')[0]);
      }}

      subs.forEach((sub, i) => {{
        const track = document.createElement('track');
        track.kind = 'subtitles';
        track.label = sub.language || ('Sub ' + (i+1));
        track.srclang = (sub.language || 'en').toLowerCase().slice(0, 2);
        track.src = sub.url;
        if (i === 0) track.default = true;
        video.appendChild(track);

        const badge = document.createElement('span');
        badge.className = 'source-btn';
        badge.style.fontSize = '.75rem';
        badge.style.padding = '4px 10px';
        badge.textContent = '💬 ' + (sub.language || 'Subtitle');
        container.appendChild(badge);
      }});
    }}

    async function fetchAndPlay(endpoint) {{
      const status = document.getElementById('status');
      status.textContent = 'Fetching...';
      try {{
        const resp = await fetch(endpoint);
        const data = await resp.json();
        let sources = data.data?.sources || data.sources || [];
        let subs = data.data?.subtitles || data.subtitles || [];
        if (sources.length === 0) {{ status.textContent = 'No sources found'; return; }}
        renderSources(sources);
        renderSubtitles(subs);
        playBestSource(sources);
        status.textContent = sources.length + ' source(s) loaded';
      }} catch (e) {{
        status.textContent = 'Error: ' + e.message;
      }}
    }}

    const initialUrl = '{url}';
    const initialSources = '{sources}';
    const initialSubs = '{subtitles}';

    if (initialUrl) play({{ quality: 'Auto', url: initialUrl }});
    if (initialSources) {{
      try {{
        const list = JSON.parse(initialSources);
        if (Array.isArray(list) && list.length > 0) {{
          renderSources(list);
          playBestSource(list);
        }}
      }} catch(e) {{}}
    }}
    if (initialSubs) {{
      try {{
        const subs = JSON.parse(initialSubs);
        if (Array.isArray(subs)) renderSubtitles(subs);
      }} catch(e) {{}}
    }}
  </script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def root() -> str:
    provider_options = "".join(
        f'<option value="{p.name}">{p.name}</option>'
        for p in AVAILABLE
    )

    provider_badges_moviebox = f'<span class="pill pill-green" style="margin-right:6px">Moviebox</span>'

    provider_badges = "".join(
        f'<span class="pill pill-{'green' if i < 2 else 'amber' if i < 3 else 'slate'}" style="margin-right:6px">{p.name}</span>'
        for i, p in enumerate(AVAILABLE)
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Videasy Decryptor</title>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
  <style>
    *{{box-sizing:border-box;margin:0;padding:0}}
    body{{font-family:'Inter',sans-serif;background:#0a0a0f;color:#e2e8f0;line-height:1.6}}
    .nav{{display:flex;align-items:center;justify-content:space-between;padding:14px 28px;border-bottom:1px solid rgba(255,255,255,.06);background:rgba(10,10,15,.8);backdrop-filter:blur(10px);position:sticky;top:0;z-index:50}}
    .logo{{font-weight:700;font-size:1rem;letter-spacing:-.01em}} .logo span{{color:#60a5fa}}
    .nav-links a{{color:#94a3b8;text-decoration:none;font-size:.85rem;margin-left:20px;transition:color .15s}} .nav-links a:hover{{color:#e2e8f0}}
    .hero{{text-align:center;padding:64px 24px 40px}}
    .hero h1{{font-size:clamp(2rem,5vw,3.2rem);font-weight:800;letter-spacing:-.03em;margin-bottom:10px}} .hero h1 span{{color:#60a5fa}}
    .hero p{{color:#94a3b8;font-size:1rem;max-width:500px;margin:0 auto}}
    .container{{max-width:800px;margin:0 auto;padding:0 24px 60px}}
    .card{{background:#111118;border:1px solid rgba(255,255,255,.06);border-radius:12px;padding:24px;margin-bottom:20px}}
    .card-title{{font-size:.75rem;text-transform:uppercase;letter-spacing:.08em;color:#64748b;font-weight:600;margin-bottom:16px}}
    .row{{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:14px;align-items:end}}
    .field{{flex:1;min-width:140px}}
    .field label{{display:block;font-size:.72rem;text-transform:uppercase;letter-spacing:.06em;color:#64748b;font-weight:600;margin-bottom:4px}}
    .field input,.field select{{width:100%;padding:9px 12px;background:#1a1a24;border:1px solid rgba(255,255,255,.08);border-radius:8px;color:#e2e8f0;font-family:'JetBrains Mono',monospace;font-size:.82rem;outline:none;transition:border .15s}}
    .field input:focus,.field select:focus{{border-color:#60a5fa}}
    .field select option{{background:#1a1a24}}
    .btn{{padding:9px 20px;border:none;border-radius:8px;font-weight:600;font-size:.85rem;cursor:pointer;transition:all .15s;white-space:nowrap}}
    .btn-primary{{background:#2563eb;color:#fff}} .btn-primary:hover{{background:#1d4ed8}}
    .btn-secondary{{background:#1e293b;color:#94a3b8}} .btn-secondary:hover{{background:#334155;color:#e2e8f0}}
    .btn-group{{display:flex;gap:6px;flex-wrap:wrap}}
    .btn-provider{{padding:7px 16px;border:1px solid rgba(255,255,255,.08);border-radius:8px;background:transparent;color:#94a3b8;font-size:.82rem;font-weight:500;cursor:pointer;transition:all .15s;font-family:'Inter',sans-serif}}
    .btn-provider:hover{{border-color:#60a5fa;color:#e2e8f0;background:rgba(96,165,250,.08)}}
    .btn-provider.active{{border-color:#60a5fa;color:#60a5fa;background:rgba(96,165,250,.12)}}
    pre{{background:#0d0d14;border:1px solid rgba(255,255,255,.06);border-radius:8px;padding:16px;font-family:'JetBrains Mono',monospace;font-size:.78rem;overflow-x:auto;line-height:1.5;min-height:60px;color:#94a3b8;white-space:pre-wrap}}
    pre .key{{color:#60a5fa}} pre .str{{color:#34d399}} pre .num{{color:#f472b6}} pre .bool{{color:#fbbf24}} pre .null{{color:#64748b}} pre .bracket{{color:#64748b}}
    .status-bar{{display:flex;align-items:center;gap:10px;padding:12px 16px;border-radius:8px;font-size:.82rem;margin-bottom:14px}}
    .status-bar.loading{{background:rgba(96,165,250,.08);border:1px solid rgba(96,165,250,.15);color:#93c5fd}}
    .status-bar.ok{{background:rgba(52,211,153,.08);border:1px solid rgba(52,211,153,.15);color:#34d399}}
    .status-bar.err{{background:rgba(248,113,113,.08);border:1px solid rgba(248,113,113,.15);color:#fca5a5}}
    .spinner{{width:14px;height:14px;border:2px solid rgba(96,165,250,.2);border-top-color:#60a5fa;border-radius:50%;animation:spin .6s linear infinite;display:inline-block}}
    @keyframes spin{{to{{transform:rotate(360deg)}}}}
    .pill{{display:inline-block;padding:2px 10px;border-radius:6px;font-size:.72rem;font-weight:600;letter-spacing:.03em}}
    .pill-green{{background:rgba(52,211,153,.12);color:#34d399;border:1px solid rgba(52,211,153,.15)}}
    .pill-amber{{background:rgba(251,191,36,.12);color:#fbbf24;border:1px solid rgba(251,191,36,.15)}}
    .pill-slate{{background:rgba(148,163,184,.1);color:#94a3b8;border:1px solid rgba(148,163,184,.12)}}
    .quick-list{{display:flex;flex-wrap:wrap;gap:8px;margin-top:12px}}
    .quick-tag{{display:inline-flex;align-items:center;gap:6px;padding:6px 14px;background:#1a1a24;border:1px solid rgba(255,255,255,.06);border-radius:8px;color:#94a3b8;text-decoration:none;font-size:.82rem;transition:all .12s;cursor:pointer}}
    .quick-tag:hover{{border-color:#60a5fa;color:#e2e8f0;background:rgba(96,165,250,.06)}}
    .quick-tag .tag-prov{{color:#60a5fa;font-weight:600}}
    .flex{{display:flex;align-items:center;gap:12px;flex-wrap:wrap}}
    .mt-2{{margin-top:12px}}
    .mb-2{{margin-bottom:12px}}
    .text-muted{{color:#64748b;font-size:.82rem}}
    hr{{border:none;border-top:1px solid rgba(255,255,255,.06);margin:20px 0}}
    .endpoint-row{{display:flex;align-items:center;gap:12px;padding:12px 16px;border-radius:8px;background:#0d0d14;font-family:'JetBrains Mono',monospace;font-size:.82rem}}
    .endpoint-row:hover{{background:#111118}}
    .method{{padding:3px 8px;border-radius:5px;font-weight:700;font-size:.7rem;letter-spacing:.05em}}
    .method.get{{background:rgba(52,211,153,.12);color:#34d399;border:1px solid rgba(52,211,153,.15)}}
    .ep-desc{{color:#64748b;font-family:'Inter',sans-serif;font-size:.82rem;margin-left:auto}}
    @media(max-width:600px){{.row{{flex-direction:column}} .field{{min-width:100%}} .nav-links{{display:none}}}}
  </style>
</head>
<body>
  <nav class="nav">
    <div class="logo">▶ <span>videasy</span> decryptor</div>
    <div class="nav-links">
      <a href="#try">Try it</a>
      <a href="#moviebox">Moviebox</a>
      <a href="#endpoints">Endpoints</a>
      <a href="/player" target="_blank">Player</a>
      <a href="/docs" target="_blank">OpenAPI</a>
    </div>
  </nav>

  <div class="hero">
    <h1><span>videasy</span> decryptor</h1>
    <p>Pick a provider, get HLS streams + subtitles.</p>
  </div>

  <div class="container">
    <div class="card" id="try">
      <div class="card-title">Try it now</div>
      <div class="row">
        <div class="field">
          <label>tmdbId</label>
          <input id="tmdb" value="157336" placeholder="e.g. 157336">
        </div>
        <div class="field">
          <label>title</label>
          <input id="title" value="Interstellar" placeholder="Movie title">
        </div>
        <div class="field" style="flex:0.7">
          <label>year</label>
          <input id="year" value="2014" placeholder="Year">
        </div>
        <div class="field" style="flex:0.7">
          <label>type</label>
          <select id="type"><option value="movie" selected>movie</option><option value="tv">tv</option></select>
        </div>
      </div>
      <div class="row">
        <div class="field" style="flex:0.6">
          <label>season</label>
          <input id="season" value="1" placeholder="1">
        </div>
        <div class="field" style="flex:0.6">
          <label>episode</label>
          <input id="episode" value="1" placeholder="1">
        </div>
        <div class="field" style="flex:0.7">
          <label>imdbId</label>
          <input id="imdb" value="tt0816692" placeholder="tt0816692">
        </div>
      </div>
      <div class="mb-2">
        <label style="display:block;font-size:.72rem;text-transform:uppercase;letter-spacing:.06em;color:#64748b;font-weight:600;margin-bottom:6px">Provider</label>
        <div class="btn-group" id="provider-group">
          <button class="btn-provider active" data-provider="Yoru">Yoru</button>
          <button class="btn-provider" data-provider="Neon">Neon</button>
          <button class="btn-provider" data-provider="Cypher">Cypher</button>
          <button class="btn-provider" data-provider="Breach">Breach</button>
        </div>
      </div>
      <div class="flex">
        <button class="btn btn-primary" id="fetch-btn" onclick="fetchSources()">Fetch sources</button>
        <button class="btn btn-secondary" id="play-btn" onclick="openPlayer()" style="display:none">▶ Play in Player</button>
        <select id="sub-source" class="field select" style="max-width:140px; padding: 7px 12px;">
          <option value="opensubtitles">OpenSubtitles</option>
          <option value="yoru">Yoru</option>
          <option value="neon">Neon</option>
          <option value="cypher">Cypher</option>
          <option value="breach">Breach</option>
        </select>
        <button class="btn btn-secondary" id="fetch-sub-btn" onclick="fetchSubtitles()">💬 Fetch Subs</button>
        <select id="os-dropdown" style="display:none; flex:1; max-width:200px" onchange="copyOsLink(this)" class="field select">
          <option value="">-- Select Subtitle --</option>
        </select>
        <span class="text-muted" id="req-url" style="font-family:'JetBrains Mono',monospace;font-size:.75rem"></span>
      </div>
      <div id="status" class="mt-2"></div>
      <pre id="output" class="mt-2">Response will appear here</pre>
    </div>

    <div class="card" id="endpoints">
      <div class="card-title">Endpoints</div>
      <div class="endpoint-row">
        <span class="method get">GET</span>
        <code>/sources</code>
        <span class="ep-desc">Fetch decrypted streams</span>
      </div>
      <div class="endpoint-row">
        <span class="method get">GET</span>
        <code>/moviebox/sources</code>
        <span class="ep-desc">Fetch Moviebox sources</span>
      </div>
      <div class="endpoint-row">
        <span class="method get">GET</span>
        <code>/providers</code>
        <span class="ep-desc">List providers</span>
      </div>
      <div class="endpoint-row">
        <span class="method get">GET</span>
        <code>/player</code>
        <span class="ep-desc">Video player with HLS/MP4 support</span>
      </div>
      <div class="endpoint-row">
        <span class="method get">GET</span>
        <code>/health</code>
        <span class="ep-desc">Health check</span>
      </div>
    </div>

    <div class="card">
      <div class="card-title">Quick links</div>
      <div class="quick-list">
        <a class="quick-tag" href="/sources?tmdbId=157336&mediaType=movie&title=Interstellar&year=2014&imdbId=tt0816692&provider=Yoru" target="_blank">
          Interstellar <span class="tag-prov">Yoru</span>
        </a>
        <a class="quick-tag" href="/sources?tmdbId=157336&mediaType=movie&title=Interstellar&year=2014&imdbId=tt0816692&provider=Neon" target="_blank">
          Interstellar <span class="tag-prov">Neon</span>
        </a>
        <a class="quick-tag" href="/sources?tmdbId=27205&mediaType=movie&title=Inception&year=2010&imdbId=tt1375666&provider=Yoru" target="_blank">
          Inception <span class="tag-prov">Yoru</span>
        </a>
        <a class="quick-tag" href="/sources?tmdbId=1396&mediaType=tv&title=Breaking+Bad&year=2008&seasonId=1&episodeId=1&provider=Yoru" target="_blank">
          Breaking Bad S1E1 <span class="tag-prov">Yoru</span>
        </a>
        <a class="quick-tag" href="/sources?tmdbId=66732&mediaType=tv&title=Stranger+Things&year=2016&seasonId=1&episodeId=1&provider=Neon" target="_blank">
          Stranger Things S1E1 <span class="tag-prov">Neon</span>
        </a>
      </div>
    </div>

    <div class="card" id="moviebox">
      <div class="card-title">Moviebox</div>
      <div class="row">
        <div class="field" style="flex:2">
          <label>URL / Slug / Subject ID</label>
          <input id="mb-input" value="https://themoviebox.xyz/movies/check-out-sekarang-pay-later-caper-QMd0JfnG5U9?id=8313012068559605176" placeholder="https://themoviebox.xyz/... or subjectId or slug">
        </div>
        <div class="field" style="flex:1">
          <label>Cookie (Optional for Full Movie)</label>
          <input id="mb-cookie" value="" placeholder="mb_token=...; token=...">
        </div>
        <div class="field" style="flex:0.4">
          <label>type</label>
          <select id="mb-type"><option value="movie" selected>movie</option><option value="tv">tv</option></select>
        </div>
      </div>
      <div class="flex">
        <button class="btn btn-primary" onclick="fetchMoviebox()">Fetch Moviebox sources</button>
        <button class="btn btn-secondary" id="mb-play-btn" onclick="openMbPlayer()" style="display:none">▶ Play in Player</button>
        <span class="text-muted" id="mb-req-url" style="font-family:'JetBrains Mono',monospace;font-size:.75rem"></span>
      </div>
      <div id="mb-status" class="mt-2"></div>
      <pre id="mb-output" class="mt-2">Response will appear here</pre>
      <div class="quick-list mt-2">
        <a class="quick-tag" onclick="setMbInput('https://themoviebox.xyz/movies/check-out-sekarang-pay-later-caper-QMd0JfnG5U9?id=8313012068559605176'); fetchMoviebox();">
          Check Out Sekarang <span class="tag-prov">Moviebox</span>
        </a>
      </div>
    </div>

    <div class="card">
      <div class="card-title">Providers</div>
      <div class="flex">
        {provider_badges}
      </div>
      <div class="text-muted mt-2">Try each provider if one fails — availability varies per title.</div>
    </div>
  </div>

  <script>
    let activeProvider = "Yoru";

    document.querySelectorAll('.btn-provider').forEach(btn => {{
      btn.addEventListener('click', () => {{
        document.querySelectorAll('.btn-provider').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        activeProvider = btn.dataset.provider;
      }});
    }});

    function jsonHighlight(obj) {{
      if (typeof obj === 'string') return '<span class="str">"' + obj.replace(/"/g, '\\"') + '"</span>';
      if (typeof obj === 'number') return '<span class="num">' + obj + '</span>';
      if (typeof obj === 'boolean') return '<span class="bool">' + obj + '</span>';
      if (obj === null) return '<span class="null">null</span>';
      if (Array.isArray(obj)) {{
        if (obj.length === 0) return '<span class="bracket">[]</span>';
        let items = obj.map(v => jsonHighlight(v));
        return '<span class="bracket">[</span><br>' + items.map(v => '  ' + v).join(',<br>') + '<br><span class="bracket">]</span>';
      }}
      if (typeof obj === 'object') {{
        let keys = Object.keys(obj);
        if (keys.length === 0) return '<span class="bracket">{{}}</span>';
        let pairs = keys.map(k => '<span class="key">"' + k + '"</span>: ' + jsonHighlight(obj[k]));
        return '<span class="bracket">{{</span><br>' + pairs.map(p => '  ' + p).join(',<br>') + '<br><span class="bracket">}}</span>';
      }}
      return String(obj);
    }}

    let lastSources = null;

    async function fetchSources() {{
      const tmdb = document.getElementById('tmdb').value.trim();
      const title = document.getElementById('title').value.trim();
      const year = document.getElementById('year').value.trim();
      const type = document.getElementById('type').value;
      const season = document.getElementById('season').value.trim() || '1';
      const episode = document.getElementById('episode').value.trim() || '1';
      const imdb = document.getElementById('imdb').value.trim();

      if (!tmdb || !title) {{
        document.getElementById('output').innerHTML = '<span class="null">Fill in tmdbId and title</span>';
        return;
      }}

      const params = new URLSearchParams({{
        tmdbId: tmdb, mediaType: type, title, provider: activeProvider,
        ...(year && {{year}}), ...(type === 'tv' && {{seasonId: season, episodeId: episode}}),
        ...(imdb && {{imdbId: imdb}})
      }});

      const url = '/sources?' + params.toString();
      document.getElementById('req-url').textContent = 'GET ' + url;

      const statusDiv = document.getElementById('status');
      const output = document.getElementById('output');
      const playBtn = document.getElementById('play-btn');
      statusDiv.className = 'status-bar loading';
      statusDiv.innerHTML = '<span class="spinner"></span> Fetching from ' + activeProvider + '...';
      output.textContent = '';
      playBtn.style.display = 'none';
      lastSources = null;

      try {{
        const resp = await fetch(url);
        const data = await resp.json();
        if (resp.ok) {{
          statusDiv.className = 'status-bar ok';
          const count = data.data?.sources?.length || 0;
          const subCount = data.data?.subtitles?.length || 0;
          statusDiv.innerHTML = '✓ ' + activeProvider + ' — ' + count + ' source' + (count !== 1 ? 's' : '') + ', ' + subCount + ' subtitle' + (subCount !== 1 ? 's' : '');
          if (count > 0) {{
            lastSources = data.data.sources;
            playBtn.style.display = 'inline-block';
          }}
        }} else {{
          statusDiv.className = 'status-bar err';
          statusDiv.textContent = '✗ ' + (data.detail || data.error || 'Unknown error');
        }}
        output.innerHTML = jsonHighlight(data);
      }} catch (e) {{
        statusDiv.className = 'status-bar err';
        statusDiv.textContent = '✗ ' + e.message;
        output.textContent = e.message;
      }}
    }}

    function openPlayer() {{
      if (!lastSources || lastSources.length === 0) return;
      const json = JSON.stringify(lastSources);
      window.open('/player?sources=' + encodeURIComponent(json), '_blank');
    }}

    async function fetchSubtitles() {{
      const source = document.getElementById('sub-source').value;
      const tmdb = document.getElementById('tmdb').value.trim();
      const title = document.getElementById('title').value.trim();
      const year = document.getElementById('year').value.trim();
      const type = document.getElementById('type').value;
      const season = document.getElementById('season').value.trim() || '1';
      const episode = document.getElementById('episode').value.trim() || '1';
      const imdb = document.getElementById('imdb').value.trim();

      if (source === 'opensubtitles' && !imdb) {{
        alert("Please fill in imdbId for OpenSubtitles");
        return;
      }}
      if (source !== 'opensubtitles' && (!tmdb || !title)) {{
        alert("Please fill in tmdbId and title for " + source);
        return;
      }}
      
      const subBtn = document.getElementById('fetch-sub-btn');
      const osDropdown = document.getElementById('os-dropdown');
      
      subBtn.innerHTML = '<span class="spinner" style="margin-right:6px"></span> Fetching...';
      subBtn.disabled = true;
      osDropdown.style.display = 'none';
      osDropdown.innerHTML = '<option value="">-- Select Subtitle --</option>';
      
      const params = new URLSearchParams({{
        title, mediaType: type, tmdbId: tmdb,
        ...(year && {{year}}), ...(type === 'tv' && {{seasonId: season, episodeId: episode}}),
        ...(imdb && {{imdbId: imdb}})
      }});
      
      try {{
        const resp = await fetch('/subtitles/' + source + '?' + params.toString());
        const data = await resp.json();
        
        if (resp.ok && data.subtitles && data.subtitles.length > 0) {{
          data.subtitles.forEach(sub => {{
            const opt = document.createElement('option');
            opt.value = sub.url;
            opt.textContent = sub.language;
            osDropdown.appendChild(opt);
          }});
          osDropdown.style.display = 'block';
          subBtn.innerHTML = '💬 Subs (' + data.subtitles.length + ')';
        }} else {{
          subBtn.innerHTML = '💬 No Subs found';
        }}
      }} catch (e) {{
        subBtn.innerHTML = '💬 Error';
      }}
      subBtn.disabled = false;
    }}

    let lastMbSources = null;

    function setMbInput(val) {{
      document.getElementById('mb-input').value = val;
    }}

    async function fetchMoviebox() {{
      const inputVal = document.getElementById('mb-input').value.trim();
      const cookieVal = document.getElementById('mb-cookie').value.trim();
      if (!inputVal) {{
        document.getElementById('mb-output').innerHTML = '<span class="null">Fill in URL, slug, or subjectId</span>';
        return;
      }}
      const params = new URLSearchParams({{ url: inputVal, ...(cookieVal && {{ cookie: cookieVal }}) }});
      const url = '/moviebox/sources?' + params.toString();
      document.getElementById('mb-req-url').textContent = 'GET ' + url;
      const statusDiv = document.getElementById('mb-status');
      const output = document.getElementById('mb-output');
      const playBtn = document.getElementById('mb-play-btn');
      statusDiv.className = 'status-bar loading';
      statusDiv.innerHTML = '<span class="spinner"></span> Fetching from Moviebox...';
      output.textContent = '';
      playBtn.style.display = 'none';
      lastMbSources = null;
      try {{
        const resp = await fetch(url);
        const data = await resp.json();
        if (resp.ok) {{
          statusDiv.className = 'status-bar ok';
          const count = data.data?.sources?.length || 0;
          statusDiv.innerHTML = '✓ Moviebox — ' + count + ' source' + (count !== 1 ? 's' : '');
          if (count > 0) {{
            lastMbSources = data.data.sources;
            playBtn.style.display = 'inline-block';
          }}
        }} else {{
          statusDiv.className = 'status-bar err';
          statusDiv.textContent = '✗ ' + (data.detail || 'Unknown error');
        }}
        output.innerHTML = jsonHighlight(data);
      }} catch (e) {{
        statusDiv.className = 'status-bar err';
        statusDiv.textContent = '✗ ' + e.message;
        output.textContent = e.message;
      }}
    }}

    function openMbPlayer() {{
      if (!lastMbSources || lastMbSources.length === 0) return;
      const json = JSON.stringify(lastMbSources);
      window.open('/player?sources=' + encodeURIComponent(json), '_blank');
    }}

    function copyOsLink(select) {{
      if(select.value) {{
        navigator.clipboard.writeText(select.value);
        alert("Subtitle URL copied to clipboard!");
        select.value = "";
      }}
    }}

    document.addEventListener('keydown', e => {{ if (e.key === 'Enter') fetchSources(); }});
  </script>
</body>
</html>"""
