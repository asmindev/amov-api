from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class MediaMeta(BaseModel):
    title: str = Field(..., description="Media title")
    provider: str = Field(..., description="Provider display name (e.g. Yoru, Moviebox)")
    mediaType: Literal["movie", "tv"] = Field(..., description="Media type: movie or tv")
    tmdbId: str | None = Field(default=None, description="TMDB numerical ID if available")
    imdbId: str | None = Field(default=None, description="IMDB ID if available (e.g. tt0816692)")
    year: str | None = Field(default=None, description="Release year")
    cover: str | None = Field(default=None, description="Cover image URL if available")
    requestedTitle: str | None = Field(
        default=None, description="Title the client asked for (original title from params)"
    )
    titleMatched: bool | None = Field(
        default=None,
        description="True when the resolved Moviebox title equals requestedTitle or the "
        "English title (normalized). False means the resolved subject is a different "
        "film or a translation mismatch — in which case sources are returned empty.",
    )
    yearMismatch: bool | None = Field(
        default=None,
        description="True when the resolved Moviebox release year differs from the "
        "requested year — sources are returned empty.",
    )


class EpisodeInfo(BaseModel):
    season: int | None = Field(default=None, description="Season number")
    episode: int | None = Field(default=None, description="Episode number")


class MediaSourceItem(BaseModel):
    quality: str = Field(..., description="Quality label (e.g. 1080p, 720p, 4K, Auto)")
    size: str | None = Field(default=None, description="File size if available (e.g. 869MB, 2106MB)")
    url: str = Field(..., description="Direct or proxied stream URL (.mp4, .m3u8, .mpd)")
    type: str = Field(default="mp4", description="Stream type: mp4, hls, or dash")
    headers: dict[str, str] | None = Field(default=None, description="Optional custom headers required for playback")
    source: str | None = Field(default=None, description="Source origin tag (e.g. play, community, trailer)")


class MediaSubtitleItem(BaseModel):
    lang: str = Field(..., description="ISO 2-letter language code (e.g. id, en, es)")
    language: str = Field(..., description="Display language name (e.g. Indonesian, English)")
    url: str = Field(..., description="Subtitle file URL (.vtt or .srt)")


class UnifiedMediaResponse(BaseModel):
    meta: MediaMeta = Field(..., description="General media metadata")
    episode: EpisodeInfo | None = Field(default=None, description="Episode/season info for TV series; null for movies")
    sources: list[MediaSourceItem] = Field(default_factory=list, description="List of video sources sorted by quality")
    subtitles: list[MediaSubtitleItem] = Field(default_factory=list, description="List of subtitle tracks")
