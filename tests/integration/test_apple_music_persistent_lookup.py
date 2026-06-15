"""Integration tests for the persistent Apple Music URL cache (LML#571).

PR #571 added ``entity.album_apple_music_lookup_cache``, the cache module
``entity/apple_music_album_cache.py``, and the read-through resolver
``resolve_apple_music_url_with_cache``. All coverage merged with that PR
is unit-level with ``AsyncMock(spec=PgSource)``; this file is the matching
``@pytest.mark.pg`` layer that drives the real schema, the real UPSERT,
and the real TTL math against an actual PostgreSQL connection.

LML#576 collapsed the cache's public surface to ``str | None`` and pushed
staleness into the SQL ``WHERE`` clause. The integration tests below
assert the new shape directly: the SQL filter must exclude stale known
misses without a Python-side check, and ``get_cached_apple_music_url``
returns ``None`` for absent rows, fresh known misses, AND stale misses
(the SQL filter erases the distinction between the latter two).

Concrete production risks the unit tests cannot catch:

* TIMESTAMPTZ codec drift -- asyncpg returns ``TIMESTAMPTZ`` as aware
  ``datetime`` by default, but a custom type codec could return naive
  ``datetime`` and break the SQL ``last_checked_at > $3`` comparison.
  We pin the contract by asserting ``last_checked.tzinfo is not None``
  on a freshly inserted row.
* Schema bootstrap against a missing entity schema -- the autouse fixture
  DROPs the schema between tests so each scenario boots from zero.
* UPSERT semantics -- only a real PG round-trip proves the
  ``ON CONFLICT ... DO UPDATE`` clause refreshes ``apple_music_url`` and
  ``last_checked_at`` instead of inserting a second row.
* ``to_match_form`` normalization parity -- the SELECT and UPSERT must
  pick the same row when callers pass different case/diacritic input.
* SQL-side TTL filter (LML#576) -- staleness moved out of Python and into
  the ``WHERE`` clause. Only a real PG round-trip proves the bind +
  comparison work end-to-end against ``TIMESTAMPTZ`` values.

Run with: pytest -m pg -v tests/integration/test_apple_music_persistent_lookup.py
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import asyncpg
import pytest
import pytest_asyncio

from clients.streaming.apple_music import AppleMusicClient
from entity.apple_music_album_cache import (
    get_cached_apple_music_url,
    resolve_apple_music_url_with_cache,
    set_cached_apple_music_url,
    set_up_apple_music_cache_schema,
)
from entity.sources import PgSource
from streaming.models import SourceMatch

DATABASE_URL = os.getenv(
    "DATABASE_URL_TEST",
    "postgresql://discogs:discogs@localhost:5433/discogs",
)


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
    """A ``PgSource`` borrowing the test pool.

    Constructed via ``__new__`` so we don't accidentally trigger the
    XOR validation in ``PgSource.__init__`` -- mirrors the pattern in
    ``tests/integration/test_release_identity.py``.
    """
    source = PgSource.__new__(PgSource)
    source._dsn = DATABASE_URL
    source._pool = pg_pool
    return source


@pytest_asyncio.fixture(autouse=True)
async def set_up_cache_schema(pg_pool, pg_source):
    """Drop + recreate the entity schema and apply the cache DDL.

    Function-scoped per the issue: every scenario starts from a fresh
    schema so UPSERT idempotency and TTL-staleness tests can't leak state
    into each other. The DROP runs both before and after the test so a
    test that fails partway through doesn't poison the next run.
    """
    async with pg_pool.acquire() as conn:
        await conn.execute("DROP SCHEMA IF EXISTS entity CASCADE")
    await set_up_apple_music_cache_schema(pg_source)
    yield
    async with pg_pool.acquire() as conn:
        await conn.execute("DROP SCHEMA IF EXISTS entity CASCADE")


@pytest.mark.pg
class TestSchemaBootstrap:
    """``set_up_apple_music_cache_schema`` runs the idempotent DDL.

    The autouse fixture already exercises a single successful boot, so
    this class focuses on the additional guarantee that a *second* run
    is a no-op (the DDL is ``IF NOT EXISTS``).
    """

    @pytest.mark.asyncio
    async def test_second_boot_is_a_no_op(self, pg_source, pg_pool):
        # First boot happened in the autouse fixture. Running it again
        # against the live schema must not raise.
        await set_up_apple_music_cache_schema(pg_source)
        # And the table still exists with the expected primary key.
        async with pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT count(*)::int AS n FROM information_schema.tables "
                "WHERE table_schema = 'entity' "
                "AND table_name = 'album_apple_music_lookup_cache'"
            )
        assert row["n"] == 1


@pytest.mark.pg
class TestRoundTrip:
    """``set`` then ``get`` returns the same row, hit and miss variants."""

    @pytest.mark.asyncio
    async def test_set_then_get_returns_cached_url(self, pg_source):
        url = "https://music.apple.com/us/album/aluminum-tunes/1234567890"
        await set_cached_apple_music_url(
            pg_source, artist="Stereolab", album="Aluminum Tunes", url=url
        )

        result = await get_cached_apple_music_url(
            pg_source, artist="Stereolab", album="Aluminum Tunes"
        )

        assert result == url

    @pytest.mark.asyncio
    async def test_set_null_url_then_get_returns_none(self, pg_source):
        # LML#576: ``url=None`` records a known miss; ``get`` reports
        # ``None`` regardless of whether the row is a fresh miss or
        # absent. Callers that need to distinguish use the resolver
        # (which branches on the private ``was_present`` bit), not this
        # public API.
        await set_cached_apple_music_url(
            pg_source, artist="ObscureArtist", album="ObscureAlbum", url=None
        )

        result = await get_cached_apple_music_url(
            pg_source, artist="ObscureArtist", album="ObscureAlbum"
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_last_checked_at_is_timezone_aware(self, pg_source, pg_pool):
        """asyncpg TIMESTAMPTZ -> aware datetime contract.

        The SQL filter compares ``last_checked_at`` to a TIMESTAMPTZ bind
        from Python (``now - miss_ttl``). If asyncpg ever returned a naive
        datetime, that comparison would silently misbehave (PG would
        infer a session-local timezone). Pin the contract by asserting
        the value comes out aware.
        """
        await set_cached_apple_music_url(
            pg_source,
            artist="Jessica Pratt",
            album="On Your Own Love Again",
            url="https://music.apple.com/us/album/on-your-own-love-again/x",
        )
        async with pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT last_checked_at FROM entity.album_apple_music_lookup_cache"
            )
        assert row is not None
        last_checked = row["last_checked_at"]
        assert isinstance(last_checked, datetime)
        assert last_checked.tzinfo is not None


@pytest.mark.pg
class TestTTLStaleness:
    """LML#576: staleness is enforced by the SQL ``WHERE`` clause, not by
    Python. A known-miss row past ``miss_ttl`` is filtered out by the
    SELECT and surfaces as ``None`` — the resolver then falls through to
    the live probe, which UPSERTs and refreshes ``last_checked_at``.
    """

    @pytest.mark.asyncio
    async def test_known_miss_past_ttl_is_filtered_by_sql(self, pg_source, pg_pool):
        # Seed a known miss, then UPDATE ``last_checked_at`` 8 days back.
        # Using a direct UPDATE instead of freezegun (not a project dep)
        # and instead of waiting -- exercising the SQL clock is the point
        # of the integration test.
        await set_cached_apple_music_url(pg_source, artist="Sessa", album="Estrela Acesa", url=None)
        now = datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC)
        past = now - timedelta(days=8)
        async with pg_pool.acquire() as conn:
            await conn.execute(
                "UPDATE entity.album_apple_music_lookup_cache "
                "SET last_checked_at = $1 "
                "WHERE artist_normalized = 'sessa' AND album_normalized = 'estrela acesa'",
                past,
            )

        result = await get_cached_apple_music_url(
            pg_source, artist="Sessa", album="Estrela Acesa", now=now
        )

        # The stale known miss is filtered out by the SQL WHERE; callers
        # see the same "no row" shape as an absent entry.
        assert result is None

    @pytest.mark.asyncio
    async def test_known_miss_within_ttl_passes_sql_filter(self, pg_source, pg_pool):
        # A fresh known miss is still surfaced by the SQL filter — the
        # resolver uses the ``_fetch_cached_row`` private seam to decide
        # whether to call Apple. ``get_cached_apple_music_url`` only
        # exposes the URL, so a fresh known miss returns ``None`` here
        # too — but the resolver's ``cache_miss_recent`` branch is the
        # tested-everywhere coverage of the fresh-vs-stale distinction.
        await set_cached_apple_music_url(pg_source, artist="Sessa", album="Estrela Acesa", url=None)
        # Confirm the SELECT actually returns the row (vs filtering it
        # out, which would re-introduce the live probe path).
        now = datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC)
        recent = now - timedelta(hours=1)
        async with pg_pool.acquire() as conn:
            await conn.execute(
                "UPDATE entity.album_apple_music_lookup_cache "
                "SET last_checked_at = $1 "
                "WHERE artist_normalized = 'sessa' AND album_normalized = 'estrela acesa'",
                recent,
            )

        # Drive the resolver — it must short-circuit Apple because the
        # SQL filter let the in-TTL row through.
        apple_music = AsyncMock(spec=AppleMusicClient)
        outcome = await resolve_apple_music_url_with_cache(
            pg_source,
            apple_music,
            artist="Sessa",
            album="Estrela Acesa",
            now=now,
        )
        assert outcome.url is None
        assert outcome.source == "cache_miss_recent"
        apple_music.find_album_match.assert_not_called()

    @pytest.mark.asyncio
    async def test_hit_past_ttl_never_goes_stale(self, pg_source, pg_pool):
        # Hits are durable: an ancient hit must still report fresh. The
        # SQL filter is ``apple_music_url IS NOT NULL OR last_checked_at
        # > $cutoff`` — the OR keeps hits unconditional.
        url = "https://music.apple.com/us/album/durable-hit/1"
        await set_cached_apple_music_url(
            pg_source, artist="Stereolab", album="Aluminum Tunes", url=url
        )
        now = datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC)
        ancient = now - timedelta(days=400)
        async with pg_pool.acquire() as conn:
            await conn.execute(
                "UPDATE entity.album_apple_music_lookup_cache "
                "SET last_checked_at = $1 "
                "WHERE artist_normalized = 'stereolab' AND album_normalized = 'aluminum tunes'",
                ancient,
            )

        result = await get_cached_apple_music_url(
            pg_source, artist="Stereolab", album="Aluminum Tunes", now=now
        )

        assert result == url


@pytest.mark.pg
class TestUpsertIdempotency:
    """Two consecutive ``set`` calls leave one row; the latest URL wins."""

    @pytest.mark.asyncio
    async def test_consecutive_sets_leave_one_row_latest_wins(self, pg_source, pg_pool):
        first_url = "https://music.apple.com/us/album/initial/1"
        second_url = "https://music.apple.com/us/album/replacement/2"
        await set_cached_apple_music_url(
            pg_source, artist="Cat Power", album="Moon Pix", url=first_url
        )
        await set_cached_apple_music_url(
            pg_source, artist="Cat Power", album="Moon Pix", url=second_url
        )

        async with pg_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT apple_music_url FROM entity.album_apple_music_lookup_cache "
                "WHERE artist_normalized = 'cat power' AND album_normalized = 'moon pix'"
            )
        assert len(rows) == 1, "ON CONFLICT must update in place, not insert"
        assert rows[0]["apple_music_url"] == second_url

    @pytest.mark.asyncio
    async def test_hit_then_null_records_miss_in_same_row(self, pg_source, pg_pool):
        # A subsequent ``url=None`` write must replace the URL with NULL
        # rather than leaving the original URL in place. This is the path
        # the resolver takes when Apple's catalog drops a previously-found
        # album (rare but possible).
        await set_cached_apple_music_url(
            pg_source,
            artist="Hyd",
            album="Hold Onto Me Infinity",
            url="https://music.apple.com/us/album/transient/1",
        )
        await set_cached_apple_music_url(
            pg_source, artist="Hyd", album="Hold Onto Me Infinity", url=None
        )

        async with pg_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT apple_music_url FROM entity.album_apple_music_lookup_cache "
                "WHERE artist_normalized = 'hyd' "
                "AND album_normalized = 'hold onto me infinity'"
            )
        assert len(rows) == 1
        assert rows[0]["apple_music_url"] is None


@pytest.mark.pg
class TestNormalizationSymmetry:
    """SELECT and UPSERT must converge on the same row regardless of the
    caller's case / diacritic / whitespace shape."""

    @pytest.mark.asyncio
    async def test_set_with_diacritics_get_without_resolves_same_row(self, pg_source, pg_pool):
        url = "https://music.apple.com/us/album/painless/abcdef"
        # Write the canonical (diacritic-bearing, uppercase) form.
        await set_cached_apple_music_url(
            pg_source, artist="Nilüfer Yanya", album="PAINLESS", url=url
        )
        # Read with the ASCII / lowercase variant -- should still hit.
        result = await get_cached_apple_music_url(
            pg_source, artist="Nilufer Yanya", album="painless"
        )
        assert result == url
        # And only one row exists -- not two collated under different keys.
        async with pg_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT artist_normalized, album_normalized "
                "FROM entity.album_apple_music_lookup_cache"
            )
        assert len(rows) == 1
        assert rows[0]["artist_normalized"] == "nilufer yanya"
        assert rows[0]["album_normalized"] == "painless"


@pytest.mark.pg
class TestResolveEndToEnd:
    """``resolve_apple_music_url_with_cache`` against real PG with a
    mocked Apple client. The cache module owns the SQL; the Apple client
    is the only seam where we still mock, because we can't hit Apple's
    catalog from CI."""

    @pytest.mark.asyncio
    async def test_live_resolved_writes_row_to_pg(self, pg_source, pg_pool):
        # Cache empty -> Apple call -> UPSERT row in PG.
        url = "https://music.apple.com/us/album/edits/77777"
        apple_music = AsyncMock(spec=AppleMusicClient)
        apple_music.find_album_match = AsyncMock(return_value=SourceMatch(url=url, confidence=95.0))

        outcome = await resolve_apple_music_url_with_cache(
            pg_source,
            apple_music,
            artist="Chuquimamani-Condori",
            album="Edits",
        )

        assert outcome.url == url
        assert outcome.source == "live_resolved"
        apple_music.find_album_match.assert_awaited_once_with("Chuquimamani-Condori", "Edits")
        # The UPSERT must have actually written -- verified directly
        # against PG, not via a mock spy.
        async with pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT apple_music_url, last_checked_at "
                "FROM entity.album_apple_music_lookup_cache "
                "WHERE artist_normalized = 'chuquimamani-condori' "
                "AND album_normalized = 'edits'"
            )
        assert row is not None
        assert row["apple_music_url"] == url
        # TIMESTAMPTZ contract -- aware datetime on the way out.
        assert row["last_checked_at"].tzinfo is not None

    @pytest.mark.asyncio
    async def test_cache_hit_short_circuits_apple_call(self, pg_source):
        # Seed the cache, then confirm the resolver returns the row
        # without touching Apple.
        cached_url = "https://music.apple.com/us/album/cached/9"
        await set_cached_apple_music_url(
            pg_source,
            artist="Juana Molina",
            album="DOGA",
            url=cached_url,
        )
        apple_music = AsyncMock(spec=AppleMusicClient)

        outcome = await resolve_apple_music_url_with_cache(
            pg_source, apple_music, artist="Juana Molina", album="DOGA"
        )

        assert outcome.url == cached_url
        assert outcome.source == "cache_hit"
        apple_music.find_album_match.assert_not_called()
