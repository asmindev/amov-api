# Videasy Decryptor API

FastAPI server that decrypts video streams from **Videasy.net** (Wingsdatabase providers: `Yoru`, `Neon`, `Cypher`, `Breach`, `Moviebox`) and fetches sources from **TheMovieBox.xyz** (MP4, DASH, HLS streams + CDN subtitles).

---

## Architecture

```
                              ┌──────────────────────────────────────┐
                         ┌───▶│  /sources  (Videasy / Wingsdatabase)│
┌─────────┐              │    └──────────────────────────────────────┘
│         │              │                                         │
│ Client  │──────────────┤    ┌──────────────────────────────────────┐
│         │              ├───▶│  /moviebox/sources (TheMovieBox.xyz) │
└─────────┘              │    └──────────────────────────────────────┘
       │                 │
       │                 │    ┌──────────────────────────────────────┐
       │                 └───▶│  /proxy  (CDN stream proxy)         │
       │                      └──────────────────────────────────────┘
       │
       │  ┌──────────────────────────────────┐
       └─▶│  /subtitles  │  /opensubtitles  │
          └──────────────────────────────────┘
```

Two isolated httpx connection pools prevent proxy streams from starving API requests:

| Client | Purpose | Read Timeout | Max Connections |
|--------|---------|-------------|-----------------|
| `api_client` | `/sources`, `/moviebox/*`, `/subtitles`, decryption | 30s | 20 |
| `proxy_client` | `/proxy`, `/dash/*` streaming | unlimited | 100 |

Seeds and guest tokens are cached in-memory with TTL, minimizing upstream network load and avoiding rate limits.

---

## Quick Start

```bash
# Install dependencies
pip install -e .

# Run the server
uvicorn main:app --host 0.0.0.0 --port 8000
```

- **Web Player**: [http://localhost:8000/player](http://localhost:8000/player)
- **Swagger Docs**: [http://localhost:8000/docs](http://localhost:8000/docs)
- **ReDoc Docs**: [http://localhost:8000/redoc](http://localhost:8000/redoc)

### Environment Variables

All settings can be overridden via environment variables with the `VIDEASY_` prefix:

| Variable | Default | Description |
|----------|---------|-------------|
| `VIDEASY_API_BASE` | `https://api.wingsdatabase.com` | Wingsdatabase API base URL |
| `VIDEASY_DEC_API` | `https://enc-dec.app/api/dec-videasy` | Decryption endpoint |
| `VIDEASY_ORIGIN` | `https://player.videasy.to` | Default Origin header |
| `VIDEASY_REFERER` | `https://player.videasy.to/` | Default Referer header |
| `VIDEASY_REQUEST_TIMEOUT` | `30` | Global request timeout (seconds) |
| `VIDEASY_MOVIEBOX_API_BASE` | `https://h5-api.aoneroom.com` | Moviebox BFF API base |
| `VIDEASY_MOVIEBOX_PLAY_BASE` | `https://themoviebox.xyz` | Moviebox play endpoint base |

---

## API Reference

### `GET /sources` — Videasy / Wingsdatabase

Fetch decrypted HLS streams and subtitles for a movie or TV show.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `tmdbId` | string | yes | TMDB numerical ID |
| `mediaType` | string | yes | `movie` or `tv` |
| `provider` | string | yes | `Yoru`, `Neon`, `Cypher`, `Breach`, or `Moviebox` |
| `title` | string | | Media title (auto-resolved via Cinemeta if omitted) |
| `year` | string | | Release year (e.g. `2014`) |
| `seasonId` | string | | Season number, TV only (default: `1`) |
| `episodeId` | string | | Episode number, TV only (default: `1`) |
| `imdbId` | string | | IMDB ID (e.g. `tt0816692`) |

```bash
# Movie
curl "http://localhost:8000/sources?tmdbId=157336&mediaType=movie&provider=Yoru"

# TV Show
curl "http://localhost:8000/sources?tmdbId=1396&mediaType=tv&title=Breaking+Bad&year=2008&seasonId=1&episodeId=1&provider=Neon"
```

---

### `GET /moviebox/sources` — TheMovieBox.xyz

Fetch MP4 / DASH / HLS streams and CDN `.srt` subtitles. Supports reverse IMDB lookup.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `imdbId` | string | * | IMDB ID (e.g. `tt9018736` — auto-resolves to Moviebox) |
| `subjectId` | string | * | Moviebox internal numerical ID |
| `url` | string | * | Full `themoviebox.xyz` URL |
| `seasonId` | string | | Season number, TV only (default: `0`) |
| `episodeId` | string | | Episode number, TV only (default: `0`) |
| `cookie` | string | | Optional user cookie (e.g. `mb_token=...; token=...`) |

*\* Provide at least one of `imdbId`, `subjectId`, or `url`.*

```bash
# By IMDB ID
curl "http://localhost:8000/moviebox/sources?imdbId=tt9018736&seasonId=1&episodeId=1"

# By subjectId
curl "http://localhost:8000/moviebox/sources?subjectId=8313012068559605176"
```

---

### `GET /moviebox/search` — Search Moviebox

Search Moviebox catalog by keyword.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `q` | string | yes | Search keyword |
| `page` | integer | | Page number (default: `1`) |

```bash
curl "http://localhost:8000/moviebox/search?q=Avatar&page=1"
```

---

### Unified Response Schema

Both `/sources` and `/moviebox/sources` return the same `UnifiedMediaResponse`:

```json
{
  "meta": {
    "title": "Avatar: The Last Airbender",
    "provider": "Moviebox",
    "mediaType": "tv",
    "tmdbId": "82452",
    "imdbId": "tt9018736",
    "year": "2024",
    "cover": "https://pbcdnw.aoneroom.com/..."
  },
  "episode": { "season": 1, "episode": 1 },
  "sources": [
    {
      "quality": "1080p",
      "size": "619MB",
      "url": "https://bcdnw.hakunaymatata.com/...",
      "type": "mp4",
      "headers": null,
      "source": "play"
    },
    {
      "quality": "Auto",
      "size": null,
      "url": "http://localhost:8000/proxy?url=https%3A%2F%2F...",
      "type": "dash",
      "headers": { "X-MB-Token": "..." },
      "source": "play"
    }
  ],
  "subtitles": [
    { "lang": "en", "language": "English", "url": "https://cacdn.hakunaymatata.com/..." },
    { "lang": "id", "language": "Indonesian", "url": "https://cacdn.hakunaymatata.com/..." }
  ]
}
```

`episode` is `null` for movies. `size` is a human-readable string or `null`.

---

### `GET /subtitles` — List Subtitle Providers

```bash
curl "http://localhost:8000/subtitles"
# { "sources": ["yoru", "neon", "cypher", "breach", "moviebox", "opensubtitles"] }
```

### `GET /subtitles/{provider}` — Fetch Subtitles

Fetch subtitles from a specific video provider.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `title` | string | yes | Media title |
| `tmdbId` | string | yes | TMDB numerical ID |
| `mediaType` | string | | `movie` or `tv` (default: `movie`) |
| `seasonId` | string | | Season number (default: `1`) |
| `episodeId` | string | | Episode number (default: `1`) |
| `imdbId` | string | | IMDB ID |

### `GET /opensubtitles` — OpenSubtitles

Fetch subtitles from OpenSubtitles by IMDB ID.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `imdbId` | string | yes | IMDB ID (e.g. `tt1234567`) |

---

### `GET /proxy` — CDN Stream Proxy

Proxy HLS manifests, MP4 videos, DASH segments, and subtitle files with Range request forwarding and CORS bypass.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `url` | string | yes | Full URL of the resource to proxy |
| `headers` | string | | Optional JSON headers to forward |

DASH `.mpd` manifests are automatically rewritten to route segments through `/dash/{token}/...`.

---

### `GET /providers` — List Providers

```json
{
  "providers": [
    { "name": "Yoru", "endpoint": "cdn" },
    { "name": "Neon", "endpoint": "neon2" },
    { "name": "Cypher", "endpoint": "downloader2" },
    { "name": "Breach", "endpoint": "m4uhd" },
    { "name": "Moviebox", "endpoint": "moviebox" }
  ]
}
```

### `GET /health` — Health Check

```json
{ "status": "ok", "version": "3.0.0" }
```

---

## Project Structure

```
main.py                       # Uvicorn entry point
passenger_wsgi.py             # Passenger WSGI entry point
pyproject.toml                # Dependencies & build config
videasy/
  app.py                      # FastAPI factory, lifespan, middleware
  config.py                   # Pydantic settings (env vars)
  deps.py                     # Dependency injection helpers
  core/
    cache.py                  # In-memory TTLCache
    exceptions.py             # Global exception handlers
    http_client.py            # Dual httpx clients (api + proxy)
  models/
    media.py                  # UnifiedMediaResponse schema
    source.py                 # Wingsdatabase source models
    subtitle.py               # Subtitle model
    common.py                 # ProviderInfo, ErrorDetail
  features/
    sources/                  # Videasy / Wingsdatabase
      routes.py               # GET /sources
      service.py              # Seed fetch, decrypt pipeline
      providers.py            # Provider definitions
    moviebox/                 # TheMovieBox.xyz
      routes.py               # GET /moviebox/sources, /moviebox/search
      service.py              # Detail, play streams, IMDB resolution, captions
    subtitles/                # Subtitle fetching
      routes.py               # GET /subtitles, /subtitles/{provider}, /opensubtitles
    info/                     # System endpoints
      routes.py               # GET /providers, /health
    player/                   # Embedded web player
      routes.py               # GET /player, GET /
      templates/
        player.html           # HTML5 video player
        index.html            # Landing page
  integrations/
    decryption.py             # enc-dec.app decryption + M3U8 expansion
    opensubtitles.py          # OpenSubtitles API v1
  proxy/                      # CDN proxy
    routes.py                 # GET /proxy
    stream.py                 # Streaming proxy with Range support
    middleware.py              # DASH segment ASGI middleware
    headers.py                # Domain-specific CDN headers
  utils/
    m3u8.py                   # Master M3U8 expansion
    quality.py                # Quality normalization & sorting
tests/                        # Pytest suite
```

---

## Testing

```bash
.venv/bin/pytest
```

---

## License

MIT
