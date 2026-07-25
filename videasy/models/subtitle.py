from pydantic import BaseModel, Field


class SubtitleItem(BaseModel):
    lang: str = Field(..., description="Subtitle language code")
    language: str = Field(..., description="Subtitle language name")
    url: str = Field(..., description="Subtitle file URL")


class WyzieSubtitle(BaseModel):
    id: str = ""
    url: str = ""
    flagUrl: str = ""
    format: str = ""
    encoding: str = ""
    display: str = ""
    language: str = ""
    media: str = ""
    isHearingImpaired: bool = False
    source: str = ""
    release: str = ""
    releases: list[str] = Field(default_factory=list)
    fileName: str = ""
    downloadCount: int = 0
    origin: str | None = ""
    ai: bool = False


class SubtitleGroup(BaseModel):
    language: str = Field(..., description="Language code")
    display: str = Field(..., description="Language display name")
    flagUrl: str = Field(default="", description="Flag URL")
    subtitles: list[WyzieSubtitle] = Field(default_factory=list)
