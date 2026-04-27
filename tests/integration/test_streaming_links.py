"""Integration tests for streaming links in library/db.py with real SQLite."""

import aiosqlite
import pytest

from library.db import LibraryDB


async def _create_library_db_with_streaming(db_path, *, include_streaming_table=True):
    """Create a library.db with library + optional streaming_links tables."""
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(
            "CREATE TABLE library ("
            "id INTEGER PRIMARY KEY, title TEXT, artist TEXT, "
            "call_letters TEXT, artist_call_number INTEGER, "
            "release_call_number INTEGER, genre TEXT, format TEXT)"
        )
        await conn.execute(
            "INSERT INTO library VALUES "
            "(100, 'Aluminum Tunes', 'Stereolab', 'RO', 87, 1, 'Rock', 'CD')"
        )
        await conn.execute(
            "INSERT INTO library VALUES "
            "(200, 'Confield', 'Autechre', 'EL', 16, 1, 'Electronic', 'CD')"
        )
        await conn.execute(
            "INSERT INTO library VALUES (300, 'Moon Pix', 'Cat Power', 'RO', 23, 1, 'Rock', 'CD')"
        )

        if include_streaming_table:
            await conn.execute(
                "CREATE TABLE streaming_links ("
                "library_id INTEGER PRIMARY KEY, spotify_url TEXT, apple_music_url TEXT, "
                "deezer_url TEXT, bandcamp_url TEXT, tidal_url TEXT, "
                "youtube_music_url TEXT, soundcloud_url TEXT)"
            )
            await conn.execute(
                "INSERT INTO streaming_links VALUES "
                "(100, 'https://open.spotify.com/album/stereolab-aluminum-tunes', "
                "'https://music.apple.com/album/stereolab-aluminum-tunes', "
                "NULL, 'https://stereolab.bandcamp.com/album/aluminum-tunes', "
                "NULL, NULL, NULL)"
            )
            await conn.execute(
                "INSERT INTO streaming_links VALUES "
                "(200, NULL, NULL, "
                "'https://www.deezer.com/album/autechre-confield', "
                "'https://autechre.bandcamp.com/album/confield', "
                "NULL, NULL, NULL)"
            )

        await conn.commit()


class TestGetStreamingLinksWithRealData:
    @pytest.mark.asyncio
    async def test_get_streaming_links_with_real_data(self, tmp_path):
        """Create full library.db with library + streaming_links. Verify correct URLs."""
        db_path = tmp_path / "library.db"
        await _create_library_db_with_streaming(db_path)

        db = LibraryDB(db_path)
        await db.connect()

        links = await db.get_streaming_links(100)
        assert links is not None
        assert links["spotify_url"] == "https://open.spotify.com/album/stereolab-aluminum-tunes"
        assert links["apple_music_url"] == "https://music.apple.com/album/stereolab-aluminum-tunes"
        assert links["bandcamp_url"] == "https://stereolab.bandcamp.com/album/aluminum-tunes"
        assert links["deezer_url"] is None
        assert links["tidal_url"] is None
        assert links["youtube_music_url"] is None
        assert links["soundcloud_url"] is None

        await db.close()


class TestGetStreamingStatusBatch:
    @pytest.mark.asyncio
    async def test_get_streaming_status_batch(self, tmp_path):
        """Call with [100, 200, 999]. 100 and 200 have links, 999 is absent."""
        db_path = tmp_path / "library.db"
        await _create_library_db_with_streaming(db_path)

        db = LibraryDB(db_path)
        await db.connect()

        status = await db.get_streaming_status([100, 200, 999])

        assert status[100] is True
        assert status[200] is True
        assert 999 not in status

        await db.close()


class TestStreamingLinksNotPresent:
    @pytest.mark.asyncio
    async def test_streaming_links_not_present(self, tmp_path):
        """Library DB without streaming_links table. get_streaming_links returns None."""
        db_path = tmp_path / "library.db"
        await _create_library_db_with_streaming(db_path, include_streaming_table=False)

        db = LibraryDB(db_path)
        await db.connect()

        assert db._has_streaming_links is False
        links = await db.get_streaming_links(100)
        assert links is None

        status = await db.get_streaming_status([100, 200])
        assert status == {}

        await db.close()
