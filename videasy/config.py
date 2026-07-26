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
    wyzie_api_key: str = ""

    # Cinemeta (Stremio addon for metadata)
    cinemeta_base: str = "https://v3-cinemeta.strem.io"

    # Moviebox / Aoneroom API
    moviebox_api_base: str = "https://h5-api.aoneroom.com"
    moviebox_detail_endpoint: str = "/wefeed-h5api-bff/detail"
    moviebox_search_endpoint: str = "/wefeed-h5api-bff/subject/search"
    moviebox_site_base: str = "https://themoviebox.xyz"
    moviebox_play_base: str = "https://themoviebox.xyz"
    moviebox_play_endpoint: str = "/wefeed-h5api-bff/subject/play"

    model_config = {"env_prefix": "VIDEASY_", "env_file": ".env"}


settings = Settings()
