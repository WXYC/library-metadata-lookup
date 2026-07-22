"""Integration tests for the track-scoped streaming-URL cache (LML#893, L1).

The unit tests mock ``PgSource``; this is the matching ``@pytest.mark.pg`` layer
that drives the real schema, the real UPSERT, and — critically — the
different-song-same-album MISS against an actual PostgreSQL connection.

Concrete production risks the unit tests cannot catch:

* The named CHECK constraint actually rejects an unknown ``service`` value.
* The composite PK carries the song axis, so a DIFFERENT song of the same
  album is a genuine MISS (a wrong-track hit would ship the first song's URL).
* ``url NOT NULL`` structurally rejects a null write (the #782/BS#1192 guard).
* ``to_match_form`` normalization parity between SELECT and UPSERT.

Run with: pytest -m pg -v tests/integration/test_track_streaming_url_cache_pg.py
"""

from __future__ import annotations

import asyncpg
import pytest
import pytest_asyncio

from entity.sources import PgSource
from entity.track_streaming_url_cache import (
    APPLE_MUSIC_TRACK_SERVICE,
    get_cached_track_streaming_url,
    set_cached_track_streaming_url,
    set_up_track_streaming_url_cache_schema,
)
from tests.integration.conftest import skip_if_named_tables_populated

_ARTIST = "Juana Molina"
_ALBUM = "DOGA"
_SONG = "la paradoja"
_OTHER_SONG = "eras"
_TRACK_URL = "https://music.apple.com/us/song/la-paradoja/1234567890"


@pytest_asyncio.fixture
async def pg_source(pg_pool):
    """A ``PgSource`` borrowing the test pool (no-op close)."""
    return PgSource(pool=pg_pool)


@pytest_asyncio.fixture(autouse=True)
async def set_up_cache_schema(pg_pool, pg_source):
    """Reset just ``lml_cache.track_streaming_url_cache``, then apply the DDL.

    Surgical: drops only the one table this suite owns — not the whole
    ``lml_cache`` schema — and refuses to run if that table already holds rows
    (a mispointed ``DATABASE_URL_TEST`` would otherwise drop real cached URLs).
    """
    async with pg_pool.acquire() as conn:
        await skip_if_named_tables_populated(conn, (("lml_cache", "track_streaming_url_cache"),))
        await conn.execute("DROP TABLE IF EXISTS lml_cache.track_streaming_url_cache")
    await set_up_track_streaming_url_cache_schema(pg_source)
    yield
    async with pg_pool.acquire() as conn:
        await conn.execute("DROP TABLE IF EXISTS lml_cache.track_streaming_url_cache")


@pytest.mark.pg
class TestSchemaBootstrap:
    @pytest.mark.asyncio
    async def test_table_created_with_song_axis(self, pg_pool):
        async with pg_pool.acquire() as conn:
            cols = await conn.fetch(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'lml_cache' "
                "AND table_name = 'track_streaming_url_cache'"
            )
        names = {r["column_name"] for r in cols}
        assert {"service", "artist_normalized", "album_normalized", "song_normalized", "url"} <= (
            names
        )

    @pytest.mark.asyncio
    async def test_named_check_constraint_rejects_unknown_service(self, pg_pool):
        with pytest.raises(asyncpg.PostgresError):
            async with pg_pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO lml_cache.track_streaming_url_cache "
                    "(service, artist_normalized, album_normalized, song_normalized, url) "
                    "VALUES ('spotify_track', 'x', 'y', 'z', 'https://example.test')"
                )

    @pytest.mark.asyncio
    async def test_url_column_rejects_null(self, pg_pool):
        # The #782/BS#1192 guard is enforced at the schema level.
        with pytest.raises(asyncpg.PostgresError):
            async with pg_pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO lml_cache.track_streaming_url_cache "
                    "(service, artist_normalized, album_normalized, song_normalized, url) "
                    "VALUES ($1, 'x', 'y', 'z', NULL)",
                    APPLE_MUSIC_TRACK_SERVICE,
                )


@pytest.mark.pg
class TestCacheReadWrite:
    @pytest.mark.asyncio
    async def test_write_then_read_is_a_hit(self, pg_source):
        await set_cached_track_streaming_url(
            pg_source,
            service=APPLE_MUSIC_TRACK_SERVICE,
            artist=_ARTIST,
            album=_ALBUM,
            song=_SONG,
            url=_TRACK_URL,
        )
        got = await get_cached_track_streaming_url(
            pg_source,
            service=APPLE_MUSIC_TRACK_SERVICE,
            artist=_ARTIST,
            album=_ALBUM,
            song=_SONG,
        )
        assert got == _TRACK_URL

    @pytest.mark.asyncio
    async def test_different_song_same_album_is_a_miss(self, pg_source):
        # The acceptance-critical case: caching one song's deep-link must NOT
        # serve it for a different song of the same album.
        await set_cached_track_streaming_url(
            pg_source,
            service=APPLE_MUSIC_TRACK_SERVICE,
            artist=_ARTIST,
            album=_ALBUM,
            song=_SONG,
            url=_TRACK_URL,
        )
        got = await get_cached_track_streaming_url(
            pg_source,
            service=APPLE_MUSIC_TRACK_SERVICE,
            artist=_ARTIST,
            album=_ALBUM,
            song=_OTHER_SONG,
        )
        assert got is None

    @pytest.mark.asyncio
    async def test_upsert_updates_in_place(self, pg_source, pg_pool):
        for url in (_TRACK_URL, _TRACK_URL + "-v2"):
            await set_cached_track_streaming_url(
                pg_source,
                service=APPLE_MUSIC_TRACK_SERVICE,
                artist=_ARTIST,
                album=_ALBUM,
                song=_SONG,
                url=url,
            )
        got = await get_cached_track_streaming_url(
            pg_source,
            service=APPLE_MUSIC_TRACK_SERVICE,
            artist=_ARTIST,
            album=_ALBUM,
            song=_SONG,
        )
        assert got == _TRACK_URL + "-v2"
        async with pg_pool.acquire() as conn:
            n = await conn.fetchval("SELECT count(*)::int FROM lml_cache.track_streaming_url_cache")
        assert n == 1

    @pytest.mark.asyncio
    async def test_normalization_parity_between_write_and_read(self, pg_source):
        # Diacritics/case/whitespace differ between write and read; the shared
        # ``to_match_form`` normalization must still resolve the same row.
        await set_cached_track_streaming_url(
            pg_source,
            service=APPLE_MUSIC_TRACK_SERVICE,
            artist="Hermanos Gutiérrez",
            album="El Bueno Y El Malo",
            song="Thunderbird",
            url=_TRACK_URL,
        )
        got = await get_cached_track_streaming_url(
            pg_source,
            service=APPLE_MUSIC_TRACK_SERVICE,
            artist="  hermanos gutierrez ",
            album="el bueno y el malo",
            song="THUNDERBIRD",
        )
        assert got == _TRACK_URL

    @pytest.mark.asyncio
    async def test_none_album_keys_stably(self, pg_source):
        await set_cached_track_streaming_url(
            pg_source,
            service=APPLE_MUSIC_TRACK_SERVICE,
            artist=_ARTIST,
            album=None,
            song=_SONG,
            url=_TRACK_URL,
        )
        got = await get_cached_track_streaming_url(
            pg_source,
            service=APPLE_MUSIC_TRACK_SERVICE,
            artist=_ARTIST,
            album=None,
            song=_SONG,
        )
        assert got == _TRACK_URL
