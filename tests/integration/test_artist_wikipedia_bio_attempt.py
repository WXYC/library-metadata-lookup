"""Integration tests for the Wikipedia bio program's write-nothing attempt record (LML#1192 review round 4, P0-8).

Mirrors ``tests/integration/test_artist_wikipedia_bio.py``. All unit coverage
mocks ``PgSource``; this file is the matching ``@pytest.mark.pg`` layer
driving the real schema and the real UPSERT (repeated attempts refresh
``attempted_at`` in place rather than growing a second row) against an
actual PostgreSQL connection.

Run with: pytest -m pg -v tests/integration/test_artist_wikipedia_bio_attempt.py
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from entity.artist_wikipedia_bio_attempt import (
    record_artist_wikipedia_bio_attempt,
    set_up_artist_wikipedia_bio_attempt_schema,
)
from tests.integration.conftest import skip_if_named_tables_populated


@pytest_asyncio.fixture(autouse=True)
async def set_up_attempt_schema(pg_pool, pg_source):
    """Reset just ``lml_cache.artist_wikipedia_bio_attempt``, then apply the DDL.

    Surgical: drops only the one table this suite owns, and refuses to run
    at all if that table already holds rows (a mispointed
    ``DATABASE_URL_TEST`` at the shared discogs-cache PG would otherwise
    drop real attempt-tracking rows).
    """
    async with pg_pool.acquire() as conn:
        await skip_if_named_tables_populated(conn, (("lml_cache", "artist_wikipedia_bio_attempt"),))
        await conn.execute("DROP TABLE IF EXISTS lml_cache.artist_wikipedia_bio_attempt")
    await set_up_artist_wikipedia_bio_attempt_schema(pg_source)
    yield
    async with pg_pool.acquire() as conn:
        await conn.execute("DROP TABLE IF EXISTS lml_cache.artist_wikipedia_bio_attempt")


@pytest.mark.pg
class TestSchemaBootstrap:
    @pytest.mark.asyncio
    async def test_second_boot_is_a_no_op(self, pg_source, pg_pool):
        await set_up_artist_wikipedia_bio_attempt_schema(pg_source)
        async with pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT count(*)::int AS n FROM information_schema.tables "
                "WHERE table_schema = 'lml_cache' AND table_name = 'artist_wikipedia_bio_attempt'"
            )
        assert row["n"] == 1


@pytest.mark.pg
class TestRecordArtistWikipediaBioAttempt:
    @pytest.mark.asyncio
    async def test_records_a_row(self, pg_source, pg_pool):
        await record_artist_wikipedia_bio_attempt(
            pg_source, discogs_artist_id=99, outcome="fetch_error"
        )
        async with pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT outcome, attempted_at FROM lml_cache.artist_wikipedia_bio_attempt "
                "WHERE discogs_artist_id = 99"
            )
        assert row is not None
        assert row["outcome"] == "fetch_error"
        assert row["attempted_at"] is not None

    @pytest.mark.asyncio
    async def test_a_repeated_attempt_upserts_in_place_not_a_second_row(self, pg_source, pg_pool):
        # LML#1192 review round 4, P0-8: a chronically-failing artist must
        # not grow an unbounded number of attempt rows across sessions --
        # ON CONFLICT (discogs_artist_id) DO UPDATE keeps it to one.
        await record_artist_wikipedia_bio_attempt(
            pg_source, discogs_artist_id=99, outcome="fetch_error"
        )
        await record_artist_wikipedia_bio_attempt(
            pg_source, discogs_artist_id=99, outcome="unexpected_error"
        )
        async with pg_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT outcome FROM lml_cache.artist_wikipedia_bio_attempt "
                "WHERE discogs_artist_id = 99"
            )
        assert len(rows) == 1
        assert rows[0]["outcome"] == "unexpected_error"

    @pytest.mark.asyncio
    async def test_a_repeated_attempt_advances_attempted_at(self, pg_source, pg_pool):
        await record_artist_wikipedia_bio_attempt(
            pg_source, discogs_artist_id=99, outcome="fetch_error"
        )
        async with pg_pool.acquire() as conn:
            first = await conn.fetchval(
                "SELECT attempted_at FROM lml_cache.artist_wikipedia_bio_attempt "
                "WHERE discogs_artist_id = 99"
            )
            # Force a measurable gap -- CI hosts are fast enough that two
            # back-to-back now() calls can land in the same microsecond.
            await conn.execute("SELECT pg_sleep(0.01)")
        await record_artist_wikipedia_bio_attempt(
            pg_source, discogs_artist_id=99, outcome="fetch_error"
        )
        async with pg_pool.acquire() as conn:
            second = await conn.fetchval(
                "SELECT attempted_at FROM lml_cache.artist_wikipedia_bio_attempt "
                "WHERE discogs_artist_id = 99"
            )
        assert second > first
