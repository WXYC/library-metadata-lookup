"""Integration test for LML#401 — streaming URLs survive a Discogs miss.

When a flowsheet entry's release isn't in the WXYC/Discogs catalog
(``DiscogsService.search`` returns an empty result), the artwork block
used to short-circuit at ``None`` inside ``enrich_artwork_results``.
Backend-Service (WXYC/Backend-Service#1184) then had no Apple/Spotify
URLs to project onto ``result.album``, so iOS rendered all-greyed
streaming buttons for releases that demonstrably ARE on Apple Music
(e.g. the "Tragic Magic" by Julianna Barwick & Mary Lattimore case
that motivated this fix).

The fix synthesizes a streaming-only ``DiscogsSearchResult`` with
``release_id=0`` and ``release_url=""`` as sentinels — these tell
Backend-Service (BS#1185) "no Discogs match, but streaming URLs are
valid; do NOT call ``extractAlbumMetadata``".
"""

from unittest.mock import AsyncMock

import pytest

from clients.streaming.apple_music import AppleMusicClient, AppleMusicTrackMatch
from discogs.models import DiscogsSearchResponse
from lookup.models import LookupRequest
from lookup.orchestrator import perform_lookup
from tests.conftest import make_lml_telemetry


class TestStreamingOnDiscogsMiss:
    @pytest.mark.asyncio
    async def test_apple_music_url_populated_when_discogs_returns_no_artwork(self, library_db):
        """End-to-end: artist+album+song hit the library, Discogs returns
        empty (no artwork), Apple Music matches → top-1 result carries
        the Apple Music URL via the synthesized streaming-only artwork.

        Uses the seeded Stereolab entries (per CLAUDE.md fixture preference)
        — the test forces the no-artwork branch by mocking
        ``DiscogsService.search`` to return empty results regardless of
        input. This mirrors the production failure mode where a release
        exists in the WXYC library catalog (or matches an artist by FTS)
        but the per-album Discogs search yields nothing.
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
        # Empty Discogs search → every library hit gets ``artwork=None``,
        # exercising the LML#401 no-Discogs-match enrichment branch.
        mock_service.search = AsyncMock(return_value=DiscogsSearchResponse(results=[]))
        mock_service.get_release = AsyncMock(return_value=None)
        mock_service.validate_track_on_release = AsyncMock(return_value=False)

        apple_url = "https://music.apple.com/us/album/aluminum-tunes/1234567890"
        apple_music = AsyncMock(spec=AppleMusicClient)
        # LML#487: synthesis path now drives ``find_track_metadata`` so the
        # same Apple Music search response can yield artwork + release_year
        # alongside the URL. Use ``artwork_url=None`` / ``release_year=None``
        # to pin the pre-LML#487 URL-only invariant (this test predates the
        # artwork extraction; LML#487's TestExternalArtworkProbe covers the
        # full-payload shape).
        apple_music.find_track_metadata = AsyncMock(
            return_value=AppleMusicTrackMatch(url=apple_url, artwork_url=None, release_year=None)
        )

        request = LookupRequest(
            artist="Stereolab",
            album="Aluminum Tunes",
            song="Cybele's Reverie",
            raw_message="Stereolab - Aluminum Tunes - Cybele's Reverie",
        )
        response = await perform_lookup(
            request,
            library_db,
            mock_service,
            make_lml_telemetry(),
            apple_music=apple_music,
        )

        assert len(response.results) >= 1, "Expected at least one library result"

        top = response.results[0]
        assert top.artwork is not None, (
            "Synthetic streaming-only artwork should be present on Discogs miss"
        )
        # Sentinel pair that BS#1185 keys off of to skip extractAlbumMetadata.
        assert top.artwork.release_id == 0
        assert top.artwork.release_url == ""
        # The whole point of LML#401: Apple URL surfaces on the no-Discogs-match path.
        assert top.artwork.apple_music_url == apple_url
        # Album-derived fields stay None — positional-gating invariant.
        assert top.artwork.release_year is None
        assert top.artwork.artist_bio is None
        assert top.artwork.wikipedia_url is None
        # Search-URL fallbacks fill (artist + song are non-empty).
        assert top.artwork.spotify_url is not None
        assert top.artwork.youtube_music_url is not None
        assert top.artwork.bandcamp_url is not None
        assert top.artwork.soundcloud_url is not None

    @pytest.mark.asyncio
    async def test_search_urls_fill_when_apple_match_fails(self, library_db):
        """When Apple Music has no clearable match either, the synthesized
        artwork still carries the Spotify/YT/BC/SC search URLs so iOS isn't
        stuck with all-greyed streaming buttons.
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

        apple_music = AsyncMock(spec=AppleMusicClient)
        apple_music.find_track_metadata = AsyncMock(return_value=None)

        request = LookupRequest(
            artist="Stereolab",
            album="Aluminum Tunes",
            song="Cybele's Reverie",
            raw_message="Stereolab - Aluminum Tunes - Cybele's Reverie",
        )
        response = await perform_lookup(
            request,
            library_db,
            mock_service,
            make_lml_telemetry(),
            apple_music=apple_music,
        )

        assert len(response.results) >= 1
        top = response.results[0]
        assert top.artwork is not None
        assert top.artwork.release_id == 0
        assert top.artwork.apple_music_url is None  # Apple Music had no match
        # But search-URL fallbacks still fill — iOS gets at least four buttons.
        assert top.artwork.spotify_url is not None
        assert top.artwork.youtube_music_url is not None
        assert top.artwork.bandcamp_url is not None
        assert top.artwork.soundcloud_url is not None
