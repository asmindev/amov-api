from __future__ import annotations

import re
from typing import Any

import httpx

from videasy.config import settings


async def expand_master_m3u8(client: httpx.AsyncClient, url: str) -> list[dict[str, Any]]:
    try:
        resp = await client.get(url, headers={"User-Agent": settings.user_agent})
        resp.raise_for_status()
        body = resp.text
    except Exception:
        return [{"quality": "Auto", "url": url}]

    base = url.rsplit("/", 1)[0]
    results: list[dict[str, Any]] = []

    for i, line in enumerate(body.splitlines()):
        if line.startswith("#EXT-X-STREAM-INF:"):
            params = line.split(":", 1)[1]
            m = re.search(r"RESOLUTION=(\d+)x(\d+)", params)
            quality: str = "Auto"
            if m:
                h = int(m.group(2))
                if h >= 2160:
                    quality = "4K"
                elif h >= 1080:
                    quality = "1080p"
                elif h >= 720:
                    quality = "720p"
                elif h >= 480:
                    quality = "480p"
                elif h >= 360:
                    quality = "360p"
                else:
                    quality = f"{h}p"

            lines = body.splitlines()
            if i + 1 < len(lines) and not lines[i + 1].startswith("#"):
                seg = lines[i + 1].strip()
                seg_url = seg if seg.startswith("http") else f"{base}/{seg}"
                results.append({"quality": quality, "url": seg_url})

    return results if results else [{"quality": "Auto", "url": url}]
