from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class SourceItem(BaseModel):
    quality: str = Field(..., description="Video quality label (e.g. 4K, 1080p, 720p)")
    url: str = Field(..., description="HLS stream URL (.m3u8)")


class SubtitleItem(BaseModel):
    lang: str = Field(..., description="Subtitle language code")
    language: str = Field(..., description="Subtitle language name")
    url: str = Field(..., description="Subtitle file URL")


class DecryptedData(BaseModel):
    sources: list[SourceItem] = Field(..., description="List of video sources sorted by quality (highest first)")
    subtitles: list[SubtitleItem] = Field(default_factory=list, description="Available subtitle tracks")

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> DecryptedData:
        return cls(
            sources=[SourceItem(**s) for s in raw.get("sources", [])],
            subtitles=[SubtitleItem(**s) for s in raw.get("subtitles", [])],
        )


class SourceResponse(BaseModel):
    tmdbId: str = Field(..., description="TMDB ID of the requested media")
    provider: str = Field(..., description="Provider that served the sources")
    data: DecryptedData = Field(..., description="Decrypted stream data")


class ProviderInfo(BaseModel):
    name: str = Field(..., description="Provider display name")
    endpoint: str = Field(..., description="Provider endpoint slug used in API calls")


class ProviderList(BaseModel):
    providers: list[ProviderInfo] = Field(..., description="List of active providers")


class ErrorDetail(BaseModel):
    error: str = Field(..., description="Error code")
    detail: str = Field(..., description="Human-readable error description")


class SourceParams(BaseModel):
    title: str = Field(..., min_length=1, description="Media title")
    mediaType: Literal["movie", "tv"] = Field(..., description="Media type")
    tmdbId: str = Field(..., pattern=r"^\d+$", description="TMDB numerical ID")
    provider: str = Field(..., min_length=1, description="Provider name")
    year: str = Field(default="", pattern=r"^\d{4}$|^$", description="Release year")
    episodeId: str = Field(default="1", pattern=r"^\d+$", description="Episode number (TV)")
    seasonId: str = Field(default="1", pattern=r"^\d+$", description="Season number (TV)")
    imdbId: str = Field(default="", pattern=r"^tt\d+$|^$", description="IMDB ID")
