from __future__ import annotations

from typing import ClassVar


class Provider:
    __slots__ = ("name", "endpoint")

    def __init__(self, name: str, endpoint: str) -> None:
        self.name = name
        self.endpoint = endpoint

    def __repr__(self) -> str:
        return f"Provider({self.name}, {self.endpoint})"


YORU: ClassVar[Provider] = Provider("Yoru", "cdn")
NEON: ClassVar[Provider] = Provider("Neon", "neon2")
CYPHER: ClassVar[Provider] = Provider("Cypher", "downloader2")
BREACH: ClassVar[Provider] = Provider("Breach", "m4uhd")
MOVIEBOX: ClassVar[Provider] = Provider("Moviebox", "moviebox")

AVAILABLE: list[Provider] = [YORU, NEON, CYPHER, BREACH, MOVIEBOX]
PROVIDER_MAP: dict[str, Provider] = {p.name.lower(): p for p in AVAILABLE}
