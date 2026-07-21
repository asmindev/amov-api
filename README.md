# Videasy Decryptor API

Headless FastAPI server that decrypts [Videasy.net](https://player.videasy.to) video streams. Picks a provider (`Yoru`, `Neon`, `Cypher`, or `Breach`), fetches encrypted sources from `api.wingsdatabase.com`, decrypts via `enc-dec.app`, and returns quality-labelled HLS streams + subtitles.

## Quick Start

```bash
pip install -e .
uvicorn main:app --host 0.0.0.0 --port 8080
```

## Usage

```http
GET /sources?tmdbId=157336&mediaType=movie&title=Interstellar&year=2014&provider=Yoru
```

### Parameters

| Param      | Required | Description |
|------------|----------|-------------|
| `tmdbId`   | ✓        | TMDB numerical ID |
| `mediaType`| ✓        | `movie` or `tv` |
| `title`    | ✓        | Media title |
| `provider` | ✓        | `Yoru`, `Neon`, `Cypher`, `Breach` |
| `year`     |          | Release year |
| `seasonId` |          | Season number (TV, default: 1) |
| `episodeId`|          | Episode number (TV, default: 1) |
| `imdbId`   |          | IMDB ID (e.g. `tt0816692`) |

### Response

```json
{
  "tmdbId": "157336",
  "provider": "Yoru",
  "data": {
    "sources": [
      {"quality": "4K", "url": "https://..."},
      {"quality": "1080p", "url": "https://..."}
    ],
    "subtitles": [
      {"lang": "En", "language": "En", "url": "https://..."}
    ]
  }
}
```

### Other Endpoints

| Endpoint      | Description |
|---------------|-------------|
| `GET /`       | Landing page with docs |
| `GET /health` | Health check |
| `GET /providers` | List active providers |

## Providers

| Name   | Endpoint       | Notes |
|--------|----------------|-------|
| Yoru   | `cdn`          | Best quality (up to 4K), most reliable |
| Neon   | `neon2`        | Lower quality, works for many titles |
| Cypher | `downloader2`  | Good fallback |
| Breach | `m4uhd`        | Direct streams, quality mapped (playhq→1080p, bk→480p) |

Try each provider manually if one fails — availability varies per title.

## Deployment

### Local (uvicorn)

```bash
uvicorn main:app --host 0.0.0.0 --port 8080
```

### Shared Hosting (Passenger)

1. Upload all files to your hosting
2. Set Python app entry point to `passenger_wsgi.py`
3. Install dependencies: `pip install -r requirements.txt` (or generate with `pip freeze > requirements.txt`)
4. Restart: `touch tmp/restart.txt`

## Architecture

```
Client → /sources → fetch seed (api.wingsdatabase.com)
                  → fetch cipher (api.wingsdatabase.com)
                  → decrypt (enc-dec.app)
                  → expand master m3u8 → sort by quality → JSON response
```

Seed is cached in-memory per `tmdbId` with TTL from upstream API to reduce redundant requests.

## Tech

- Python 3.11+ · FastAPI · httpx (async) · a2wsgi (Passenger bridge)
- No legacy WASM/JS dependencies — pure Python decryption pipeline
