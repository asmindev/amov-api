from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from videasy.config import settings

logger = logging.getLogger("moviebox")

# Config aliases
API_BASE = settings.moviebox_api_base
DETAIL_ENDPOINT = settings.moviebox_detail_endpoint
SEARCH_ENDPOINT = settings.moviebox_search_endpoint
PLAY_BASE = settings.moviebox_play_base
PLAY_ENDPOINT = settings.moviebox_play_endpoint

# In-memory guest JWT token cache (token, expires_at)
_guest_token: list[str] = [""]  # mutable container so it's writable from async funcs

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
    ref = referer or "https://themoviebox.xyz/"
    return {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:152.0) Gecko/20100101 Firefox/152.0",
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://themoviebox.xyz",
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
            resp = await client.get(url, headers=_build_headers())
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


async def ensure_guest_token(client: httpx.AsyncClient) -> str:
    """Fetch and cache a fresh guest JWT token from /wefeed-h5api-bff/country-code if needed."""
    if _guest_token[0]:
        return _guest_token[0]
    try:
        url = f"{API_BASE}/wefeed-h5api-bff/country-code"
        resp = await client.get(url, headers=_build_headers())
        if resp.status_code == 200:
            token = resp.cookies.get("token") or ""
            if not token:
                set_cookie = resp.headers.get("set-cookie", "")
                m = re.search(r"token=([A-Za-z0-9._-]+)", set_cookie)
                if m:
                    token = m.group(1)
            if token:
                _guest_token[0] = token
                logger.debug("automatically initialized guest JWT token via country-code")
                return token
    except Exception as exc:
        logger.debug("failed to initialize guest token via country-code: %s", exc)
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
        f"https://themoviebox.xyz/movies/{detail_path}?id={subject_id}&type=/movie/detail&detailSe={se}&detailEp={ep}&lang={lang}"
        if detail_path else "https://themoviebox.xyz/"
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
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()

        # Step 1: Extract returned token from Set-Cookie header
        set_cookie = resp.headers.get("set-cookie", "")
        m = re.search(r"token=([A-Za-z0-9._-]+)", set_cookie)
        returned_token = m.group(1) if m else ""
        if returned_token:
            _guest_token[0] = returned_token

        data = resp.json()
        play_data = data.get("data", {})
        raw_streams = play_data.get("streams", []) or play_data.get("hls", []) or play_data.get("dash", [])

        # Step 2: If dash array is missing/empty but server issued a session token, perform automatic 2-hop handshake to unlock DASH
        if returned_token and not play_data.get("dash"):
            enc_ret_tok = quote(json.dumps(returned_token))
            headers["Cookie"] = f'i18n_lang=en; mb_token={enc_ret_tok}; token={returned_token}'
            logger.info("performing automatic 2-hop token handshake to unlock DASH streams...")
            resp_retry = await client.get(url, headers=headers)
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
    # Sort play streams so highest resolution MP4 (1080p -> 720p -> 480p -> 360p) appears first
    def _source_priority(s: dict[str, Any]) -> tuple[int, int]:
        q = str(s.get("quality", "")).lower()
        url = str(s.get("url", "")).lower()
        # Quality order: 1080p (0), 720p (1), 480p (2), 360p (3), Auto/DASH (4), others (5)
        if "1080p" in q or "1080" in q:
            p_qual = 0
        elif "720p" in q or "720" in q:
            p_qual = 1
        elif "480p" in q or "480" in q:
            p_qual = 2
        elif "360p" in q or "360" in q:
            p_qual = 3
        elif "auto" in q or ".mpd" in url or ".m3u8" in url:
            p_qual = 4
        else:
            p_qual = 5
        return (p_qual, 0)

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

    def _final_sort_key(s: dict[str, Any]) -> int:
        src = s.get("source", "")
        q = str(s.get("quality", "")).lower()
        u = str(s.get("url", "")).lower()
        if src == "play":
            if "1080" in q or "4k" in q: return 0
            if "720" in q: return 1
            if "480" in q: return 2
            if "360" in q: return 3
            if "auto" in q or ".mpd" in u or ".m3u8" in u: return 4
            return 5
        if src == "community": return 6
        if src == "trailer": return 7
        return 8

    deduped.sort(key=_final_sort_key)

    # --- Subtitles ---
    subtitles_raw = subject.get("subtitles", "")
    subtitles: list[dict[str, str]] = []
    if subtitles_raw:
        for lang_name in subtitles_raw.split(","):
            lang_name = lang_name.strip()
            if lang_name:
                subtitles.append({
                    "lang": lang_name[:2].lower(),
                    "language": lang_name,
                    "url": "",
                })

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
async def fetch_sources(
    client: httpx.AsyncClient,
    url_or_id: str,
    se: int = 0,
    ep: int = 0,
    lang: str = "en",
    try_play: bool = True,
    cookie: str = "",
) -> dict[str, Any]:
    """
    Fetch video sources for a TheMovieBox title.

    Args:
        url_or_id: Numeric subjectId, full themoviebox.xyz URL, or slug.
        se:        Season number (for TV shows, 0 for movies).
        ep:        Episode number (for TV shows, 0 for movies).
        lang:      Preferred subtitle language code.
        try_play:  Whether to attempt the authenticated play endpoint.
        cookie:    Optional user cookie string (e.g. mb_token=...; token=...)

    Returns a dict with metadata and ``sources`` / ``subtitles`` lists.
    """
    parsed = parse_moviebox_input(url_or_id)

    detail = await fetch_detail(
        client,
        subject_id=parsed.subject_id,
        detail_path=parsed.detail_path,
    )

    # Resolve the actual subjectId from the detail response for the play endpoint
    subject_id = str(detail.get("subject", {}).get("subjectId") or parsed.subject_id or "")
    detail_path = str(detail.get("subject", {}).get("detailPath") or parsed.detail_path or "")

    req_se = se if se > 0 else parsed.se
    req_ep = ep if ep > 0 else parsed.ep

    play_streams: list[dict] = []
    if try_play and subject_id:
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

    return extract_sources(detail, include_play_streams=play_streams)


async def search_titles(
    client: httpx.AsyncClient, query: str, page: int = 1, per_page: int = 12
) -> list[dict[str, Any]]:
    """Search for titles on TheMovieBox using POST /wefeed-h5api-bff/subject/search."""
    token = await ensure_guest_token(client)
    if not token:
        try:
            r0 = await client.get(
                f"{API_BASE}/wefeed-h5api-bff/subject/play?subjectId=8313012068559605176",
                headers=_build_headers(),
            )
            set_cookie = r0.headers.get("set-cookie", "")
            m = re.search(r"token=([A-Za-z0-9._-]+)", set_cookie)
            if m:
                token = m.group(1)
                _guest_token[0] = token
        except Exception:
            pass

    headers = _build_headers()
    if token:
        headers["token"] = token
        headers["Authorization"] = f"Bearer {token}"
        headers["Cookie"] = f"i18n_lang=en; token={token}"

    url = f"{API_BASE}/wefeed-h5api-bff/subject/search"
    logger.info("searching moviebox: query=%r page=%s", query, page)
    payload = {"keyword": query, "page": page, "perPage": per_page}

    try:
        resp = await client.post(url, json=payload, headers=headers)
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
            f"https://themoviebox.xyz/{media_segment}/{detail_path}?id={subject_id}"
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
