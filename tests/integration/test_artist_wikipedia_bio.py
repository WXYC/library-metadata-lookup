"""Integration tests for the artist Wikipedia bio cache (LML#513/#1192).

Mirrors ``tests/integration/test_release_resolution_cache.py``. All unit
coverage mocks ``PgSource``; this file is the matching ``@pytest.mark.pg``
layer driving the real schema, the real UPSERT, the real dual-TTL math, and
the self-healing URL-mismatch predicate against an actual PostgreSQL
connection.

Concrete production risks the unit tests cannot catch:

* TIMESTAMPTZ codec drift -- asyncpg returns aware ``datetime``; a naive
  value would break the ``fetched_at`` cutoff comparison.
* UPSERT semantics -- the single-column-PK ``ON CONFLICT`` updates in place,
  including ``wikipedia_url`` itself (the self-healing write path).
* The self-healing read predicate: a row stored under a DIFFERENT
  ``wikipedia_url`` than the caller's current pick is invisible to
  ``get_cached_artist_wikipedia_bio``, regardless of freshness.
* The dual TTL: a positive hit reads stale after 30 days; a negative hit
  reads stale after 7 days, on its own shorter cutoff.

Run with: pytest -m pg -v tests/integration/test_artist_wikipedia_bio.py
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

from entity.artist_wikipedia_bio import (
    get_cached_artist_wikipedia_bio,
    set_cached_artist_wikipedia_bio,
    set_up_artist_wikipedia_bio_schema,
    touch_artist_wikipedia_bio_last_checked_at,
)
from tests.integration.conftest import skip_if_named_tables_populated

_URL = "https://en.wikipedia.org/wiki/Stereolab"


@pytest_asyncio.fixture(autouse=True)
async def set_up_cache_schema(pg_pool, pg_source):
    """Reset just ``lml_cache.artist_wikipedia_bio``, then apply the DDL.

    Surgical: drops only the one table this suite owns, and refuses to run
    at all if that table already holds rows (a mispointed
    ``DATABASE_URL_TEST`` at the shared discogs-cache PG would otherwise
    drop real bio-cache results).
    """
    async with pg_pool.acquire() as conn:
        await skip_if_named_tables_populated(conn, (("lml_cache", "artist_wikipedia_bio"),))
        await conn.execute("DROP TABLE IF EXISTS lml_cache.artist_wikipedia_bio")
    await set_up_artist_wikipedia_bio_schema(pg_source)
    yield
    async with pg_pool.acquire() as conn:
        await conn.execute("DROP TABLE IF EXISTS lml_cache.artist_wikipedia_bio")


@pytest.mark.pg
class TestSchemaBootstrap:
    @pytest.mark.asyncio
    async def test_second_boot_is_a_no_op(self, pg_source, pg_pool):
        await set_up_artist_wikipedia_bio_schema(pg_source)
        async with pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT count(*)::int AS n FROM information_schema.tables "
                "WHERE table_schema = 'lml_cache' AND table_name = 'artist_wikipedia_bio'"
            )
        assert row["n"] == 1


@pytest.mark.pg
class TestRoundTrip:
    @pytest.mark.asyncio
    async def test_set_then_get_returns_extract(self, pg_source):
        await set_cached_artist_wikipedia_bio(
            pg_source,
            discogs_artist_id=99,
            wikipedia_url=_URL,
            slug_score=97.6,
            lang="en",
            extract="Stereolab are a band.",
        )
        result = await get_cached_artist_wikipedia_bio(
            pg_source, discogs_artist_id=99, wikipedia_url=_URL
        )
        assert result.was_present is True
        assert result.value == "Stereolab are a band."

    @pytest.mark.asyncio
    async def test_negative_result_round_trips_as_present_with_none_value(self, pg_source):
        await set_cached_artist_wikipedia_bio(
            pg_source,
            discogs_artist_id=99,
            wikipedia_url=_URL,
            slug_score=85.0,
            lang="en",
            extract=None,
        )
        result = await get_cached_artist_wikipedia_bio(
            pg_source, discogs_artist_id=99, wikipedia_url=_URL
        )
        assert result.was_present is True
        assert result.value is None

    @pytest.mark.asyncio
    async def test_upsert_updates_in_place(self, pg_source, pg_pool):
        await set_cached_artist_wikipedia_bio(
            pg_source,
            discogs_artist_id=99,
            wikipedia_url=_URL,
            slug_score=90.0,
            lang="en",
            extract="Old text.",
        )
        await set_cached_artist_wikipedia_bio(
            pg_source,
            discogs_artist_id=99,
            wikipedia_url=_URL,
            slug_score=95.0,
            lang="en",
            extract="New text.",
        )
        async with pg_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT extract FROM lml_cache.artist_wikipedia_bio WHERE discogs_artist_id = 99"
            )
        assert len(rows) == 1, "ON CONFLICT must update in place, not insert"
        assert rows[0]["extract"] == "New text."

    @pytest.mark.asyncio
    async def test_upsert_replaces_the_stored_wikipedia_url_too(self, pg_source, pg_pool):
        # The self-healing write path: a later fetch under a recalibrated
        # pick must replace wikipedia_url, not just the extract.
        await set_cached_artist_wikipedia_bio(
            pg_source,
            discogs_artist_id=99,
            wikipedia_url="https://en.wikipedia.org/wiki/Old_Pick",
            slug_score=82.0,
            lang="en",
            extract="Old pick's text.",
        )
        await set_cached_artist_wikipedia_bio(
            pg_source,
            discogs_artist_id=99,
            wikipedia_url=_URL,
            slug_score=97.0,
            lang="en",
            extract="Correct text.",
        )
        async with pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT wikipedia_url, extract FROM lml_cache.artist_wikipedia_bio "
                "WHERE discogs_artist_id = 99"
            )
        assert row["wikipedia_url"] == _URL
        assert row["extract"] == "Correct text."


@pytest.mark.pg
class TestSqlLevelNullOverwriteGuard:
    """LML#1192 review round 4, P0-5: real ``ON CONFLICT ... WHERE`` clause
    behavior needs a live Postgres round-trip to prove -- the unit-level
    mock only pins that the SQL text carries the guard.
    """

    @pytest.mark.asyncio
    async def test_a_would_be_negative_write_leaves_an_existing_positive_row_untouched(
        self, pg_source, pg_pool
    ):
        await set_cached_artist_wikipedia_bio(
            pg_source,
            discogs_artist_id=99,
            wikipedia_url=_URL,
            slug_score=97.0,
            lang="en",
            extract="The real, already-collected biography.",
        )
        # A later write attempts to null it out (e.g. a --repick whose
        # fresh pick diverged into a rejected/404 page) -- must be a no-op
        # at the database level, not just skipped by the Python caller.
        await set_cached_artist_wikipedia_bio(
            pg_source,
            discogs_artist_id=99,
            wikipedia_url="https://en.wikipedia.org/wiki/A_Different_Diverged_Pick",
            slug_score=40.0,
            lang="en",
            extract=None,
        )
        async with pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT wikipedia_url, extract FROM lml_cache.artist_wikipedia_bio "
                "WHERE discogs_artist_id = 99"
            )
        assert row["extract"] == "The real, already-collected biography."
        # The WHERE clause blocks the WHOLE update, not just extract --
        # wikipedia_url must ALSO stay the original, not the diverged pick.
        assert row["wikipedia_url"] == _URL

    @pytest.mark.asyncio
    async def test_a_positive_write_over_an_existing_negative_row_still_updates_normally(
        self, pg_source, pg_pool
    ):
        # The guard is one-directional: it only blocks positive->negative.
        # A negative row (or no row at all) accepting a fresh positive
        # extract is the whole point of the cache and must be unaffected.
        await set_cached_artist_wikipedia_bio(
            pg_source,
            discogs_artist_id=99,
            wikipedia_url=_URL,
            slug_score=40.0,
            lang="en",
            extract=None,
        )
        await set_cached_artist_wikipedia_bio(
            pg_source,
            discogs_artist_id=99,
            wikipedia_url=_URL,
            slug_score=97.0,
            lang="en",
            extract="Newly-found biography.",
        )
        result = await get_cached_artist_wikipedia_bio(
            pg_source, discogs_artist_id=99, wikipedia_url=_URL
        )
        assert result.value == "Newly-found biography."

    @pytest.mark.asyncio
    async def test_touch_advances_last_checked_at_without_touching_fetched_at(
        self, pg_source, pg_pool
    ):
        await set_cached_artist_wikipedia_bio(
            pg_source,
            discogs_artist_id=99,
            wikipedia_url=_URL,
            slug_score=97.0,
            lang="en",
            extract="Text.",
        )
        async with pg_pool.acquire() as conn:
            before = await conn.fetchrow(
                "SELECT fetched_at, last_checked_at FROM lml_cache.artist_wikipedia_bio "
                "WHERE discogs_artist_id = 99"
            )
            # Move last_checked_at into the past so the touch's advance is
            # observable regardless of same-millisecond now() collisions.
            await conn.execute(
                "UPDATE lml_cache.artist_wikipedia_bio "
                "SET last_checked_at = last_checked_at - interval '1 day' "
                "WHERE discogs_artist_id = 99"
            )
        await touch_artist_wikipedia_bio_last_checked_at(pg_source, discogs_artist_id=99)
        async with pg_pool.acquire() as conn:
            after = await conn.fetchrow(
                "SELECT fetched_at, last_checked_at FROM lml_cache.artist_wikipedia_bio "
                "WHERE discogs_artist_id = 99"
            )
        assert after["fetched_at"] == before["fetched_at"]
        assert after["last_checked_at"] > before["last_checked_at"]


@pytest.mark.pg
class TestSelfHealingUrlMismatch:
    @pytest.mark.asyncio
    async def test_stored_row_under_a_different_url_is_invisible(self, pg_source):
        await set_cached_artist_wikipedia_bio(
            pg_source,
            discogs_artist_id=99,
            wikipedia_url="https://en.wikipedia.org/wiki/Stale_Pick",
            slug_score=82.0,
            lang="en",
            extract="Stale text.",
        )
        # A read for the CURRENT pick (a different URL) sees no fresh row —
        # a scorer recalibration self-heals to a miss, not stale served text.
        result = await get_cached_artist_wikipedia_bio(
            pg_source, discogs_artist_id=99, wikipedia_url=_URL
        )
        assert result.was_present is False
        assert result.value is None

    @pytest.mark.asyncio
    async def test_negative_row_under_a_different_url_is_also_a_miss(self, pg_source):
        await set_cached_artist_wikipedia_bio(
            pg_source,
            discogs_artist_id=99,
            wikipedia_url="https://en.wikipedia.org/wiki/A_404_Page",
            slug_score=82.0,
            lang="en",
            extract=None,
        )
        result = await get_cached_artist_wikipedia_bio(
            pg_source, discogs_artist_id=99, wikipedia_url=_URL
        )
        assert result.was_present is False


@pytest.mark.pg
class TestDualTTL:
    @pytest.mark.asyncio
    async def test_absent_entry_reads_not_present(self, pg_source):
        result = await get_cached_artist_wikipedia_bio(
            pg_source, discogs_artist_id=99, wikipedia_url=_URL
        )
        assert result.was_present is False
        assert result.value is None

    @pytest.mark.asyncio
    async def test_positive_hit_within_30_days_is_returned(self, pg_source, pg_pool):
        await set_cached_artist_wikipedia_bio(
            pg_source,
            discogs_artist_id=99,
            wikipedia_url=_URL,
            slug_score=95.0,
            lang="en",
            extract="Fresh text.",
        )
        now = datetime(2026, 8, 14, tzinfo=UTC)
        recent = now - timedelta(days=20)
        async with pg_pool.acquire() as conn:
            await conn.execute(
                "UPDATE lml_cache.artist_wikipedia_bio SET fetched_at = $1 "
                "WHERE discogs_artist_id = 99",
                recent,
            )
        result = await get_cached_artist_wikipedia_bio(
            pg_source, discogs_artist_id=99, wikipedia_url=_URL, now=now
        )
        assert result.was_present is True
        assert result.value == "Fresh text."

    @pytest.mark.asyncio
    async def test_positive_hit_past_30_days_reads_stale(self, pg_source, pg_pool):
        await set_cached_artist_wikipedia_bio(
            pg_source,
            discogs_artist_id=99,
            wikipedia_url=_URL,
            slug_score=95.0,
            lang="en",
            extract="Aging text.",
        )
        now = datetime(2026, 8, 14, tzinfo=UTC)
        ancient = now - timedelta(days=31)
        async with pg_pool.acquire() as conn:
            await conn.execute(
                "UPDATE lml_cache.artist_wikipedia_bio SET fetched_at = $1 "
                "WHERE discogs_artist_id = 99",
                ancient,
            )
        result = await get_cached_artist_wikipedia_bio(
            pg_source, discogs_artist_id=99, wikipedia_url=_URL, now=now
        )
        assert result.was_present is False
        assert result.value is None

    @pytest.mark.asyncio
    async def test_negative_hit_within_7_days_is_returned(self, pg_source, pg_pool):
        await set_cached_artist_wikipedia_bio(
            pg_source,
            discogs_artist_id=99,
            wikipedia_url=_URL,
            slug_score=95.0,
            lang="en",
            extract=None,
        )
        now = datetime(2026, 8, 14, tzinfo=UTC)
        recent = now - timedelta(days=5)
        async with pg_pool.acquire() as conn:
            await conn.execute(
                "UPDATE lml_cache.artist_wikipedia_bio SET fetched_at = $1 "
                "WHERE discogs_artist_id = 99",
                recent,
            )
        result = await get_cached_artist_wikipedia_bio(
            pg_source, discogs_artist_id=99, wikipedia_url=_URL, now=now
        )
        assert result.was_present is True
        assert result.value is None

    @pytest.mark.asyncio
    async def test_negative_hit_past_7_days_reads_stale(self, pg_source, pg_pool):
        await set_cached_artist_wikipedia_bio(
            pg_source,
            discogs_artist_id=99,
            wikipedia_url=_URL,
            slug_score=95.0,
            lang="en",
            extract=None,
        )
        now = datetime(2026, 8, 14, tzinfo=UTC)
        ancient = now - timedelta(days=8)
        async with pg_pool.acquire() as conn:
            await conn.execute(
                "UPDATE lml_cache.artist_wikipedia_bio SET fetched_at = $1 "
                "WHERE discogs_artist_id = 99",
                ancient,
            )
        result = await get_cached_artist_wikipedia_bio(
            pg_source, discogs_artist_id=99, wikipedia_url=_URL, now=now
        )
        assert result.was_present is False
        assert result.value is None

    @pytest.mark.asyncio
    async def test_negative_hit_at_29_days_is_stale_but_positive_at_29_days_is_fresh(
        self, pg_source, pg_pool
    ):
        # The two TTLs are genuinely different clocks, not one shared cutoff
        # applied to both row flavors: 29 days is well past the 7-day
        # negative TTL but still inside the 30-day positive TTL.
        await set_cached_artist_wikipedia_bio(
            pg_source,
            discogs_artist_id=1,
            wikipedia_url=_URL,
            slug_score=95.0,
            lang="en",
            extract=None,
        )
        await set_cached_artist_wikipedia_bio(
            pg_source,
            discogs_artist_id=2,
            wikipedia_url=_URL,
            slug_score=95.0,
            lang="en",
            extract="Still fresh.",
        )
        now = datetime(2026, 8, 14, tzinfo=UTC)
        aged = now - timedelta(days=29)
        async with pg_pool.acquire() as conn:
            await conn.execute("UPDATE lml_cache.artist_wikipedia_bio SET fetched_at = $1", aged)
        negative = await get_cached_artist_wikipedia_bio(
            pg_source, discogs_artist_id=1, wikipedia_url=_URL, now=now
        )
        positive = await get_cached_artist_wikipedia_bio(
            pg_source, discogs_artist_id=2, wikipedia_url=_URL, now=now
        )
        assert negative.was_present is False
        assert positive.was_present is True
        assert positive.value == "Still fresh."
