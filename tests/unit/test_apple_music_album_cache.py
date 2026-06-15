"""Unit tests for ``entity.apple_music_album_cache``.

The cache persists Apple Music album-URL resolutions keyed by
(artist_normalized, album_normalized), so ``/api/v1/lookup`` does not have
to re-query Apple Music for albums it has already resolved — regardless of
whether the album has a row in ``library.db``.

Normalization is owned by the cache module via ``wxyc_etl.text.to_match_form``
so callers can pass raw request strings and rely on case / diacritic / extra-
whitespace symmetry. The TTL parameter is the staleness threshold for known
misses (``apple_music_url IS NULL``); hits never go stale.

LML#576 collapsed the cache's return shape from ``CacheResult(url,
is_known_miss, is_stale)`` to ``str | None``. Staleness moved into the SQL
``WHERE`` clause (``last_checked_at > now() - miss_ttl``), mirroring
``discogs/cache_service.py::lookup_negative_hit``. Callers see one of:

* a non-null URL (cache hit), or
* ``None`` for any of (a) absent row, (b) fresh known miss, or (c) stale
  miss — the SQL filter already excluded (c) so the resolver's
  ``_fetch_cached_row`` ``was_present`` bit is enough to tell (b) from
  (a)+(c).

The unit tests pin both the public ``str | None`` contract and the SQL
bind-shape (third bind = ``now - miss_ttl``) so a careless edit to the
SELECT can't silently regress to the pre-#576 behavior.

PG interaction is mocked with ``AsyncMock(spec=PgSource)``; the integration
tests at ``tests/integration/test_apple_music_persistent_lookup.py`` cover
the real SQL behavior end-to-end.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from entity.apple_music_album_cache import (
    DEFAULT_MISS_TTL,
    get_cached_apple_music_url,
    set_cached_apple_music_url,
    set_up_apple_music_cache_schema,
)
from entity.sources import PgSource


@pytest.mark.asyncio
class TestGetCachedAppleMusicURL:
    """``get_cached_apple_music_url`` returns ``str | None``.

    Hits return the URL. Absent rows, stale misses (excluded by the SQL
    filter), and fresh known misses all return ``None``. The fresh-vs-
    stale-vs-absent distinction is no longer surfaced — callers that
    need it use the ``_fetch_cached_row`` internal seam (the resolver
    does this to decide whether to call Apple).
    """

    async def test_returns_none_when_row_absent(self):
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(return_value=None)

        result = await get_cached_apple_music_url(pg, artist="Hyd", album="Hold Onto Me Infinity")

        assert result is None

    async def test_returns_url_when_row_carries_url(self):
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(
            return_value={
                "apple_music_url": "https://music.apple.com/us/album/foo/1234567890",
            }
        )

        result = await get_cached_apple_music_url(pg, artist="Stereolab", album="Aluminum Tunes")

        assert result == "https://music.apple.com/us/album/foo/1234567890"

    async def test_returns_none_for_fresh_known_miss(self):
        # A row whose URL is NULL but inside the TTL still returns a row from
        # the SQL filter — the public API surface ``get_cached_apple_music_url``
        # collapses it to ``None`` because the URL is None. The resolver
        # distinguishes fresh-miss from absent via the private
        # ``_fetch_cached_row.was_present`` seam.
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(return_value={"apple_music_url": None})

        result = await get_cached_apple_music_url(pg, artist="Sessa", album="Estrela Acesa")

        assert result is None

    async def test_sql_bind_carries_now_minus_miss_ttl_cutoff(self):
        # LML#576: the SQL ``WHERE`` clause filters known-misses to those
        # with ``last_checked_at > $3``. Bind ``$3`` must be ``now - miss_ttl``
        # so the DB-side filter matches the public TTL semantic.
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(return_value=None)
        now = datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC)
        miss_ttl = timedelta(days=7)

        await get_cached_apple_music_url(
            pg, artist="Hyd", album="Hold Onto Me Infinity", miss_ttl=miss_ttl, now=now
        )

        assert pg.fetchone.await_count == 1
        call = pg.fetchone.await_args
        # SQL first; then artist, album, cutoff.
        sql = call.args[0]
        assert "WHERE artist_normalized = $1 AND album_normalized = $2" in sql
        # The TTL filter must be DB-side, not Python-side.
        assert "last_checked_at > $3" in sql
        assert "apple_music_url IS NOT NULL OR" in sql
        # The cutoff bind is ``now - miss_ttl``.
        cutoff_bind = call.args[3]
        assert cutoff_bind == now - miss_ttl

    async def test_normalizes_artist_and_album_via_to_match_form(self):
        # Cache lookup with diacritics + casing variants should query the SAME
        # row as the normalized form. We assert the SQL bind values are the
        # ``to_match_form`` output, not the raw input strings.
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(return_value=None)

        await get_cached_apple_music_url(pg, artist="Nilüfer Yanya", album="PAINLESS")

        # Exactly one query, bind-args 2 and 3 are the normalized strings.
        assert pg.fetchone.await_count == 1
        call_args = pg.fetchone.await_args
        # First positional arg is the SQL; bind values follow.
        bind_artist = call_args.args[1]
        bind_album = call_args.args[2]
        # ``to_match_form`` strips diacritics + lowercases.
        assert bind_artist == "nilufer yanya"
        assert bind_album == "painless"

    async def test_returns_none_on_pg_error(self):
        # PG outage must not break the lookup; the cache layer degrades to
        # "no cached value" so the caller falls through to the live probe.
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(side_effect=RuntimeError("PG unreachable"))

        result = await get_cached_apple_music_url(pg, artist="Hyd", album="Hold Onto Me Infinity")

        assert result is None


@pytest.mark.asyncio
class TestSetCachedAppleMusicURL:
    """``set_cached_apple_music_url`` UPSERTs the cache row keyed by the
    normalized (artist, album) pair. Hits and misses are both persisted; the
    caller chooses by passing ``url=None`` for a known miss."""

    async def test_inserts_hit(self):
        pg = AsyncMock(spec=PgSource)
        pg.execute = AsyncMock(return_value="INSERT 0 1")

        await set_cached_apple_music_url(
            pg,
            artist="Hyd",
            album="Hold Onto Me Infinity",
            url="https://music.apple.com/us/album/foo/1234567890",
        )

        assert pg.execute.await_count == 1
        call = pg.execute.await_args
        # SQL is the first positional arg; bind values follow.
        sql = call.args[0]
        assert "INSERT INTO entity.album_apple_music_lookup_cache" in sql
        assert "ON CONFLICT" in sql
        # Bind: artist_normalized, album_normalized, apple_music_url
        assert call.args[1] == "hyd"
        assert call.args[2] == "hold onto me infinity"
        assert call.args[3] == "https://music.apple.com/us/album/foo/1234567890"

    async def test_inserts_known_miss(self):
        pg = AsyncMock(spec=PgSource)
        pg.execute = AsyncMock(return_value="INSERT 0 1")

        await set_cached_apple_music_url(pg, artist="Sessa", album="Estrela Acesa", url=None)

        call = pg.execute.await_args
        assert call.args[1] == "sessa"
        assert call.args[2] == "estrela acesa"
        # url bound as NULL
        assert call.args[3] is None

    async def test_swallows_pg_error(self):
        # Cache write failures must not break the request — log and continue.
        pg = AsyncMock(spec=PgSource)
        pg.execute = AsyncMock(side_effect=RuntimeError("PG unreachable"))

        # No exception raised.
        await set_cached_apple_music_url(
            pg, artist="Hyd", album="Angel", url="https://music.apple.com/us/album/foo/1"
        )


@pytest.mark.asyncio
class TestSetUpAppleMusicCacheSchema:
    """``set_up_apple_music_cache_schema`` runs the idempotent DDL. Failures
    are swallowed by the caller's lifespan hook, but the helper itself must
    actually call execute when given a working PG."""

    async def test_executes_schema_then_create_table_if_not_exists(self):
        pg = AsyncMock(spec=PgSource)
        pg.execute = AsyncMock(return_value="CREATE TABLE")

        await set_up_apple_music_cache_schema(pg)

        # Two calls: schema first (so a fresh PG without the entity schema
        # applied can still bootstrap LML), then the table.
        assert pg.execute.await_count == 2
        schema_sql = pg.execute.await_args_list[0].args[0]
        table_sql = pg.execute.await_args_list[1].args[0]
        assert "CREATE SCHEMA IF NOT EXISTS entity" in schema_sql
        assert "CREATE TABLE IF NOT EXISTS entity.album_apple_music_lookup_cache" in table_sql
        # PK matches the design: (artist_normalized, album_normalized).
        assert "PRIMARY KEY (artist_normalized, album_normalized)" in table_sql


def test_default_miss_ttl_is_seven_days():
    # Pin the published default so a careless change to the constant gets
    # caught by review. The TTL governs how quickly Apple Music's catalog
    # additions become visible after an initial "not found" — 7d trades
    # freshness for cache amortization.
    assert DEFAULT_MISS_TTL == timedelta(days=7)
