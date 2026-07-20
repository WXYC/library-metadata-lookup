"""Unit tests for discogs/lookup.py."""

import asyncio
import time
from unittest.mock import AsyncMock, patch

import pytest

from discogs.lookup import lookup_releases_by_artist, lookup_releases_by_track
from discogs.models import (
    DiscogsSearchResponse,
    ReleaseInfo,
    TrackReleasesResponse,
)
from lookup.matching import MAX_SEARCH_RESULTS
from tests.factories import make_discogs_result, make_library_item

# ---------------------------------------------------------------------------
# lookup_releases_by_track
# ---------------------------------------------------------------------------


class TestLookupReleasesByTrack:
    @pytest.mark.asyncio
    async def test_returns_validated_releases(self):
        service = AsyncMock()
        service.search_releases_by_track = AsyncMock(
            return_value=TrackReleasesResponse(
                track="Bohemian Rhapsody",
                artist="Queen",
                releases=[
                    ReleaseInfo(
                        album="A Night at the Opera",
                        artist="Queen",
                        release_id=12345,
                        release_url="https://discogs.com/release/12345",
                    )
                ],
                total=1,
            )
        )
        service.validate_track_on_release = AsyncMock(return_value=True)

        result = await lookup_releases_by_track("Bohemian Rhapsody", "Queen", service=service)
        assert len(result) == 1
        assert result[0] == ("Queen", "A Night at the Opera")

    @pytest.mark.asyncio
    async def test_skips_invalid_releases(self):
        service = AsyncMock()
        service.search_releases_by_track = AsyncMock(
            return_value=TrackReleasesResponse(
                track="Song",
                artist="Artist",
                releases=[
                    ReleaseInfo(
                        album="Album1",
                        artist="Artist",
                        release_id=111,
                        release_url="https://discogs.com/release/111",
                    ),
                    ReleaseInfo(
                        album="Album2",
                        artist="Artist",
                        release_id=222,
                        release_url="https://discogs.com/release/222",
                    ),
                ],
                total=2,
            )
        )
        service.validate_track_on_release = AsyncMock(side_effect=[False, True])

        result = await lookup_releases_by_track("Song", "Artist", service=service)
        assert len(result) == 1
        assert result[0][1] == "Album2"

    @pytest.mark.asyncio
    async def test_no_service_returns_empty(self):
        with patch("discogs.lookup._get_service", return_value=None):
            result = await lookup_releases_by_track("Song", "Artist")
        assert result == []

    @pytest.mark.asyncio
    async def test_fallback_service_no_token_returns_empty(self):
        with patch("discogs.lookup._get_service", return_value=None):
            result = await lookup_releases_by_track("Song")
        assert result == []

    @pytest.mark.asyncio
    async def test_no_artist_skips_validation(self):
        """Without artist, releases are returned without track validation."""
        service = AsyncMock()
        service.search_releases_by_track = AsyncMock(
            return_value=TrackReleasesResponse(
                track="Song",
                releases=[
                    ReleaseInfo(
                        album="Album",
                        artist="SomeArtist",
                        release_id=999,
                        release_url="https://discogs.com/release/999",
                    )
                ],
                total=1,
            )
        )

        result = await lookup_releases_by_track("Song", artist=None, service=service)
        assert len(result) == 1
        service.validate_track_on_release.assert_not_called()

    @pytest.mark.asyncio
    async def test_validations_run_concurrently_not_serially(self):
        """Part A (LML#866): per-release validation fans out via _chunked_gather.

        Ten candidate releases each take 0.05s to validate; a serial loop would
        cost ~0.5s. With bounded-concurrency + early-exit, the first chunk-wave
        (MAX_SEARCH_RESULTS releases) fills the accumulator and the rest never
        dispatch, so wall time stays well under the serial floor.
        """
        service = AsyncMock()
        service.search_releases_by_track = AsyncMock(
            return_value=TrackReleasesResponse(
                track="Windowlicker",
                artist="Aphex Twin",
                releases=[
                    ReleaseInfo(
                        album=f"Release {i}",
                        artist="Aphex Twin",
                        release_id=1000 + i,
                        release_url=f"https://discogs.com/release/{1000 + i}",
                    )
                    for i in range(10)
                ],
                total=10,
            )
        )

        async def _slow_validate(*args, **kwargs):
            await asyncio.sleep(0.05)
            return True

        service.validate_track_on_release = AsyncMock(side_effect=_slow_validate)

        start = time.perf_counter()
        result = await lookup_releases_by_track("Windowlicker", "Aphex Twin", service=service)
        elapsed = time.perf_counter() - start

        assert elapsed < 0.2  # serial (10 x 0.05s) would be ~0.5s
        assert service.validate_track_on_release.call_count == MAX_SEARCH_RESULTS
        assert len(result) == MAX_SEARCH_RESULTS

    @pytest.mark.asyncio
    async def test_non_library_artist_skips_all_validation(self):
        """Part B (LML#866): a non-library artist skips every tracklist fetch."""
        service = AsyncMock()
        service.search_releases_by_track = AsyncMock(
            return_value=TrackReleasesResponse(
                track="Some Track",
                artist="Some Non-Library Artist",
                releases=[
                    ReleaseInfo(
                        album=f"Album {i}",
                        artist="Some Non-Library Artist",
                        release_id=2000 + i,
                        release_url=f"https://discogs.com/release/{2000 + i}",
                    )
                    for i in range(3)
                ],
                total=3,
            )
        )
        service.validate_track_on_release = AsyncMock(return_value=True)

        db = AsyncMock()
        db.search = AsyncMock(return_value=[])  # artist not in library

        result = await lookup_releases_by_track(
            "Some Track", "Some Non-Library Artist", service=service, db=db
        )

        assert result == []
        service.validate_track_on_release.assert_not_called()
        db.search.assert_awaited()

    @pytest.mark.asyncio
    async def test_library_artist_still_validates(self):
        """Part B (LML#866): a library artist passes the gate and still validates."""
        service = AsyncMock()
        service.search_releases_by_track = AsyncMock(
            return_value=TrackReleasesResponse(
                track="Vordhosbn",
                artist="Aphex Twin",
                releases=[
                    ReleaseInfo(
                        album="Drukqs",
                        artist="Aphex Twin",
                        release_id=3001,
                        release_url="https://discogs.com/release/3001",
                    ),
                    ReleaseInfo(
                        album="Drukqs (Deluxe)",
                        artist="Aphex Twin",
                        release_id=3002,
                        release_url="https://discogs.com/release/3002",
                    ),
                ],
                total=2,
            )
        )
        service.validate_track_on_release = AsyncMock(return_value=True)

        db = AsyncMock()
        db.search = AsyncMock(return_value=[make_library_item(artist="Aphex Twin", title="Drukqs")])

        result = await lookup_releases_by_track("Vordhosbn", "Aphex Twin", service=service, db=db)

        assert len(result) == 2
        assert service.validate_track_on_release.call_count == 2

    @pytest.mark.asyncio
    async def test_false_hit_excluded_even_when_album_in_library(self):
        """Acceptance (c) (LML#866): the library-first gate does not let an
        unvalidated keyword-search false hit through.

        The gate passes (the artist is in the library), so validation still
        runs per release. A real hit validates True; a keyword-search false hit
        whose album title collides with a library title but does NOT contain the
        track validates False — and must be excluded from the result.
        """
        service = AsyncMock()
        service.search_releases_by_track = AsyncMock(
            return_value=TrackReleasesResponse(
                track="Nannou",
                artist="Aphex Twin",
                releases=[
                    ReleaseInfo(
                        album="Windowlicker",  # real hit
                        artist="Aphex Twin",
                        release_id=4001,
                        release_url="https://discogs.com/release/4001",
                    ),
                    ReleaseInfo(
                        album="Drukqs",  # keyword false hit; track not on it
                        artist="Aphex Twin",
                        release_id=4002,
                        release_url="https://discogs.com/release/4002",
                    ),
                ],
                total=2,
            )
        )
        service.validate_track_on_release = AsyncMock(side_effect=[True, False])

        db = AsyncMock()
        db.search = AsyncMock(return_value=[make_library_item(artist="Aphex Twin", title="Drukqs")])

        result = await lookup_releases_by_track("Nannou", "Aphex Twin", service=service, db=db)

        albums = [album for _artist, album in result]
        assert "Windowlicker" in albums
        assert "Drukqs" not in albums


# ---------------------------------------------------------------------------
# lookup_releases_by_artist
# ---------------------------------------------------------------------------


class TestLookupReleasesByArtist:
    @pytest.mark.asyncio
    async def test_returns_releases(self):
        service = AsyncMock()
        service.search = AsyncMock(
            return_value=DiscogsSearchResponse(
                results=[
                    make_discogs_result(
                        release_id=1,
                        album="OK Computer",
                        artist="Radiohead",
                    )
                ],
                total=1,
            )
        )

        result = await lookup_releases_by_artist("Radiohead", service=service)
        assert len(result) == 1
        # Widened (LML#631) to surface the full DiscogsSearchResult so the
        # row-less path can carry release_id / release_url, not just the title.
        assert result[0].artist == "Radiohead"
        assert result[0].album == "OK Computer"
        assert result[0].release_id == 1

    @pytest.mark.asyncio
    async def test_no_service_returns_empty(self):
        with patch("discogs.lookup._get_service", return_value=None):
            result = await lookup_releases_by_artist("Artist")
        assert result == []

    @pytest.mark.asyncio
    async def test_handles_none_fields(self):
        service = AsyncMock()
        service.search = AsyncMock(
            return_value=DiscogsSearchResponse(
                results=[
                    make_discogs_result(
                        release_id=1,
                        album=None,
                        artist=None,
                    )
                ],
                total=1,
            )
        )

        result = await lookup_releases_by_artist("Artist", service=service)
        # Raw passthrough now — no ``or ""`` coercion; None stays None.
        assert len(result) == 1
        assert result[0].artist is None
        assert result[0].album is None
