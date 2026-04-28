"""Integration tests for Discogs API endpoints."""

import os

import pytest

from discogs.models import DiscogsSearchRequest
from discogs.service import DiscogsService


class TestDiscogsEndpoints:
    @pytest.mark.asyncio
    async def test_track_releases_503_without_service(self, app_client):
        resp = await app_client.get("/api/v1/discogs/track-releases", params={"track": "Song"})
        assert resp.status_code == 503

    @pytest.mark.asyncio
    async def test_release_503_without_service(self, app_client):
        resp = await app_client.get("/api/v1/discogs/release/123")
        assert resp.status_code == 503

    # /api/v1/discogs/search was removed in commit c5f1e65 (PR #157).

    @pytest.mark.asyncio
    async def test_artist_503_without_service(self, app_client):
        resp = await app_client.get("/api/v1/discogs/artist/45")
        assert resp.status_code == 503

    @pytest.mark.asyncio
    async def test_entity_503_without_service(self, app_client):
        resp = await app_client.get("/api/v1/discogs/entity/artist/45")
        assert resp.status_code == 503


@pytest.mark.external_api
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


@pytest.mark.external_api
class TestEntityResolution:
    """Integration tests for entity resolution using real Discogs API."""

    @pytest.mark.asyncio
    async def test_resolve_artist(self):
        """Resolve a known artist (Stereolab, ID 388) and verify name.

        IDs 2206 / 64977 / 17854 used to point at Stereolab + Dots And Loops
        but Discogs reorganized the data behind those IDs around 2026-04-28.
        Re-pinned to the canonical IDs verified via direct API probe and
        cross-checked against LML's `entity.identity.discogs_artist_id` for
        Stereolab.
        """
        token = os.environ.get("DISCOGS_TOKEN")
        if not token:
            pytest.skip("DISCOGS_TOKEN not set")

        service = DiscogsService(token=token, cache_service=None)
        try:
            result = await service.get_artist_details(388)
        finally:
            await service.close()

        assert result is not None
        assert result.name == "Stereolab"
        assert result.artist_id == 388

    @pytest.mark.asyncio
    async def test_resolve_release(self):
        """Resolve a known release (Dots And Loops, vinyl LP, ID 90416) and verify title."""
        token = os.environ.get("DISCOGS_TOKEN")
        if not token:
            pytest.skip("DISCOGS_TOKEN not set")

        service = DiscogsService(token=token, cache_service=None)
        try:
            result = await service.get_release(90416)
        finally:
            await service.close()

        assert result is not None
        assert result.title == "Dots And Loops"
        assert result.release_id == 90416

    @pytest.mark.asyncio
    async def test_resolve_master(self):
        """Resolve a known master release (Dots And Loops, master ID 30682) and verify title."""
        token = os.environ.get("DISCOGS_TOKEN")
        if not token:
            pytest.skip("DISCOGS_TOKEN not set")

        service = DiscogsService(token=token, cache_service=None)
        try:
            result = await service.get_master(30682)
        finally:
            await service.close()

        assert result is not None
        assert result.title == "Dots And Loops"
        assert result.master_id == 30682

    @pytest.mark.asyncio
    async def test_artist_not_found(self):
        """Non-existent artist ID returns None."""
        token = os.environ.get("DISCOGS_TOKEN")
        if not token:
            pytest.skip("DISCOGS_TOKEN not set")

        service = DiscogsService(token=token, cache_service=None)
        try:
            result = await service.get_artist_details(999999999)
        finally:
            await service.close()

        assert result is None

    @pytest.mark.asyncio
    async def test_release_not_found(self):
        """Non-existent release ID returns None."""
        token = os.environ.get("DISCOGS_TOKEN")
        if not token:
            pytest.skip("DISCOGS_TOKEN not set")

        service = DiscogsService(token=token, cache_service=None)
        try:
            result = await service.get_release(999999999)
        finally:
            await service.close()

        assert result is None

    @pytest.mark.asyncio
    async def test_master_not_found(self):
        """Non-existent master ID returns None."""
        token = os.environ.get("DISCOGS_TOKEN")
        if not token:
            pytest.skip("DISCOGS_TOKEN not set")

        service = DiscogsService(token=token, cache_service=None)
        try:
            result = await service.get_master(999999999)
        finally:
            await service.close()

        assert result is None
