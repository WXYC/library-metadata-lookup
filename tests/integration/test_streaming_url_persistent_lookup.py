"""Integration tests for the persistent streaming-URL cache (LML#573).

LML#573 generalized the Apple-only ``entity.album_apple_music_lookup_cache``
(LML#571) into the polymorphic ``lml_cache.album_streaming_url_cache`` table,
the cache module ``entity/streaming_url_cache.py``, and the read-through
resolver ``resolve_streaming_url_with_cache``. All unit coverage mocks
``PgSource``; this file is the matching ``@pytest.mark.pg`` layer that drives
the real schema, the real UPSERT, and the real TTL math against an actual
PostgreSQL connection.

Concrete production risks the unit tests cannot catch:

* The named CHECK constraint actually rejects an unknown ``service`` value.
* TIMESTAMPTZ codec drift — asyncpg returns aware ``datetime``; a naive value
  would break the SQL ``last_checked_at > $4`` comparison.
* UPSERT semantics — the composite-PK ``ON CONFLICT`` updates in place.
* ``to_match_form`` normalization parity between SELECT and UPSERT.

Run with: pytest -m pg -v tests/integration/test_streaming_url_persistent_lookup.py
"""

from __future__ import annotations

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
from tests.integration.conftest import skip_if_named_tables_populated

_SERVICE_CASES = [
    ("apple_music_album", "https://music.apple.com/us/album/aluminum-tunes/1234567890"),
    ("spotify_album", "https://open.spotify.com/album/1A2GTWGt0LBTGQAyA3OKAf"),
    ("bandcamp", "https://juanamolina.bandcamp.com/album/doga"),
]

_SERVICE_CHECK_OID = (
    "SELECT oid FROM pg_constraint "
    "WHERE conname = 'album_streaming_url_cache_service_valid' "
    "AND conrelid = 'lml_cache.album_streaming_url_cache'::regclass"
)


@pytest_asyncio.fixture
async def pg_pool(pg_pool_large):
    """Alias to the conftest ``max_size=4`` pool.

    This module's downstream fixtures and tests request ``pg_pool`` by name and
    historically got a ``max_size=4`` pool; aliasing keeps that cap without
    re-typing every request.
    """
    yield pg_pool_large


@pytest_asyncio.fixture
async def pg_source(pg_pool):
    """A ``PgSource`` borrowing the test pool (no-op close)."""
    return PgSource(pool=pg_pool)


@pytest_asyncio.fixture(autouse=True)
async def set_up_cache_schema(pg_pool, pg_source):
    """Reset just ``lml_cache.album_streaming_url_cache``, then apply the DDL.

    Surgical: drops only the one table this suite owns — not the whole
    ``lml_cache`` schema, which hosts every other LML cache (release-
    resolution, rate bucket, override, streaming catalog) — and refuses to run
    at all if that table already holds rows (a mispointed ``DATABASE_URL_TEST``
    at the shared discogs-cache PG would otherwise drop real cached URLs).
    """
    async with pg_pool.acquire() as conn:
        await skip_if_named_tables_populated(conn, (("lml_cache", "album_streaming_url_cache"),))
        await conn.execute("DROP TABLE IF EXISTS lml_cache.album_streaming_url_cache")
    await set_up_streaming_url_cache_schema(pg_source)
    yield
    async with pg_pool.acquire() as conn:
        await conn.execute("DROP TABLE IF EXISTS lml_cache.album_streaming_url_cache")


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
        # The CHECK must actually fire for a service outside the shipped set.
        with pytest.raises(asyncpg.PostgresError):
            async with pg_pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO lml_cache.album_streaming_url_cache "
                    "(service, artist_normalized, album_normalized, url) "
                    "VALUES ('deezer_album', 'x', 'y', 'https://example.test')"
                )

    @pytest.mark.asyncio
    async def test_alter_widens_check_on_preexisting_table(self, pg_pool, pg_source):
        # The production migration path: a prod table frozen at the pre-bandcamp
        # service set must pick up 'bandcamp' on the next boot. CREATE TABLE IF
        # NOT EXISTS is a no-op on the existing table; the idempotent ALTER is
        # what widens the named CHECK in place.
        async with pg_pool.acquire() as conn:
            # The autouse fixture already veto-checked and dropped this table;
            # drop again here only to replace it with the frozen pre-bandcamp
            # shape (never the whole schema — sibling caches live there too).
            await conn.execute("DROP TABLE IF EXISTS lml_cache.album_streaming_url_cache")
            await conn.execute("CREATE SCHEMA IF NOT EXISTS lml_cache")
            await conn.execute(
                "CREATE TABLE lml_cache.album_streaming_url_cache ("
                "  service TEXT NOT NULL,"
                "  artist_normalized TEXT NOT NULL,"
                "  album_normalized TEXT NOT NULL,"
                "  url TEXT,"
                "  last_checked_at TIMESTAMPTZ NOT NULL DEFAULT now(),"
                "  PRIMARY KEY (service, artist_normalized, album_normalized),"
                "  CONSTRAINT album_streaming_url_cache_service_valid CHECK ("
                "    service IN ('apple_music_album', 'spotify_album')"
                "  )"
                ")"
            )

        # Pre-ALTER: bandcamp violates the frozen constraint.
        with pytest.raises(asyncpg.PostgresError):
            async with pg_pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO lml_cache.album_streaming_url_cache "
                    "(service, artist_normalized, album_normalized, url) "
                    "VALUES ('bandcamp', 'x', 'y', 'https://x.bandcamp.com/album/y')"
                )

        # Re-boot: the ALTER widens the CHECK so bandcamp now inserts.
        await set_up_streaming_url_cache_schema(pg_source)
        async with pg_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO lml_cache.album_streaming_url_cache "
                "(service, artist_normalized, album_normalized, url) "
                "VALUES ('bandcamp', 'x', 'y', 'https://x.bandcamp.com/album/y')"
            )
            row = await conn.fetchrow(
                "SELECT url FROM lml_cache.album_streaming_url_cache WHERE service = 'bandcamp'"
            )
        assert row["url"] == "https://x.bandcamp.com/album/y"

    @pytest.mark.asyncio
    async def test_second_boot_leaves_the_check_constraint_untouched(self, pg_pool, pg_source):
        # Re-booting over an up-to-date table must not DROP+ADD the CHECK:
        # the constraint OID is the tell (a rewrite allocates a new one), and
        # a rewrite costs an ACCESS EXCLUSIVE lock plus a full-table
        # re-validation scan on every production boot.
        async with pg_pool.acquire() as conn:
            oid_before = await conn.fetchval(_SERVICE_CHECK_OID)

        await set_up_streaming_url_cache_schema(pg_source)

        async with pg_pool.acquire() as conn:
            oid_after = await conn.fetchval(_SERVICE_CHECK_OID)
        assert oid_before is not None
        assert oid_after == oid_before

    @pytest.mark.asyncio
    async def test_boot_preserves_a_preexisting_superset_constraint(self, pg_pool, pg_source):
        # A deployed table may admit services this code version doesn't ship
        # (a newer deploy widened it, then this build rolled back). Boot must
        # merge, never narrow: narrowing re-validates and would abort on any
        # row using the extra service, taking the whole bootstrap transaction
        # down with it. Simulate by hand-widening the constraint to a strict
        # superset and seeding a row using the extra service.
        async with pg_pool.acquire() as conn:
            await conn.execute(
                "ALTER TABLE lml_cache.album_streaming_url_cache "
                "DROP CONSTRAINT album_streaming_url_cache_service_valid, "
                "ADD CONSTRAINT album_streaming_url_cache_service_valid CHECK ("
                "  service IN ('apple_music_album', 'spotify_album', 'bandcamp', 'deezer_album')"
                ")"
            )
            await conn.execute(
                "INSERT INTO lml_cache.album_streaming_url_cache "
                "(service, artist_normalized, album_normalized, url) "
                "VALUES ('deezer_album', 'x', 'y', 'https://example.test/deezer')"
            )
            oid_before = await conn.fetchval(_SERVICE_CHECK_OID)

        await set_up_streaming_url_cache_schema(pg_source)

        async with pg_pool.acquire() as conn:
            # Nothing to add (existing set is a superset), so the constraint
            # must be untouched — and the collected deezer row intact.
            oid_after = await conn.fetchval(_SERVICE_CHECK_OID)
            preserved = await conn.fetchval(
                "SELECT count(*) FROM lml_cache.album_streaming_url_cache "
                "WHERE service = 'deezer_album'"
            )
        assert oid_after == oid_before
        assert preserved == 1


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
