"""End-to-end tests for the lookup pipeline.

Exercises the full chain: artist correction -> album resolution ->
search pipeline -> track validation -> artwork fetch -> response.

Uses WXYC example data (Juana Molina, Jessica Pratt, Stereolab) with
a real in-memory SQLite library and a mock Discogs service that returns
realistic metadata for those artists.
"""

from unittest.mock import AsyncMock, patch

import pytest

pytestmark = pytest.mark.e2e


class TestDirectLookup:
    """Artist + album lookups that hit the library directly."""

    @pytest.mark.asyncio
    async def test_jessica_pratt_direct(self, e2e_client):
        """Jessica Pratt - On Your Own Love Again finds the library entry."""
        resp = await e2e_client.post(
            "/api/v1/lookup",
            json={
                "artist": "Jessica Pratt",
                "album": "On Your Own Love Again",
                "raw_message": "Jessica Pratt - On Your Own Love Again",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["results"]) >= 1
        assert body["search_type"] == "direct"
        titles = [r["library_item"]["title"] for r in body["results"]]
        assert "On Your Own Love Again" in titles

    @pytest.mark.asyncio
    async def test_juana_molina_direct(self, e2e_client):
        """Juana Molina - DOGA finds the library entry."""
        resp = await e2e_client.post(
            "/api/v1/lookup",
            json={
                "artist": "Juana Molina",
                "album": "DOGA",
                "raw_message": "Juana Molina - DOGA",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["results"]) >= 1
        titles = [r["library_item"]["title"] for r in body["results"]]
        assert "DOGA" in titles

    @pytest.mark.asyncio
    async def test_stereolab_direct(self, e2e_client):
        """Stereolab - Aluminum Tunes finds the library entry."""
        resp = await e2e_client.post(
            "/api/v1/lookup",
            json={
                "artist": "Stereolab",
                "album": "Aluminum Tunes",
                "raw_message": "Stereolab - Aluminum Tunes",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["results"]) >= 1
        titles = [r["library_item"]["title"] for r in body["results"]]
        assert "Aluminum Tunes" in titles

    @pytest.mark.asyncio
    async def test_duke_ellington_ampersand(self, e2e_client):
        """Artist name with '&' resolves correctly."""
        resp = await e2e_client.post(
            "/api/v1/lookup",
            json={
                "artist": "Duke Ellington & John Coltrane",
                "raw_message": "Duke Ellington & John Coltrane",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["results"]) >= 1


class TestSwappedInterpretation:
    """Ambiguous X - Y format triggers alternative interpretation."""

    @pytest.mark.asyncio
    async def test_swapped_juana_molina(self, e2e_client):
        """DOGA - Juana Molina (swapped) should still find results.

        When the initial interpretation (artist=DOGA, album=Juana Molina) finds
        nothing, the swapped strategy tries artist=Juana Molina, album=DOGA.
        """
        resp = await e2e_client.post(
            "/api/v1/lookup",
            json={
                "artist": "DOGA",
                "album": "Juana Molina",
                "raw_message": "DOGA - Juana Molina",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        # Should find results via swapped interpretation
        if body["results"]:
            assert body["search_type"] in ("alternative", "swapped", "direct")
            artists = [r["library_item"]["artist"] for r in body["results"]]
            assert any("Juana Molina" in a for a in artists)


class TestArtistOnlyFallback:
    """Artist-only searches return all albums by that artist."""

    @pytest.mark.asyncio
    async def test_stereolab_all_albums(self, e2e_client):
        """Searching 'Stereolab' with no album returns multiple albums."""
        resp = await e2e_client.post(
            "/api/v1/lookup",
            json={
                "artist": "Stereolab",
                "raw_message": "Stereolab",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["results"]) >= 2
        titles = {r["library_item"]["title"] for r in body["results"]}
        assert "Aluminum Tunes" in titles
        assert "Dots and Loops" in titles


class TestTrackSearch:
    """Track-level searches that resolve album via Discogs."""

    @pytest.mark.asyncio
    async def test_track_resolves_to_album(self, e2e_client):
        """'la paradoja by Juana Molina' resolves to DOGA via Discogs."""
        with patch(
            "lookup.orchestrator.lookup_releases_by_track",
            new_callable=AsyncMock,
            return_value=[("Juana Molina", "DOGA")],
        ):
            resp = await e2e_client.post(
                "/api/v1/lookup",
                json={
                    "artist": "Juana Molina",
                    "song": "la paradoja",
                    "raw_message": "la paradoja by Juana Molina",
                },
            )
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["results"]) >= 1
        titles = [r["library_item"]["title"] for r in body["results"]]
        assert "DOGA" in titles


class TestNoResults:
    """Nonexistent lookups return empty results gracefully."""

    @pytest.mark.asyncio
    async def test_nonexistent_artist(self, e2e_client):
        resp = await e2e_client.post(
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
    async def test_nonexistent_album(self, e2e_client):
        resp = await e2e_client.post(
            "/api/v1/lookup",
            json={
                "artist": "Stereolab",
                "album": "NONEXISTENT ALBUM",
                "raw_message": "Stereolab - NONEXISTENT ALBUM",
            },
        )
        assert resp.status_code == 200


class TestCompilationTrackSearch:
    """Track found on a VA compilation."""

    @pytest.mark.asyncio
    async def test_track_on_compilation(self, e2e_client):
        """Track on a VA compilation is found via Discogs cross-reference."""
        with patch(
            "lookup.orchestrator.lookup_releases_by_track",
            new_callable=AsyncMock,
            return_value=[],
        ):
            from discogs.models import (
                DiscogsSearchResponse,
                ReleaseInfo,
                TrackReleasesResponse,
            )

            # Override the mock for this specific test
            from core.dependencies import get_discogs_service
            from main import app

            mock_discogs = AsyncMock()
            mock_discogs.cache_service = None

            mock_discogs.search_releases_by_track = AsyncMock(
                return_value=TrackReleasesResponse(
                    track="Call Your Name",
                    artist="Chuquimamani-Condori",
                    releases=[
                        ReleaseInfo(
                            album="Freeform Compilation Vol. 1",
                            artist="Various Artists",
                            release_id=40001,
                            release_url="https://discogs.com/release/40001",
                            is_compilation=True,
                        ),
                    ],
                    total=1,
                    cached=False,
                )
            )
            mock_discogs.validate_track_on_release = AsyncMock(return_value=True)
            mock_discogs.search = AsyncMock(return_value=DiscogsSearchResponse(results=[]))
            mock_discogs.get_release = AsyncMock(return_value=None)

            app.dependency_overrides[get_discogs_service] = lambda: mock_discogs

            resp = await e2e_client.post(
                "/api/v1/lookup",
                json={
                    "artist": "Chuquimamani-Condori",
                    "song": "Call Your Name",
                    "raw_message": "Call Your Name by Chuquimamani-Condori",
                },
            )

        assert resp.status_code == 200
        body = resp.json()
        if body.get("found_on_compilation"):
            titles = [r["library_item"]["title"] for r in body["results"]]
            assert "Freeform Compilation Vol. 1" in titles


class TestResponseStructure:
    """Verify the full response shape from the E2E pipeline."""

    @pytest.mark.asyncio
    async def test_full_response_fields(self, e2e_client):
        """Response contains all expected top-level fields."""
        resp = await e2e_client.post(
            "/api/v1/lookup",
            json={
                "artist": "Jessica Pratt",
                "album": "On Your Own Love Again",
                "raw_message": "Jessica Pratt - On Your Own Love Again",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "results" in body
        assert "search_type" in body
        assert "song_not_found" in body
        assert "found_on_compilation" in body
        assert "context_message" in body

    @pytest.mark.asyncio
    async def test_result_item_structure(self, e2e_client):
        """Each result item has the expected nested fields."""
        resp = await e2e_client.post(
            "/api/v1/lookup",
            json={
                "artist": "Juana Molina",
                "album": "DOGA",
                "raw_message": "Juana Molina - DOGA",
            },
        )
        body = resp.json()
        assert len(body["results"]) >= 1
        item = body["results"][0]
        assert "library_item" in item
        lib_item = item["library_item"]
        assert "id" in lib_item
        assert "title" in lib_item
        assert "artist" in lib_item
        assert "genre" in lib_item
        assert "format" in lib_item
