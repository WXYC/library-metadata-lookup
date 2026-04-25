"""E2E tests for Discogs HTTP endpoints using the real Discogs API.

Tests are skipped at collection time when neither DISCOGS_TOKEN nor
DISCOGS_API_KEY + DISCOGS_API_SECRET is set (see conftest).
"""

import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.asyncio]


class TestDiscogsSearch:
    async def test_search_returns_results_for_known_artist(self, real_discogs_client):
        response = await real_discogs_client.post(
            "/api/v1/discogs/search",
            json={"artist": "Stereolab", "album": "Dots and Loops"},
        )
        assert response.status_code == 200
        data = response.json()
        assert len(data["results"]) > 0
        assert data["results"][0]["release_id"] is not None
        assert "release_url" in data["results"][0]

    async def test_search_returns_empty_for_nonexistent(self, real_discogs_client):
        response = await real_discogs_client.post(
            "/api/v1/discogs/search",
            json={"artist": "xyznonexistent12345", "album": "nothing"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["results"] == []


class TestDiscogsRelease:
    async def test_release_returns_metadata(self, real_discogs_client):
        response = await real_discogs_client.get("/api/v1/discogs/release/90416")
        assert response.status_code == 200
        data = response.json()
        assert "Dots" in data["title"]  # "Dots And Loops"
        assert data["tracklist"] is not None
        assert len(data["tracklist"]) > 0

    async def test_release_404_for_nonexistent(self, real_discogs_client):
        response = await real_discogs_client.get("/api/v1/discogs/release/999999999")
        assert response.status_code == 404


class TestDiscogsArtist:
    async def test_artist_returns_details(self, real_discogs_client):
        response = await real_discogs_client.get("/api/v1/discogs/artist/388")
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "Stereolab"
        assert data["artist_id"] == 388
        assert data["profile"] is not None and len(data["profile"]) > 0

    async def test_artist_404_for_nonexistent(self, real_discogs_client):
        response = await real_discogs_client.get("/api/v1/discogs/artist/999999999")
        assert response.status_code == 404


class TestDiscogsEntity:
    async def test_entity_resolves_artist(self, real_discogs_client):
        response = await real_discogs_client.get("/api/v1/discogs/entity/artist/388")
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "Stereolab"
        assert data["type"] == "artist"
        assert data["id"] == 388
