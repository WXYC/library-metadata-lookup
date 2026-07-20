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
        cost ~0.5s. With bounded concurrency (MAX_SEARCH_RESULTS per wave), the
        ten validations run in two parallel waves (~0.1s), well under the serial
        floor. There is no raw-count early-exit — every candidate up to the search
        limit is validated (see test_artist_album_not_starved_by_leading_compilations
        for why truncating the raw list would drop the artist's own album).
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

        # Two parallel waves of five ≈ 0.1s; the serial floor (10 × 0.05s) is 0.5s.
        # The generous threshold keeps the parallelism assertion robust under CI load.
        assert elapsed < 0.35
        assert service.validate_track_on_release.call_count == 10
        assert len(result) == 10

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

    @pytest.mark.asyncio
    async def test_artist_album_not_starved_by_leading_compilations(self):
        """Recall (LML#866): the artist's own album must survive even when
        several validated Various-Artists compilations precede it.

        ``search_releases_by_track`` returns each release's *primary* artist, so
        VA compilations carry ``artist="Various"`` and the keyword-supplement leg
        injects non-artist-scoped releases. Those validate True (the track really
        is on the comp) but are dropped downstream by ``resolve_albums_for_track``'s
        artist-prefix filter. If validation stops after collecting the first
        ``MAX_SEARCH_RESULTS`` *raw* releases, a run of leading comps starves the
        artist's own album — a false ``song_not_found`` for a library artist whose
        album the library holds. Every candidate up to the search limit is
        validated and returned.
        """
        leading_comps = [
            ReleaseInfo(
                album=f"Jazz Sampler {i}",
                artist="Various",
                release_id=6000 + i,
                release_url=f"https://discogs.com/release/{6000 + i}",
                is_compilation=True,
            )
            for i in range(MAX_SEARCH_RESULTS)
        ]
        artist_album = ReleaseInfo(
            album="Duke Ellington & John Coltrane",
            artist="Duke Ellington & John Coltrane",
            release_id=6999,
            release_url="https://discogs.com/release/6999",
        )
        service = AsyncMock()
        service.search_releases_by_track = AsyncMock(
            return_value=TrackReleasesResponse(
                track="In a Sentimental Mood",
                artist="Duke Ellington & John Coltrane",
                releases=[*leading_comps, artist_album],
                total=MAX_SEARCH_RESULTS + 1,
            )
        )
        service.validate_track_on_release = AsyncMock(return_value=True)

        db = AsyncMock()
        db.search = AsyncMock(
            return_value=[
                make_library_item(
                    artist="Duke Ellington & John Coltrane",
                    title="Duke Ellington & John Coltrane",
                )
            ]
        )

        result = await lookup_releases_by_track(
            "In a Sentimental Mood",
            "Duke Ellington & John Coltrane",
            service=service,
            db=db,
        )

        assert ("Duke Ellington & John Coltrane", "Duke Ellington & John Coltrane") in result

    @pytest.mark.asyncio
    async def test_gate_uses_artist_column_not_truncatable_fts(self):
        """Recall (LML#866): the library-first gate must not wrongly suppress a
        library artist whose bare-name FTS query truncates past its own rows.

        ``db.search(query=...)`` is a cross-column FTS with a fixed LIMIT and no
        rank ordering (``library/db.py``), so a common single-token artist
        ("Women", "Low", "Wire") can have its rows crowded out by unrelated title
        matches. The album-fed consumer searches the far more selective
        ``"{artist} {album}"`` and would surface the row, so the gate must probe
        the artist column directly (``db.search(artist=...)``) — a sound lower
        bound — rather than the truncatable FTS path.
        """
        service = AsyncMock()
        service.search_releases_by_track = AsyncMock(
            return_value=TrackReleasesResponse(
                track="Black Rice",
                artist="Women",
                releases=[
                    ReleaseInfo(
                        album="Public Strain",
                        artist="Women",
                        release_id=7001,
                        release_url="https://discogs.com/release/7001",
                    )
                ],
                total=1,
            )
        )
        service.validate_track_on_release = AsyncMock(return_value=True)

        async def _fake_search(query=None, artist=None, title=None, **kwargs):
            # FTS (query=) truncates past the artist's row; the artist-column
            # filter (artist=) finds it.
            if artist is not None:
                return [make_library_item(artist="Women", title="Public Strain")]
            return []

        db = AsyncMock()
        db.search = AsyncMock(side_effect=_fake_search)

        result = await lookup_releases_by_track("Black Rice", "Women", service=service, db=db)

        assert result == [("Women", "Public Strain")]


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
