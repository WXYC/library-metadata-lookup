"""Integration test for the artist-fallback album-match floor (LML#400).

End-to-end exercise of ``perform_lookup`` against a real (in-memory)
``LibraryDB`` with the seeded Nina Simone fixture (top-2 prod offender from
the #400 evidence table — `/release/1402136`, 150 distinct DJ-typed albums
across 419 contaminated rows).

When a DJ types a Nina Simone album that the WXYC library doesn't physically
carry (e.g. "Wild Is the Wind") plus a song that Nina Simone DOES have on a
DIFFERENT album in the library ("Sinnerman" → "Pastel Blues"), the
artist-fallback cascade historically surfaced the wrong-album library row,
and ``enrich_artwork_results`` then attached Pastel Blues' Discogs release
year / Apple Music URL / Spotify URL / Discogs URL / artwork URL onto a
flowsheet row tagged with album "Wild Is the Wind". The floor rejects the
candidate before enrichment runs.

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
    async def test_nina_simone_wild_is_the_wind_does_not_surface_pastel_blues(self, library_db):
        """The exact prod-evidence failure mode: artist matches the library but
        the typed album doesn't, and the artist-fallback cascade would
        otherwise contaminate the flowsheet row with a wrong-album Discogs
        release.

        Library seed has Nina Simone / Pastel Blues only. Request types album
        "Wild Is the Wind" + song "Sinnerman". Without the floor:
        artist-fallback surfaces Pastel Blues, enrichment attaches its
        release metadata, BS persists `release_year` / `apple_music_url` /
        `spotify_url` / `discogs_url` onto a row tagged "Wild Is the Wind".

        With the floor: token_set_ratio("Wild Is the Wind", "Pastel Blues")
        is well below 80 (no shared significant tokens), the candidate is
        dropped, and no album-derived enrichment leaks. The response mirrors
        the unknown-artist shape.
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

        # Floor dropped Pastel Blues → no library row surfaces. The response
        # mirrors the unknown-artist shape and BS won't write any
        # album-derived metadata onto the flowsheet row.
        result_albums = [r.library_item.title for r in response.results]
        assert "Pastel Blues" not in result_albums, (
            "Wrong-album fallback leaked: Pastel Blues surfaced for typed album "
            "'Wild Is the Wind'. This is the exact #400 prod contamination shape."
        )

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
