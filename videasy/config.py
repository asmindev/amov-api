from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    api_base: str = "https://api.wingsdatabase.com"
    origin: str = "https://player.videasy.to"
    referer: str = "https://player.videasy.to/"
    dec_api: str = "https://enc-dec.app/api/dec-videasy"
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/137.0.0.0 Safari/537.36"
    )
    request_timeout: int = 30
    cache_ttl_offset: int = 5_000
    opensubtitles_api_key: str = "bOPLedTRgxt5u4jRtkXIbxMf36OSbvEH"

    model_config = {"env_prefix": "VIDEASY_", "env_file": ".env"}


settings = Settings()
