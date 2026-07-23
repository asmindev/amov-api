from __future__ import annotations

import time
from typing import Any


class TTLCache:
    """Simple in-memory cache with per-entry TTL."""

    def __init__(self, default_ttl: float = 30.0) -> None:
        self._store: dict[str, tuple[Any, float]] = {}
        self.default_ttl = default_ttl

    def get(self, key: str, default: Any = None) -> Any:
        entry = self._store.get(key)
        if entry:
            value, expiry = entry
            if time.monotonic() < expiry:
                return value
            del self._store[key]
        return default

    def set(self, key: str, value: Any, ttl_seconds: float) -> None:
        expiry = time.monotonic() + ttl_seconds
        self._store[key] = (value, expiry)

    def clear(self) -> None:
        self._store.clear()

    def __getitem__(self, key: str) -> Any:
        val = self.get(key)
        if val is None:
            raise KeyError(key)
        return val

    def __setitem__(self, key: str, value: Any) -> None:
        if isinstance(value, tuple) and len(value) == 2 and isinstance(value[1], (int, float)):
            val, seconds_or_expiry = value
            if seconds_or_expiry > time.monotonic():
                ttl = seconds_or_expiry - time.monotonic()
            else:
                ttl = float(seconds_or_expiry)
            self.set(key, val, max(ttl, 0.0))
        else:
            self.set(key, value, self.default_ttl)

    def __contains__(self, key: str) -> bool:
        return self.get(key) is not None

    def __delitem__(self, key: str) -> None:
        self._store.pop(key, None)

