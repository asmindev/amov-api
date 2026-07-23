from __future__ import annotations

import re

QUALITY_MAP: dict[str, str] = {"playhq": "1080p", "bk": "480p", "hd": "720p"}
QUALITY_ORDER: dict[str, int] = {
    "4K": 0,
    "2160p": 1,
    "1080p": 2,
    "720p": 3,
    "480p": 4,
    "360p": 5,
    "Auto": 6,
}


def normalize_quality(quality: str, url: str = "") -> str:
    if url:
        m = re.search(r"index-s(\d+)p|/(\d+)p/|_(\d+)p\.|/(\d+)p_", url, re.IGNORECASE)
        if m:
            h = int(m.group(1) or m.group(2) or m.group(3) or m.group(4))
            if h >= 2160:
                return "4K"
            elif h >= 1080:
                return "1080p"
            elif h >= 720:
                return "720p"
            elif h >= 480:
                return "480p"
            elif h >= 360:
                return "360p"
            elif h > 0:
                return f"{h}p"
        if "2160p" in url.lower() or "/4k/" in url.lower():
            return "4K"

    return QUALITY_MAP.get(quality, quality)


def quality_sort_key(quality: str) -> int:
    return QUALITY_ORDER.get(quality, 99)
