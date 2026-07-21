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
from fastapi.responses import HTMLResponse, JSONResponse

from app.config import settings
from app.decrypt import decrypt as _decrypt
from app.models import DecryptedData, ErrorDetail, ProviderInfo, ProviderList, SourceParams, SourceResponse
from app.providers import AVAILABLE, PROVIDER_MAP, Provider

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
    version="2.0.0",
    lifespan=lifespan,
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


@app.get("/sources", response_model=SourceResponse, responses={400: {"model": ErrorDetail}, 502: {"model": ErrorDetail}, 429: {"model": ErrorDetail}})
async def get_sources(
    title: str = Query(..., min_length=1),
    mediaType: str = Query(..., pattern=r"^(movie|tv)$"),
    tmdbId: str = Query(..., pattern=r"^\d+$"),
    provider: str = Query(..., min_length=1),
    year: str = Query(default="", pattern=r"^\d{4}$|^$"),
    episodeId: str = Query(default="1", pattern=r"^\d+$"),
    seasonId: str = Query(default="1", pattern=r"^\d+$"),
    imdbId: str = Query(default="", pattern=r"^tt\d+$|^$"),
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
    except (RuntimeError, json.JSONDecodeError, ValueError, KeyError) as e:
        raise HTTPException(status_code=502, detail=f"{prov.name}: {e}")

    return SourceResponse(
        tmdbId=tmdbId,
        provider=provider_name,
        data=DecryptedData.from_raw(raw),
    )


@app.get("/providers", response_model=ProviderList)
async def list_providers() -> ProviderList:
    return ProviderList(
        providers=[ProviderInfo(name=p.name, endpoint=p.endpoint) for p in AVAILABLE]
    )


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "version": "2.0.0"}


@app.get("/", response_class=HTMLResponse)
async def root() -> str:
    provider_rows = "".join(
        f'<tr><td><code>{p.name}</code></td>'
        f'<td><span class="badge-green">Active</span></td>'
        f'<td><code>{p.endpoint}</code></td></tr>'
        for p in AVAILABLE
    )

    provider_links = " ".join(
        f'<a href="/sources?title=Interstellar&mediaType=movie&year=2014&tmdbId=157336&provider={p.name}" class="prov-link">{p.name} ↗</a>'
        for p in AVAILABLE
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Videasy Decryptor API</title>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
  <style>
    :root {{
      --bg: #030712; --panel: #0f172a; --card: #1e293b;
      --text: #f8fafc; --muted: #94a3b8;
      --accent: #38bdf8; --grad: linear-gradient(135deg,#0ea5e9,#6366f1);
      --border: rgba(255,255,255,0.08);
    }}
    *{{box-sizing:border-box;margin:0;padding:0}}
    body{{font-family:'Inter',sans-serif;background:var(--bg);color:var(--text);line-height:1.7;-webkit-font-smoothing:antialiased}}
    .nav{{padding:18px 32px;border-bottom:1px solid var(--border);background:rgba(3,7,18,.85);backdrop-filter:blur(12px);position:sticky;top:0;z-index:50;display:flex;align-items:center;gap:12px}}
    .logo-text{{font-size:1.2rem;font-weight:700;letter-spacing:-.02em}}
    .hero{{padding:90px 32px 50px;text-align:center}}
    .badge{{display:inline-block;background:rgba(56,189,248,.1);color:var(--accent);padding:5px 14px;border-radius:20px;font-size:.82rem;font-weight:600;letter-spacing:.05em;border:1px solid rgba(56,189,248,.2);margin-bottom:22px}}
    h1{{font-size:clamp(2.5rem,6vw,4rem);font-weight:800;letter-spacing:-.04em;background:var(--grad);-webkit-background-clip:text;-webkit-text-fill-color:transparent;line-height:1.1;margin-bottom:18px}}
    .hero p{{font-size:1.15rem;color:var(--muted);max-width:580px;margin:0 auto}}
    .container{{max-width:960px;margin:0 auto;padding:20px 32px 80px}}
    h2{{font-size:1.6rem;font-weight:600;margin:50px 0 20px;letter-spacing:-.02em;display:flex;align-items:center;gap:14px}}
    h2::after{{content:'';flex:1;height:1px;background:var(--border)}}
    .grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:20px}}
    .card{{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:28px;transition:.2s}}
    .card:hover{{transform:translateY(-3px);box-shadow:0 16px 36px rgba(0,0,0,.35);border-color:rgba(56,189,248,.25)}}
    .card-num{{width:36px;height:36px;border-radius:50%;background:rgba(56,189,248,.1);color:var(--accent);display:flex;align-items:center;justify-content:center;font-weight:700;margin-bottom:18px;border:1px solid rgba(56,189,248,.2)}}
    .card h3{{margin-bottom:8px;font-size:1.1rem}}
    .card p{{color:var(--muted);font-size:.9rem}}
    .endpoint-box{{background:var(--panel);border:1px solid var(--border);border-radius:14px;overflow:hidden;margin-top:24px}}
    .ep-head{{padding:16px 22px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:14px}}
    .get{{background:rgba(16,185,129,.15);color:#34d399;padding:4px 12px;border-radius:7px;font-weight:700;font-size:.85rem;border:1px solid rgba(16,185,129,.2)}}
    .ep-url{{font-family:'JetBrains Mono',monospace;font-size:1rem;color:#e2e8f0}}
    table{{width:100%;border-collapse:collapse}}
    th,td{{padding:14px 22px;text-align:left;border-bottom:1px solid var(--border)}}
    th{{font-size:.72rem;text-transform:uppercase;letter-spacing:.1em;color:var(--muted);font-weight:600;background:rgba(255,255,255,.02)}}
    tr:last-child td{{border-bottom:none}}
    code{{font-family:'JetBrains Mono',monospace;background:rgba(255,255,255,.06);padding:3px 7px;border-radius:5px;font-size:.88em;border:1px solid rgba(255,255,255,.05)}}
    .prov-link{{font-size:.84rem;font-weight:600;color:var(--accent);text-decoration:none;background:rgba(56,189,248,.08);border:1px solid rgba(56,189,248,.2);padding:4px 12px;border-radius:7px;transition:.15s;display:inline-block}}
    .prov-link:hover{{background:rgba(56,189,248,.18)}}
    .badge-green{{background:rgba(16,185,129,.15);color:#34d399;padding:2px 8px;border-radius:5px;font-size:.8em;font-weight:600;border:1px solid rgba(16,185,129,.2)}}
    .ex-box{{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:24px;margin-top:16px}}
    .ex-box h4{{margin-bottom:8px;font-size:1.05rem;font-weight:600}}
    .ex-box p{{color:var(--muted);font-size:.9rem;margin-bottom:12px}}
    .ex-box code{{display:block;padding:10px;font-size:.78rem;overflow-x:auto;white-space:nowrap;margin-bottom:12px;background:var(--panel)}}
    .ex-links{{display:flex;gap:10px;flex-wrap:wrap}}
    .footer{{text-align:center;padding:40px 32px;border-top:1px solid var(--border);color:var(--muted);font-size:.9rem;margin-top:30px}}
    .footer span{{color:var(--accent)}}
  </style>
</head>
<body>
  <nav class="nav">
    <div class="logo-text">Videasy Decryptor API</div>
  </nav>

  <div class="hero">
    <div class="badge">V2.0 · ASYNC · PICK-YOUR-PROVIDER</div>
    <h1>Stream Unlocker</h1>
    <p>Async decryption engine. Choose a provider and get quality-labelled HLS streams.</p>
  </div>

  <div class="container">
    <h2>How it works</h2>
    <div class="grid">
      <div class="card"><div class="card-num">1</div><h3>Find Media</h3><p>Get the <strong>TMDB ID</strong> from themoviedb.org for any movie or TV show.</p></div>
      <div class="card"><div class="card-num">2</div><h3>Pick Provider</h3><p>Choose <strong>Yoru</strong>, <strong>Neon</strong>, <strong>Cypher</strong>, or <strong>Breach</strong>. Hit <code>/sources</code> with your chosen provider.</p></div>
      <div class="card"><div class="card-num">3</div><h3>Get Streams</h3><p>Receive clean JSON with quality-labelled <code>.m3u8</code> URLs and subtitle tracks.</p></div>
    </div>

    <h2>Endpoint</h2>
    <div class="endpoint-box">
      <div class="ep-head"><span class="get">GET</span><span class="ep-url">/sources</span></div>
      <table>
        <tr><th>Parameter</th><th>Type</th><th>Required</th><th>Description</th></tr>
        <tr><td><code>tmdbId</code></td><td><span class="get">int</span></td><td>✓</td><td>TMDB numerical ID</td></tr>
        <tr><td><code>mediaType</code></td><td><span class="get">string</span></td><td>✓</td><td><code>movie</code> or <code>tv</code></td></tr>
        <tr><td><code>title</code></td><td><span class="get">string</span></td><td>✓</td><td>Media title</td></tr>
        <tr><td><code>provider</code></td><td><span class="get">string</span></td><td>✓</td><td><code>Yoru</code>, <code>Neon</code>, <code>Cypher</code>, <code>Breach</code></td></tr>
        <tr><td><code>year</code></td><td>string</td><td></td><td>Release year (e.g. <code>2014</code>)</td></tr>
        <tr><td><code>seasonId</code></td><td>string</td><td></td><td>Season — TV only (default: 1)</td></tr>
        <tr><td><code>episodeId</code></td><td>string</td><td></td><td>Episode — TV only (default: 1)</td></tr>
        <tr><td><code>imdbId</code></td><td>string</td><td></td><td>IMDB ID (e.g. <code>tt0816692</code>)</td></tr>
      </table>
    </div>

    <h2>Providers</h2>
    <div class="endpoint-box">
      <table>
        <tr><th>Name</th><th>Status</th><th>Endpoint</th></tr>
        {provider_rows}
      </table>
    </div>

    <h2>Example — Interstellar</h2>
    <div class="ex-box">
      <h4>Interstellar <span style="font-size:.75rem;background:rgba(255,255,255,.08);padding:2px 7px;border-radius:12px;font-weight:600">MOVIE</span></h4>
      <p>TMDB ID: 157336</p>
      <code>GET /sources?title=Interstellar&mediaType=movie&year=2014&tmdbId=157336&imdbId=tt0816692&provider=Yoru</code>
      <div class="ex-links">{provider_links}</div>
    </div>

    <h2>Health</h2>
    <div class="endpoint-box">
      <div class="ep-head"><span class="get">GET</span><span class="ep-url">/health</span></div>
    </div>

    <h2>Other Endpoints</h2>
    <div class="endpoint-box">
      <div class="ep-head"><span class="get">GET</span><span class="ep-url">/providers</span></div>
    </div>
  </div>

  <div class="footer">Videasy Decryptor API v2.0</div>
</body>
</html>"""
