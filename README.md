# Videasy & Moviebox Decryptor API

FastAPI server that decrypts video streams and fetches subtitles from **Videasy.net** (Wingsdatabase providers `Yoru`, `Neon`, `Cypher`, `Breach`) and **TheMovieBox.xyz** (Moviebox MP4 and DASH streams + CDN captions).

---

## 🏗️ Architecture

```
┌─────────┐    ┌──────────────────────────────────────────────┐
│ Client  │───▶│ /sources (Videasy) | /moviebox/sources      │
└─────────┘    └──────────────────────────────────────────────┘
                     │                                  │
          ┌──────────▼──────────┐            ┌──────────▼──────────┐
          │ Wingsdatabase       │            │ Moviebox BFF        │
          │ Decrypt Pipeline    │            │ API & Caption CDN   │
          └──────────┬──────────┘            └──────────┬──────────┘
                     │                                  │
                     └────────────────┬─────────────────┘
                                      │
                           ┌──────────▼──────────┐
                           │ UnifiedMediaResponse│
                           │  (meta, episode,    │
                           │  sources, subtitles)│
                           └─────────────────────┘
```

Seeds and tokens are cached in-memory with TTL, minimizing upstream network load and avoiding rate limits.

---

## ⚡ Quick Start

```bash
# Install dependencies
pip install -e .

# Run the server
uvicorn main:app --host 0.0.0.0 --port 8000
```

- **Interactive Web Player**: [http://localhost:8000/player](http://localhost:8000/player)
- **Swagger OpenAPI Docs**: [http://localhost:8000/docs](http://localhost:8000/docs)

---

## 📖 API Reference

### 1. `GET /sources` (Videasy / Wingsdatabase)

Fetch decrypted HLS streams and subtitles for a movie or TV show using Wingsdatabase providers (`Yoru`, `Neon`, `Cypher`, `Breach`).

#### Query Parameters

| Parameter    | Type   | Required | Description |
|-------------|--------|----------|-------------|
| `tmdbId`    | string | ✓        | TMDB numerical ID (e.g. `157336` for Interstellar) |
| `mediaType` | string | ✓        | `movie` or `tv` |
| `provider`  | string | ✓        | `Yoru`, `Neon`, `Cypher`, or `Breach` |
| `title`     | string |          | Media title (optional if `tmdbId` or `imdbId` is provided) |
| `year`      | string |          | Release year (e.g. `2014`) |
| `seasonId`  | string |          | Season number — TV only (default: `1`) |
| `episodeId` | string |          | Episode number — TV only (default: `1`) |
| `imdbId`    | string |          | IMDB ID (e.g. `tt0816692`) |

#### Example Request
```bash
# Movie
curl "http://localhost:8000/sources?tmdbId=157336&mediaType=movie&imdbId=tt0816692&provider=Yoru"

# TV Show
curl "http://localhost:8000/sources?tmdbId=1396&mediaType=tv&title=Breaking+Bad&year=2008&seasonId=1&episodeId=1&provider=Yoru"
```

---

### 2. `GET /moviebox/sources` (TheMovieBox.xyz)

Fetch MP4 / DASH streams and CDN `.srt` subtitles for Moviebox titles. Supports fetching by `imdbId` (automatic reverse lookup), `subjectId`, or full URL.

#### Query Parameters

| Parameter   | Type   | Required | Description |
|------------|--------|----------|-------------|
| `imdbId`   | string | *        | IMDB ID (e.g. `tt9018736` or `tt33311069` — automatically resolves to Moviebox title) |
| `subjectId`| string | *        | Moviebox internal numerical ID (e.g. `7850278583678682192`) |
| `url`      | string | *        | Full `themoviebox.xyz` URL |
| `seasonId` | string |          | Season number for TV series (default: `0`) |
| `episodeId`| string |          | Episode number for TV series (default: `0`) |
| `cookie`   | string |          | Optional user cookie header |

*\* Provide at least one of `imdbId`, `subjectId`, or `url`.*

#### Example Request
```bash
# Query by IMDB ID (Reverse Lookup)
curl "http://localhost:8000/moviebox/sources?imdbId=tt33311069"

# Query TV Episode by IMDB ID
curl "http://localhost:8000/moviebox/sources?imdbId=tt9018736&seasonId=1&episodeId=1"
```

---

### 🌟 Unified Response Schema (`UnifiedMediaResponse`)

Both `/sources` and `/moviebox/sources` return the identical, standardized JSON schema:

```json
{
  "meta": {
    "title": "Avatar: The Last Airbender S1-S2",
    "provider": "Moviebox",
    "mediaType": "tv",
    "tmdbId": null,
    "imdbId": "tt9018736",
    "year": "2024",
    "cover": "https://pbcdnw.aoneroom.com/..."
  },
  "episode": {
    "season": 1,
    "episode": 1
  },
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
      "url": "http://localhost:8000/proxy?url=https%3A%2F%2Fsbcdnw2.hakunaymatata.com%2Fdash%2Findex_web.mpd",
      "type": "dash",
      "headers": {
        "X-MB-Token": "Edge-Cache-Cookie=..."
      },
      "source": "play"
    }
  ],
  "subtitles": [
    {
      "lang": "id",
      "language": "Indonesian",
      "url": "https://cacdn.hakunaymatata.com/subtitle/c1ff715920d380eb177494461a8958d5.srt?Policy=..."
    },
    {
      "lang": "en",
      "language": "English",
      "url": "https://cacdn.hakunaymatata.com/subtitle/343a6d0628e27cd7a592fbf74181c424.srt?Policy=..."
    }
  ]
}
```

*Note: `episode` is `null` for movies (`mediaType: "movie"`). Field `size` represents file size (e.g., `"619MB"`, `"2106MB"`) or `null` if unavailable.*

---

### 3. `GET /moviebox/search`

Search Moviebox catalog by keyword.

```bash
curl "http://localhost:8000/moviebox/search?q=Avatar&page=1"
```

---

### 4. `GET /player`

Embedded HTML5 Web Player interface supporting video quality selection, HLS/MP4 playback, and WebVTT/SRT subtitle track switching.

```bash
http://localhost:8000/player?sources=[...]
```

---

### 5. `GET /proxy`

Proxy endpoint for streaming video segments, `.m4s` DASH segments, and WebVTT/SRT subtitles with custom `Origin` and `Referer` headers to bypass CORS and CDN protections.

---

### 6. `GET /providers`

List available Wingsdatabase providers.

```json
{
  "providers": [
    {"name": "Yoru", "endpoint": "cdn"},
    {"name": "Neon", "endpoint": "neon2"},
    {"name": "Cypher", "endpoint": "downloader2"},
    {"name": "Breach", "endpoint": "m4uhd"}
  ]
}
```

---

### 7. `GET /health`

```json
{"status": "ok", "version": "2.0.0"}
```

---

## 🛠️ Project Structure

```
main.py                      # Uvicorn entry point
passenger_wsgi.py            # Passenger WSGI entry point
pyproject.toml               # Dependencies & build config
videasy/
├── app.py                   # FastAPI application factory & lifespan
├── config.py                # App settings & URLs
├── deps.py                  # Dependency injection helpers
├── core/
│   └── cache.py             # In-memory TTLCache implementation
├── models/
│   ├── media.py             # UnifiedMediaResponse models (meta, episode, sources with size, subtitles)
│   ├── source.py            # Wingsdatabase source models
│   ├── subtitle.py          # Subtitle models
│   └── common.py            # Common provider info models
├── features/
│   ├── sources/             # Videasy / Wingsdatabase feature
│   │   ├── routes.py
│   │   ├── service.py
│   │   └── providers.py
│   ├── moviebox/            # Moviebox feature
│   │   ├── routes.py
│   │   └── service.py
│   └── player/              # Player feature
│       ├── routes.py
│       └── templates/player.html
└── proxy/                   # CDN Proxy feature
    ├── routes.py
    ├── middleware.py
    └── headers.py
tests/                       # Pytest suite
```

---

## 🧪 Testing

Run the full pytest suite:

```bash
.venv/bin/pytest
```

---

## 📜 License

MIT
