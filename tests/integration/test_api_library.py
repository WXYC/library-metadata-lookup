"""Integration tests for library API endpoints with real SQLite."""

import pytest

pytestmark = pytest.mark.integration


class TestLibrarySearchEndpoint:
    @pytest.mark.asyncio
    async def test_query_search(self, app_client):
        resp = await app_client.get("/api/v1/library/search", params={"q": "Stereolab"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] >= 1
        assert any("Stereolab" in r["artist"] for r in body["results"])

    @pytest.mark.asyncio
    async def test_artist_filter(self, app_client):
        resp = await app_client.get("/api/v1/library/search", params={"artist": "Jessica Pratt"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] >= 1

    @pytest.mark.asyncio
    async def test_title_filter(self, app_client):
        resp = await app_client.get(
            "/api/v1/library/search", params={"title": "On Your Own Love Again"}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] >= 1

    @pytest.mark.asyncio
    async def test_no_params_returns_400(self, app_client):
        resp = await app_client.get("/api/v1/library/search")
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_empty_results(self, app_client):
        resp = await app_client.get("/api/v1/library/search", params={"q": "ZZZNONEXISTENT"})
        assert resp.status_code == 200
        assert resp.json()["total"] == 0

    @pytest.mark.asyncio
    async def test_multiword_fts_query(self, app_client):
        resp = await app_client.get(
            "/api/v1/library/search", params={"q": "Stereolab Aluminum Tunes"}
        )
        assert resp.status_code == 200
        body = resp.json()
        if body["total"] > 0:
            assert any("Aluminum" in r["title"] for r in body["results"])

    @pytest.mark.asyncio
    async def test_combined_artist_and_title(self, app_client):
        resp = await app_client.get(
            "/api/v1/library/search",
            params={"artist": "Stereolab", "title": "Dots"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] >= 1
