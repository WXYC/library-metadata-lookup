"""Unit tests for the artist Wikipedia bio cache read/write helpers (LML#513/#1192).

PG interaction is mocked with ``AsyncMock(spec=PgSource)``, mirroring
``tests/unit/test_streaming_url_cache.py``. Covers the three-valued
``CachedValue`` read contract (positive hit / negative hit / miss), the
self-healing URL-match predicate (a stored row for a DIFFERENT
``wikipedia_url`` than the caller's current pick reads as a miss), the two
TTL cutoffs bound as separate SQL params, and the UPSERT write shape. The
``pg``-marked integration layer drives the real DDL/TTL math.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from entity.artist_wikipedia_bio import (
    DEFAULT_SUCCESS_TTL,
    get_cached_artist_wikipedia_bio,
    set_cached_artist_wikipedia_bio,
    set_up_artist_wikipedia_bio_schema,
    touch_artist_wikipedia_bio_last_checked_at,
)
from entity.cache_toolkit import DEFAULT_MISS_TTL
from entity.sources import PgSource

_ARTIST_ID = 99
_URL = "https://en.wikipedia.org/wiki/Stereolab"
_NOW = datetime(2026, 8, 14, tzinfo=UTC)


@pytest.mark.asyncio
class TestGetCachedArtistWikipediaBio:
    async def test_returns_absent_when_row_missing(self):
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(return_value=None)

        result = await get_cached_artist_wikipedia_bio(
            pg, discogs_artist_id=_ARTIST_ID, wikipedia_url=_URL, now=_NOW
        )

        assert result.was_present is False
        assert result.value is None

    async def test_returns_positive_hit(self):
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(return_value={"extract": "Stereolab are a band."})

        result = await get_cached_artist_wikipedia_bio(
            pg, discogs_artist_id=_ARTIST_ID, wikipedia_url=_URL, now=_NOW
        )

        assert result.was_present is True
        assert result.value == "Stereolab are a band."

    async def test_returns_negative_hit_distinct_from_absent(self):
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(return_value={"extract": None})

        result = await get_cached_artist_wikipedia_bio(
            pg, discogs_artist_id=_ARTIST_ID, wikipedia_url=_URL, now=_NOW
        )

        assert result.was_present is True
        assert result.value is None

    async def test_binds_artist_id_url_and_both_cutoffs(self):
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(return_value=None)

        await get_cached_artist_wikipedia_bio(
            pg, discogs_artist_id=_ARTIST_ID, wikipedia_url=_URL, now=_NOW
        )

        args = pg.fetchone.await_args.args
        sql, artist_id, url, positive_cutoff, negative_cutoff = args
        assert artist_id == _ARTIST_ID
        assert url == _URL
        assert positive_cutoff == _NOW - DEFAULT_SUCCESS_TTL
        assert negative_cutoff == _NOW - DEFAULT_MISS_TTL

    async def test_url_mismatch_is_a_miss_not_a_hit(self):
        # The SQL WHERE clause filters on the caller's wikipedia_url, so a
        # row stored under a DIFFERENT (now-stale-pick) URL is invisible to
        # this call -- the fake PG here just proves the caller's URL is what
        # gets bound; the real self-healing lives in the SQL predicate,
        # pinned by the pg-marked integration test.
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(return_value=None)

        result = await get_cached_artist_wikipedia_bio(
            pg,
            discogs_artist_id=_ARTIST_ID,
            wikipedia_url="https://en.wikipedia.org/wiki/A_Different_Pick",
            now=_NOW,
        )

        assert result.was_present is False
        bound_url = pg.fetchone.await_args.args[2]
        assert bound_url == "https://en.wikipedia.org/wiki/A_Different_Pick"

    async def test_pg_error_degrades_to_absent(self):
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(side_effect=RuntimeError("PG unreachable"))

        result = await get_cached_artist_wikipedia_bio(
            pg, discogs_artist_id=_ARTIST_ID, wikipedia_url=_URL, now=_NOW
        )

        assert result.was_present is False
        assert result.value is None


@pytest.mark.asyncio
class TestSetCachedArtistWikipediaBio:
    async def test_inserts_positive_result(self):
        pg = AsyncMock(spec=PgSource)
        pg.execute = AsyncMock(return_value="INSERT 0 1")

        await set_cached_artist_wikipedia_bio(
            pg,
            discogs_artist_id=_ARTIST_ID,
            wikipedia_url=_URL,
            slug_score=97.6,
            lang="en",
            extract="Stereolab are a band.",
        )

        args = pg.execute.await_args.args
        _sql, artist_id, url, slug_score, lang, extract = args
        assert artist_id == _ARTIST_ID
        assert url == _URL
        assert slug_score == 98  # rounded to nearest int for the SMALLINT column
        assert lang == "en"
        assert extract == "Stereolab are a band."

    async def test_inserts_negative_result_with_none_extract(self):
        pg = AsyncMock(spec=PgSource)
        pg.execute = AsyncMock(return_value="INSERT 0 1")

        await set_cached_artist_wikipedia_bio(
            pg,
            discogs_artist_id=_ARTIST_ID,
            wikipedia_url=_URL,
            slug_score=85.0,
            lang="en",
            extract=None,
        )

        extract = pg.execute.await_args.args[5]
        assert extract is None

    async def test_write_failure_is_swallowed(self):
        pg = AsyncMock(spec=PgSource)
        pg.execute = AsyncMock(side_effect=RuntimeError("PG unreachable"))

        # Must not raise.
        await set_cached_artist_wikipedia_bio(
            pg,
            discogs_artist_id=_ARTIST_ID,
            wikipedia_url=_URL,
            slug_score=90.0,
            lang="en",
            extract="Text.",
        )

    async def test_upsert_also_advances_last_checked_at(self):
        # LML#1192 review round 3/4, finding 13: fetched_at describes
        # CONTENT age (the TTL clock get_cached_artist_wikipedia_bio
        # reads); last_checked_at is the offline drain's own progress
        # cursor. A real fetch (this UPSERT) advances BOTH -- new content
        # is by definition freshly checked too.
        pg = AsyncMock(spec=PgSource)
        pg.execute = AsyncMock(return_value="INSERT 0 1")

        await set_cached_artist_wikipedia_bio(
            pg,
            discogs_artist_id=_ARTIST_ID,
            wikipedia_url=_URL,
            slug_score=97.6,
            lang="en",
            extract="Stereolab are a band.",
        )

        sql = pg.execute.await_args.args[0]
        assert "last_checked_at" in sql

    async def test_upsert_never_overwrites_a_positive_extract_with_null_at_the_sql_level(self):
        # LML#1192 review round 4, P0-5: the Python-level guard in
        # scripts/warm_wikipedia_bios.py::_write_bio is necessary but not
        # sufficient -- this repo's standing rule (never overwrite
        # successfully collected data without explicit approval) must
        # hold even against a caller that bypasses that guard. The UPSERT
        # itself carries a WHERE clause that blocks the whole update
        # (leaving every column, not just extract, untouched) whenever the
        # existing row already has a positive extract and the incoming
        # write would null it. Real conflict-clause behavior needs a live
        # Postgres round-trip -- see the pg-marked integration test for
        # the end-to-end proof; this only pins the SQL text carries the
        # guard clause.
        pg = AsyncMock(spec=PgSource)
        pg.execute = AsyncMock(return_value="INSERT 0 1")

        await set_cached_artist_wikipedia_bio(
            pg,
            discogs_artist_id=_ARTIST_ID,
            wikipedia_url=_URL,
            slug_score=40.0,
            lang="en",
            extract=None,
        )

        sql = pg.execute.await_args.args[0]
        assert "WHERE" in sql
        assert "extract IS NOT NULL" in sql
        assert "EXCLUDED.extract IS NULL" in sql


@pytest.mark.asyncio
class TestTouchArtistWikipediaBioLastCheckedAt:
    """LML#1192 review round 3/4, finding 13: split fetched_at's two
    meanings. The drain's ``--repick`` cursor-advance bumps ONLY
    last_checked_at, never fetched_at, so a repick that confirms an
    unchanged pick doesn't also grant the existing prose another full
    DEFAULT_SUCCESS_TTL of content-freshness authority it didn't earn.
    """

    async def test_touches_last_checked_at_only(self):
        pg = AsyncMock(spec=PgSource)
        pg.execute = AsyncMock(return_value="UPDATE 1")

        await touch_artist_wikipedia_bio_last_checked_at(pg, discogs_artist_id=_ARTIST_ID)

        args = pg.execute.await_args.args
        sql, artist_id = args
        assert artist_id == _ARTIST_ID
        assert "last_checked_at" in sql
        assert "fetched_at" not in sql

    async def test_write_failure_is_swallowed(self):
        pg = AsyncMock(spec=PgSource)
        pg.execute = AsyncMock(side_effect=RuntimeError("PG unreachable"))

        # Must not raise.
        await touch_artist_wikipedia_bio_last_checked_at(pg, discogs_artist_id=_ARTIST_ID)


class _FakeTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return None


class _FakeConnection:
    def __init__(self, source: _FakePgSource) -> None:
        self._source = source

    def transaction(self) -> _FakeTransaction:
        return _FakeTransaction()

    async def execute(self, sql: str, *args: object) -> str:
        self._source.executed.append(sql)
        return "ALTER TABLE"


class _FakePgSource:
    """Minimal fake mirroring tests/unit/test_ddl.py's _FakePgSource --
    records every statement issued and how many times a bootstrap
    ``pg.acquire()``'d, so a test can assert the CREATE TABLE and the
    additive ALTER TABLE run as SEPARATE bootstrap_lml_cache_table calls
    (entity/streaming_url_cache.py's LML#1121 FIX 5 lock-isolation
    precedent), not bundled into one transaction."""

    def __init__(self) -> None:
        self.executed: list[str] = []
        self.acquire_count = 0

    @asynccontextmanager
    async def acquire(self):
        self.acquire_count += 1
        yield _FakeConnection(self)


@pytest.mark.asyncio
class TestSchemaCarriesLastCheckedAtColumn:
    """LML#1192 review round 4, P0-1: last_checked_at was introduced only
    in #1196 (the downstream drain PR), not #1194 (the PR that actually
    creates lml_cache.artist_wikipedia_bio via CREATE TABLE IF NOT
    EXISTS) -- so #1194 alone deploys a table missing the column
    entirely, and #1196's later CREATE TABLE IF NOT EXISTS is then a
    no-op forever. Both the CREATE TABLE (fresh deploys) and an idempotent
    ALTER TABLE ADD COLUMN IF NOT EXISTS (a table #1194 already created
    without it) must exist here, mirroring
    entity/streaming_url_cache.py's is_error column precedent."""

    async def test_bootstrap_issues_an_add_column_statement_for_last_checked_at(self):
        pg = _FakePgSource()

        await set_up_artist_wikipedia_bio_schema(pg)

        assert any("CREATE TABLE" in s and "last_checked_at" in s for s in pg.executed)
        assert any("ADD COLUMN IF NOT EXISTS" in s and "last_checked_at" in s for s in pg.executed)

    async def test_the_add_column_alter_runs_as_its_own_bootstrap_call(self):
        # LML#1121 FIX 5 precedent: PG16 still takes an AccessExclusiveLock
        # on ADD COLUMN IF NOT EXISTS even on the no-op path, so it must
        # not share a transaction with schema/table creation -- a
        # lock_timeout there must not roll back the already-succeeded
        # CREATE TABLE too.
        pg = _FakePgSource()

        await set_up_artist_wikipedia_bio_schema(pg)

        assert pg.acquire_count >= 2
