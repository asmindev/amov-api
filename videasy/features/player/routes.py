from __future__ import annotations

import os

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader

router = APIRouter(include_in_schema=False)

_template_dir = os.path.join(os.path.dirname(__file__), "templates")
_jinja_env = Environment(loader=FileSystemLoader(_template_dir), autoescape=True)


@router.get("/player", response_class=HTMLResponse)
async def player_page(
    request: Request,
    url: str = Query(default="", description="Video URL to play"),
    sources: str = Query(default="", description="JSON array of sources (quality/url pairs)"),
    subtitles: str = Query(default="", description="JSON array of subtitles (language/url pairs)"),
) -> str:
    template = _jinja_env.get_template("player.html")
    return template.render(url=url, sources=sources, subtitles=subtitles)


@router.get("/", response_class=HTMLResponse)
async def landing_page(request: Request) -> str:
    from videasy.features.sources.providers import AVAILABLE
    template = _jinja_env.get_template("index.html")

    provider_badges = "".join(
        f'<span class="pill pill-{"green" if i < 2 else "amber" if i < 3 else "slate"}" style="margin-right:6px">{p.name}</span>'
        for i, p in enumerate(AVAILABLE)
    )

    return template.render(provider_badges=provider_badges)
