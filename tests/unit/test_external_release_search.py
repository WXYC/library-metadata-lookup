"""Unit tests for the album + track external-cache fallback branches.

Phase 1.7 of the mojibake-cleanup project (WXYC/library-metadata-lookup#195).
The lossy-mojibake matcher sends column-typed lookup bodies — a SONG_TITLE
skeleton becomes ``{song: ...}``, a RELEASE_TITLE skeleton becomes
``{album: ...}``. Phase 1.5 only handled artist-keyed bodies; this phase
broadens the fallback so SONG_TITLE / RELEASE_TITLE skeletons get a fair
shot at the discogs-cache + musicbrainz-cache PostgreSQL DBs.
"""

from unittest.mock import AsyncMock

import pytest

from discogs.cache_service import CacheUnavailableError
from lookup.external_search import (
    search_external_albums,
    search_external_tracks,
)


@pytest.fixture
def mock_discogs_cache():
    cache = AsyncMock()
    cache.search_releases_by_title = AsyncMock(return_value=[])
    cache.search_tracks_by_title = AsyncMock(return_value=[])
    return cache


@pytest.fixture
def mock_mb_pg():
    pg = AsyncMock()
    pg.fetchall = AsyncMock(return_value=[])
    return pg


class TestSearchExternalAlbums:
    @pytest.mark.asyncio
    async def test_returns_discogs_album_hit(self, mock_discogs_cache, mock_mb_pg):
        """Discogs release hit returns canonical artist + release title."""
        mock_discogs_cache.search_releases_by_title.return_value = [
            {"id": 12345, "title": "DOGA", "artist": "Juana Molina", "score": 0.81},
        ]

        items, source = await search_external_albums(
            "DOG",
            discogs_cache=mock_discogs_cache,
            mb_pg=mock_mb_pg,
        )

        assert source == "discogs"
        assert items == [{"id": 12345, "artist": "Juana Molina", "title": "DOGA"}]
        mock_mb_pg.fetchall.assert_not_called()

    @pytest.mark.asyncio
    async def test_falls_back_to_musicbrainz_release(self, mock_discogs_cache, mock_mb_pg):
        """Discogs miss -> MB ``mb_release`` query returns canonical artist credit."""
        mock_discogs_cache.search_releases_by_title.return_value = []
        mock_mb_pg.fetchall.return_value = [
            {"id": 7, "title": "Aluminum Tunes", "artist": "Stereolab", "score": 0.78},
        ]

        items, source = await search_external_albums(
            "Aluminum Tnes",
            discogs_cache=mock_discogs_cache,
            mb_pg=mock_mb_pg,
        )

        assert source == "musicbrainz"
        assert items == [{"id": 7, "artist": "Stereolab", "title": "Aluminum Tunes"}]
        # MB query must hit mb_release + mb_artist_credit
        sql = mock_mb_pg.fetchall.call_args.args[0]
        assert "mb_release" in sql
        assert "mb_artist_credit" in sql

    @pytest.mark.asyncio
    async def test_returns_empty_on_double_miss(self, mock_discogs_cache, mock_mb_pg):
        items, source = await search_external_albums(
            "ZZZNothingHere",
            discogs_cache=mock_discogs_cache,
            mb_pg=mock_mb_pg,
        )
        assert items == []
        assert source is None

    @pytest.mark.asyncio
    async def test_skips_blank_skeleton(self, mock_discogs_cache, mock_mb_pg):
        items, source = await search_external_albums(
            "   ",
            discogs_cache=mock_discogs_cache,
            mb_pg=mock_mb_pg,
        )
        assert items == []
        assert source is None
        mock_discogs_cache.search_releases_by_title.assert_not_called()
        mock_mb_pg.fetchall.assert_not_called()

    @pytest.mark.asyncio
    async def test_caches_disabled(self):
        items, source = await search_external_albums("DOGA", discogs_cache=None, mb_pg=None)
        assert items == []
        assert source is None

    @pytest.mark.asyncio
    async def test_discogs_unavailable_falls_through_to_mb(self, mock_discogs_cache, mock_mb_pg):
        mock_discogs_cache.search_releases_by_title.side_effect = CacheUnavailableError("pool down")
        mock_mb_pg.fetchall.return_value = [
            {"id": 1, "title": "Edits", "artist": "Chuquimamani-Condori", "score": 0.9},
        ]
        items, source = await search_external_albums(
            "Edits",
            discogs_cache=mock_discogs_cache,
            mb_pg=mock_mb_pg,
        )
        assert source == "musicbrainz"
        assert items[0]["title"] == "Edits"


class TestSearchExternalTracks:
    @pytest.mark.asyncio
    async def test_returns_discogs_track_hit(self, mock_discogs_cache, mock_mb_pg):
        """Discogs ``release_track`` hit returns track title + parent release artist."""
        mock_discogs_cache.search_tracks_by_title.return_value = [
            {
                "id": 555,
                "title": "Back, Baby",
                "artist": "Jessica Pratt",
                "score": 0.92,
            },
        ]
        items, source = await search_external_tracks(
            "Back Baby",
            discogs_cache=mock_discogs_cache,
            mb_pg=mock_mb_pg,
        )
        assert source == "discogs"
        assert items == [{"id": 555, "artist": "Jessica Pratt", "title": "Back, Baby"}]
        mock_mb_pg.fetchall.assert_not_called()

    @pytest.mark.asyncio
    async def test_falls_back_to_mb_recording(self, mock_discogs_cache, mock_mb_pg):
        """Discogs miss -> MB ``mb_recording`` query returns canonical artist credit."""
        mock_discogs_cache.search_tracks_by_title.return_value = []
        mock_mb_pg.fetchall.return_value = [
            {"id": 42, "title": "la paradoja", "artist": "Juana Molina", "score": 0.85},
        ]
        items, source = await search_external_tracks(
            "la paradja",
            discogs_cache=mock_discogs_cache,
            mb_pg=mock_mb_pg,
        )
        assert source == "musicbrainz"
        assert items == [{"id": 42, "artist": "Juana Molina", "title": "la paradoja"}]
        sql = mock_mb_pg.fetchall.call_args.args[0]
        assert "mb_recording" in sql
        assert "mb_artist_credit" in sql

    @pytest.mark.asyncio
    async def test_skips_blank_skeleton(self, mock_discogs_cache, mock_mb_pg):
        items, source = await search_external_tracks(
            "",
            discogs_cache=mock_discogs_cache,
            mb_pg=mock_mb_pg,
        )
        assert items == []
        assert source is None
        mock_discogs_cache.search_tracks_by_title.assert_not_called()
        mock_mb_pg.fetchall.assert_not_called()

    @pytest.mark.asyncio
    async def test_double_miss(self, mock_discogs_cache, mock_mb_pg):
        items, source = await search_external_tracks(
            "ZZZNotARealSong",
            discogs_cache=mock_discogs_cache,
            mb_pg=mock_mb_pg,
        )
        assert items == []
        assert source is None
