import logging
import httpx
from typing import Any

from app.config import settings
from app.models import SubtitleItem

logger = logging.getLogger("videasy.opensubtitles")

async def fetch_opensubtitles(client: httpx.AsyncClient, imdb_id: str) -> list[SubtitleItem]:
    if not imdb_id or not imdb_id.startswith("tt"):
        return []

    numeric_id = imdb_id.replace("tt", "").lstrip("0")
    if not numeric_id:
        return []

    headers = {
        "User-Agent": settings.user_agent,
    }
    if hasattr(settings, "opensubtitles_api_key") and settings.opensubtitles_api_key:
        headers["Api-Key"] = settings.opensubtitles_api_key
    else:
        logger.warning("OpenSubtitles API key is not set. You might be rate-limited or blocked.")
        # Proceeding anyway as some endpoints might work or they might set it later

    url = f"https://api.opensubtitles.com/api/v1/subtitles?imdb_id={numeric_id}"
    
    try:
        resp = await client.get(url, headers=headers)
        if resp.status_code in (401, 403):
            logger.warning(f"OpenSubtitles API unauthorized/forbidden: {resp.status_code}")
            return []
        if resp.status_code == 429:
            logger.warning("OpenSubtitles API rate limited")
            return []
            
        resp.raise_for_status()
        data = resp.json()
        
        subs = []
        for item in data.get("data", []):
            attrs = item.get("attributes", {})
            files = attrs.get("files", [])
            if not files:
                continue
                
            lang_code = attrs.get("language", "en")
            # Create a presentable language name
            lang_name = lang_code.capitalize()
            
            # If release name is present, use it to make the language string more identifiable
            release = attrs.get("release")
            if release:
                lang_name = f"{lang_name} ({release})"
                
            file_id = files[0].get("file_id")
            if not file_id:
                continue

            # Note: For the actual download URL, the REST API requires a POST request to /api/v1/download
            # Here we will attempt to fetch the download URL
            dl_url = f"https://api.opensubtitles.com/api/v1/download"
            dl_resp = await client.post(dl_url, headers=headers, json={"file_id": file_id})
            
            if dl_resp.status_code == 200:
                dl_data = dl_resp.json()
                link = dl_data.get("link")
                if link:
                    subs.append(SubtitleItem(
                        lang=lang_code,
                        language=f"OS - {lang_name}",
                        url=link
                    ))
                    
        return subs
        
    except Exception as e:
        logger.error(f"Failed to fetch OpenSubtitles for {imdb_id}: {e}")
        return []
