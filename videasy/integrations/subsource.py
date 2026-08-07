import io
import logging
import re
import zipfile
from collections import OrderedDict

import httpx

from videasy.models.subtitle import SubtitleGroup, WyzieSubtitle

logger = logging.getLogger("videasy.subsource")

# Official SubSource REST API — verified against the .NET wrapper:
# https://github.com/moviecollection/sub-source/blob/main/Source/MovieCollection.SubSource/SubSourceService.cs
API_BASE_URL = "https://api.subsource.net"
API_SEARCH_PATH = "/api/v1/movies/search"
API_SUBTITLES_PATH = "/api/v1/subtitles"
API_SUBTITLE_DETAIL_PATH = "/api/v1/subtitles/{subtitle_id}"
API_DOWNLOAD_PATH = "/api/v1/subtitles/{subtitle_id}/download"
API_AUTH_HEADER = "X-API-Key"

# Language name used by the API for ISO 639-1 codes (default set)
LANG_MAP = {
    "id": "indonesian",
    "en": "english",
    "es": "spanish",
    "fr": "french",
    "de": "german",
    "it": "italian",
    "pt": "portuguese",
    "vi": "vietnamese",
    "th": "thai",
    "ja": "japanese",
    "ko": "korean",
    "zh": "chinese",
}

# ISO 639-1 code derived from a full language name returned by the API
LANG_CODE_BY_NAME = {name: code for code, name in LANG_MAP.items()}


def _auth_headers(api_key: str) -> dict[str, str]:
    return {
        API_AUTH_HEADER: api_key,
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    }


def _extract_list(data) -> list[dict]:
    """Unwrap the `data` field shared by ListResponse / PagedResponse."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        items = data.get("data")
        if isinstance(items, list):
            return items
    return []


def _is_srt_compatible(sub: dict) -> bool:
    """Detect the subtitle format from the API `preview` field.

    Native <track> elements only render SRT/VTT, so ASS/SSA releases are
    excluded (same default behavior as SubDL's `is_srt`). Unknown previews
    are treated as SRT since most SubSource uploads are SRT.
    """
    preview = (sub.get("preview") or "").lstrip("\ufeff\uFEFF").strip()
    if not preview:
        return True
    low = preview.lower()
    if "[script info]" in low or "scripttype: v4.00" in low:
        return False
    return True


async def search_movies(
    client: httpx.AsyncClient,
    api_key: str,
    *,
    query: str = "",
    imdb_id: str = "",
    year: str = "",
    media_type: str = "",
    season: int | None = None,
) -> list[dict]:
    """Search movies/series. Ref: SearchMoviesAsync.

    GET /api/v1/movies/search?searchType=text|imdb&q=<query>[&year=<year>][&type=<type>][&season=<n>]
    Response: { "data": [ { "movieId", "title", "type", "releaseYear", "imdbId", ... }, ... ] }
    """
    if imdb_id:
        params: dict[str, str | int] = {"searchType": "imdb", "imdb": imdb_id}
    elif query:
        params = {"searchType": "text", "q": query}
    else:
        return []

    if year:
        params["year"] = year
    if media_type in ("movie", "series"):
        params["type"] = media_type
    if season is not None:
        params["season"] = season

    try:
        resp = await client.get(
            API_BASE_URL + API_SEARCH_PATH,
            params=params,
            headers=_auth_headers(api_key),
        )
        resp.raise_for_status()
        return _extract_list(resp.json())
    except Exception as e:
        logger.error(f"SubSource search failed: {e}")
        return []


async def list_subtitles(
    client: httpx.AsyncClient,
    api_key: str,
    movie_id: int,
    language: str = "",
    limit: int = 100,
) -> list[dict]:
    """List subtitles for a movie, paginating through all results.

    Ref: GetSubtitlesAsync.

    GET /api/v1/subtitles?movieId=<id>[&language=<lang>]&limit=<n>&page=<p>
    Response: { "data": [ { "subtitleId", "language", "releaseInfo", ... } ], "pagination": { ... } }
    """
    params: dict[str, str | int] = {"movieId": movie_id, "limit": limit}
    if language:
        params["language"] = language

    results: list[dict] = []
    page = 1
    try:
        while True:
            p = dict(params, page=page)
            resp = await client.get(
                API_BASE_URL + API_SUBTITLES_PATH,
                params=p,
                headers=_auth_headers(api_key),
            )
            resp.raise_for_status()
            data = resp.json()
            items = _extract_list(data)
            results.extend(items)

            pagination = data.get("pagination") if isinstance(data, dict) else None
            total = pagination.get("total") if isinstance(pagination, dict) else None
            if total is not None:
                if len(results) >= int(total):
                    break
            elif not items or len(items) < limit:
                break
            page += 1
    except Exception as e:
        logger.error(f"SubSource subtitles failed: {e}")
        return []

    return results


def _pick_movie(
    movies: list[dict],
    *,
    year: str = "",
    media_type: str = "movie",
    tmdb_id: str = "",
    season: int | None = None,
) -> dict | None:
    """Pick the best match from a search result list.

    Priority: exact tmdbId match > exact releaseYear match > exact season match
    (TV only) > type match > first.
    """
    if not movies:
        return None

    def _type_ok(m: dict) -> bool:
        t = str(m.get("type") or "").lower()
        if media_type == "series":
            return t in ("series", "tv", "tvseries", "show")
        return t in ("movie", "film", "movies")

    matches = [m for m in movies if _type_ok(m)] or list(movies)

    if tmdb_id:
        for m in matches:
            if str(m.get("tmdbId") or "") == str(tmdb_id):
                return m
    if year:
        for m in matches:
            if str(m.get("releaseYear")) == str(year):
                return m
    if season is not None:
        for m in matches:
            if str(m.get("season") or "") == str(season):
                return m
    return matches[0]


async def fetch_subsource_grouped(
    client: httpx.AsyncClient,
    api_key: str,
    *,
    title: str = "",
    year: str = "",
    media_type: str = "movie",
    imdb_id: str = "",
    tmdb_id: str = "",
    language: str = "",
    season: int | None = None,
    episode: int | None = None,
) -> list[SubtitleGroup]:
    """Fetch SubSource subtitles via the REST API and group them by language."""
    if not api_key:
        logger.warning("SubSource API key not configured")
        return []

    media_type = "series" if media_type == "tv" else "movie"
    movies = await search_movies(
        client, api_key, query=title, imdb_id=imdb_id,
        year=year, media_type=media_type, season=season,
    )

    # The release year on SubSource can differ from TMDB (e.g. Kraken listed as
    # 2025 while TMDB says 2026). When the year filter misses the target — either
    # no results at all, or no tmdbId match — retry the search without the year.
    if year:
        if not movies or (tmdb_id and not any(str(m.get("tmdbId") or "") == str(tmdb_id) for m in movies)):
            retried = await search_movies(
                client, api_key, query=title, imdb_id=imdb_id,
                year="", media_type=media_type, season=season,
            )
            if retried:
                movies = retried

    chosen = _pick_movie(movies, year=year, media_type=media_type, tmdb_id=tmdb_id, season=season)
    if chosen is None:
        return []

    movie_id = chosen.get("movieId")
    if movie_id is None:
        return []

    # Fetch subtitles. A single unfiltered request (paginated) already returns
    # every language; the `language` filter is honored when explicitly given.
    subs: list[dict] = await list_subtitles(client, api_key, movie_id, language=language)

    if not subs:
        return []

    grouped: OrderedDict[str, SubtitleGroup] = OrderedDict()
    for sub in subs:
        if not _is_srt_compatible(sub):
            continue
        lang_name = str(sub.get("language") or "").strip()
        if not lang_name:
            continue
        lang_display = lang_name.capitalize()
        lang_code = LANG_CODE_BY_NAME.get(lang_name.lower(), lang_name.lower())

        if lang_display not in grouped:
            grouped[lang_display] = SubtitleGroup(
                language=lang_code,
                display=lang_display,
                flagUrl="",
            )

        release_info = sub.get("releaseInfo") or []
        release_name = release_info[0] if isinstance(release_info, list) and release_info else ""
        if isinstance(release_info, list):
            releases = [str(r) for r in release_info if str(r)]
        else:
            releases = [str(release_info)] if release_info else []

        sub_id = sub.get("subtitleId")
        download_url = f"/subsource/download?subtitleId={sub_id}"
        if sub_id is not None and episode is not None:
            download_url += f"&episode={episode}"
        grouped[lang_display].subtitles.append(
            WyzieSubtitle(
                id=str(sub_id) if sub_id is not None else "",
                url=download_url if sub_id is not None else "",
                display=lang_display,
                language=lang_code,
                source="SubSource",
                release=release_name,
                releases=releases,
                fileName=release_name,
                isHearingImpaired=bool(sub.get("hearingImpaired")),
                downloadCount=sub.get("downloads"),
                format=str(sub.get("releaseType") or ""),
            )
        )

    return list(grouped.values())


def _pick_srt_from_zip(zf: zipfile.ZipFile, episode: int | None) -> str | None:
    """Select the SRT/VTT matching the requested episode from a season pack.

    SubSource season packs contain one SRT per episode (e.g.
    ``Breaking.Bad.S01E05.Gray.Matter...srt``). When no episode is given, or no
    name matches, fall back to the first subtitle file.
    """
    names = [
        n for n in zf.namelist()
        if n.lower().endswith((".srt", ".vtt")) and not n.startswith("__MACOSX")
    ]
    if not names:
        return None
    if len(names) == 1 or episode is None:
        return names[0]

    ep = str(int(episode))
    pattern = re.compile(rf"[EeXx]0?{ep}(?!\d)")
    for n in names:
        if pattern.search(n):
            return n
    return names[0]


async def extract_subsource_vtt(
    client: httpx.AsyncClient,
    api_key: str,
    subtitle_id: int,
    episode: int | None = None,
) -> str | None:
    """Download a SubSource subtitle (ZIP or raw SRT) and return VTT text."""
    try:
        resp = await client.get(
            API_BASE_URL + API_DOWNLOAD_PATH.format(subtitle_id=subtitle_id),
            headers=_auth_headers(api_key),
        )
        resp.raise_for_status()
    except Exception as e:
        logger.error(f"SubSource download failed: {e}")
        return None

    data = resp.content
    if not data:
        return None

    # ZIP archive (magic bytes) or a raw SRT file.
    if data[:4] == b"PK\x03\x04":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                name = _pick_srt_from_zip(z, episode)
                if name is None:
                    return None
                text = z.read(name).decode("utf-8", errors="replace")
        except Exception as e:
            logger.error(f"SubSource ZIP extract failed: {e}")
            return None
    else:
        text = data.decode("utf-8", errors="replace")

    if not text.lstrip().startswith("WEBVTT"):
        text = convert_srt_to_vtt(text)
    return text


def convert_srt_to_vtt(srt_text: str) -> str:
    """Basic SRT to VTT conversion."""
    vtt = "WEBVTT\n\n"
    lines = srt_text.splitlines()
    for line in lines:
        if "-->" in line:
            line = line.replace(",", ".")
        vtt += line + "\n"
    return vtt
