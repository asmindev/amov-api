from __future__ import annotations

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


def normalize_quality(quality: str) -> str:
    return QUALITY_MAP.get(quality, quality)


def quality_sort_key(quality: str) -> int:
    return QUALITY_ORDER.get(quality, 99)
