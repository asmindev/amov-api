from __future__ import annotations

import re
from typing import Any

import httpx

from app.config import settings

_QUALITY_MAP: dict[str, str] = {"playhq": "1080p", "bk": "480p", "hd": "720p"}
_ORDER: dict[str, int] = {"4K": 0, "2160p": 1, "1080p": 2, "720p": 3, "480p": 4, "360p": 5, "Auto": 6}
_DEC_HEADERS: dict[str, str] = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "User-Agent": settings.user_agent,
}


async def _expand_master_m3u8(client: httpx.AsyncClient, url: str) -> list[dict[str, Any]]:
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


async def decrypt(client: httpx.AsyncClient, ciphertext: str, tmdb_id: str, seed: str) -> dict[str, Any]:
    body = {"text": ciphertext, "id": tmdb_id, "seed": seed}
    resp = await client.post(settings.dec_api, json=body, headers=_DEC_HEADERS)
    if resp.status_code == 403:
        raise RuntimeError("dec-videasy: 403 Forbidden — bad User-Agent or blocked")
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") != 200:
        raise RuntimeError(f"dec-videasy error: {data.get('error', 'unknown')}")

    result = data["result"]
    expanded: list[dict[str, Any]] = []
    for src in result.get("sources", []):
        u = src.get("url", "")
        if "/master.m3u8" in u:
            expanded.extend(await _expand_master_m3u8(client, u))
        else:
            expanded.append(src)

    for src in expanded:
        src["quality"] = _QUALITY_MAP.get(src["quality"], src["quality"])

    expanded.sort(key=lambda s: _ORDER.get(s["quality"], 99))
    result["sources"] = expanded
    return result
