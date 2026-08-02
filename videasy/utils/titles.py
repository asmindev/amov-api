"""Title normalization helpers.

Moviebox (TheMovieBox.xyz) stores titles with localization tags and season
markers ("Call Me Dad [English]", "Avatar: The Last Airbender S1-S2"). When the
client asks for a title by its original name, we compare it against the title
Moviebox resolved — but only after normalizing both sides so those decorations
don't break an exact match.

The comparison itself is deliberately a plain ``==`` after normalization:
Cinemeta/TMDB titles are consistent, and when a mismatch happens it has exactly
two causes — the requested original title was translated on the meta side
(e.g. "Ketindihan" vs "Sleep Paralysis"), or it is genuinely a different film.
The caller reports the mismatch and lets the client decide which one it is.
"""
from __future__ import annotations

import re

# Bracketed language tags Moviebox appends to localized titles, e.g.
# "Call Me Dad [English]", "Parasite [Hindi]", "Exhuma [Indonesian]".
_LANG_TAG_RE = re.compile(r"\s*\[[^\]]+\]\s*")
# Season markers: "S1-S2", "S1", "Season 1" — belong to a TV show, not its title.
_SEASON_RE = re.compile(r"\s*(?:S\d+(?:-S\d+)?|Season\s*\d+)\s*$", re.IGNORECASE)
# Trailing parenthesized year, e.g. "Spider-Man: No Way Home (2021)".
_YEAR_RE = re.compile(r"\s*\(?(?:19|20)\d{2}\)?\s*$")


def normalize_title(title: str) -> str:
    """Normalize a title for exact comparison.

    Lowercases, strips bracketed language tags, drops season markers and a
    trailing parenthesized year, removes punctuation, and collapses whitespace.
    Safe for titles in any script (Indonesian, Korean, Latin, ...).
    """
    if not title:
        return ""
    t = title.strip()
    t = _LANG_TAG_RE.sub(" ", t)
    t = _SEASON_RE.sub(" ", t)
    t = _YEAR_RE.sub(" ", t)
    t = re.sub(r"[^\w\s]", " ", t, flags=re.UNICODE)
    t = re.sub(r"\s+", " ", t).strip()
    return t.lower()
