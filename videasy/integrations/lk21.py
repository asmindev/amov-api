from __future__ import annotations
import re
from urllib.parse import quote
import httpx

LK21_DOMAIN = "https://tv12.lk21official.cc"
SEARCH_API = "https://gudangvape.com/search.php"
PLAYER_API = "https://playcdn.de/api2.php"

def _normalize(s: str) -> str:
    return re.sub(r'[^a-z0-9]', '', str(s).lower())

async def search_lk21_slug(client: httpx.AsyncClient, title: str, year: str = "") -> str | None:
    """Finds the LK21 slug for a given title and year."""
    url = f"{SEARCH_API}?s={quote(title)}&page=1"
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Origin": LK21_DOMAIN,
        "Referer": f"{LK21_DOMAIN}/"
    }
    
    resp = await client.get(url, headers=headers)
    if resp.status_code != 200:
        return None
        
    try:
        data = resp.json()
        items = data.get("data") or data.get("items") or []
        
        req_norm = _normalize(title)
        
        for item in items:
            item_title = item.get("title", "").split(" (")[0]
            item_year = str(item.get("year", ""))
            if _normalize(item_title) == req_norm:
                if year and item_year and item_year != year:
                    continue
                return item.get("slug")
    except Exception:
        pass
        
    return None

async def extract_lk21_video_id(client: httpx.AsyncClient, slug: str) -> str | None:
    """Fetches the movie page and extracts the video ID from the iframe."""
    url = f"{LK21_DOMAIN}/{slug}"
    headers = {"User-Agent": "Mozilla/5.0"}
    
    resp = await client.get(url, headers=headers, follow_redirects=True)
    if resp.status_code != 200:
        return None
        
    m = re.search(r'iframe[^>]+src=["\']https?://(?:videonode\.de/iframe/p2p/|playcdn\.de/video\.php\?id=)([^"\']+)["\']', resp.text)
    if m:
        return m.group(1).split('?')[0].split('&')[0]
    return None

async def get_lk21_m3u8(client: httpx.AsyncClient, video_id: str, slug: str) -> dict | None:
    """Calls playcdn API to get the m3u8 source."""
    url = f"{PLAYER_API}?id={video_id}"
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://playcdn.de/",
    }
    payload = {
        "r": f"{LK21_DOMAIN}/{slug}",
        "d": "playcdn.de"
    }
    
    resp = await client.post(url, data=payload, headers=headers)
    if resp.status_code != 200:
        return None
        
    try:
        data = resp.json()
        if "file" in data:
            return {
                "file": f"https://playcdn.de{data['file']}",
                "title": data.get("title", "")
            }
    except Exception:
        pass
        
    return None

async def fetch_lk21_source(client: httpx.AsyncClient, title: str, year: str = "") -> dict | None:
    """High-level function to fetch LK21 source dict."""
    
    # 1. Try guessing the slug format: judul-lengkap-tahun
    guessed_slug = f"{re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')}-{year}"
    slug = guessed_slug
    video_id = await extract_lk21_video_id(client, slug)
    
    # 2. Fallback to Search API if guessing failed
    if not video_id:
        slug = await search_lk21_slug(client, title, year)
        if not slug:
            return None
        video_id = await extract_lk21_video_id(client, slug)
        
    if not video_id:
        return None
        
    result = await get_lk21_m3u8(client, video_id, slug)
    if result and result["file"]:
        return {
            "sources": [{
                "quality": "Auto (LK21)",
                "url": result["file"],
                "type": "hls",
                "source": "lk21"
            }],
            "meta": {
                "title": result["title"] or title,
                "titleMatched": True,
                "yearMismatch": False
            }
        }
    return None
