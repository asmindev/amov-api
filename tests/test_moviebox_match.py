"""Unit tests for Moviebox title-resolution logic.

The design is deliberately simple: after fetching the Moviebox detail, the
resolved title is compared against the requested title with a normalized ``==``.
A mismatch is NOT blocked — it is reported via ``requestedTitle`` /
``titleMatched`` on the result so the client can judge whether the difference
is a translation (e.g. "Ketindihan" vs "Sleep Paralysis") or a different film.

Fixtures mirror the real Moviebox search/detail responses we probed.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from videasy.features.moviebox.service import (
    _match_search_result,
    fetch_sources,
)
from videasy.utils.titles import normalize_title


def _res(subject_id: str, title: str, year: str, subject_type: int = 1) -> dict:
    return {"subjectId": subject_id, "title": title, "year": year, "subjectType": subject_type}


# ── _match_search_result ──────────────────────────────────────────────────────

def test_match_exact_normalized_title():
    results = [
        _res("A", "Senin Harga Naik", "2026"),
        _res("B", "1 Kakak 7 Ponakan", "2025"),
    ]
    got = _match_search_result(results, "1 Kakak 7 Ponakan", subject_type=1, year="2025")
    assert got and got["subjectId"] == "B"


def test_match_lang_tag_variant():
    # Normalization strips "[Indonesian]", so the plain == matches either A or B.
    results = [
        _res("A", "Exhuma", "2024"),
        _res("B", "Exhuma [Indonesian]", "2024"),
        _res("C", "Colony", "2026"),
    ]
    got = _match_search_result(results, "Exhuma", subject_type=1, year="2024")
    assert got is not None
    assert normalize_title(got["title"]) == normalize_title("Exhuma")


def test_match_falls_back_to_same_type_result():
    # No exact title match: return the first type-appropriate result (so the
    # caller still has a subject), and let the mismatch be reported upstream.
    results = [
        _res("A", "Senin Harga Naik", "2026"),
        _res("B", "Me Before Me", "2026"),
    ]
    got = _match_search_result(results, "Tunggu Aku Sukses Nanti", subject_type=1, year="2026")
    assert got is not None  # caller reports titleMatched=False
    assert got["subjectId"] == "A"


def test_match_respects_media_type():
    # Search can return a TV show (type 2) for a movie query (type 1); an exact
    # title match is only accepted when the subject type matches too.
    results = [
        _res("A", "The Divorce Insurance", "2025", subject_type=2),
        _res("B", "The Divorce Insurance", "2025", subject_type=2),
    ]
    assert _match_search_result(results, "The Divorce Insurance", subject_type=1, year="2025") is None


# ── 2025 corpus: 3 Indonesian, 3 Korean, 3 Western (original titles) ─────────

# (imdb_id, original_title, media_type, year) — real IDs resolved via Cinemeta.
CORPUS_2025 = [
    # Indonesian 2025
    ("tt32881480", "1 Kakak 7 Ponakan", "movie", "2025"),
    ("tt36553680", "Andai Ibu Tidak Menikah dengan Ayah", "movie", "2025"),
    ("tt34883346", "Ketindihan", "movie", "2025"),
    # Korean 2025
    ("tt23630030", "Harbin", "movie", "2025"),
    ("tt22507374", "Bogota: City of the Lost", "movie", "2025"),
    ("tt34603257", "The Divorce Insurance", "tv", "2025"),
    # Western 2025
    ("tt20969586", "Thunderbolts*", "movie", "2025"),
    ("tt26743210", "How to Train Your Dragon", "movie", "2025"),
    ("tt19847976", "Wicked: For Good", "movie", "2025"),
]


def test_normalized_titles_match_whole_2025_corpus():
    """Each requested original title must equal its Moviebox detail title after
    normalization — i.e. titleMatched would be True for the corpus films."""
    expected_detail_titles = {
        "1 Kakak 7 Ponakan": "1 Kakak 7 Ponakan",
        "Andai Ibu Tidak Menikah dengan Ayah": "Andai Ibu Tidak Menikah Dengan Ayah",
        "Ketindihan": "Ketindihan",
        "Harbin": "Harbin",
        "Bogota: City of the Lost": "Bogota: City of the Lost",
        "The Divorce Insurance": "The Divorce Insurance [Indonesian]",
        "Thunderbolts*": "Thunderbolts*",
        "How to Train Your Dragon": "How to Train Your Dragon [Indonesian]",
        "Wicked: For Good": "Wicked: For Good",
    }
    for _, title, _, _ in CORPUS_2025:
        detail_title = expected_detail_titles[title]
        assert normalize_title(title) == normalize_title(detail_title), (
            f"{title!r} should normalize-match its Moviebox detail title {detail_title!r}"
        )


def test_different_films_do_not_normalize_match():
    """Different films must not compare equal after normalization."""
    different_pairs = [
        ("Tunggu Aku Sukses Nanti", "Senin Harga Naik"),  # log bug scenario
        ("Tunggu Aku Sukses Nanti", "1 Kakak 7 Ponakan"),
        ("Senin Harga Naik", "Check Out Sekarang, Pay Later (Caper)"),
        ("Kiss of the Rabbit God", "Kiss of the Spider Woman"),
        ("28 Years Later", "28 Weeks Later"),
        ("The Fantastic Four", "The Fantastic Four: First Steps"),
    ]
    for a, b in different_pairs:
        assert normalize_title(a) != normalize_title(b), f"{a!r} and {b!r} must not normalize-match"


# ── full fetch_sources pipeline (resolve → detail → report) ──────────────────

def _search_payload(client, title: str) -> list[dict]:
    """Realistic search payload for the given title (subjectType 1 = movie)."""
    by_title = {
        "1 Kakak 7 Ponakan": [_res("955166268645066984", "1 Kakak 7 Ponakan", "2025")],
        "Andai Ibu Tidak Menikah dengan Ayah": [_res("6294269002252796048", "Andai Ibu Tidak Menikah Dengan Ayah", "2025")],
        "Ketindihan": [_res("7253520119266525728", "Ketindihan", "2025")],
        "Harbin": [_res("1801610857218983248", "Harbin", "2025")],
        "Bogota: City of the Lost": [_res("4132652923279081800", "Bogota: City of the Lost", "2025")],
        "The Divorce Insurance": [_res("5821353542019334456", "The Divorce Insurance", "2025", subject_type=2)],
        "Thunderbolts*": [_res("6127914234610600632", "Thunderbolts*", "2025")],
        "How to Train Your Dragon": [_res("5433466751935826544", "How to Train Your Dragon [Indonesian]", "2025")],
        "Wicked: For Good": [_res("5847851643399816272", "Wicked: For Good", "2025")],
        # the log bug — exact title absent, only same-year different films
        "Tunggu Aku Sukses Nanti": [
            _res("6155414219254125176", "Senin Harga Naik", "2026"),
            _res("9040054622295058032", "Me Before Me", "2026"),
            _res("8313012068559605176", "Check Out Sekarang, Pay Later (Caper)", "2026"),
        ],
    }
    return by_title.get(title, [])


def _detail_payload(client, subject_id: str | None = None, detail_path: str | None = None) -> dict:
    """Realistic fetch_detail response keyed by subject_id."""
    # {subject_id: (detail_title, year, subject_type)} — mirrors real probe data
    by_id = {
        "955166268645066984": ("1 Kakak 7 Ponakan", "2025", 1),
        "6294269002252796048": ("Andai Ibu Tidak Menikah Dengan Ayah", "2025", 1),
        "7253520119266525728": ("Ketindihan", "2025", 1),
        "1801610857218983248": ("Harbin", "2025", 1),
        "4132652923279081800": ("Bogota: City of the Lost", "2025", 1),
        "5821353542019334456": ("The Divorce Insurance", "2025", 2),
        "6127914234610600632": ("Thunderbolts*", "2025", 1),
        "5433466751935826544": ("How to Train Your Dragon [Indonesian]", "2025", 1),
        "5847851643399816272": ("Wicked: For Good", "2025", 1),
        # the wrong film that used to be served for Tunggu Aku Sukses Nanti
        "6155414219254125176": ("Senin Harga Naik", "2026", 1),
    }
    d_title, d_year, d_type = by_id.get(subject_id, ("resolved", "2025", 1))
    return {
        "subject": {
            "subjectId": subject_id,
            "title": d_title,
            "releaseDate": f"{d_year}-01-01",
            "subjectType": d_type,
        }
    }


@pytest.mark.anyio
async def test_fetch_sources_resolves_corpus_via_mock():
    """fetch_sources must resolve each 2025 corpus film to the same film, and
    report titleMatched=True."""
    with (
        patch("videasy.features.moviebox.service.search_titles", new=AsyncMock(side_effect=_search_payload)),
        patch("videasy.features.moviebox.service.fetch_detail", new=AsyncMock(side_effect=_detail_payload)),
        patch("videasy.features.moviebox.service.fetch_play_streams", new=AsyncMock(return_value=[])),
        patch("videasy.features.moviebox.service.fetch_moviebox_captions", new=AsyncMock(return_value=[])),
        patch("videasy.features.moviebox.service.resolve_moviebox_imdb_id", new=AsyncMock(return_value="")),
    ):
        client = AsyncMock()
        for imdb_id, title, mtype, year in CORPUS_2025:
            result = await fetch_sources(
                client, imdb_id,
                original_title=title, media_type=mtype, year=year, try_play=False,
            )
            assert result["title"], f"no title resolved for {title!r}"
            assert result["titleMatched"] is True, (
                f"{title!r} resolved to {result['title']!r} but titleMatched was False"
            )
            assert result["requestedTitle"] == title


@pytest.mark.anyio
async def test_fetch_sources_reports_mismatch_without_raising():
    """The log bug scenario: requesting a film whose exact title is absent must
    NOT silently serve a different film with 200 — it reports titleMatched=False
    so the client can judge (translation vs different film)."""
    with (
        patch("videasy.features.moviebox.service.search_titles", new=AsyncMock(side_effect=_search_payload)),
        patch("videasy.features.moviebox.service.fetch_detail", new=AsyncMock(side_effect=_detail_payload)),
        patch("videasy.features.moviebox.service.fetch_play_streams", new=AsyncMock(return_value=[])),
        patch("videasy.features.moviebox.service.fetch_moviebox_captions", new=AsyncMock(return_value=[])),
        patch("videasy.features.moviebox.service.resolve_moviebox_imdb_id", new=AsyncMock(return_value="")),
    ):
        client = AsyncMock()
        result = await fetch_sources(
            client, "tt39292550",
            original_title="Tunggu Aku Sukses Nanti",
            media_type="movie", year="2026", try_play=False,
        )
        # The subject resolves (Senin Harga Naik exists) but the title does not match.
        assert result["titleMatched"] is False
        assert result["requestedTitle"] == "Tunggu Aku Sukses Nanti"
        assert result["title"] == "Senin Harga Naik"
        # A mismatched film must not stream anything.
        assert result["sources"] == []


@pytest.mark.anyio
async def test_fetch_sources_year_mismatch_forces_empty_sources():
    """Exact year mismatch must force empty sources (never stream a different year)."""
    with (
        patch("videasy.features.moviebox.service.search_titles", new=AsyncMock(side_effect=_search_payload)),
        patch("videasy.features.moviebox.service.fetch_detail", new=AsyncMock(side_effect=_detail_payload)),
        patch("videasy.features.moviebox.service.fetch_play_streams", new=AsyncMock(return_value=[])),
        patch("videasy.features.moviebox.service.fetch_moviebox_captions", new=AsyncMock(return_value=[])),
        patch("videasy.features.moviebox.service.resolve_moviebox_imdb_id", new=AsyncMock(return_value="")),
    ):
        client = AsyncMock()
        # "1 Kakak 7 Ponakan" exists (2025) but the caller says 2024 → year mismatch.
        result = await fetch_sources(
            client, "tt32881480",
            original_title="1 Kakak 7 Ponakan",
            media_type="movie", year="2024", try_play=False,
        )
        assert result["titleMatched"] is True  # title still matches
        assert result["yearMismatch"] is True
        assert result["sources"] == []


@pytest.mark.anyio
async def test_fetch_sources_english_title_or_match():
    """When the Moviebox detail is stored under the English (translated) title,
    the OR match against english_title must still pass — not only the original."""
    # Search for the original "Ketindihan" but the resolved detail is stored
    # under its English name "Sleep Paralysis".
    def english_search(client, title):
        return [_res("7253520119266525728", "Sleep Paralysis", "2025")]

    def english_detail(client, subject_id=None, detail_path=None):
        return {
            "subject": {
                "subjectId": "7253520119266525728",
                "title": "Sleep Paralysis",
                "releaseDate": "2025-01-01",
                "subjectType": 1,
            }
        }

    with (
        patch("videasy.features.moviebox.service.search_titles", new=AsyncMock(side_effect=english_search)),
        patch("videasy.features.moviebox.service.fetch_detail", new=AsyncMock(side_effect=english_detail)),
        patch("videasy.features.moviebox.service.fetch_play_streams", new=AsyncMock(return_value=[])),
        patch("videasy.features.moviebox.service.fetch_moviebox_captions", new=AsyncMock(return_value=[])),
        patch("videasy.features.moviebox.service.resolve_moviebox_imdb_id", new=AsyncMock(return_value="")),
    ):
        client = AsyncMock()
        result = await fetch_sources(
            client, "tt34883346",
            original_title="Ketindihan",
            english_title="Sleep Paralysis",
            media_type="movie", year="2025", try_play=False,
        )
        assert result["titleMatched"] is True, (
            f"expected english_title match, got titleMatched={result['titleMatched']} "
            f"for detail {result['title']!r}"
        )
