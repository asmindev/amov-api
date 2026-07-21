from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class SourceItem(BaseModel):
    quality: str
    url: str


class SubtitleItem(BaseModel):
    lang: str
    language: str
    url: str


class DecryptedData(BaseModel):
    sources: list[SourceItem]
    subtitles: list[SubtitleItem]

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> DecryptedData:
        return cls(
            sources=[SourceItem(**s) for s in raw.get("sources", [])],
            subtitles=[SubtitleItem(**s) for s in raw.get("subtitles", [])],
        )


class SourceResponse(BaseModel):
    tmdbId: str
    provider: str
    data: DecryptedData


class ProviderInfo(BaseModel):
    name: str
    endpoint: str


class ProviderList(BaseModel):
    providers: list[ProviderInfo]


class ErrorDetail(BaseModel):
    error: str
    detail: str


class SourceParams(BaseModel):
    title: str = Field(..., min_length=1)
    mediaType: Literal["movie", "tv"]
    tmdbId: str = Field(..., pattern=r"^\d+$")
    provider: str = Field(..., min_length=1)
    year: str = Field(default="", pattern=r"^\d{4}$|^$")
    episodeId: str = Field(default="1", pattern=r"^\d+$")
    seasonId: str = Field(default="1", pattern=r"^\d+$")
    imdbId: str = Field(default="", pattern=r"^tt\d+$|^$")
