from __future__ import annotations

from typing import Any

import httpx

from videasy.config import settings
from videasy.utils.m3u8 import expand_master_m3u8
from videasy.utils.quality import normalize_quality, quality_sort_key

_DEC_HEADERS: dict[str, str] = {
    "Content-Type": "application/json",
    "Accept": "application/json",
}


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
            expanded.extend(await expand_master_m3u8(client, u))
        else:
            expanded.append(src)

    for src in expanded:
        src["quality"] = normalize_quality(src["quality"])

    expanded.sort(key=lambda s: quality_sort_key(s["quality"]))
    result["sources"] = expanded
    return result
