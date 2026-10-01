"""Integration test for the artist-fallback album-match floor (LML#400, LML#1391).

End-to-end exercise of ``perform_lookup`` against a real (in-memory)
``LibraryDB`` with the seeded Nina Simone fixture (top-2 prod offender from
the #400 evidence table — `/release/1402136`, 150 distinct DJ-typed albums
across 419 contaminated rows).

When a DJ types a Nina Simone album that the WXYC library doesn't physically
carry (e.g. "Wild Is the Wind") plus a song that Nina Simone DOES have on a
DIFFERENT album in the library ("Sinnerman" → "Pastel Blues"), the
artist-fallback cascade used to surface the wrong-album library row, and
``enrich_artwork_results`` then attached Pastel Blues' Discogs release year /
Apple Music URL / Spotify URL / Discogs URL / artwork URL onto a flowsheet
row tagged with album "Wild Is the Wind". LML#400's fix dropped the candidate
before enrichment ran — which meant a typed album matching nothing emptied
the artist's whole shelf (LML#1391). Since then, LML#477/#487 moved the
metadata guard to enrichment's serve gate, so the search layer (LML#1391) now
ranks instead of drops: Pastel Blues surfaces (it's Nina Simone's only
shelved row), but carries no Discogs-derived metadata.

Companion to the unit tests in ``tests/unit/test_album_match_floor.py``.
"""

from unittest.mock import AsyncMock

import pytest

from discogs.models import DiscogsSearchResponse
from lookup.models import LookupRequest
from lookup.orchestrator import perform_lookup
from tests.conftest import make_lml_telemetry


class TestAlbumMatchFloorIntegration:
    """Pin the #400 prod contamination shape against a real LibraryDB."""

    @pytest.mark.asyncio
    async def test_nina_simone_wild_is_the_wind_surfaces_pastel_blues_without_metadata(
        self, library_db
    ):
        """LML#1391: artist matches the library but the typed album doesn't —
        the artist-fallback cascade must not empty the shelf. Library seed has
        Nina Simone / Pastel Blues only; request types album "Wild Is the
        Wind" + song "Sinnerman". Pastel Blues still surfaces (it's the only
        Nina Simone row on the shelf), but with no Discogs-derived metadata:
        the #400 contamination guard is enforced at enrichment now, not by
        emptying the search result.
        """
        from wxyc_fastapi.observability import init_cache_stats

        init_cache_stats()

        # Discogs returns nothing — exercise pure library-side cascade. The
        # floor must fire on the LML side regardless of whether Discogs
        # would have rescued the lookup with the right album.
        mock_service = AsyncMock(
            spec_set=[
                "search",
                "search_releases_by_track",
                "validate_track_on_release",
                "get_release",
                "cache_service",
            ]
        )
        mock_service.cache_service = None
        mock_service.search = AsyncMock(return_value=DiscogsSearchResponse(results=[]))
        mock_service.get_release = AsyncMock(return_value=None)
        mock_service.validate_track_on_release = AsyncMock(return_value=False)

        request = LookupRequest(
            artist="Nina Simone",
            album="Wild Is the Wind",
            song="Sinnerman",
            raw_message="Sinnerman by Nina Simone — Wild Is the Wind",
        )
        response = await perform_lookup(
            request,
            library_db,
            mock_service,
            make_lml_telemetry(),
        )

        # The shelf survives (LML#1391) -- Pastel Blues is Nina Simone's only
        # library row, so it comes back rather than an empty response.
        assert response.song_not_found is True
        result_albums = [r.library_item.title for r in response.results]
        assert "Pastel Blues" in result_albums
        pastel = next(r for r in response.results if r.library_item.title == "Pastel Blues")
        # No album-derived metadata leaks onto the mismatched-album request
        # (the #400 guard, now enforced at the enrichment serve gate).
        assert pastel.artwork is None or pastel.artwork.release_id == 0

    @pytest.mark.asyncio
    async def test_nina_simone_pastel_blues_typed_correctly_still_surfaces(self, library_db):
        """Companion green case: when the DJ types Pastel Blues, the library
        row surfaces normally. The floor doesn't gate correctly-typed
        requests.
        """
        from wxyc_fastapi.observability import init_cache_stats

        init_cache_stats()

        mock_service = AsyncMock(
            spec_set=[
                "search",
                "search_releases_by_track",
                "validate_track_on_release",
                "get_release",
                "cache_service",
            ]
        )
        mock_service.cache_service = None
        mock_service.search = AsyncMock(return_value=DiscogsSearchResponse(results=[]))
        mock_service.get_release = AsyncMock(return_value=None)
        mock_service.validate_track_on_release = AsyncMock(return_value=False)

        request = LookupRequest(
            artist="Nina Simone",
            album="Pastel Blues",
            song="Sinnerman",
            raw_message="Sinnerman by Nina Simone — Pastel Blues",
        )
        response = await perform_lookup(
            request,
            library_db,
            mock_service,
            make_lml_telemetry(),
        )

        result_albums = [r.library_item.title for r in response.results]
        assert "Pastel Blues" in result_albums, (
            f"Expected Pastel Blues in results when typed correctly, got {result_albums}"
        )
