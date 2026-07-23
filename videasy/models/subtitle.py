from pydantic import BaseModel, Field


class SubtitleItem(BaseModel):
    lang: str = Field(..., description="Subtitle language code")
    language: str = Field(..., description="Subtitle language name")
    url: str = Field(..., description="Subtitle file URL")
