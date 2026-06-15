"""Unit tests for ``entity.apple_music_album_cache``.

The cache persists Apple Music album-URL resolutions keyed by
(artist_normalized, album_normalized), so ``/api/v1/lookup`` does not have
to re-query Apple Music for albums it has already resolved — regardless of
whether the album has a row in ``library.db``.

Normalization is owned by the cache module via ``wxyc_etl.text.to_match_form``
so callers can pass raw request strings and rely on case / diacritic / extra-
whitespace symmetry. The TTL parameter is the staleness threshold for known
misses (``apple_music_url IS NULL``); hits never go stale.

PG interaction is mocked with ``AsyncMock(spec=PgSource)``; the integration
tests at ``tests/integration/test_apple_music_persistent_lookup.py`` cover
the real SQL behavior.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from entity.apple_music_album_cache import (
    DEFAULT_MISS_TTL,
    CacheResult,
    get_cached_apple_music_url,
    set_cached_apple_music_url,
    set_up_apple_music_cache_schema,
)
from entity.sources import PgSource


@pytest.mark.asyncio
class TestGetCachedAppleMusicURL:
    """``get_cached_apple_music_url`` returns ``CacheResult`` carrying the URL,
    whether the row is a known miss, and whether the entry is stale."""

    async def test_returns_empty_result_when_row_absent(self):
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(return_value=None)

        result = await get_cached_apple_music_url(pg, artist="Hyd", album="Hold Onto Me Infinity")

        assert result == CacheResult(url=None, is_known_miss=False, is_stale=False)

    async def test_returns_url_when_row_carries_url(self):
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(
            return_value={
                "apple_music_url": "https://music.apple.com/us/album/foo/1234567890",
                "last_checked_at": datetime.now(UTC),
            }
        )

        result = await get_cached_apple_music_url(pg, artist="Stereolab", album="Aluminum Tunes")

        assert result.url == "https://music.apple.com/us/album/foo/1234567890"
        assert result.is_known_miss is False
        assert result.is_stale is False

    async def test_flags_known_miss_when_url_null(self):
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(
            return_value={
                "apple_music_url": None,
                "last_checked_at": datetime.now(UTC),
            }
        )

        result = await get_cached_apple_music_url(pg, artist="Sessa", album="Estrela Acesa")

        assert result.url is None
        assert result.is_known_miss is True
        assert result.is_stale is False

    async def test_flags_stale_when_known_miss_past_ttl(self):
        now = datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC)
        # 8 days back: past the 7d default miss TTL.
        last_checked = now - timedelta(days=8)
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(
            return_value={"apple_music_url": None, "last_checked_at": last_checked}
        )

        result = await get_cached_apple_music_url(
            pg, artist="Sessa", album="Estrela Acesa", now=now
        )

        assert result.url is None
        assert result.is_known_miss is True
        assert result.is_stale is True

    async def test_hits_never_go_stale(self):
        # Even a year-old hit should still report fresh — durable storage,
        # only misses get a TTL.
        now = datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC)
        last_checked = now - timedelta(days=400)
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(
            return_value={
                "apple_music_url": "https://music.apple.com/us/album/foo/1",
                "last_checked_at": last_checked,
            }
        )

        result = await get_cached_apple_music_url(
            pg, artist="Stereolab", album="Aluminum Tunes", now=now
        )

        assert result.is_stale is False

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
        # First positional arg is the SQL; the rest are the bind values.
        bind_artist = call_args.args[1]
        bind_album = call_args.args[2]
        # ``to_match_form`` strips diacritics + lowercases.
        assert bind_artist == "nilufer yanya"
        assert bind_album == "painless"

    async def test_returns_empty_result_on_pg_error(self):
        # PG outage must not break the lookup; the cache layer degrades to
        # "no cached value" so the caller falls through to the live probe.
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(side_effect=RuntimeError("PG unreachable"))

        result = await get_cached_apple_music_url(pg, artist="Hyd", album="Hold Onto Me Infinity")

        assert result == CacheResult(url=None, is_known_miss=False, is_stale=False)


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
