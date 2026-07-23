from __future__ import annotations

import pytest


@pytest.mark.anyio
async def test_proxy_mpd(client):
    mpd_url = "https://sbcdnw2.hakunaymatata.com/dash/7850278583678682192_2_1_1080_h265_361/index_web.mpd"
    token = 'Edge-Cache-Cookie=urlprefix=aHR0cHM6Ly9zYmNkbncyLmhha3VuYXltYXRhdGEuY29tL2Rhc2gvNzg1MDI3ODU4MzY3ODY4MjE5Ml8yXzFfMTA4MF9oMjY1XzM2MS8:sign=b453dc1724c38d917a809f22bf0b0a9e:t=1784788312'
    import json
    headers_str = json.dumps({"X-MB-Token": token})

    r = await client.get("/proxy", params={"url": mpd_url, "headers": headers_str})
    assert r.status_code == 200
    assert "dash+xml" in r.headers.get("content-type", "")
    assert "/dash/" in r.text  # URLs should be rewritten to /dash/{token}/...


@pytest.mark.anyio
async def test_proxy_dash_segment(client):
    """Test that /dash/{token}/{segment} middleware works."""
    mpd_url = "https://sbcdnw2.hakunaymatata.com/dash/7850278583678682192_2_1_1080_h265_361/index_web.mpd"
    token = 'Edge-Cache-Cookie=urlprefix=aHR0cHM6Ly9zYmNkbncyLmhha3VuYXltYXRhdGEuY29tL2Rhc2gvNzg1MDI3ODU4MzY3ODY4MjE5Ml8yXzFfMTA4MF9oMjY1XzM2MS8:sign=b453dc1724c38d917a809f22bf0b0a9e:t=1784788312'
    import json
    headers_str = json.dumps({"X-MB-Token": token})

    # First fetch MPD to get the token
    r = await client.get("/proxy", params={"url": mpd_url, "headers": headers_str})
    assert r.status_code == 200

    # Extract dash token from rewritten URLs
    dash_token = None
    for line in r.text.split("\n"):
        if "/dash/" in line:
            start = line.index("/dash/") + 6
            rest = line[start:]
            end = rest.index("/") if "/" in rest else len(rest)
            dash_token = rest[:end]
            break

    assert dash_token is not None, "No /dash/ token found in rewritten MPD"

    # Fetch init segment via middleware
    r2 = await client.get(f"/dash/{dash_token}/init-stream3.m4s")
    assert r2.status_code == 200
    assert len(r2.content) > 0
