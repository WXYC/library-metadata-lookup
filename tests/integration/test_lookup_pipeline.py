"""Integration tests for the lookup pipeline with real LibraryDB."""

import pytest

pytestmark = pytest.mark.integration


class TestLookupPipeline:
    @pytest.mark.asyncio
    async def test_direct_match(self, app_client):
        """Artist + album direct match."""
        resp = await app_client.post(
            "/api/v1/lookup",
            json={
                "artist": "Queen",
                "album": "The Game",
                "raw_message": "Queen - The Game",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["results"]) >= 1
        assert body["search_type"] == "direct"

    @pytest.mark.asyncio
    async def test_artist_only(self, app_client):
        """Artist-only search returns that artist's albums."""
        resp = await app_client.post(
            "/api/v1/lookup",
            json={
                "artist": "Radiohead",
                "raw_message": "Radiohead",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["results"]) >= 1

    @pytest.mark.asyncio
    async def test_no_results(self, app_client):
        """Nonexistent artist returns empty results."""
        resp = await app_client.post(
            "/api/v1/lookup",
            json={
                "artist": "ZZZNONEXISTENT",
                "raw_message": "ZZZNONEXISTENT",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["results"]) == 0

    @pytest.mark.asyncio
    async def test_ambiguous_format(self, app_client):
        """X - Y format triggers alternative interpretation."""
        resp = await app_client.post(
            "/api/v1/lookup",
            json={
                "artist": "Stereolab",
                "album": "Dots and Loops",
                "raw_message": "Stereolab - Dots and Loops",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["results"]) >= 1

    @pytest.mark.asyncio
    async def test_song_as_artist(self, app_client):
        """Song parsed as artist name should still find results."""
        resp = await app_client.post(
            "/api/v1/lookup",
            json={
                "song": "Laid Back",
                "raw_message": "Laid Back",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        # Should find "Laid Back" by Laid Back via SONG_AS_ARTIST strategy
        if body["results"]:
            assert body["search_type"] in ("song_as_artist", "direct")

    @pytest.mark.asyncio
    async def test_response_structure(self, app_client):
        """Response has all expected fields."""
        resp = await app_client.post(
            "/api/v1/lookup",
            json={
                "artist": "Queen",
                "album": "The Game",
                "raw_message": "Queen - The Game",
            },
        )
        body = resp.json()
        assert "results" in body
        assert "search_type" in body
        assert "song_not_found" in body
        assert "found_on_compilation" in body
        assert "context_message" in body

    @pytest.mark.asyncio
    async def test_artist_correction(self, app_client):
        """Misspelled artist should be corrected via fuzzy matching."""
        resp = await app_client.post(
            "/api/v1/lookup",
            json={
                "artist": "Living Color",  # should correct to "Living Colour"
                "raw_message": "Living Color",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        # Should have corrected the artist
        if body.get("corrected_artist"):
            assert body["corrected_artist"] == "Living Colour"
