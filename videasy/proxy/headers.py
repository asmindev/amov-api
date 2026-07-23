from __future__ import annotations

# Domain-specific headers for known CDN providers
DOMAIN_HEADERS: dict[str, dict[str, str]] = {
    "hakunaymatata.com": {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:152.0) Gecko/20100101 Firefox/152.0",
        "Accept": "video/webm,video/ogg,video/*;q=0.9,application/ogg;q=0.7,audio/*;q=0.6,*/*;q=0.5",
        "Origin": "https://themoviebox.xyz",
        "Referer": "https://themoviebox.xyz/",
        "Sec-Fetch-Dest": "video",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "cross-site",
    },
    "aoneroom.com": {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:152.0) Gecko/20100101 Firefox/152.0",
        "Accept": "video/webm,video/ogg,video/*;q=0.9,application/ogg;q=0.7,audio/*;q=0.6,*/*;q=0.5",
        "Origin": "https://themoviebox.xyz",
        "Referer": "https://themoviebox.xyz/",
        "Sec-Fetch-Dest": "video",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "cross-site",
    },
    "themoviebox.xyz": {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:152.0) Gecko/20100101 Firefox/152.0",
        "Accept": "video/webm,video/ogg,video/*;q=0.9,application/ogg;q=0.7,audio/*;q=0.6,*/*;q=0.5",
        "Origin": "https://themoviebox.xyz",
        "Referer": "https://themoviebox.xyz/",
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
