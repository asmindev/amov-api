import io
import logging
import re
import zipfile
from collections import OrderedDict

import httpx

from videasy.models.subtitle import SubtitleGroup, WyzieSubtitle

logger = logging.getLogger("videasy.subsource")


async def fetch_subsource_grouped(
    client: httpx.AsyncClient,
    *,
    title: str = "",
    year: str = "",
    media_type: str = "movie",
) -> list[SubtitleGroup]:
    """Search subsource.net and return grouped subtitles."""
    if not title:
        return []

    # 1. Search for the media
    search_url = "https://api.subsource.net/v1/movie/search"
    payload = {"query": title, "includeSeasons": False, "limit": 15}
    headers = {
        "Origin": "https://subsource.net",
        "Referer": "https://subsource.net/",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }
    
    try:
        resp = await client.post(search_url, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.error(f"SubSource search failed: {e}")
        return []

    results = data.get("results", [])
    slug_link = None
    for res in results:
        # For simplicity, pick the first matching year or just first result if no year
        if year and str(res.get("releaseYear")) != str(year):
            continue
        slug_link = res.get("link")
        break

    if not slug_link:
        if results and not year:
            slug_link = results[0].get("link")
        else:
            return []

    # 2. Fetch the movie page (specifically Indonesian to ensure it is included)
    try:
        # Since the user requested focusing on Indonesian, we directly fetch the Indonesian language page
        # because the main movie page might only show the first few languages (e.g. Arabic, Bengali)
        page_resp = await client.get(f"https://subsource.net{slug_link}/indonesian", headers=headers)
        page_resp.raise_for_status()
        html = page_resp.text
    except Exception as e:
        logger.error(f"SubSource page fetch failed: {e}")
        return []

    # 3. Extract subtitles using regex
    # e.g. <a href="/subtitle/evil-dead-burn-2026/arabic/10247749">Arabic — Evil.Dead.Burn.2026.WEB.H264-RBB</a>
    pattern = re.compile(r'<a href="(/subtitle/[^"]+)">([^<]+)</a>')
    grouped: OrderedDict[str, SubtitleGroup] = OrderedDict()

    # To avoid duplicates if the same link appears multiple times
    seen_urls = set()

    for m in pattern.finditer(html):
        url_path = m.group(1)
        full_text = m.group(2) # "Arabic — Evil.Dead.Burn..."
        
        if url_path in seen_urls:
            continue
        seen_urls.add(url_path)

        parts = full_text.split(" — ", 1)
        lang_display = parts[0].strip()
        release_name = parts[1].strip() if len(parts) > 1 else full_text

        if lang_display.lower() not in ("indonesian", "indonesia"):
            continue

        # Create a proxy download URL that our backend will handle
        from urllib.parse import quote
        proxy_url = f"/subsource/download?url={quote(url_path)}"

        if lang_display not in grouped:
            # simple language code mapping fallback or just use lowercase
            lang_code = lang_display.lower()
            grouped[lang_display] = SubtitleGroup(
                language=lang_code,
                display=lang_display,
                flagUrl="",
            )

        grouped[lang_display].subtitles.append(
            WyzieSubtitle(
                id=url_path.split("/")[-1],
                url=proxy_url,
                display=lang_display,
                language=lang_code,
                source="SubSource",
                release=release_name,
                fileName=release_name,
            )
        )

    return list(grouped.values())


async def extract_subsource_vtt(client: httpx.AsyncClient, subtitle_page_url: str) -> str | None:
    """Given a subsource subtitle page path, find the ZIP and extract the VTT/SRT."""
    headers = {
        "Origin": "https://subsource.net",
        "Referer": "https://subsource.net/",
        "User-Agent": "Mozilla/5.0",
    }
    # 1. Fetch the subtitle page to get the download token
    try:
        resp = await client.get(f"https://subsource.net{subtitle_page_url}", headers=headers)
        resp.raise_for_status()
        html = resp.text
    except Exception as e:
        logger.error(f"Failed to fetch subtitle page: {e}")
        return None

    # <a href="https://api.subsource.net/v1/subtitle/download/b7b2a6dd..." download="">
    m = re.search(r'<a href="(https://api\.subsource\.net/v1/subtitle/download/[^"]+)"\s*download', html)
    if not m:
        logger.error("Could not find download link in SubSource page")
        return None
    
    download_url = m.group(1)

    # 2. Download the ZIP
    try:
        zip_resp = await client.get(download_url, headers=headers)
        zip_resp.raise_for_status()
    except Exception as e:
        logger.error(f"Failed to download SubSource ZIP: {e}")
        return None

    # 3. Extract the SRT/VTT
    try:
        with zipfile.ZipFile(io.BytesIO(zip_resp.content)) as z:
            for name in z.namelist():
                if name.endswith(".vtt") or name.endswith(".srt"):
                    content = z.read(name)
                    # Very simple SRT to VTT if needed, or just return as is
                    text = content.decode("utf-8", errors="replace")
                    if name.endswith(".srt"):
                        text = convert_srt_to_vtt(text)
                    return text
    except Exception as e:
        logger.error(f"Failed to extract ZIP: {e}")
        return None

    return None

def convert_srt_to_vtt(srt_text: str) -> str:
    """Basic SRT to VTT conversion."""
    vtt = "WEBVTT\n\n"
    # Replace comma with dot in timestamps
    lines = srt_text.splitlines()
    for line in lines:
        if "-->" in line:
            line = line.replace(",", ".")
        vtt += line + "\n"
    return vtt
