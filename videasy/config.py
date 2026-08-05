from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    api_base: str = "https://api.speedracelight.com"
    origin: str = "https://player.videasy.to"
    referer: str = "https://player.videasy.to/"
    dec_api: str = "https://enc-dec.app/api/dec-videasy"
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/137.0.0.0 Safari/537.36"
    )
    request_timeout: int = 30
    proxy_connect_timeout: float = 10.0
    proxy_pool_timeout: float = 10.0
    # Optional outbound proxy (e.g. "http://user:pass@host:port") used only by the
    # streaming proxy client. Set this when the host's egress IP is blocked/dropped
    # by the CDNs (ConnectTimeout) so CDN traffic can exit through a non-blocked IP.
    proxy_outbound: str = ""
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

    # Flikhub Fallback Proxy
    flikhub_proxy_base: str = "https://proxy1.flikhub.net"

    model_config = {"env_prefix": "VIDEASY_", "env_file": ".env"}


settings = Settings()
