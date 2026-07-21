from __future__ import annotations

import json
import logging
import time
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlencode, quote

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from app.config import settings
from app.decrypt import decrypt as _decrypt
from app.models import DecryptedData, ErrorDetail, ProviderInfo, ProviderList, SourceParams, SourceResponse, SubtitleItem
from app.providers import AVAILABLE, PROVIDER_MAP, Provider
from app.opensubtitles import fetch_opensubtitles

logger = logging.getLogger("videasy")

SeedCache = dict[str, tuple[str, float]]


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
    
    import asyncio
    os_task = None
    if params.imdbId:
        os_task = asyncio.create_task(fetch_opensubtitles(app.state.client, params.imdbId))

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
    except (RuntimeError, json.JSONDecodeError, ValueError, KeyError) as e:
        raise HTTPException(status_code=502, detail=f"{prov.name}: {e}")

    decrypted_data = DecryptedData.from_raw(raw)
    
    if os_task:
        try:
            os_subs = await os_task
            if os_subs:
                decrypted_data.subtitles.extend(os_subs)
        except Exception as e:
            logger.error(f"Error awaiting OpenSubtitles task: {e}")

    return SourceResponse(
        tmdbId=tmdbId,
        provider=provider_name,
        data=decrypted_data,
    )


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
    summary="HLS stream proxy",
    description="Proxy HLS manifests and segments with correct Referer/Origin headers to bypass CDN 403 restrictions. Used by the frontend player.",
    tags=["Proxy"],
    response_class=StreamingResponse,
)
async def proxy_hls(
    url: str = Query(..., description="Full URL of the HLS resource to proxy"),
) -> StreamingResponse:
    """Stream any HLS URL through the backend with the configured Referer/Origin headers."""
    req = app.state.client.build_request("GET", url, headers=_build_headers())
    resp = await app.state.client.send(req, stream=True, follow_redirects=True)

    if resp.status_code == 403:
        await resp.aclose()
        raise HTTPException(status_code=403, detail=f"CDN rejected request for: {url}")
    if resp.status_code == 404:
        await resp.aclose()
        raise HTTPException(status_code=404, detail=f"Resource not found: {url}")

    content_type = resp.headers.get("content-type", "application/octet-stream")
    return StreamingResponse(
        resp.aiter_bytes(chunk_size=64 * 1024),
        status_code=resp.status_code,
        media_type=content_type,
        headers={
            "Access-Control-Allow-Origin": "*",
            "Cache-Control": "public, max-age=3600",
        },
    )


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def root() -> str:
    provider_options = "".join(
        f'<option value="{p.name}">{p.name}</option>'
        for p in AVAILABLE
    )

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
      <a href="#endpoints">Endpoints</a>
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
        <code>/providers</code>
        <span class="ep-desc">List providers</span>
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
      statusDiv.className = 'status-bar loading';
      statusDiv.innerHTML = '<span class="spinner"></span> Fetching from ' + activeProvider + '...';
      output.textContent = '';

      try {{
        const resp = await fetch(url);
        const data = await resp.json();
        if (resp.ok) {{
          statusDiv.className = 'status-bar ok';
          const count = data.data?.sources?.length || 0;
          const subCount = data.data?.subtitles?.length || 0;
          statusDiv.innerHTML = '✓ ' + activeProvider + ' — ' + count + ' source' + (count !== 1 ? 's' : '') + ', ' + subCount + ' subtitle' + (subCount !== 1 ? 's' : '');
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

    document.addEventListener('keydown', e => {{ if (e.key === 'Enter') fetchSources(); }});
  </script>
</body>
</html>"""
