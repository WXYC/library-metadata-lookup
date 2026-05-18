"""Integration tests for `DiscogsCacheService.lookup_negative_hit` /
`record_lookup_negative` against a real PostgreSQL.

The unit tests in `tests/unit/test_cache_service.py` mock the asyncpg pool;
this tier exercises the round-trip across an actual `lookup_negative` table
so a divergence between the migration (in `WXYC/discogs-etl`) and the LML
read/write path can be caught before it ships.

The table is created inline rather than via alembic because the alembic
migration ships separately in `WXYC/discogs-etl`. Schema mirrors
`alembic/versions/0006_lookup_negative.py` verbatim — if the discogs-etl
migration ever diverges, fix it here and there in the same PR. Per the
A4 ticket (`WXYC/library-metadata-lookup#341`).

Run with: pytest -m pg -v
"""

from __future__ import annotations

import os

import asyncpg
import pytest
import pytest_asyncio

from discogs.cache_service import DiscogsCacheService

DATABASE_URL = os.getenv(
    "DATABASE_URL_TEST",
    "postgresql://discogs:discogs@localhost:5433/discogs",
)


@pytest_asyncio.fixture
async def pg_pool():
    try:
        pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=3)
    except Exception as e:
        pytest.skip(f"Cannot connect to test PostgreSQL: {e}")
        return
    yield pool
    await pool.close()


@pytest_asyncio.fixture(autouse=True)
async def set_up_lookup_negative_table(pg_pool):
    """Create a fresh `lookup_negative` table for each test."""
    async with pg_pool.acquire() as conn:
        await conn.execute("DROP TABLE IF EXISTS lookup_negative CASCADE")
        await conn.execute(
            """
            CREATE TABLE lookup_negative (
                key_hash          bytea PRIMARY KEY,
                artist            text,
                track             text,
                artist_as_keyword boolean,
                attempted_at      timestamptz NOT NULL DEFAULT now(),
                ttl_seconds       integer NOT NULL DEFAULT 604800
            )
            """
        )
        await conn.execute(
            "CREATE INDEX idx_lookup_negative_attempted_at ON lookup_negative(attempted_at)"
        )
    yield
    async with pg_pool.acquire() as conn:
        await conn.execute("DROP TABLE IF EXISTS lookup_negative CASCADE")


@pytest.mark.pg
@pytest.mark.asyncio
async def test_record_then_hit_round_trip(pg_pool):
    """The classic happy path: write a negative verdict, then read it back."""
    cache = DiscogsCacheService(pg_pool)

    # Initial state: no entry, no hit.
    assert await cache.lookup_negative_hit("Stereolab", "Made-up Track", False) is False

    # Write the negative verdict.
    await cache.record_lookup_negative("Stereolab", "Made-up Track", False)

    # Second look: hit.
    assert await cache.lookup_negative_hit("Stereolab", "Made-up Track", False) is True


@pytest.mark.pg
@pytest.mark.asyncio
async def test_expired_entry_returns_miss(pg_pool):
    """A row whose `attempted_at + ttl_seconds * interval '1 second'` has
    elapsed must NOT count as a hit. Verifies the TTL gate in the WHERE clause."""
    cache = DiscogsCacheService(pg_pool)

    # Write with a 1-second TTL...
    await cache.record_lookup_negative("Cat Power", "Imaginary Song", False, ttl_seconds=1)
    # ...then push attempted_at back two seconds so the row is past TTL.
    async with pg_pool.acquire() as conn:
        await conn.execute("UPDATE lookup_negative SET attempted_at = now() - interval '2 seconds'")

    assert await cache.lookup_negative_hit("Cat Power", "Imaginary Song", False) is False


@pytest.mark.pg
@pytest.mark.asyncio
async def test_artist_as_keyword_dimension_is_independent(pg_pool):
    """A negative entry written with `artist_as_keyword=False` does NOT hit
    a lookup with `artist_as_keyword=True`. The two API call shapes have
    independent negative caches."""
    cache = DiscogsCacheService(pg_pool)
    await cache.record_lookup_negative("Stereolab", "Fuses", False)

    assert await cache.lookup_negative_hit("Stereolab", "Fuses", False) is True
    assert await cache.lookup_negative_hit("Stereolab", "Fuses", True) is False


@pytest.mark.pg
@pytest.mark.asyncio
async def test_diacritic_and_case_normalization_collapses_to_one_key(pg_pool):
    """Two queries that differ only by diacritics/case must share one entry —
    the cache key normalizes through `to_match_form` (A5/LML#272 semantics)."""
    cache = DiscogsCacheService(pg_pool)

    # Write with canonical casing + diacritics.
    await cache.record_lookup_negative("Nilüfer Yanya", "Heavyweight Champion", False)

    # ASCII / lowercase variant must hit the same row.
    assert await cache.lookup_negative_hit("nilufer yanya", "HEAVYWEIGHT CHAMPION", False) is True

    # And the row count is 1, not 2.
    async with pg_pool.acquire() as conn:
        count = await conn.fetchval("SELECT COUNT(*) FROM lookup_negative")
        assert count == 1


@pytest.mark.pg
@pytest.mark.asyncio
async def test_repeated_record_refreshes_attempted_at(pg_pool):
    """A second `record_lookup_negative` for the same key MUST refresh
    `attempted_at` (ON CONFLICT DO UPDATE). Without that, a steady stream
    of `record_*` calls would keep stamping rows that are already there
    but never extend their TTL — defeating the cross-deploy refresh."""
    cache = DiscogsCacheService(pg_pool)

    await cache.record_lookup_negative("Juana Molina", "Made-up Track", False)
    async with pg_pool.acquire() as conn:
        first_stamp = await conn.fetchval("SELECT attempted_at FROM lookup_negative LIMIT 1")

    # Re-write the same key.
    await cache.record_lookup_negative("Juana Molina", "Made-up Track", False)
    async with pg_pool.acquire() as conn:
        second_stamp = await conn.fetchval("SELECT attempted_at FROM lookup_negative LIMIT 1")
        # Still exactly one row, not two.
        count = await conn.fetchval("SELECT COUNT(*) FROM lookup_negative")

    assert count == 1
    assert second_stamp >= first_stamp


@pytest.mark.pg
@pytest.mark.asyncio
async def test_null_artist_supported(pg_pool):
    """LML's `q=` keyword path may pass artist=None. The key hash must
    still be deterministic, and the row writes / reads cleanly."""
    cache = DiscogsCacheService(pg_pool)

    await cache.record_lookup_negative(None, "Anonymous Track", False)

    assert await cache.lookup_negative_hit(None, "Anonymous Track", False) is True
    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT artist, track FROM lookup_negative LIMIT 1")
        assert row["artist"] is None
        assert row["track"] == "Anonymous Track"
