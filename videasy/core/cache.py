from __future__ import annotations

import time


class TTLCache:
    """Simple in-memory cache with per-entry TTL."""

    def __init__(self) -> None:
        self._store: dict[str, tuple[str, float]] = {}

    def get(self, key: str) -> str | None:
        entry = self._store.get(key)
        if entry:
            value, expiry = entry
            if time.monotonic() < expiry:
                return value
            del self._store[key]
        return None

    def set(self, key: str, value: str, ttl_seconds: float) -> None:
        expiry = time.monotonic() + ttl_seconds
        self._store[key] = (value, expiry)

    def clear(self) -> None:
        self._store.clear()
