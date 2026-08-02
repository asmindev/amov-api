from __future__ import annotations

from unittest.mock import patch

import pytest


@pytest.mark.anyio
async def test_moviebox_sources_route(client):
    with patch("videasy.features.moviebox.service.fetch_sources") as mock_fetch:
        mock_fetch.return_value = {
            "subjectId": "7850278583678682192",
            "title": "Avatar: The Last Airbender S1-S2",
            "year": "2024",
            "imdbId": "tt9018736",
            "subjectType": 2,
            "cover": "https://pbcdnw.aoneroom.com/cover.jpg",
            "sources": [
                {"quality": "1080p (619MB)", "url": "https://bcdnw.hakunaymatata.com/test.mp4", "type": "mp4", "source": "play"}
            ],
            "subtitles": [
                {"lang": "id", "language": "Indonesian", "url": "https://cacdn.hakunaymatata.com/sub.srt"}
            ],
        }

        r = await client.get("/moviebox/sources?subjectId=7850278583678682192&seasonId=1&episodeId=1")
        assert r.status_code == 200
        data = r.json()
        assert data["meta"]["title"] == "Avatar: The Last Airbender S1-S2"
        assert data["meta"]["provider"] == "Moviebox"
        assert data["meta"]["mediaType"] == "tv"
        assert data["meta"]["imdbId"] == "tt9018736"
        assert data["meta"]["year"] == "2024"
        assert data["episode"]["season"] == 1
        assert data["episode"]["episode"] == 1
        assert len(data["sources"]) == 1
        assert data["sources"][0]["quality"] == "1080p"
        assert data["sources"][0]["size"] == "619MB"
        assert len(data["subtitles"]) == 1
        assert data["subtitles"][0]["language"] == "Indonesian"


@pytest.mark.anyio
async def test_moviebox_sources_route_by_imdb_id(client):
    with patch("videasy.features.moviebox.service.fetch_sources") as mock_fetch:
        mock_fetch.return_value = {
            "subjectId": "1061509861831229840",
            "title": "Avatar: The Last Airbender [Indonesian] S1-S2",
            "year": "2024",
            "imdbId": "tt9018736",
            "subjectType": 2,
            "cover": "https://pbcdnw.aoneroom.com/cover.jpg",
            "sources": [
                {"quality": "1080p (619MB)", "url": "https://bcdnw.hakunaymatata.com/test.mp4", "type": "mp4", "source": "play"}
            ],
            "subtitles": [],
        }

        r = await client.get("/moviebox/sources?imdbId=tt9018736&originalTitle=Avatar%3A+The+Last+Airbender&mediaType=tv&year=2024&seasonId=1&episodeId=1")
        assert r.status_code == 200
        data = r.json()
        assert data["meta"]["imdbId"] == "tt9018736"
        assert data["meta"]["provider"] == "Moviebox"
        mock_fetch.assert_called_once()
        assert mock_fetch.call_args[0][1] == "tt9018736"


@pytest.mark.anyio
async def test_moviebox_sources_route_reports_title_mismatch(client):
    """When the resolved Moviebox title differs from the requested title, the
    response reports it via requestedTitle/titleMatched instead of blocking."""
    with patch("videasy.features.moviebox.service.fetch_sources") as mock_fetch:
        mock_fetch.return_value = {
            "subjectId": "6155414219254125176",
            "title": "Senin Harga Naik",
            "year": "2026",
            "imdbId": "tt39292550",
            "subjectType": 1,
            "cover": "https://pbcdnw.aoneroom.com/cover.jpg",
            "sources": [
                {"quality": "1080p (619MB)", "url": "https://bcdnw.hakunaymatata.com/test.mp4", "type": "mp4", "source": "play"}
            ],
            "subtitles": [],
            "requestedTitle": "Tunggu Aku Sukses Nanti",
            "titleMatched": False,
        }

        r = await client.get("/moviebox/sources?imdbId=tt39292550&originalTitle=Tunggu+Aku+Sukses+Nanti&mediaType=movie&year=2026")
        assert r.status_code == 200
        data = r.json()
        assert data["meta"]["title"] == "Senin Harga Naik"
        assert data["meta"]["requestedTitle"] == "Tunggu Aku Sukses Nanti"
        assert data["meta"]["titleMatched"] is False


def test_extract_token_from_response():
    import httpx
    from videasy.features.moviebox.service import _extract_token_from_response

    # Test extraction from Set-Cookie header
    resp1 = httpx.Response(
        200,
        headers=[("set-cookie", "token=eyJhbGci...; Path=/")],
        request=httpx.Request("GET", "https://h5-api.aoneroom.com"),
    )
    assert _extract_token_from_response(resp1) == "eyJhbGci..."

    # Test handling when multiple token cookies are in response headers
    resp2 = httpx.Response(
        200,
        headers=[
            ("set-cookie", "token=tok1; Domain=.aoneroom.com; Path=/"),
            ("set-cookie", "token=tok2; Domain=h5-api.aoneroom.com; Path=/bff"),
        ],
        request=httpx.Request("GET", "https://h5-api.aoneroom.com"),
    )
    assert _extract_token_from_response(resp2) == "tok1"

