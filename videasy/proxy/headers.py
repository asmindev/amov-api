from __future__ import annotations

from videasy.config import settings

MOVIEBOX_SITE_BASE = settings.moviebox_site_base.rstrip("/")

# Domain-specific headers for known CDN providers
DOMAIN_HEADERS: dict[str, dict[str, str]] = {
    "hakunaymatata.com": {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:152.0) Gecko/20100101 Firefox/152.0",
        "Accept": "video/webm,video/ogg,video/*;q=0.9,application/ogg;q=0.7,audio/*;q=0.6,*/*;q=0.5",
        "Accept-Encoding": "identity",
        "Origin": MOVIEBOX_SITE_BASE,
        "Referer": f"{MOVIEBOX_SITE_BASE}/",
        "Sec-Fetch-Dest": "video",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "cross-site",
    },
    "aoneroom.com": {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:152.0) Gecko/20100101 Firefox/152.0",
        "Accept": "video/webm,video/ogg,video/*;q=0.9,application/ogg;q=0.7,audio/*;q=0.6,*/*;q=0.5",
        "Accept-Encoding": "identity",
        "Origin": MOVIEBOX_SITE_BASE,
        "Referer": f"{MOVIEBOX_SITE_BASE}/",
        "Sec-Fetch-Dest": "video",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "cross-site",
    },
    "themoviebox.xyz": {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:152.0) Gecko/20100101 Firefox/152.0",
        "Accept": "video/webm,video/ogg,video/*;q=0.9,application/ogg;q=0.7,audio/*;q=0.6,*/*;q=0.5",
        "Accept-Encoding": "identity",
        "Origin": MOVIEBOX_SITE_BASE,
        "Referer": f"{MOVIEBOX_SITE_BASE}/",
        "Sec-Fetch-Dest": "video",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "cross-site",
    },
    # Videasy HLS segment CDN — serves .ts/.m3u8 behind token-gated URLs.
    # Use the Videasy player origin/referer so the CDN does not serve an
    # empty 200 body or 403 to our proxy requests.
    "ironwallnet.net": {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:152.0) Gecko/20100101 Firefox/152.0",
        "Accept": "video/webm,video/ogg,video/*;q=0.9,application/ogg;q=0.7,audio/*;q=0.6,*/*;q=0.5",
        "Accept-Encoding": "identity",
        "Origin": "https://player.videasy.to",
        "Referer": "https://player.videasy.to/",
        "Sec-Fetch-Dest": "video",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "cross-site",
    },
    # Cypher/Videasy MP4 CDN behind Cloudflare Workers. Cloudflare WAF may
    # still block datacenter IPs — these headers match a real browser session.
    "realworkers.workers.dev": {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:152.0) Gecko/20100101 Firefox/152.0",
        "Accept": "video/webm,video/ogg,video/*;q=0.9,application/ogg;q=0.7,audio/*;q=0.6,*/*;q=0.5",
        "Accept-Encoding": "identity",
        "Origin": "https://player.videasy.to",
        "Referer": "https://player.videasy.to/",
        "Sec-Fetch-Dest": "video",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "cross-site",
    },
    "flikhub.net": {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:152.0) Gecko/20100101 Firefox/152.0",
        "Accept": "*/*",
        "Accept-Encoding": "identity",
        "Origin": "https://player.cinezo.live",
        "Referer": "https://player.cinezo.live/",
        "Sec-Fetch-Dest": "video",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "cross-site",
    },
}


def get_domain_headers(url: str) -> dict[str, str] | None:
    """Return domain-specific headers if URL matches a known CDN, else None."""
    url_lower = url.lower()
    for domain, headers in DOMAIN_HEADERS.items():
        if domain in url_lower:
            return headers
    return None
