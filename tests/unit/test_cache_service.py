"""Unit tests for discogs/cache_service.py."""

from unittest.mock import AsyncMock

import pytest

from discogs.cache_service import CacheUnavailableError, DiscogsCacheService
from discogs.models import ReleaseMetadataResponse, TrackItem


@pytest.fixture
def cache_service(mock_asyncpg_pool):
    return DiscogsCacheService(mock_asyncpg_pool)


# ---------------------------------------------------------------------------
# is_available
# ---------------------------------------------------------------------------


class TestIsAvailable:
    @pytest.mark.asyncio
    async def test_healthy(self, cache_service, mock_asyncpg_pool):
        mock_asyncpg_pool.fetchval = AsyncMock(return_value=1)
        assert await cache_service.is_available() is True

    @pytest.mark.asyncio
    async def test_exception(self, cache_service, mock_asyncpg_pool):
        mock_asyncpg_pool.fetchval = AsyncMock(side_effect=Exception("down"))
        assert await cache_service.is_available() is False


# ---------------------------------------------------------------------------
# search_releases_by_track
# ---------------------------------------------------------------------------


class TestSearchReleasesByTrack:
    @pytest.mark.asyncio
    async def test_returns_results(self, cache_service, mock_asyncpg_pool):
        mock_asyncpg_pool.fetch = AsyncMock(
            return_value=[
                {
                    "release_id": 1,
                    "title": "Album",
                    "artist_name": "Artist",
                    "track_title": "Song",
                    "is_compilation": False,
                }
            ]
        )

        results = await cache_service.search_releases_by_track("Song", "Artist")
        assert len(results) == 1
        assert results[0].album == "Album"

    @pytest.mark.asyncio
    async def test_deduplicates(self, cache_service, mock_asyncpg_pool):
        mock_asyncpg_pool.fetch = AsyncMock(
            return_value=[
                {"release_id": 1, "title": "Album", "artist_name": "A", "track_title": "S", "is_compilation": False},
                {"release_id": 2, "title": "Album", "artist_name": "A", "track_title": "S", "is_compilation": False},
            ]
        )

        results = await cache_service.search_releases_by_track("S")
        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_respects_limit(self, cache_service, mock_asyncpg_pool):
        rows = [
            {"release_id": i, "title": f"Album{i}", "artist_name": "A", "track_title": "S", "is_compilation": False}
            for i in range(10)
        ]
        mock_asyncpg_pool.fetch = AsyncMock(return_value=rows)

        results = await cache_service.search_releases_by_track("S", limit=3)
        assert len(results) == 3

    @pytest.mark.asyncio
    async def test_error_raises_cache_unavailable(self, cache_service, mock_asyncpg_pool):
        mock_asyncpg_pool.fetch = AsyncMock(side_effect=Exception("db error"))

        with pytest.raises(CacheUnavailableError):
            await cache_service.search_releases_by_track("S")


# ---------------------------------------------------------------------------
# get_release
# ---------------------------------------------------------------------------


class TestGetRelease:
    @pytest.mark.asyncio
    async def test_not_found(self, cache_service, mock_asyncpg_pool):
        mock_asyncpg_pool.fetchrow = AsyncMock(return_value=None)
        result = await cache_service.get_release(999)
        assert result is None

    @pytest.mark.asyncio
    async def test_full_metadata(self, cache_service, mock_asyncpg_pool):
        mock_asyncpg_pool.fetchrow = AsyncMock(
            return_value={
                "id": 123,
                "title": "The Game",
                "release_year": 1980,
                "artwork_url": "https://img.com/a.jpg",
            }
        )
        mock_asyncpg_pool.fetch = AsyncMock(
            side_effect=[
                # artist_rows
                [{"artist_name": "Queen", "extra": 0}],
                # track_rows
                [{"position": "1", "title": "Play the Game", "duration": "3:30", "sequence": 1}],
                # track_artist_rows
                [],
            ]
        )

        result = await cache_service.get_release(123)
        assert result is not None
        assert result.title == "The Game"
        assert result.artist == "Queen"
        assert len(result.tracklist) == 1
        assert result.cached is True

    @pytest.mark.asyncio
    async def test_with_track_artists(self, cache_service, mock_asyncpg_pool):
        mock_asyncpg_pool.fetchrow = AsyncMock(
            return_value={"id": 1, "title": "Compilation", "release_year": 2000, "artwork_url": None}
        )
        mock_asyncpg_pool.fetch = AsyncMock(
            side_effect=[
                [{"artist_name": "Various Artists", "extra": 0}],
                [{"position": "1", "title": "Track1", "duration": None, "sequence": 1}],
                [{"track_sequence": 1, "artist_name": "Some Artist"}],
            ]
        )

        result = await cache_service.get_release(1)
        assert result.tracklist[0].artists == ["Some Artist"]

    @pytest.mark.asyncio
    async def test_error_raises(self, cache_service, mock_asyncpg_pool):
        mock_asyncpg_pool.fetchrow = AsyncMock(side_effect=Exception("db error"))
        with pytest.raises(CacheUnavailableError):
            await cache_service.get_release(1)


# ---------------------------------------------------------------------------
# write_release
# ---------------------------------------------------------------------------


class TestWriteRelease:
    @pytest.mark.asyncio
    async def test_writes_release(self, cache_service, mock_asyncpg_pool):
        release = ReleaseMetadataResponse(
            release_id=1,
            title="Album",
            artist="Artist",
            year=2020,
            artwork_url="https://img.com/a.jpg",
            tracklist=[TrackItem(position="1", title="Track1", artists=["ArtistA"])],
            release_url="https://discogs.com/release/1",
        )

        await cache_service.write_release(release)
        conn = mock_asyncpg_pool._mock_conn
        assert conn.execute.call_count >= 3  # insert release, artist, delete tracks, cache_metadata
        assert conn.executemany.call_count >= 1  # insert tracks

    @pytest.mark.asyncio
    async def test_error_raises(self, cache_service, mock_asyncpg_pool):
        mock_asyncpg_pool.acquire.return_value.__aenter__ = AsyncMock(side_effect=Exception("fail"))
        release = ReleaseMetadataResponse(
            release_id=1, title="A", artist="B",
            release_url="https://discogs.com/release/1",
        )
        with pytest.raises(CacheUnavailableError):
            await cache_service.write_release(release)


# ---------------------------------------------------------------------------
# search_releases
# ---------------------------------------------------------------------------


class TestSearchReleases:
    @pytest.mark.asyncio
    async def test_no_params_returns_empty(self, cache_service):
        result = await cache_service.search_releases()
        assert result == []

    @pytest.mark.asyncio
    async def test_artist_and_album(self, cache_service, mock_asyncpg_pool):
        mock_asyncpg_pool.fetch = AsyncMock(
            return_value=[
                {"release_id": 1, "title": "Album", "artist_name": "Artist", "artwork_url": None, "score": 0.8}
            ]
        )
        result = await cache_service.search_releases(artist="Artist", album="Album")
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_artist_only(self, cache_service, mock_asyncpg_pool):
        mock_asyncpg_pool.fetch = AsyncMock(
            return_value=[
                {"release_id": 1, "title": "Album", "artist_name": "Artist", "artwork_url": None, "score": 0.8}
            ]
        )
        result = await cache_service.search_releases(artist="Artist")
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_album_only(self, cache_service, mock_asyncpg_pool):
        mock_asyncpg_pool.fetch = AsyncMock(
            return_value=[
                {"release_id": 1, "title": "Album", "artist_name": "Artist", "artwork_url": None, "score": 0.8}
            ]
        )
        result = await cache_service.search_releases(album="Album")
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_deduplicates(self, cache_service, mock_asyncpg_pool):
        mock_asyncpg_pool.fetch = AsyncMock(
            return_value=[
                {"release_id": 1, "title": "Album", "artist_name": "A1", "artwork_url": None, "score": 0.8},
                {"release_id": 2, "title": "Album", "artist_name": "A2", "artwork_url": None, "score": 0.7},
            ]
        )
        result = await cache_service.search_releases(artist="A1")
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_error_raises(self, cache_service, mock_asyncpg_pool):
        mock_asyncpg_pool.fetch = AsyncMock(side_effect=Exception("db error"))
        with pytest.raises(CacheUnavailableError):
            await cache_service.search_releases(artist="A")


# ---------------------------------------------------------------------------
# validate_track_on_release
# ---------------------------------------------------------------------------


class TestValidateTrackOnRelease:
    @pytest.mark.asyncio
    async def test_not_cached_returns_none(self, cache_service, mock_asyncpg_pool):
        mock_asyncpg_pool.fetchrow = AsyncMock(return_value=None)
        result = await cache_service.validate_track_on_release(999, "Song", "Artist")
        assert result is None

    @pytest.mark.asyncio
    async def test_found(self, cache_service, mock_asyncpg_pool):
        mock_asyncpg_pool.fetchrow = AsyncMock(
            return_value={"id": 1, "title": "Album", "release_year": 2020, "artwork_url": None}
        )
        mock_asyncpg_pool.fetch = AsyncMock(
            side_effect=[
                [{"artist_name": "Artist", "extra": 0}],
                [{"position": "1", "title": "Song", "duration": None, "sequence": 1}],
                [],
            ]
        )
        result = await cache_service.validate_track_on_release(1, "Song", "Artist")
        assert result is True

    @pytest.mark.asyncio
    async def test_not_found(self, cache_service, mock_asyncpg_pool):
        mock_asyncpg_pool.fetchrow = AsyncMock(
            return_value={"id": 1, "title": "Album", "release_year": 2020, "artwork_url": None}
        )
        mock_asyncpg_pool.fetch = AsyncMock(
            side_effect=[
                [{"artist_name": "Artist", "extra": 0}],
                [{"position": "1", "title": "Other Song", "duration": None, "sequence": 1}],
                [],
            ]
        )
        result = await cache_service.validate_track_on_release(1, "Missing Song", "Artist")
        assert result is False
