"""Integration tests for Discogs API endpoints."""

import os

import pytest

from discogs.models import DiscogsSearchRequest
from discogs.service import DiscogsService

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


class TestDiscogsApiSearch:
    """Tests that hit the real Discogs API (no cache) to verify search results."""

    @pytest.mark.asyncio
    async def test_high_rise_disallow_returns_correct_releases(self):
        """Discogs API should return 'Disallow' releases for High Rise, not unrelated albums.

        This is the regression test for the cache OR-vs-AND bug: the cache returned
        'Psychedelic Speed Freaks' (matching artist only), shadowing the correct results.
        """
        token = os.environ.get("DISCOGS_TOKEN")
        if not token:
            pytest.skip("DISCOGS_TOKEN not set")

        service = DiscogsService(token=token, cache_service=None)
        try:
            response = await service.search(
                DiscogsSearchRequest(artist="High Rise", album="Disallow")
            )
        finally:
            await service.close()

        assert response.results, "Expected at least one result from Discogs API"
        top_result = response.results[0]
        assert "disallow" in top_result.album.lower(), (
            f"Top result should be 'Disallow', got '{top_result.album}'"
        )
