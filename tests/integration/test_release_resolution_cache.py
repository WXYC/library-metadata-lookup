"""Integration tests for the positive release-resolution cache (LML#632).

Mirrors ``tests/integration/test_streaming_url_persistent_lookup.py``. LML#632
adds the LML-owned ``lml_cache.release_resolution_cache`` table plus the cache
module ``entity/release_resolution_cache.py`` (read/write helpers). All unit
coverage mocks ``PgSource``; this file is the matching ``@pytest.mark.pg`` layer
driving the real schema, the real UPSERT, and the real triple-TTL math against an
actual PostgreSQL connection.

Concrete production risks the unit tests cannot catch:

* The named CHECK constraint rejects a non-positive ``release_id``.
* TIMESTAMPTZ codec drift — asyncpg returns aware ``datetime``; a naive value
  would break the ``resolved_at`` cutoff comparison.
* UPSERT semantics — the composite-PK ``ON CONFLICT`` updates in place.
* ``to_match_form`` normalization parity between SELECT and UPSERT.
* The triple TTL (LML#824): a positive hit reads stale after 90 days; a normal
  miss reads fresh inside 7 days and stale after; a crowd-out miss reads fresh
  inside 1 hour and stale after, on its own shorter cutoff.
* The additive ``crowd_out`` column ALTER backfills a pre-#824 table in place.

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
from tests.integration.conftest import skip_if_named_tables_populated


@pytest_asyncio.fixture
async def pg_source(pg_pool):
    """A ``PgSource`` borrowing the test pool (no-op close)."""
    return PgSource(pool=pg_pool)


@pytest_asyncio.fixture(autouse=True)
async def set_up_cache_schema(pg_pool, pg_source):
    """Reset just ``lml_cache.release_resolution_cache``, then apply the DDL.

    Surgical: drops only the one table this suite owns — not the whole
    ``lml_cache`` schema, which hosts every other LML cache (streaming-URL,
    rate bucket, override, streaming catalog) — and refuses to run at all if
    that table already holds rows (a mispointed ``DATABASE_URL_TEST`` at the
    shared discogs-cache PG would otherwise drop real resolution results).
    """
    async with pg_pool.acquire() as conn:
        await skip_if_named_tables_populated(conn, (("lml_cache", "release_resolution_cache"),))
        await conn.execute("DROP TABLE IF EXISTS lml_cache.release_resolution_cache")
    await set_up_release_resolution_cache_schema(pg_source)
    yield
    async with pg_pool.acquire() as conn:
        await conn.execute("DROP TABLE IF EXISTS lml_cache.release_resolution_cache")


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


@pytest.mark.pg
class TestCrowdOutMissTTL:
    """LML#824: a crowd-out empty pins a short-TTL miss (``crowd_out=true``), so it
    self-heals within the hour instead of being suppressed for the full 7 days —
    while a genuine exhausted miss (``crowd_out=false``) keeps the 7-day TTL."""

    @pytest.mark.asyncio
    async def test_crowd_out_miss_within_1h_is_returned(self, pg_source):
        await set_cached_release_id(
            pg_source,
            artist="Juana Molina",
            title="Paraguaya",
            is_track=True,
            release_id=None,
            crowd_out=True,
        )
        # A fresh crowd-out miss is honored exactly like any known miss inside its
        # TTL: present row, null release_id — the caller skips the live probe.
        result = await get_cached_release_id(
            pg_source, artist="Juana Molina", title="Paraguaya", is_track=True
        )
        assert result.was_present is True
        assert result.release_id is None

    @pytest.mark.asyncio
    async def test_crowd_out_miss_past_1h_reads_stale(self, pg_source, pg_pool):
        await set_cached_release_id(
            pg_source,
            artist="Juana Molina",
            title="Paraguaya",
            is_track=True,
            release_id=None,
            crowd_out=True,
        )
        now = datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC)
        past = now - timedelta(hours=2)
        async with pg_pool.acquire() as conn:
            await conn.execute(
                "UPDATE lml_cache.release_resolution_cache SET resolved_at = $1 "
                "WHERE artist_normalized = 'juana molina' AND title_normalized = 'paraguaya' "
                "AND is_track = true",
                past,
            )
        # 2h old crowd-out miss is past the 1h crowd-out TTL: filtered by the SQL,
        # reads as absent so the caller re-probes (the self-heal).
        result = await get_cached_release_id(
            pg_source, artist="Juana Molina", title="Paraguaya", is_track=True, now=now
        )
        assert result.was_present is False
        assert result.release_id is None

    @pytest.mark.asyncio
    async def test_normal_miss_same_age_as_a_stale_crowd_out_is_still_fresh(
        self, pg_source, pg_pool
    ):
        # The discriminating case: at exactly the same 2h age, a crowd-out miss
        # (short TTL) is stale but a normal exhausted miss (7-day TTL) is fresh.
        # Proves the marker column selects a distinct cutoff, not just a shared one.
        await set_cached_release_id(
            pg_source,
            artist="Sessa",
            title="Sonho Da Maçã",
            is_track=True,
            release_id=None,
            crowd_out=False,
        )
        now = datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC)
        past = now - timedelta(hours=2)
        async with pg_pool.acquire() as conn:
            await conn.execute(
                "UPDATE lml_cache.release_resolution_cache SET resolved_at = $1 "
                "WHERE artist_normalized = 'sessa' AND title_normalized = 'sonho da maca' "
                "AND is_track = true",
                past,
            )
        result = await get_cached_release_id(
            pg_source, artist="Sessa", title="Sonho Da Maçã", is_track=True, now=now
        )
        assert result.was_present is True
        assert result.release_id is None

    @pytest.mark.asyncio
    async def test_positive_reresolve_clears_the_crowd_out_marker(self, pg_source, pg_pool):
        # A crowd-out miss that later resolves must have its marker cleared, so it
        # ages on the 90-day positive TTL, not the 1h crowd-out TTL.
        key = ("chuquimamani-condori", "call your name", True)
        await set_cached_release_id(
            pg_source,
            artist="Chuquimamani-Condori",
            title="Call Your Name",
            is_track=True,
            release_id=None,
            crowd_out=True,
        )
        await set_cached_release_id(
            pg_source,
            artist="Chuquimamani-Condori",
            title="Call Your Name",
            is_track=True,
            release_id=555,
        )
        async with pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT release_id, crowd_out FROM lml_cache.release_resolution_cache "
                "WHERE artist_normalized = $1 AND title_normalized = $2 AND is_track = $3",
                *key,
            )
        assert row["release_id"] == 555
        assert row["crowd_out"] is False

    @pytest.mark.asyncio
    async def test_positive_write_is_never_marked_crowd_out(self, pg_source, pg_pool):
        # crowd_out is a miss-only marker: passing crowd_out=True alongside a real
        # release_id must be coerced to false (a positive is never a crowd-out).
        await set_cached_release_id(
            pg_source,
            artist="Duke Ellington & John Coltrane",
            title="In a Sentimental Mood",
            is_track=True,
            release_id=777,
            crowd_out=True,
        )
        async with pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT release_id, crowd_out FROM lml_cache.release_resolution_cache "
                "WHERE artist_normalized = 'duke ellington & john coltrane' "
                "AND title_normalized = 'in a sentimental mood' AND is_track = true"
            )
        assert row["release_id"] == 777
        assert row["crowd_out"] is False

    @pytest.mark.asyncio
    async def test_alter_backfills_pre_existing_rows_to_false(self, pg_source, pg_pool):
        # The prod-upgrade path: an existing table (rows written before #824) gets
        # the crowd_out column added in place with DEFAULT false, so legacy misses
        # keep the 7-day TTL rather than being reclassified as short-lived.
        await set_cached_release_id(
            pg_source,
            artist="Stereolab",
            title="Metronomic Underground",
            is_track=True,
            release_id=None,
        )
        async with pg_pool.acquire() as conn:
            await conn.execute(
                "ALTER TABLE lml_cache.release_resolution_cache DROP COLUMN crowd_out"
            )
        # Re-running the bootstrap issues the idempotent ALTER that re-adds it.
        await set_up_release_resolution_cache_schema(pg_source)
        async with pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT crowd_out FROM lml_cache.release_resolution_cache "
                "WHERE artist_normalized = 'stereolab' "
                "AND title_normalized = 'metronomic underground' AND is_track = true"
            )
        assert row["crowd_out"] is False
