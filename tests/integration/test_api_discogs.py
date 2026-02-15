"""Integration tests for Discogs API endpoints."""

import pytest

pytestmark = pytest.mark.integration


class TestDiscogsEndpoints:
    @pytest.mark.asyncio
    async def test_track_releases_503_without_service(self, app_client):
        resp = await app_client.get("/api/v1/discogs/track-releases", params={"track": "Song"})
        assert resp.status_code == 503

    @pytest.mark.asyncio
    async def test_release_503_without_service(self, app_client):
        resp = await app_client.get("/api/v1/discogs/release/123")
        assert resp.status_code == 503

    @pytest.mark.asyncio
    async def test_search_503_without_service(self, app_client):
        resp = await app_client.post("/api/v1/discogs/search", json={"artist": "Queen"})
        assert resp.status_code == 503
