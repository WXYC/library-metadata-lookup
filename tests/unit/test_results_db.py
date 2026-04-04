"""Unit tests for scripts/streaming_availability/results_db.py."""

import json

import pytest
import pytest_asyncio

from scripts.streaming_availability.dedup import DeduplicatedAlbum
from scripts.streaming_availability.results_db import ResultsDB


@pytest_asyncio.fixture
async def db():
    """In-memory results database."""
    results_db = ResultsDB(":memory:")
    await results_db.connect()
    yield results_db
    await results_db.close()


def _make_album(**kwargs) -> DeduplicatedAlbum:
    defaults = {
        "normalized_artist": "stereolab",
        "normalized_title": "aluminum tunes",
        "display_artist": "Stereolab",
        "display_title": "Aluminum Tunes",
        "library_ids": [1, 2],
        "formats": ["cd", "vinyl - LP"],
        "genre": "Rock",
        "label": "Duophonic",
        "is_compilation": False,
    }
    defaults.update(kwargs)
    return DeduplicatedAlbum(**defaults)


class TestInsertAlbums:
    @pytest.mark.asyncio
    async def test_insert_single_album(self, db):
        album = _make_album()
        count = await db.insert_albums([album])
        assert count == 1

    @pytest.mark.asyncio
    async def test_insert_duplicate_ignored(self, db):
        album = _make_album()
        await db.insert_albums([album])
        count = await db.insert_albums([album])
        assert count == 0

    @pytest.mark.asyncio
    async def test_insert_multiple_albums(self, db):
        albums = [
            _make_album(),
            _make_album(
                normalized_artist="autechre",
                normalized_title="confield",
                display_artist="Autechre",
                display_title="Confield",
                library_ids=[3],
                formats=["cd"],
                genre="Electronic",
                label="Warp",
            ),
        ]
        count = await db.insert_albums(albums)
        assert count == 2

    @pytest.mark.asyncio
    async def test_library_ids_stored_as_json(self, db):
        album = _make_album(library_ids=[10, 20, 30])
        await db.insert_albums([album])
        rows = await db.get_pending("spotify", limit=10)
        assert json.loads(rows[0]["library_ids"]) == [10, 20, 30]

    @pytest.mark.asyncio
    async def test_compilation_stored(self, db):
        album = _make_album(
            normalized_artist="various artists",
            display_artist="Various Artists",
            is_compilation=True,
        )
        await db.insert_albums([album])
        rows = await db.get_pending("spotify", limit=10)
        assert rows[0]["is_compilation"] == 1


class TestGetPending:
    @pytest.mark.asyncio
    async def test_returns_pending_rows(self, db):
        await db.insert_albums([_make_album()])
        rows = await db.get_pending("spotify", limit=10)
        assert len(rows) == 1
        assert rows[0]["spotify_status"] == "pending"

    @pytest.mark.asyncio
    async def test_respects_limit(self, db):
        albums = [
            _make_album(),
            _make_album(
                normalized_artist="autechre",
                normalized_title="confield",
                display_artist="Autechre",
                display_title="Confield",
                library_ids=[3],
                formats=["cd"],
            ),
        ]
        await db.insert_albums(albums)
        rows = await db.get_pending("spotify", limit=1)
        assert len(rows) == 1

    @pytest.mark.asyncio
    async def test_excludes_non_pending(self, db):
        await db.insert_albums([_make_album()])
        rows = await db.get_pending("spotify", limit=10)
        album_id = rows[0]["id"]
        await db.update_result(album_id, "spotify", "found", url="https://open.spotify.com/album/x")
        rows = await db.get_pending("spotify", limit=10)
        assert len(rows) == 0


class TestGetSpotifyMissesPendingApple:
    @pytest.mark.asyncio
    async def test_returns_not_found_on_spotify_pending_on_apple(self, db):
        await db.insert_albums([_make_album()])
        rows = await db.get_pending("spotify", limit=10)
        await db.update_result(rows[0]["id"], "spotify", "not_found")
        rows = await db.get_spotify_misses_pending_apple(limit=10)
        assert len(rows) == 1

    @pytest.mark.asyncio
    async def test_excludes_spotify_found(self, db):
        await db.insert_albums([_make_album()])
        rows = await db.get_pending("spotify", limit=10)
        await db.update_result(
            rows[0]["id"], "spotify", "found", url="https://open.spotify.com/album/x"
        )
        rows = await db.get_spotify_misses_pending_apple(limit=10)
        assert len(rows) == 0


class TestUpdateResult:
    @pytest.mark.asyncio
    async def test_update_spotify_found(self, db):
        await db.insert_albums([_make_album()])
        rows = await db.get_pending("spotify", limit=10)
        album_id = rows[0]["id"]
        await db.update_result(
            album_id,
            "spotify",
            "found",
            url="https://open.spotify.com/album/abc123",
            spotify_id="abc123",
            confidence=95.0,
            matched_artist="Stereolab",
            matched_title="Aluminium Tunes",
        )
        stats = await db.get_stats()
        assert stats["spotify"]["found"] == 1

    @pytest.mark.asyncio
    async def test_update_apple_found(self, db):
        await db.insert_albums([_make_album()])
        rows = await db.get_pending("spotify", limit=10)
        album_id = rows[0]["id"]
        await db.update_result(
            album_id,
            "apple",
            "found",
            url="https://music.apple.com/us/album/123",
            confidence=92.0,
            matched_artist="Stereolab",
            matched_title="Aluminum Tunes",
        )
        stats = await db.get_stats()
        assert stats["apple"]["found"] == 1


class TestGetStats:
    @pytest.mark.asyncio
    async def test_stats_all_pending(self, db):
        await db.insert_albums([_make_album()])
        stats = await db.get_stats()
        assert stats["spotify"]["pending"] == 1
        assert stats["apple"]["pending"] == 1
        assert stats["total"] == 1

    @pytest.mark.asyncio
    async def test_stats_empty_db(self, db):
        stats = await db.get_stats()
        assert stats["total"] == 0
