"""Integration tests for the persistent streaming-URL cache (LML#573).

LML#573 generalized the Apple-only ``entity.album_apple_music_lookup_cache``
(LML#571) into the polymorphic ``lml_cache.album_streaming_url_cache`` table,
the cache module ``entity/streaming_url_cache.py``, and the read-through
resolver ``resolve_streaming_url_with_cache``. All unit coverage mocks
``PgSource``; this file is the matching ``@pytest.mark.pg`` layer that drives
the real schema, the real UPSERT, the real TTL math, and the cross-schema
backfill against an actual PostgreSQL connection.

Concrete production risks the unit tests cannot catch:

* The named CHECK constraint actually rejects an unknown ``service`` value.
* TIMESTAMPTZ codec drift — asyncpg returns aware ``datetime``; a naive value
  would break the SQL ``last_checked_at > $4`` comparison.
* UPSERT semantics — the composite-PK ``ON CONFLICT`` updates in place.
* ``to_match_form`` normalization parity between SELECT and UPSERT.
* The backfill preserves each row's ``last_checked_at`` (the TTL-preservation
  AC) instead of resetting it to ``now()``.

Run with: pytest -m pg -v tests/integration/test_streaming_url_persistent_lookup.py
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import asyncpg
import pytest
import pytest_asyncio

from clients.streaming.base import BaseStreamingClient
from entity.sources import PgSource
from entity.streaming_url_cache import (
    get_cached_streaming_url,
    resolve_streaming_url_with_cache,
    set_cached_streaming_url,
    set_up_streaming_url_cache_schema,
)
from streaming.models import SourceMatch

DATABASE_URL = os.getenv(
    "DATABASE_URL_TEST",
    "postgresql://discogs:discogs@localhost:5433/discogs",
)

# Old apple-only table DDL (LML#571) — recreated by the backfill test so it
# can drive the cross-schema copy. Mirrors entity/apple_music_album_cache.py.
_OLD_APPLE_TABLE_DDL = """\
CREATE TABLE IF NOT EXISTS entity.album_apple_music_lookup_cache (
    artist_normalized TEXT NOT NULL,
    album_normalized TEXT NOT NULL,
    apple_music_url TEXT,
    last_checked_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (artist_normalized, album_normalized)
)
"""

_SERVICE_CASES = [
    ("apple_music_album", "https://music.apple.com/us/album/aluminum-tunes/1234567890"),
    ("spotify_album", "https://open.spotify.com/album/1A2GTWGt0LBTGQAyA3OKAf"),
]


@pytest_asyncio.fixture
async def pg_pool():
    try:
        pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=4)
    except Exception as e:
        pytest.skip(f"Cannot connect to test PostgreSQL: {e}")
        return
    yield pool
    await pool.close()


@pytest_asyncio.fixture
async def pg_source(pg_pool):
    """A ``PgSource`` borrowing the test pool (no-op close)."""
    return PgSource(pool=pg_pool)


@pytest_asyncio.fixture(autouse=True)
async def set_up_cache_schema(pg_pool, pg_source):
    """Reset to a clean ``lml_cache`` schema and apply the cache DDL.

    Surgical (not ``DROP SCHEMA entity CASCADE``): only the LML-owned
    ``lml_cache`` schema and the grandfathered apple-only table are touched, so
    the discogs-cache-owned ``entity.*`` identity tables in the shared test PG
    stay intact. With the old apple table dropped, the bootstrap's
    ``to_regclass`` guard sees it absent and the backfill is a no-op for the
    round-trip tests (the backfill test recreates + seeds it, then re-runs the
    idempotent bootstrap).
    """
    async with pg_pool.acquire() as conn:
        await conn.execute("DROP SCHEMA IF EXISTS lml_cache CASCADE")
        await conn.execute("DROP TABLE IF EXISTS entity.album_apple_music_lookup_cache")
        await conn.execute("CREATE SCHEMA IF NOT EXISTS entity")
    await set_up_streaming_url_cache_schema(pg_source)
    yield
    async with pg_pool.acquire() as conn:
        await conn.execute("DROP SCHEMA IF EXISTS lml_cache CASCADE")
        await conn.execute("DROP TABLE IF EXISTS entity.album_apple_music_lookup_cache")


@pytest.mark.pg
class TestSchemaBootstrap:
    @pytest.mark.asyncio
    async def test_second_boot_is_a_no_op(self, pg_source, pg_pool):
        await set_up_streaming_url_cache_schema(pg_source)
        async with pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT count(*)::int AS n FROM information_schema.tables "
                "WHERE table_schema = 'lml_cache' "
                "AND table_name = 'album_streaming_url_cache'"
            )
        assert row["n"] == 1

    @pytest.mark.asyncio
    async def test_named_check_constraint_rejects_unknown_service(self, pg_pool):
        # The CHECK must actually fire for a service outside the PR-1 set.
        with pytest.raises(asyncpg.PostgresError):
            async with pg_pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO lml_cache.album_streaming_url_cache "
                    "(service, artist_normalized, album_normalized, url) "
                    "VALUES ('deezer_album', 'x', 'y', 'https://example.test')"
                )


@pytest.mark.pg
@pytest.mark.parametrize(("service", "sample_url"), _SERVICE_CASES)
class TestRoundTrip:
    @pytest.mark.asyncio
    async def test_set_then_get_returns_cached_url(self, pg_source, service, sample_url):
        await set_cached_streaming_url(
            pg_source, service=service, artist="Stereolab", album="Aluminum Tunes", url=sample_url
        )
        result = await get_cached_streaming_url(
            pg_source, service=service, artist="Stereolab", album="Aluminum Tunes"
        )
        assert result == sample_url

    @pytest.mark.asyncio
    async def test_two_services_coexist_under_same_album_key(self, pg_source, service, sample_url):
        # The service column is part of the PK — the same (artist, album) can
        # carry independent rows per service without colliding.
        other_service = "spotify_album" if service == "apple_music_album" else "apple_music_album"
        await set_cached_streaming_url(
            pg_source, service=service, artist="Juana Molina", album="DOGA", url=sample_url
        )
        await set_cached_streaming_url(
            pg_source,
            service=other_service,
            artist="Juana Molina",
            album="DOGA",
            url="https://other.test/x",
        )
        assert (
            await get_cached_streaming_url(
                pg_source, service=service, artist="Juana Molina", album="DOGA"
            )
            == sample_url
        )
        assert (
            await get_cached_streaming_url(
                pg_source, service=other_service, artist="Juana Molina", album="DOGA"
            )
            == "https://other.test/x"
        )

    @pytest.mark.asyncio
    async def test_known_miss_past_ttl_filtered_by_sql(
        self, pg_source, pg_pool, service, sample_url
    ):
        await set_cached_streaming_url(
            pg_source, service=service, artist="Sessa", album="Estrela Acesa", url=None
        )
        now = datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC)
        past = now - timedelta(days=8)
        async with pg_pool.acquire() as conn:
            await conn.execute(
                "UPDATE lml_cache.album_streaming_url_cache SET last_checked_at = $1 "
                "WHERE service = $2 AND artist_normalized = 'sessa' "
                "AND album_normalized = 'estrela acesa'",
                past,
                service,
            )
        result = await get_cached_streaming_url(
            pg_source, service=service, artist="Sessa", album="Estrela Acesa", now=now
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_live_resolved_writes_row_to_pg(self, pg_source, pg_pool, service, sample_url):
        client = AsyncMock(spec=BaseStreamingClient)
        client.find_album_match = AsyncMock(
            return_value=SourceMatch(url=sample_url, confidence=95.0)
        )

        outcome = await resolve_streaming_url_with_cache(
            pg_source, client, service=service, artist="Chuquimamani-Condori", album="Edits"
        )

        assert outcome.url == sample_url
        assert outcome.source == "live_resolved"
        async with pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT url, last_checked_at FROM lml_cache.album_streaming_url_cache "
                "WHERE service = $1 AND artist_normalized = 'chuquimamani-condori' "
                "AND album_normalized = 'edits'",
                service,
            )
        assert row is not None
        assert row["url"] == sample_url
        assert row["last_checked_at"].tzinfo is not None

    @pytest.mark.asyncio
    async def test_upsert_updates_in_place(self, pg_source, pg_pool, service, sample_url):
        await set_cached_streaming_url(
            pg_source, service=service, artist="Cat Power", album="Moon Pix", url="https://a.test/1"
        )
        await set_cached_streaming_url(
            pg_source, service=service, artist="Cat Power", album="Moon Pix", url=sample_url
        )
        async with pg_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT url FROM lml_cache.album_streaming_url_cache "
                "WHERE service = $1 AND artist_normalized = 'cat power' "
                "AND album_normalized = 'moon pix'",
                service,
            )
        assert len(rows) == 1, "ON CONFLICT must update in place, not insert"
        assert rows[0]["url"] == sample_url

    @pytest.mark.asyncio
    async def test_normalization_symmetry(self, pg_source, pg_pool, service, sample_url):
        await set_cached_streaming_url(
            pg_source, service=service, artist="Nilüfer Yanya", album="PAINLESS", url=sample_url
        )
        result = await get_cached_streaming_url(
            pg_source, service=service, artist="Nilufer Yanya", album="painless"
        )
        assert result == sample_url


@pytest.mark.pg
class TestAppleBackfill:
    """The lifespan backfill copies the grandfathered apple-only rows into the
    new table, keyed under ``service = 'apple_music_album'``, preserving each
    row's ``last_checked_at`` (the TTL-preservation AC)."""

    @pytest.mark.asyncio
    async def test_backfill_preserves_last_checked_at(self, pg_source, pg_pool):
        # Recreate + seed the OLD apple table: one hit, one known-miss with an
        # 8-day-old timestamp. The known-miss timestamp is the load-bearing
        # assertion — a naive backfill resetting it to now() would resurrect a
        # stale miss for another 7 days.
        old_miss_ts = datetime(2026, 6, 9, 9, 0, 0, tzinfo=UTC)
        async with pg_pool.acquire() as conn:
            await conn.execute(_OLD_APPLE_TABLE_DDL)
            await conn.execute(
                "INSERT INTO entity.album_apple_music_lookup_cache "
                "(artist_normalized, album_normalized, apple_music_url, last_checked_at) "
                "VALUES ('stereolab', 'aluminum tunes', "
                "'https://music.apple.com/us/album/aluminum-tunes/1', now())"
            )
            await conn.execute(
                "INSERT INTO entity.album_apple_music_lookup_cache "
                "(artist_normalized, album_normalized, apple_music_url, last_checked_at) "
                "VALUES ('sessa', 'estrela acesa', NULL, $1)",
                old_miss_ts,
            )

        # Re-run the idempotent bootstrap — now the to_regclass guard fires and
        # the backfill copies both rows.
        await set_up_streaming_url_cache_schema(pg_source)

        async with pg_pool.acquire() as conn:
            hit = await conn.fetchrow(
                "SELECT url FROM lml_cache.album_streaming_url_cache "
                "WHERE service = 'apple_music_album' AND artist_normalized = 'stereolab' "
                "AND album_normalized = 'aluminum tunes'"
            )
            miss = await conn.fetchrow(
                "SELECT url, last_checked_at FROM lml_cache.album_streaming_url_cache "
                "WHERE service = 'apple_music_album' AND artist_normalized = 'sessa' "
                "AND album_normalized = 'estrela acesa'"
            )
        assert hit is not None
        assert hit["url"] == "https://music.apple.com/us/album/aluminum-tunes/1"
        assert miss is not None
        assert miss["url"] is None
        # The original timestamp survived — NOT reset to now().
        assert miss["last_checked_at"] == old_miss_ts

    @pytest.mark.asyncio
    async def test_backfill_does_not_clobber_existing_rows(self, pg_source, pg_pool):
        # ON CONFLICT DO NOTHING: a row already present in the new table wins
        # over the backfill source (idempotent re-runs never overwrite).
        await set_cached_streaming_url(
            pg_source,
            service="apple_music_album",
            artist="Stereolab",
            album="Aluminum Tunes",
            url="https://music.apple.com/us/album/new/2",
        )
        async with pg_pool.acquire() as conn:
            await conn.execute(_OLD_APPLE_TABLE_DDL)
            await conn.execute(
                "INSERT INTO entity.album_apple_music_lookup_cache "
                "(artist_normalized, album_normalized, apple_music_url) "
                "VALUES ('stereolab', 'aluminum tunes', "
                "'https://music.apple.com/us/album/old/1')"
            )
        await set_up_streaming_url_cache_schema(pg_source)
        async with pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT url FROM lml_cache.album_streaming_url_cache "
                "WHERE service = 'apple_music_album' AND artist_normalized = 'stereolab' "
                "AND album_normalized = 'aluminum tunes'"
            )
        assert row["url"] == "https://music.apple.com/us/album/new/2"
