from __future__ import annotations

import logging
import re
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from videasy.config import settings
from videasy.utils.titles import normalize_title

logger = logging.getLogger("moviebox")

# Config aliases
API_BASE = settings.moviebox_api_base
DETAIL_ENDPOINT = settings.moviebox_detail_endpoint
SEARCH_ENDPOINT = settings.moviebox_search_endpoint
SITE_BASE = settings.moviebox_site_base.rstrip("/")
PLAY_BASE = SITE_BASE
PLAY_ENDPOINT = settings.moviebox_play_endpoint
CINEMETA_BASE = settings.cinemeta_base.rstrip("/")

# In-memory guest JWT token cache (token, expires_at)
_guest_token: list[str] = [""]  # mutable container so it's writable from async funcs
_guest_token_expiry: float = 0.0  # monotonic timestamp when token expires
_GUEST_TOKEN_TTL: float = 300.0  # 5 minutes

_QUALITY_MAP: dict[int, str] = {
    360: "360p",
    480: "480p",
    720: "720p",
    1080: "1080p",
    2160: "4K",
}

_ORDER: dict[str, int] = {"4K": 0, "1080p": 1, "720p": 2, "480p": 3, "360p": 4}

# Supported themoviebox.xyz URL patterns
# e.g. https://themoviebox.xyz/movies/some-title-SLUG?id=12345&...
# e.g. https://themoviebox.xyz/tv/some-title-SLUG?id=12345&...
_MOVIEBOX_URL_RE = re.compile(
    r"(?:https?://)?(?:www\.)?themoviebox\.xyz"
    r"/(?P<media_type>movies|tv)"
    r"/(?P<slug>[^/?#]+)"
    r"(?:\?(?P<qs>.+))?"
)


class MovieboxInput:
    """Parsed input for TheMovieBox queries."""
    subject_id: str | None
    detail_path: str | None
    media_type: str  # "movie" | "tv"
    se: int
    ep: int

    def __init__(
        self,
        subject_id: str | None,
        detail_path: str | None,
        media_type: str = "movie",
        se: int = 0,
        ep: int = 0,
    ):
        self.subject_id = subject_id
        self.detail_path = detail_path
        self.media_type = media_type
        self.se = se
        self.ep = ep


def parse_moviebox_input(url_or_id: str) -> MovieboxInput:
    """
    Parse a TheMovieBox URL or raw ID into a MovieboxInput.

    Accepted inputs:
    - Numeric subjectId (e.g. ``8313012068559605176``)
    - Full themoviebox.xyz URL:
      ``https://themoviebox.xyz/movies/title-slug-HASH?id=8313...&type=/movie/detail``
    - Slug path (e.g. ``check-out-sekarang-pay-later-caper-QMd0JfnG5U9``)
    """
    raw = url_or_id.strip()

    # Plain numeric ID
    if raw.isdigit():
        return MovieboxInput(subject_id=raw, detail_path=None)

    def _parse_se_ep(qs_map: dict) -> tuple[int, int]:
        s_val = (qs_map.get("detailSe") or qs_map.get("se") or [0])[0]
        e_val = (qs_map.get("detailEp") or qs_map.get("ep") or [0])[0]
        s_int = int(s_val) if str(s_val).isdigit() else 0
        e_int = int(e_val) if str(e_val).isdigit() else 0
        return (s_int, e_int)

    # Full URL
    m = _MOVIEBOX_URL_RE.match(raw)
    if m:
        media_type = "tv" if m.group("media_type") == "tv" else "movie"
        slug = m.group("slug")
        qs_str = m.group("qs") or ""
        qs = parse_qs(qs_str)
        # prefer numeric id from ?id= param
        ids = qs.get("id", [])
        subject_id = ids[0] if ids and ids[0].isdigit() else None
        s_int, e_int = _parse_se_ep(qs)
        return MovieboxInput(subject_id=subject_id, detail_path=slug, media_type=media_type, se=s_int, ep=e_int)

    # Fallback: try parsing as generic URL with ?id= param
    parsed = urlparse(raw)
    if parsed.scheme in ("http", "https"):
        qs = parse_qs(parsed.query)
        ids = qs.get("id", [])
        subject_id = ids[0] if ids and ids[0].isdigit() else None
        s_int, e_int = _parse_se_ep(qs)
        return MovieboxInput(subject_id=subject_id, detail_path=None, se=s_int, ep=e_int)

    # Slug-like string (no spaces, no scheme)
    if "/" not in raw and " " not in raw and not raw.startswith("http"):
        return MovieboxInput(subject_id=None, detail_path=raw)

    raise ValueError(
        "Cannot parse input. Provide a numeric subjectId, a themoviebox.xyz URL "
        "(e.g. https://themoviebox.xyz/movies/title-SLUG?id=12345), or a slug "
        "(e.g. check-out-sekarang-pay-later-caper-QMd0JfnG5U9)."
    )


def _build_headers(referer: str | None = None) -> dict[str, str]:
    ref = referer or f"{SITE_BASE}/"
    return {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:152.0) Gecko/20100101 Firefox/152.0",
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": SITE_BASE,
        "Referer": ref,
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "Sec-GPC": "1",
        "X-Client-Info": '{"timezone":"Asia/Makassar"}',
    }


def _guess_quality_from_str(s: str) -> str | None:
    """Guess video quality from a URL or title string."""
    m = re.search(r"[_-](\d+)[Pp]", s)
    if m:
        q = int(m.group(1))
        return _QUALITY_MAP.get(q, f"{q}p")
    m = re.search(r"[.-](ld|sd|hd|4k|uhd)[.-]", s.lower())
    if m:
        qmap = {"ld": "360p", "sd": "480p", "hd": "720p", "4k": "4K", "uhd": "4K"}
        return qmap.get(m.group(1))
    return None


async def fetch_detail(
    client: httpx.AsyncClient, *, subject_id: str | None = None, detail_path: str | None = None
) -> dict[str, Any]:
    """Fetch subject detail from the Aoneroom API.

    One of ``subject_id`` or ``detail_path`` must be provided.
    """
    urls_to_try = []
    if subject_id:
        urls_to_try.append((f"{API_BASE}{DETAIL_ENDPOINT}?subjectId={subject_id}", f"subjectId={subject_id}"))
    if detail_path:
        urls_to_try.append((f"{API_BASE}{DETAIL_ENDPOINT}?detailPath={detail_path}", f"detailPath={detail_path}"))

    if not urls_to_try:
        raise ValueError("Either subject_id or detail_path must be provided")

    last_exc = None
    for url, desc in urls_to_try:
        logger.info("fetching moviebox detail: %s", desc)
        try:
            resp = await client.get(url, headers=_build_headers(), timeout=15.0)
            if resp.status_code == 429:
                raise RuntimeError("rate limited by themoviebox API")
            if resp.status_code == 404:
                continue
            resp.raise_for_status()
            data = resp.json()
            if data.get("code") == 0 and data.get("data"):
                return data["data"]
        except httpx.HTTPError as e:
            last_exc = e

    if last_exc:
        raise last_exc
    raise RuntimeError("Moviebox detail not found (404)")


def _extract_token_from_response(resp: httpx.Response, client: httpx.AsyncClient | None = None) -> str:
    """Safely extract token from response Set-Cookie headers or client cookies without CookieConflict error."""
    for header in resp.headers.get_list("set-cookie"):
        m = re.search(r"token=([A-Za-z0-9._-]+)", header)
        if m:
            return m.group(1)
    if client:
        try:
            tok = client.cookies.get("token")
            if tok:
                return tok
        except Exception:
            pass
    try:
        token = resp.cookies.get("token")
        if token:
            return token
    except Exception:
        pass
    return ""


def _invalidate_guest_token(client: httpx.AsyncClient | None = None) -> None:
    """Clear the cached guest token so it is re-fetched on next use."""
    global _guest_token_expiry
    _guest_token[0] = ""
    _guest_token_expiry = 0.0
    if client:
        try:
            client.cookies.clear()
        except Exception:
            pass


async def ensure_guest_token(client: httpx.AsyncClient, force_refresh: bool = False) -> str:
    """Fetch and cache a fresh guest JWT token from /wefeed-h5api-bff/country-code if needed."""
    global _guest_token_expiry
    if force_refresh:
        _invalidate_guest_token(client)
    elif _guest_token[0] and time.monotonic() < _guest_token_expiry:
        return _guest_token[0]

    try:
        url = f"{API_BASE}/wefeed-h5api-bff/country-code"
        resp = await client.get(url, headers=_build_headers(), timeout=10.0)
        if resp.status_code == 200:
            token = _extract_token_from_response(resp, client)
            if token:
                _guest_token[0] = token
                _guest_token_expiry = time.monotonic() + _GUEST_TOKEN_TTL
                logger.debug("automatically initialized guest JWT token via country-code")
                return token
    except Exception as exc:
        logger.debug("failed to initialize guest token via country-code: %s", exc)

    try:
        url = f"{API_BASE}/wefeed-h5api-bff/subject/play?subjectId=8313012068559605176"
        resp = await client.get(url, headers=_build_headers(), timeout=10.0)
        if resp.status_code == 200:
            token = _extract_token_from_response(resp, client)
            if token:
                _guest_token[0] = token
                _guest_token_expiry = time.monotonic() + _GUEST_TOKEN_TTL
                logger.debug("automatically initialized guest JWT token via play fallback")
                return token
    except Exception as exc:
        logger.debug("failed to initialize guest token via play fallback: %s", exc)

    return ""


async def fetch_play_streams(
    client: httpx.AsyncClient,
    subject_id: str,
    detail_path: str = "",
    se: int = 0,
    ep: int = 0,
    lang: str = "en",
    cookie: str = "",
) -> list[dict[str, Any]]:
    """Fetch video streams via the TheMovieBox play endpoint.

    Automatically acquires a fresh session token via /country-code, encodes it
    using Nuxt cookie format, and performs a 2-hop session handshake to extract
    direct 1080p/480p/360p/DASH/HLS video streams completely without user intervention.
    """
    global _guest_token_expiry
    from urllib.parse import quote
    import json

    qs_parts = [f"subjectId={subject_id}", f"se={se}", f"ep={ep}", "streamSignType=1"]
    if detail_path:
        qs_parts.append(f"detailPath={detail_path}")
    if lang:
        qs_parts.append(f"lang={lang}")
    qs = "&".join(qs_parts)
    url = f"{PLAY_BASE}{PLAY_ENDPOINT}?{qs}"
    logger.info("fetching moviebox play: subjectId=%s se=%s ep=%s", subject_id, se, ep)

    full_referer = (
        f"{SITE_BASE}/movies/{detail_path}?id={subject_id}&type=/movie/detail&detailSe={se}&detailEp={ep}&lang={lang}"
        if detail_path else f"{SITE_BASE}/"
    )
    headers = _build_headers(referer=full_referer)
    cookie_str = cookie.strip()

    if cookie_str:
        # If user provided a raw JWT token (starts with eyJ), format with Nuxt quote(json.dumps())
        if cookie_str.startswith("eyJ") and "token=" not in cookie_str:
            enc_tok = quote(json.dumps(cookie_str))
            cookie_str = f'i18n_lang=en; mb_token={enc_tok}'
        headers["Cookie"] = cookie_str
    else:
        # 100% Automated token acquisition
        active_tok = await ensure_guest_token(client)
        if active_tok:
            enc_tok = quote(json.dumps(active_tok))
            headers["Cookie"] = f'i18n_lang=en; mb_token={enc_tok}; token={active_tok}'

    try:
        resp = await client.get(url, headers=headers, timeout=20.0)
        resp.raise_for_status()

        # Step 1: Extract returned token from response
        returned_token = _extract_token_from_response(resp)
        if returned_token:
            _guest_token[0] = returned_token
            _guest_token_expiry = time.monotonic() + _GUEST_TOKEN_TTL

        data = resp.json()
        play_data = data.get("data", {})
        raw_streams = play_data.get("streams", []) or play_data.get("hls", []) or play_data.get("dash", [])

        # Step 2: If dash array is missing/empty but server issued a session token, perform automatic 2-hop handshake to unlock DASH
        if returned_token and not play_data.get("dash"):
            enc_ret_tok = quote(json.dumps(returned_token))
            headers["Cookie"] = f'i18n_lang=en; mb_token={enc_ret_tok}; token={returned_token}'
            logger.info("performing automatic 2-hop token handshake to unlock DASH streams...")
            resp_retry = await client.get(url, headers=headers, timeout=15.0)
            if resp_retry.status_code == 200:
                data = resp_retry.json()
                play_data = data.get("data", {})

        if data.get("code") != 0:
            return []
        streams = []

        # HLS streams
        for hls in play_data.get("hls", []):
            stream_url = hls.get("url", "")
            if not stream_url:
                continue
            resolution = hls.get("resolutions") or hls.get("resolution", 0)
            if isinstance(resolution, str) and resolution.isdigit():
                resolution = int(resolution)
            quality = _QUALITY_MAP.get(resolution, f"{resolution}p") if isinstance(resolution, int) and resolution > 0 else "Auto"
            sign_cookie = hls.get("signCookie") or ""
            sign_header_key = hls.get("signHeaderKey") or "X-MB-Token"
            st_item: dict[str, Any] = {"quality": quality, "url": stream_url, "type": "hls"}
            if hls.get("id"):
                st_item["id"] = str(hls["id"])
            if sign_cookie:
                st_item["headers"] = {sign_header_key: sign_cookie}
            streams.append(st_item)

        # DASH streams
        for dash in play_data.get("dash", []):
            stream_url = dash.get("url", "")
            if not stream_url:
                continue
            resolution = dash.get("resolutions") or dash.get("resolution", 0)
            if isinstance(resolution, str) and resolution.isdigit():
                resolution = int(resolution)
            quality_base = _QUALITY_MAP.get(resolution, f"{resolution}p") if isinstance(resolution, int) and resolution > 0 else "Auto"
            quality = f"{quality_base} (DASH)" if "DASH" not in quality_base else quality_base
            sign_cookie = dash.get("signCookie") or ""
            sign_header_key = dash.get("signHeaderKey") or "X-MB-Token"
            st_item = {"quality": quality, "url": stream_url, "type": "dash"}
            if dash.get("id"):
                st_item["id"] = str(dash["id"])
            if sign_cookie:
                st_item["headers"] = {sign_header_key: sign_cookie}
            streams.append(st_item)

        # Generic streams list
        for s in play_data.get("streams", []):
            stream_url = s.get("url", "")
            if not stream_url:
                continue
            res_val = s.get("resolutions") or s.get("resolution") or s.get("height", 0)
            if isinstance(res_val, str) and res_val.isdigit():
                res_val = int(res_val)
            quality = _QUALITY_MAP.get(res_val, f"{res_val}p") if isinstance(res_val, int) and res_val > 0 else "Auto"
            size_mb = int(s.get("size", 0)) / (1024 * 1024) if s.get("size") else 0
            size_label = f" ({size_mb:.0f}MB)" if size_mb > 0 else ""
            sign_cookie = s.get("signCookie") or ""
            sign_header_key = s.get("signHeaderKey") or "X-MB-Token"
            st_item = {"quality": f"{quality}{size_label}", "url": stream_url, "type": "mp4"}
            if s.get("id"):
                st_item["id"] = str(s["id"])
            if sign_cookie:
                st_item["headers"] = {sign_header_key: sign_cookie}
            streams.append(st_item)

        return streams

    except Exception as exc:
        logger.debug("play endpoint returned error: %s", exc)
        return []


def extract_sources(detail: dict[str, Any], include_play_streams: list[dict] | None = None) -> dict[str, Any]:
    """Extract all video sources and metadata from a detail response."""
    subject = detail.get("subject", {})
    resource = detail.get("resource", {})
    trailer = subject.get("trailer", {})

    title = subject.get("title", "")
    cover_url = subject.get("cover", {}).get("url", "") if isinstance(subject.get("cover"), dict) else ""
    sources: list[dict[str, Any]] = []
    seen_urls: set[str] = set()

    # --- 1. Authenticated play streams (highest quality, if available) ---
    # Sort play streams so segmented formats (HLS/DASH) come first — they stream
    # through /proxy as short segment requests, which is far more reliable on
    # Passenger shared hosting than long single-file MP4 Range streams.
    # Within each format bucket, highest resolution first (1080p -> 720p -> ...).
    def _format_bucket(s: dict[str, Any]) -> int:
        url = str(s.get("url", "")).lower()
        ftype = str(s.get("type", "")).lower()
        if "hls" in ftype or ".m3u8" in url:
            return 0  # HLS segments
        if "dash" in ftype or ".mpd" in url:
            return 1  # DASH segments
        return 2  # single-file MP4

    def _quality_bucket(s: dict[str, Any]) -> int:
        q = str(s.get("quality", "")).lower()
        if "4k" in q or "2160" in q:
            return 0
        if "1080" in q:
            return 1
        if "720" in q:
            return 2
        if "480" in q:
            return 3
        if "360" in q:
            return 4
        return 5

    def _source_priority(s: dict[str, Any]) -> tuple[int, int]:
        return (_format_bucket(s), _quality_bucket(s))

    play_items = list(include_play_streams or [])
    play_items.sort(key=_source_priority)

    for s in play_items:
        stream_url = s.get("url", "")
        if not stream_url or stream_url in seen_urls:
            continue
        seen_urls.add(stream_url)
        src_item: dict[str, Any] = {
            "quality": s.get("quality", "Auto"),
            "url": stream_url,
            "source": "play",
            "type": s.get("type", "mp4"),
        }
        if s.get("headers"):
            src_item["headers"] = s["headers"]
        sources.append(src_item)

    # Helper to check if URL is a direct playable media stream
    def _is_direct_playable_media_url(u: str) -> bool:
        if not u or not u.startswith(("http://", "https://")):
            return False
        parsed = urlparse(u)
        host = (parsed.hostname or "").lower()
        path = (parsed.path or "").lower()
        # Exclude fake TLDs and non-playable web aggregator domains
        if (
            host.endswith(".cms")
            or host.endswith(".local")
            or host.endswith(".internal")
            or "ugc-video.com" in host
            or "fzmovies" in host
        ):
            return False
        # Exclude generic webpage extensions
        if path.endswith(".html") or path.endswith(".htm") or path.endswith(".php"):
            return False
        # Include direct video extensions or trusted media CDNs
        if any(path.endswith(ext) for ext in (".mp4", ".m3u8", ".mpd", ".webm", ".mkv", ".ts")):
            return True
        if any(cdn in host for cdn in ("aoneroom.com", "hakunaymatata.com", "cloudfront.net", "akamai", "fastly")):
            return True
        return True

    # --- 2. Community-posted links from postList ---
    for post in detail.get("postList", {}).get("items", []):
        if post.get("status") != 0:
            continue
        link = post.get("link") or {}
        stream_url = link.get("url", "")
        if not stream_url or not _is_direct_playable_media_url(stream_url) or stream_url in seen_urls:
            continue
        seen_urls.add(stream_url)
        quality = (
            _guess_quality_from_str(post.get("title", ""))
            or _guess_quality_from_str(stream_url)
            or "Auto"
        )
        sources.append({"quality": quality, "url": stream_url, "source": "community"})

    # --- 3. Trailer video (fallback) ---
    video_addr = trailer.get("videoAddress") if trailer else None
    if video_addr and video_addr.get("url"):
        trailer_url = video_addr["url"]
        if trailer_url not in seen_urls:
            seen_urls.add(trailer_url)
            h = video_addr.get("height", 0)
            quality = _QUALITY_MAP.get(h, f"{h}p") if h else "Trailer"
            sources.append({"quality": quality, "url": trailer_url, "source": "trailer"})

    # Deduplicate while preserving priority order
    seen_final: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for s in sources:
        if s["url"] not in seen_final:
            seen_final.add(s["url"])
            deduped.append(s)

    def _final_sort_key(s: dict[str, Any]) -> tuple[int, int, int]:
        src = s.get("source", "")
        q = str(s.get("quality", "")).lower()
        u = str(s.get("url", "")).lower()
        # Format first: segmented (HLS/DASH) before single-file MP4 — more
        # resilient through the /proxy on Passenger shared hosting.
        if "hls" in str(s.get("type", "")).lower() or ".m3u8" in u:
            fmt_bucket = 0
        elif "dash" in str(s.get("type", "")).lower() or ".mpd" in u:
            fmt_bucket = 1
        else:
            fmt_bucket = 2

        # Quality within the format bucket
        if "4k" in q or "2160" in q:
            q_bucket = 0
        elif "1080" in q:
            q_bucket = 1
        elif "720" in q:
            q_bucket = 2
        elif "480" in q:
            q_bucket = 3
        elif "360" in q:
            q_bucket = 4
        else:
            q_bucket = 5

        # Source last: play before community before trailer
        src_bucket = 0 if src == "play" else 1 if src == "community" else 2
        return (fmt_bucket, q_bucket, src_bucket)

    deduped.sort(key=_final_sort_key)

    # --- Subtitles ---
    subtitles: list[dict[str, str]] = []

    # --- Season / episode info ---
    seasons_raw = resource.get("seasons", [])
    seasons_info = []
    for s in seasons_raw:
        seasons_info.append({
            "season": s.get("se", 0),
            "maxEpisode": s.get("maxEp", 0),
            "resolutions": [r.get("resolution", 0) for r in s.get("resolutions", [])],
        })

    # --- Cast / crew ---
    stars = detail.get("stars", [])
    cast = [
        {
            "name": p.get("name", ""),
            "character": p.get("character", ""),
            "type": "director" if p.get("staffType") == 2 else "writer" if p.get("staffType") == 3 else "actor",
        }
        for p in stars
    ]

    return {
        "subjectId": subject.get("subjectId", ""),
        "detailPath": subject.get("detailPath", ""),
        "title": title,
        "description": subject.get("description", ""),
        "year": (subject.get("releaseDate", "") or "")[:4],
        "releaseDate": subject.get("releaseDate", ""),
        "genre": subject.get("genre", ""),
        "country": subject.get("countryName", ""),
        "imdbRating": subject.get("imdbRatingValue", ""),
        "cover": cover_url,
        "subjectType": subject.get("subjectType", 1),  # 1=movie, 2=TV
        "source": resource.get("source", ""),
        "uploadBy": resource.get("uploadBy", ""),
        "hasResource": subject.get("hasResource", False),
        "seasons": seasons_info,
        "cast": cast,
        "sources": deduped,
        "subtitles": subtitles,
    }
async def fetch_moviebox_captions(
    client: httpx.AsyncClient,
    subject_id: str,
    detail_path: str = "",
    resource_id: str = "",
    format_type: str = "MP4",
) -> list[dict[str, str]]:
    """Fetch rich subtitles with real CDN .srt URLs directly from the Moviebox caption API."""
    url = f"{API_BASE}/wefeed-h5api-bff/subject/caption?format={format_type}&subjectId={subject_id}"
    if detail_path:
        url += f"&detailPath={detail_path}"
    if resource_id:
        url += f"&id={resource_id}"

    token = await ensure_guest_token(client)
    headers = _build_headers()
    if token:
        headers["token"] = token
        headers["Authorization"] = f"Bearer {token}"
        headers["Cookie"] = f"i18n_lang=en; token={token}"

    try:
        resp = await client.get(url, headers=headers, timeout=3.5)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") == 0:
            captions = data.get("data", {}).get("captions", [])
            return [
                {
                    "lang": (c.get("lan") or "en")[:2].lower(),
                    "language": c.get("lanName") or "Subtitle",
                    "url": c.get("url", ""),
                }
                for c in captions
                if c.get("url")
            ]
    except Exception as exc:
        logger.warning("moviebox caption fetch failed: %s", exc)
    return []


async def _get_tmdb_id_from_imdb(client: httpx.AsyncClient, imdb_id: str) -> str:
    """Helper to fetch TMDB ID from Cinemeta for Flikhub proxy fallback."""
    if not imdb_id or not imdb_id.startswith("tt"):
        return ""
    for kind in ("movie", "series"):
        try:
            r = await client.get(f"{CINEMETA_BASE}/meta/{kind}/{imdb_id}.json", timeout=3.0)
            if r.status_code == 200:
                meta = r.json().get("meta", {}) or {}
                tid = meta.get("moviedb_id") or meta.get("external_ids", {}).get("tmdb_id")
                if tid:
                    return str(tid)
        except Exception:
            pass
    return ""


async def resolve_moviebox_imdb_id(
    client: httpx.AsyncClient,
    title: str,
    release_date: str = "",
    subject_type: int = 1,
) -> str:
    """Automatically resolve IMDB ID (e.g. tt9018736) for a Moviebox title via Cinemeta metadata lookup."""
    from urllib.parse import quote

    clean_title = re.sub(r"\s+S\d+(-S\d+)?$", "", title, flags=re.IGNORECASE).strip()
    clean_title = re.sub(r"\s+Season\s+\d+$", "", clean_title, flags=re.IGNORECASE).strip()
    year = (release_date or "")[:4]
    is_tv = subject_type == 2
    media_type = "series" if is_tv else "movie"

    try:
        url = f"{CINEMETA_BASE}/catalog/{media_type}/top/search={quote(clean_title)}.json"
        resp = await client.get(url, timeout=5.0)
        if resp.status_code == 200:
            metas = resp.json().get("metas", [])
            for m in metas:
                imdb_id = m.get("id", "")
                m_year = str(m.get("year", ""))
                if year and year in m_year:
                    return imdb_id
            if metas:
                return metas[0].get("id", "")
    except Exception as exc:
        logger.warning("moviebox IMDB resolution failed: %s", exc)

    return ""


async def _fetch_tmdb_original_title(client: httpx.AsyncClient, moviedb_id: int) -> str:
    """Fetch the original (non-English) title from a TMDB movie page."""
    try:
        resp = await client.get(
            f"https://www.themoviedb.org/movie/{moviedb_id}",
            headers={
                "Accept": "text/html",
                "Accept-Language": "en-US,en;q=0.9",
            },
            follow_redirects=True,
            timeout=8.0,
        )
        if resp.status_code == 200:
            m = re.search(r"Original Title</strong>\s*(.*?)</p>", resp.text, re.DOTALL)
            if m:
                title = re.sub(r"<[^>]+>", "", m.group(1)).strip()
                if title:
                    return title
    except Exception as exc:
        logger.debug("TMDB original title fetch failed for %s: %s", moviedb_id, exc)
    return ""


def _match_search_result(
    results: list[dict[str, Any]],
    requested_title: str,
    subject_type: int,
    year: str = "",
    english_title: str = "",
) -> dict[str, Any] | None:
    """Pick the search result that is the *same film* as ``requested_title``.

    Matching is deliberately simple: the normalized requested title must equal
    the normalized result title (``==``), with ``english_title`` as a second
    accepted candidate (OR). Cinemeta/TMDB titles are consistent, so an exact
    normalized match is sufficient and avoids the year-only fallback that used
    to resolve to a *different* same-year film.

    When no exact title match exists but a result with the right subject type /
    year is present, the first such result is still returned so the caller has a
    subject to work with — but ``fetch_sources`` then enforces the real match
    (title OR + exact year) and forces empty sources on mismatch.
    """
    candidates: list[dict[str, Any]] = []
    req_norm = normalize_title(requested_title)
    eng_norm = normalize_title(english_title) if english_title else ""
    for res in results:
        if res.get("subjectType") != subject_type:
            continue
        res_norm = normalize_title(str(res.get("title", "")))
        if res_norm == req_norm or (eng_norm and res_norm == eng_norm):
            return res

    if not year:
        # No year to disambiguate: fall back to the first type-appropriate result.
        for res in results:
            if res.get("subjectType") == subject_type:
                candidates.append(res)
        return candidates[0] if candidates else None

    # Exact title absent: prefer same subject type + same year, else the first
    # type-appropriate result.
    for res in results:
        if res.get("subjectType") == subject_type and str(res.get("year", "")) == year:
            return res
    for res in results:
        if res.get("subjectType") == subject_type:
            candidates.append(res)
    return candidates[0] if candidates else None


async def resolve_imdb_to_moviebox(
    client: httpx.AsyncClient,
    imdb_id: str,
    original_title: str = "",
    english_title: str = "",
    media_type: str = "movie",
    year: str = "",
) -> tuple[str, str]:
    """Resolve an IMDB ID (e.g. tt9018736) to Moviebox (subject_id, detail_path).

    When ``original_title``/``english_title`` are provided the Cinemeta metadata
    lookup is skipped entirely — the title is searched directly on Moviebox which
    is both faster and more accurate for localised titles. ``fetch_sources``
    performs the real title/year gate and forces empty sources on mismatch.
    """
    if not imdb_id.startswith("tt"):
        return "", ""

    subject_type = 2 if media_type == "tv" else 1

    try:
        # --- Fast path: use the caller-supplied original title directly ---
        if original_title:
            title = original_title
            results = await search_titles(client, title)
            match = _match_search_result(results, title, subject_type, year, english_title)
            if match:
                logger.info(
                    "IMDB %s matched via original title: %s -> %s (%s)",
                    imdb_id, title, match.get("subjectId"), match.get("title"),
                )
                return str(match.get("subjectId", "")), str(match.get("detailPath", ""))

        # --- Slow path: fetch metadata from Cinemeta, then search ---
        meta = {}
        for kind in ("series", "movie"):
            r = await client.get(
                f"{CINEMETA_BASE}/meta/{kind}/{imdb_id}.json",
                follow_redirects=True,
                timeout=5.0,
            )
            if r.status_code == 200:
                m = r.json().get("meta", {}) or {}
                if m:
                    meta = m
                    break

        if not meta:
            logger.warning("Reverse IMDB lookup failed for %s", imdb_id)
            return "", ""

        title = meta.get("name", "")
        year = str(meta.get("year") or meta.get("releaseInfo") or "")[:4]
        meta_type = meta.get("type", "movie")
        subject_type = 2 if meta_type == "series" else 1

        if not title:
            return "", ""

        results = await search_titles(client, title)
        match = _match_search_result(results, title, subject_type, year)
        if match:
            logger.info(
                "IMDB %s matched via Cinemeta: %s -> %s (%s)",
                imdb_id, title, match.get("subjectId"), match.get("title"),
            )
            return str(match.get("subjectId", "")), str(match.get("detailPath", ""))

        # Fallback: try TMDB original title
        moviedb_id = meta.get("moviedb_id") or meta.get("external_ids", {}).get("tmdb_id")
        if moviedb_id:
            orig_title = await _fetch_tmdb_original_title(client, int(moviedb_id))
            if orig_title and orig_title.lower() != title.lower():
                logger.info("Trying TMDB original title %r for %s", orig_title, imdb_id)
                orig_results = await search_titles(client, orig_title)
                match = _match_search_result(orig_results, orig_title, subject_type, year)
                if match:
                    logger.info(
                        "Matched via TMDB original title: %s -> %s (%s)",
                        orig_title, match.get("subjectId"), match.get("title"),
                    )
                    return str(match.get("subjectId", "")), str(match.get("detailPath", ""))

    except Exception as exc:
        logger.warning("moviebox reverse lookup failed: %s", exc)

    return "", ""


async def fetch_sources(
    client: httpx.AsyncClient,
    url_or_id: str,
    se: int = 0,
    ep: int = 0,
    lang: str = "en",
    try_play: bool = True,
    cookie: str = "",
    original_title: str = "",
    english_title: str = "",
    media_type: str = "movie",
    year: str = "",
) -> dict[str, Any]:
    """
    Fetch video sources for a TheMovieBox title.

    Args:
        url_or_id:      Numeric subjectId, IMDB ID (tt...), full themoviebox.xyz URL, or slug.
        se:             Season number (for TV shows, 0 for movies).
        ep:             Episode number (for TV shows, 0 for movies).
        lang:           Preferred subtitle language code.
        try_play:       Whether to attempt the authenticated play endpoint.
        cookie:         Optional user cookie string (e.g. mb_token=...; token=...)
        original_title: Original title (required when url_or_id is an IMDB ID).
        english_title:  English title (used alongside original_title for matching).
        media_type:     "movie" or "tv".
        year:           Release year (e.g. "2024").

    Returns a dict with metadata and ``sources`` / ``subtitles`` lists.
    """
    orig_input = url_or_id
    # Reverse lookup if url_or_id is an IMDB ID (starts with tt)
    if url_or_id.strip().startswith("tt"):
        sub_id, det_path = await resolve_imdb_to_moviebox(
            client, url_or_id.strip(),
            original_title=original_title,
            english_title=english_title,
            media_type=media_type,
            year=year,
        )
        if sub_id:
            url_or_id = sub_id
        else:
            raise ValueError(f"Could not resolve IMDB ID {url_or_id.strip()} to a Moviebox subject")

    parsed = parse_moviebox_input(url_or_id)

    detail = await fetch_detail(
        client,
        subject_id=parsed.subject_id,
        detail_path=parsed.detail_path,
    )

    # ── Post-fetch matching (all logic lives here in the backend) ─────────────
    # The resolved Moviebox subject must be the *same film* the caller asked for.
    # Two conditions, checked in order:
    #   1. Title — exact (normalized) match against original_title OR english_title.
    #   2. Year  — must equal the requested year exactly.
    # If either fails we return empty sources rather than streaming a different
    # film. titleMatched/requestedTitle are still reported for the client's info.
    detail_title = str(detail.get("subject", {}).get("title", ""))
    detail_year = (detail.get("subject", {}).get("releaseDate", "") or "")[:4]
    norm_detail = normalize_title(detail_title)
    requested_title = original_title or english_title or detail_title
    title_matched = (
        normalize_title(original_title) == norm_detail
        or (bool(english_title) and normalize_title(english_title) == norm_detail)
    ) if original_title else True

    subject_id = str(detail.get("subject", {}).get("subjectId") or parsed.subject_id or "")
    detail_path = str(detail.get("subject", {}).get("detailPath") or parsed.detail_path or "")

    # Year mismatch → force empty sources (do not stream a different year's film).
    year_mismatch = bool(year) and detail_year and detail_year != year

    req_se = se if se > 0 else parsed.se
    req_ep = ep if ep > 0 else parsed.ep

    play_streams: list[dict] = []
    # Skip play fetch entirely when the film does not match what was asked for.
    if try_play and subject_id and title_matched and not year_mismatch:
        play_streams = await fetch_play_streams(
            client, subject_id,
            detail_path=detail_path,
            se=req_se, ep=req_ep, lang=lang,
            cookie=cookie,
        )
        # Smart TV Series Fallback:
        # If no play streams were found and req_se==0 and req_ep==0, check if title has seasons and retry with first season S1E1!
        if not play_streams and req_se == 0 and req_ep == 0:
            resource = detail.get("resource", {})
            seasons = resource.get("seasons", [])
            if seasons:
                first_se = seasons[0].get("se", 1)
                logger.info("TV Series detected without season/ep parameters. Defaulting to se=%s, ep=1", first_se)
                play_streams = await fetch_play_streams(
                    client, subject_id,
                    detail_path=detail_path,
                    se=first_se, ep=1, lang=lang,
                    cookie=cookie,
                )

    result = extract_sources(detail, include_play_streams=play_streams)

    # Report the requested-vs-resolved title so the client can judge a mismatch
    # (translated title vs different film) without us guessing.
    result["requestedTitle"] = requested_title
    result["titleMatched"] = title_matched
    result["yearMismatch"] = year_mismatch

    # The film does not match what the caller asked for (title or year) — force
    # empty sources so we never stream a different film.
    if not title_matched or year_mismatch:
        logger.warning(
            "Moviebox subject mismatch: requested=%r (year=%s) resolved=%r (year=%s) "
            "titleMatched=%s yearMismatch=%s — returning empty sources",
            requested_title, year, detail_title, detail_year, title_matched, year_mismatch,
        )
        result["sources"] = []

    # Automatically resolve IMDB ID for Moviebox entry
    if orig_input.strip().startswith("tt"):
        result["imdbId"] = orig_input.strip()
    else:
        result["imdbId"] = await resolve_moviebox_imdb_id(
            client,
            title=result.get("title", ""),
            release_date=result.get("releaseDate", ""),
            subject_type=result.get("subjectType", 1),
        )

    # Fetch rich subtitles with real CDN .srt URLs from caption endpoint.
    # Skipped entirely when the film does not match (sources are already empty).
    subtitles: list[dict[str, str]] = []
    seen_sub_urls: set[str] = set()

    if title_matched and not year_mismatch:
        for s in play_streams:
            res_id = str(s.get("id", ""))
            stream_type = str(s.get("type", "")).upper()
            if res_id:
                fmt = "DASH" if "DASH" in stream_type else "MP4"
                subs = await fetch_moviebox_captions(
                    client,
                    subject_id=subject_id,
                    detail_path=detail_path,
                    resource_id=res_id,
                    format_type=fmt,
                )
                for sub in subs:
                    if sub["url"] not in seen_sub_urls:
                        seen_sub_urls.add(sub["url"])
                        subtitles.append(sub)
                if subtitles:
                    break

        if not subtitles and subject_id:
            subs = await fetch_moviebox_captions(
                client,
                subject_id=subject_id,
                detail_path=detail_path,
                resource_id="",
                format_type="MP4",
            )
            for sub in subs:
                if sub["url"] not in seen_sub_urls:
                    seen_sub_urls.add(sub["url"])
                    subtitles.append(sub)

    result["subtitles"] = subtitles

    # Flikhub Proxy Fallback
    if not result.get("sources"):
        try:
            tmdb_id = ""
            if orig_input.strip().startswith("tt"):
                tmdb_id = await _get_tmdb_id_from_imdb(client, orig_input.strip())
            if tmdb_id:
                base_url = settings.flikhub_proxy_base.rstrip("/")
                if media_type == "tv" and req_se and req_ep:
                    flik_url = f"{base_url}/tv?id={tmdb_id}&season={req_se}&episode={req_ep}&mode=json&sources=moviebox&hevc=1"
                else:
                    flik_url = f"{base_url}/movie?id={tmdb_id}&mode=json&sources=moviebox&hevc=1"
                
                headers = {
                    "Accept": "application/json",
                    "Origin": "https://player.cinezo.live",
                    "Referer": "https://player.cinezo.live/",
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                }
                flik_resp = await client.get(flik_url, headers=headers, timeout=5.0)
                if flik_resp.status_code == 200:
                    flik_data = flik_resp.json()
                    if "source" in flik_data and "qualities" in flik_data["source"]:
                        if "sources" not in result:
                            result["sources"] = []
                        for q in flik_data["source"]["qualities"]:
                            result["sources"].append({
                                "quality": f"{q.get('quality', 'Auto')} (Flikhub)",
                                "url": q.get("url", ""),
                                "type": q.get("type", "mp4"),
                                "source": "play"
                            })
                        if result["sources"]:
                            result["titleMatched"] = True
                            result["yearMismatch"] = False
                    if "subtitles" in flik_data:
                        if "subtitles" not in result:
                            result["subtitles"] = []
                        for sub in flik_data["subtitles"]:
                            result["subtitles"].append({
                                "lang": sub.get("lang", sub.get("language", "Unknown")),
                                "language": sub.get("language", "un"),
                                "url": sub.get("url", "")
                            })
        except Exception as exc:
            logger.debug("flikhub proxy error in moviebox native: %s", exc)

    return result


async def search_titles(
    client: httpx.AsyncClient, query: str, page: int = 1, per_page: int = 12
) -> list[dict[str, Any]]:
    """Search for titles on TheMovieBox using POST /wefeed-h5api-bff/subject/search."""
    token = await ensure_guest_token(client)

    headers = _build_headers()
    if token:
        headers["token"] = token
        headers["Authorization"] = f"Bearer {token}"
        headers["Cookie"] = f"i18n_lang=en; token={token}"

    url = f"{API_BASE}/wefeed-h5api-bff/subject/search"
    sanitized_query = re.sub(r"[^\w\s]", " ", query, flags=re.UNICODE)
    sanitized_query = re.sub(r"\s+", " ", sanitized_query).strip()
    payload = {"keyword": sanitized_query or query, "page": page, "perPage": per_page}

    try:
        resp = await client.post(url, json=payload, headers=headers, timeout=15.0)
        if resp.status_code == 400:
            logger.info("Moviebox search received 400 invalid token; refreshing guest token and retrying...")
            _invalidate_guest_token(client)
            token = await ensure_guest_token(client, force_refresh=True)
            headers = _build_headers()
            if token:
                headers["token"] = token
                headers["Authorization"] = f"Bearer {token}"
                headers["Cookie"] = f"i18n_lang=en; token={token}"
            resp = await client.post(url, json=payload, headers=headers, timeout=15.0)

        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            return []
        items = data.get("data", {}).get("items", []) or []
        return _format_search_results(items)
    except Exception as exc:
        logger.warning("moviebox search failed: %s", exc)
        return []


def _format_search_results(items: list[dict]) -> list[dict[str, Any]]:
    """Format raw search result items into a consistent structure."""
    results = []
    for item in items:
        cover = item.get("cover", {})
        subject_type = item.get("subjectType", 1)
        subject_id = item.get("subjectId", "")
        detail_path = item.get("detailPath", "")
        media_segment = "movies" if subject_type == 1 else "tv"
        moviebox_url = (
            f"{SITE_BASE}/{media_segment}/{detail_path}?id={subject_id}"
            if subject_id and detail_path else ""
        )
        results.append({
            "subjectId": subject_id,
            "detailPath": detail_path,
            "title": item.get("title", ""),
            "year": (item.get("releaseDate", "") or "")[:4],
            "genre": item.get("genre", ""),
            "imdbRating": item.get("imdbRatingValue", ""),
            "cover": cover.get("url", "") if isinstance(cover, dict) else "",
            "subjectType": subject_type,
            "hasResource": item.get("hasResource", False),
            "url": moviebox_url,
        })
    return results
