from __future__ import annotations

import logging

import httpx

from videasy.config import settings
from videasy.models.subtitle import SubtitleItem

logger = logging.getLogger("videasy.opensubtitles")


async def fetch_opensubtitles(client: httpx.AsyncClient, imdb_id: str) -> list[SubtitleItem]:
    if not imdb_id or not imdb_id.startswith("tt"):
        return []

    numeric_id = imdb_id.replace("tt", "").lstrip("0")
    if not numeric_id:
        return []

    headers: dict[str, str] = {"User-Agent": settings.user_agent}
    if settings.opensubtitles_api_key:
        headers["Api-Key"] = settings.opensubtitles_api_key
    else:
        logger.warning("OpenSubtitles API key not set — may be rate-limited")

    url = f"https://api.opensubtitles.com/api/v1/subtitles?imdb_id={numeric_id}"

    try:
        resp = await client.get(url, headers=headers)
        if resp.status_code in (401, 403, 429):
            logger.warning("OpenSubtitles API status %d", resp.status_code)
            return []
        resp.raise_for_status()
        data = resp.json()

        subs: list[SubtitleItem] = []
        for item in data.get("data", []):
            attrs = item.get("attributes", {})
            files = attrs.get("files", [])
            if not files:
                continue

            lang_code = attrs.get("language", "en")
            lang_name = lang_code.capitalize()
            release = attrs.get("release")
            if release:
                lang_name = f"{lang_name} ({release})"

            file_id = files[0].get("file_id")
            if not file_id:
                continue

            dl_resp = await client.post(
                "https://api.opensubtitles.com/api/v1/download",
                headers=headers,
                json={"file_id": file_id},
            )
            if dl_resp.status_code == 200:
                link = dl_resp.json().get("link")
                if link:
                    subs.append(SubtitleItem(lang=lang_code, language=f"OS - {lang_name}", url=link))

        return subs

    except Exception as e:
        logger.error("Failed to fetch OpenSubtitles for %s: %s", imdb_id, e)
        return []
