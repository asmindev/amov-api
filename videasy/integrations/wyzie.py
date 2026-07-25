from __future__ import annotations

import logging
from collections import OrderedDict

import httpx

from videasy.config import settings
from videasy.models.subtitle import SubtitleGroup, SubtitleItem, WyzieSubtitle

logger = logging.getLogger("videasy.wyzie")

WYZIE_BASE = "https://sub.wyzie.io"


async def fetch_wyzie(
    client: httpx.AsyncClient,
    *,
    tmdb_id: str = "",
    imdb_id: str = "",
    language: str = "",
    season: int | None = None,
    episode: int | None = None,
) -> list[SubtitleItem]:
    api_key = settings.wyzie_api_key
    if not api_key:
        logger.warning("Wyzie API key not set")
        return []

    content_id = imdb_id or tmdb_id
    if not content_id:
        logger.warning("No content ID provided for Wyzie")
        return []

    params: dict[str, str | int] = {"id": content_id, "key": api_key}
    if language:
        params["language"] = language
    if season is not None and episode is not None:
        params["season"] = season
        params["episode"] = episode

    try:
        resp = await client.get(f"{WYZIE_BASE}/search", params=params)
        if resp.status_code == 400:
            return []
        resp.raise_for_status()
        data = resp.json()

        subs: list[SubtitleItem] = []
        for item in data if isinstance(data, list) else []:
            lang_code = item.get("language", "en")
            display = item.get("display", lang_code)
            url = item.get("url")
            if not url:
                continue

            hi_tag = " (HI)" if item.get("isHearingImpaired") else ""
            label = f"Wyzie - {display}{hi_tag}"

            subs.append(SubtitleItem(lang=lang_code, language=label, url=url))

        return subs

    except Exception as e:
        logger.error("Failed to fetch Wyzie subtitles for %s: %s", content_id, e)
        return []


async def fetch_wyzie_grouped(
    client: httpx.AsyncClient,
    *,
    tmdb_id: str = "",
    imdb_id: str = "",
    language: str = "",
    season: int | None = None,
    episode: int | None = None,
) -> list[SubtitleGroup]:
    api_key = settings.wyzie_api_key
    if not api_key:
        logger.warning("Wyzie API key not set")
        return []

    content_id = imdb_id or tmdb_id
    if not content_id:
        logger.warning("No content ID provided for Wyzie")
        return []

    params: dict[str, str | int] = {"id": content_id, "key": api_key}
    if language:
        params["language"] = language
    if season is not None and episode is not None:
        params["season"] = season
        params["episode"] = episode

    try:
        resp = await client.get(f"{WYZIE_BASE}/search", params=params)
        if resp.status_code == 400:
            return []
        resp.raise_for_status()
        data = resp.json()

        grouped: OrderedDict[str, SubtitleGroup] = OrderedDict()

        for item in data if isinstance(data, list) else []:
            url = item.get("url")
            if not url:
                continue

            lang_code = item.get("language", "en")

            if lang_code not in grouped:
                grouped[lang_code] = SubtitleGroup(
                    language=lang_code,
                    display=item.get("display", lang_code),
                    flagUrl=item.get("flagUrl", ""),
                )

            grouped[lang_code].subtitles.append(WyzieSubtitle(**{
                k: v for k, v in item.items()
                if k in WyzieSubtitle.model_fields
            }))

        return list(grouped.values())

    except Exception as e:
        logger.error("Failed to fetch Wyzie subtitles for %s: %s", content_id, e)
        return []
