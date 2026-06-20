"""Integration tests for the positive release-resolution cache (LML#632).

Mirrors ``tests/integration/test_streaming_url_persistent_lookup.py``. LML#632
adds the LML-owned ``lml_cache.release_resolution_cache`` table plus the cache
module ``entity/release_resolution_cache.py`` (read/write helpers). All unit
coverage mocks ``PgSource``; this file is the matching ``@pytest.mark.pg`` layer
driving the real schema, the real UPSERT, and the real dual-TTL math against an
actual PostgreSQL connection.

Concrete production risks the unit tests cannot catch:

* The named CHECK constraint rejects a non-positive ``release_id``.
* TIMESTAMPTZ codec drift — asyncpg returns aware ``datetime``; a naive value
  would break the ``resolved_at`` cutoff comparison.
* UPSERT semantics — the composite-PK ``ON CONFLICT`` updates in place.
* ``to_match_form`` normalization parity between SELECT and UPSERT.
* The dual TTL: a positive hit reads stale after 90 days; a miss reads fresh
  inside 7 days and stale after.

Run with: pytest -m pg -v tests/integration/test_release_resolution_cache.py
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import asyncpg
import pytest
import pytest_asyncio

from entity.release_resolution_cache import (
    get_cached_release_id,
    set_cached_release_id,
    set_up_release_resolution_cache_schema,
)
from entity.sources import PgSource


@pytest_asyncio.fixture
async def pg_source(pg_pool):
    """A ``PgSource`` borrowing the test pool (no-op close)."""
    return PgSource(pool=pg_pool)


@pytest_asyncio.fixture(autouse=True)
async def set_up_cache_schema(pg_pool, pg_source):
    """Reset to a clean ``lml_cache`` schema and apply the cache DDL.

    Surgical (not ``DROP SCHEMA entity CASCADE``): only the LML-owned
    ``lml_cache`` schema is touched, so the discogs-cache-owned ``entity.*``
    identity tables in the shared test PG stay intact. ``DROP SCHEMA ...
    CASCADE`` also drops the sibling ``album_streaming_url_cache`` if present;
    both are LML-owned application caches and are re-bootstrapped on demand by
    their own suites, so this is safe.
    """
    async with pg_pool.acquire() as conn:
        await conn.execute("DROP SCHEMA IF EXISTS lml_cache CASCADE")
    await set_up_release_resolution_cache_schema(pg_source)
    yield
    async with pg_pool.acquire() as conn:
        await conn.execute("DROP SCHEMA IF EXISTS lml_cache CASCADE")


@pytest.mark.pg
class TestSchemaBootstrap:
    @pytest.mark.asyncio
    async def test_second_boot_is_a_no_op(self, pg_source, pg_pool):
        await set_up_release_resolution_cache_schema(pg_source)
        async with pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT count(*)::int AS n FROM information_schema.tables "
                "WHERE table_schema = 'lml_cache' "
                "AND table_name = 'release_resolution_cache'"
            )
        assert row["n"] == 1

    @pytest.mark.asyncio
    async def test_named_check_constraint_rejects_non_positive_release_id(self, pg_pool):
        # release_id must be NULL (a miss) or strictly positive.
        with pytest.raises(asyncpg.PostgresError):
            async with pg_pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO lml_cache.release_resolution_cache "
                    "(artist_normalized, title_normalized, is_track, release_id) "
                    "VALUES ('x', 'y', false, 0)"
                )


@pytest.mark.pg
class TestRoundTrip:
    @pytest.mark.asyncio
    async def test_set_then_get_returns_release_id(self, pg_source):
        await set_cached_release_id(
            pg_source, artist="Juana Molina", title="DOGA", is_track=False, release_id=12345
        )
        result = await get_cached_release_id(
            pg_source, artist="Juana Molina", title="DOGA", is_track=False
        )
        assert result.was_present is True
        assert result.release_id == 12345

    @pytest.mark.asyncio
    async def test_track_and_album_channels_coexist_under_same_key(self, pg_source):
        # ``is_track`` is part of the PK — the same (artist, title) can carry
        # independent rows for the track and album channels without colliding.
        await set_cached_release_id(
            pg_source, artist="Jessica Pratt", title="Back, Baby", is_track=True, release_id=111
        )
        await set_cached_release_id(
            pg_source, artist="Jessica Pratt", title="Back, Baby", is_track=False, release_id=222
        )
        track = await get_cached_release_id(
            pg_source, artist="Jessica Pratt", title="Back, Baby", is_track=True
        )
        album = await get_cached_release_id(
            pg_source, artist="Jessica Pratt", title="Back, Baby", is_track=False
        )
        assert track.release_id == 111
        assert album.release_id == 222

    @pytest.mark.asyncio
    async def test_upsert_updates_in_place(self, pg_source, pg_pool):
        await set_cached_release_id(
            pg_source, artist="Cat Power", title="Moon Pix", is_track=False, release_id=1
        )
        await set_cached_release_id(
            pg_source, artist="Cat Power", title="Moon Pix", is_track=False, release_id=999
        )
        async with pg_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT release_id FROM lml_cache.release_resolution_cache "
                "WHERE artist_normalized = 'cat power' AND title_normalized = 'moon pix' "
                "AND is_track = false"
            )
        assert len(rows) == 1, "ON CONFLICT must update in place, not insert"
        assert rows[0]["release_id"] == 999

    @pytest.mark.asyncio
    async def test_normalization_symmetry(self, pg_source):
        await set_cached_release_id(
            pg_source, artist="Nilüfer Yanya", title="PAINLESS", is_track=False, release_id=42
        )
        result = await get_cached_release_id(
            pg_source, artist="Nilufer Yanya", title="painless", is_track=False
        )
        assert result.release_id == 42


@pytest.mark.pg
class TestDualTTL:
    @pytest.mark.asyncio
    async def test_absent_entry_reads_not_present(self, pg_source):
        # Nothing written: an absent entry is the "run the live probe" shape.
        result = await get_cached_release_id(
            pg_source, artist="Sessa", title="Estrela Acesa", is_track=False
        )
        assert result.was_present is False
        assert result.release_id is None

    @pytest.mark.asyncio
    async def test_known_miss_within_7_days_is_returned(self, pg_source):
        await set_cached_release_id(
            pg_source, artist="Sessa", title="Estrela Acesa", is_track=False, release_id=None
        )
        # A fresh miss is a present row whose release_id is None: the caller
        # honors it (skip the probe), distinct from an absent entry.
        result = await get_cached_release_id(
            pg_source, artist="Sessa", title="Estrela Acesa", is_track=False
        )
        assert result.was_present is True
        assert result.release_id is None

    @pytest.mark.asyncio
    async def test_known_miss_past_7_days_reads_stale(self, pg_source, pg_pool):
        await set_cached_release_id(
            pg_source, artist="Sessa", title="Estrela Acesa", is_track=False, release_id=None
        )
        now = datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC)
        past = now - timedelta(days=8)
        async with pg_pool.acquire() as conn:
            await conn.execute(
                "UPDATE lml_cache.release_resolution_cache SET resolved_at = $1 "
                "WHERE artist_normalized = 'sessa' AND title_normalized = 'estrela acesa' "
                "AND is_track = false",
                past,
            )
        # Stale miss is filtered by the SQL: reads as absent (re-probe).
        result = await get_cached_release_id(
            pg_source, artist="Sessa", title="Estrela Acesa", is_track=False, now=now
        )
        assert result.was_present is False
        assert result.release_id is None

    @pytest.mark.asyncio
    async def test_positive_hit_within_90_days_is_returned(self, pg_source, pg_pool):
        await set_cached_release_id(
            pg_source, artist="Stereolab", title="Aluminum Tunes", is_track=False, release_id=7
        )
        now = datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC)
        recent = now - timedelta(days=80)
        async with pg_pool.acquire() as conn:
            await conn.execute(
                "UPDATE lml_cache.release_resolution_cache SET resolved_at = $1 "
                "WHERE artist_normalized = 'stereolab' AND title_normalized = 'aluminum tunes' "
                "AND is_track = false",
                recent,
            )
        result = await get_cached_release_id(
            pg_source, artist="Stereolab", title="Aluminum Tunes", is_track=False, now=now
        )
        assert result.was_present is True
        assert result.release_id == 7

    @pytest.mark.asyncio
    async def test_positive_hit_past_90_days_reads_stale(self, pg_source, pg_pool):
        await set_cached_release_id(
            pg_source, artist="Stereolab", title="Aluminum Tunes", is_track=False, release_id=7
        )
        now = datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC)
        ancient = now - timedelta(days=91)
        async with pg_pool.acquire() as conn:
            await conn.execute(
                "UPDATE lml_cache.release_resolution_cache SET resolved_at = $1 "
                "WHERE artist_normalized = 'stereolab' AND title_normalized = 'aluminum tunes' "
                "AND is_track = false",
                ancient,
            )
        # A positive hit older than 90 days is a derived inference that may have
        # gone stale (Discogs releases merge/delete) — filtered by the SQL, so
        # the caller sees the same "absent" shape and re-resolves.
        result = await get_cached_release_id(
            pg_source, artist="Stereolab", title="Aluminum Tunes", is_track=False, now=now
        )
        assert result.was_present is False
        assert result.release_id is None
