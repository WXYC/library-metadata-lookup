"""Unit tests for the artist Wikipedia bio cache read/write helpers (LML#513/#1192).

PG interaction is mocked with ``AsyncMock(spec=PgSource)``, mirroring
``tests/unit/test_streaming_url_cache.py``. Covers the three-valued
``CachedValue`` read contract (positive hit / negative hit / miss), the two
TTL cutoffs bound as separate SQL params, and the UPSERT write shape. The
``pg``-marked integration layer drives the real DDL/TTL math.

LML#1192 review round 5: the read predicate is no longer keyed on the
caller's current pick (``wikipedia_url`` dropped as a parameter entirely) --
see ``TestReadBackAfterAWarmWrittenUnderADifferentUrl`` for why. A hit now
carries the row's OWN stored ``wikipedia_url`` alongside ``extract`` (see
:class:`~entity.artist_wikipedia_bio.WikipediaBioHit`), since there is no
longer a caller-supplied URL to assume the hit describes.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from entity.artist_wikipedia_bio import (
    DEFAULT_REFUSAL_COOLDOWN,
    DEFAULT_SUCCESS_TTL,
    WikipediaBioReadError,
    get_cached_artist_wikipedia_bio,
    mark_artist_wikipedia_bio_refused,
    set_cached_artist_wikipedia_bio,
    set_up_artist_wikipedia_bio_schema,
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

        result = await get_cached_artist_wikipedia_bio(pg, discogs_artist_id=_ARTIST_ID, now=_NOW)

        assert result.was_present is False
        assert result.value is None

    async def test_returns_positive_hit_carrying_the_rows_own_url(self):
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(
            return_value={"extract": "Stereolab are a band.", "wikipedia_url": _URL}
        )

        result = await get_cached_artist_wikipedia_bio(pg, discogs_artist_id=_ARTIST_ID, now=_NOW)

        assert result.was_present is True
        assert result.value.extract == "Stereolab are a band."
        assert result.value.wikipedia_url == _URL

    async def test_returns_negative_hit_distinct_from_absent(self):
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(return_value={"extract": None, "wikipedia_url": _URL})

        result = await get_cached_artist_wikipedia_bio(pg, discogs_artist_id=_ARTIST_ID, now=_NOW)

        assert result.was_present is True
        assert result.value.extract is None
        assert result.value.wikipedia_url == _URL

    async def test_binds_artist_id_and_all_three_cutoffs_no_url_param(self):
        # LML#1192 review round 5: no wikipedia_url parameter exists at all
        # anymore -- the row is authoritative, read by discogs_artist_id
        # alone. Round 6, C1-1: a third cutoff joins the two TTL cutoffs --
        # the refusal-cooldown cutoff that lets a row whose refresh was
        # recently refused keep serving its existing (aging) extract
        # instead of reading as a miss forever (see TestRefusalCooldown).
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(return_value=None)

        await get_cached_artist_wikipedia_bio(pg, discogs_artist_id=_ARTIST_ID, now=_NOW)

        args = pg.fetchone.await_args.args
        sql, artist_id, positive_cutoff, negative_cutoff, refusal_cutoff = args
        assert artist_id == _ARTIST_ID
        assert positive_cutoff == _NOW - DEFAULT_SUCCESS_TTL
        assert negative_cutoff == _NOW - DEFAULT_MISS_TTL
        assert refusal_cutoff == _NOW - DEFAULT_REFUSAL_COOLDOWN
        assert "wikipedia_url = $" not in sql

    async def test_pg_error_raises_a_typed_read_error_instead_of_degrading(self):
        # LML#1192 review round 6, C2-3: through round 5 this degraded to
        # was_present=False, indistinguishable from a genuine miss -- the
        # sole production caller (lookup/enrichment/wikipedia_bio.py's
        # resolve_served_bio) then took the "genuine miss" branch and
        # scheduled a live warm, so a discogs-cache PG outage silently
        # inverted "cache down, degrade quietly" into sustained outbound
        # Wikipedia traffic with a 0% write-back rate. Mirrors
        # entity.compilation_track_location's CompilationTrackLocationProbeError
        # (LML#1026): this read's emptiness must be distinguishable from a
        # couldn't-ask, so a "could not consult the cache" outcome raises
        # instead of masquerading as "cache says miss."
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(side_effect=RuntimeError("PG unreachable"))

        with pytest.raises(WikipediaBioReadError):
            await get_cached_artist_wikipedia_bio(pg, discogs_artist_id=_ARTIST_ID, now=_NOW)

    async def test_read_error_chains_the_original_exception(self):
        pg = AsyncMock(spec=PgSource)
        original = RuntimeError("PG unreachable")
        pg.fetchone = AsyncMock(side_effect=original)

        with pytest.raises(WikipediaBioReadError) as exc_info:
            await get_cached_artist_wikipedia_bio(pg, discogs_artist_id=_ARTIST_ID, now=_NOW)

        assert exc_info.value.__cause__ is original


@pytest.mark.asyncio
class TestReadBackAfterAWarmWrittenUnderADifferentUrl:
    """LML#1192 review round 5: pins the fix for the infinite-miss-warm
    loop directly. Before this round, the read predicate additionally
    required ``wikipedia_url = $2`` (the CALLER's current sync pick) -- but
    a fetch-validated warm (LML#1192 review round 4, P0-2) can legitimately
    write under a DIFFERENT, BETTER url than the sync pick's unvalidated
    best guess. The canonical case: the sync picker's ``pick_artist_wikipedia_url``
    has no fetch to validate against, so for an artist like Low or Sade it
    returns the bare, disambiguation-page URL; the validated warm determines
    the REAL article lives at the qualified ``_(band)`` URL and writes the
    row there instead. Reproduced live against real Postgres before this
    fix: writing under the qualified URL and then re-reading with the
    caller's (unchanged) bare sync pick returned ``was_present=False`` --
    every single request re-triggers a warm forever, reintroducing exactly
    the live Wikipedia dependency on the request path the plan's Non-goals
    rule out.

    The row is now authoritative: read by ``discogs_artist_id`` alone,
    serving whichever ``wikipedia_url`` the row itself carries -- so this
    read-back is a HIT, not a miss.
    """

    async def test_a_row_written_under_a_different_url_than_the_sync_pick_still_hits(self):
        pg = AsyncMock(spec=PgSource)
        # Simulates: the sync pick for "Low" is the bare, disambiguation
        # URL, but the validated warm determined "Low_(band)" is the real
        # page and wrote the row under THAT url instead.
        pg.fetchone = AsyncMock(
            return_value={
                "extract": "Low are a band.",
                "wikipedia_url": "https://en.wikipedia.org/wiki/Low_(band)",
            }
        )

        result = await get_cached_artist_wikipedia_bio(pg, discogs_artist_id=_ARTIST_ID, now=_NOW)

        assert result.was_present is True
        assert result.value.extract == "Low are a band."
        assert result.value.wikipedia_url == "https://en.wikipedia.org/wiki/Low_(band)"


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

    async def test_a_refused_upsert_marks_the_row_refused(self):
        # LML#1192 review round 6, C1-1: "INSERT 0 0" is the asyncpg command
        # tag for this UPSERT if and only if the P0-5 null-overwrite guard
        # fired (a conflict occurred and its WHERE clause was false) --
        # verified live against real Postgres (see the round-6 report). On
        # that exact tag, the write must follow up with
        # mark_artist_wikipedia_bio_refused so the read side can widen its
        # predicate and the drain's ordering can deprioritize this row,
        # rather than leaving it silently wedged.
        pg = AsyncMock(spec=PgSource)
        pg.execute = AsyncMock(return_value="INSERT 0 0")

        await set_cached_artist_wikipedia_bio(
            pg,
            discogs_artist_id=_ARTIST_ID,
            wikipedia_url=_URL,
            slug_score=0.0,
            lang="en",
            extract=None,
        )

        # Two execute calls: the refused UPSERT itself, then the mark-refused UPDATE.
        assert pg.execute.await_count == 2
        second_call_sql = pg.execute.await_args_list[1].args[0]
        second_call_artist_id = pg.execute.await_args_list[1].args[1]
        assert "last_refused_at" in second_call_sql
        assert second_call_artist_id == _ARTIST_ID

    async def test_a_successful_insert_does_not_mark_the_row_refused(self):
        pg = AsyncMock(spec=PgSource)
        pg.execute = AsyncMock(return_value="INSERT 0 1")

        await set_cached_artist_wikipedia_bio(
            pg,
            discogs_artist_id=_ARTIST_ID,
            wikipedia_url=_URL,
            slug_score=97.0,
            lang="en",
            extract="Text.",
        )

        assert pg.execute.await_count == 1

    async def test_a_refused_upserts_own_mark_refused_follow_up_is_also_swallowed(self):
        # The follow-up write is itself best-effort -- a PG hiccup on the
        # mark-refused UPDATE must not raise out of the original set call.
        pg = AsyncMock(spec=PgSource)
        pg.execute = AsyncMock(side_effect=["INSERT 0 0", RuntimeError("PG unreachable")])

        await set_cached_artist_wikipedia_bio(
            pg,
            discogs_artist_id=_ARTIST_ID,
            wikipedia_url=_URL,
            slug_score=0.0,
            lang="en",
            extract=None,
        )  # must not raise


@pytest.mark.asyncio
class TestMarkArtistWikipediaBioRefused:
    """LML#1192 review round 6, C1-1: the third clock. ``fetched_at`` owns
    content age; ``last_attempted_at`` owns the drain's progress cursor (round
    3/4, finding 13); ``last_refused_at`` owns "when did we last try and
    fail to refresh this row" -- distinct from both, since a refusal
    advances neither of the other two under the pre-round-6 code, which is
    exactly how a wedged row became permanently unreadable (see
    ``tests/integration/test_artist_wikipedia_bio.py::TestRefusedRefreshDoesNotWedgeTheRow``).
    """

    async def test_advances_both_last_refused_at_and_last_attempted_at(self):
        pg = AsyncMock(spec=PgSource)
        pg.execute = AsyncMock(return_value="UPDATE 1")

        await mark_artist_wikipedia_bio_refused(pg, discogs_artist_id=_ARTIST_ID)

        args = pg.execute.await_args.args
        sql, artist_id = args
        assert artist_id == _ARTIST_ID
        assert "last_refused_at" in sql
        assert "last_attempted_at" in sql
        # Content columns must never move on a refusal.
        assert "fetched_at = " not in sql
        assert "extract = " not in sql

    async def test_write_failure_is_swallowed(self):
        pg = AsyncMock(spec=PgSource)
        pg.execute = AsyncMock(side_effect=RuntimeError("PG unreachable"))

        # Must not raise.
        await mark_artist_wikipedia_bio_refused(pg, discogs_artist_id=_ARTIST_ID)


@pytest.mark.asyncio
class TestRefusalCooldownReadPredicate:
    """LML#1192 review round 6, C1-1: a recent refusal widens the POSITIVE
    read arm so a row whose refresh was refused stays readable -- serving
    the existing, aging extract -- instead of reading as a miss on every
    subsequent request forever. Never widens the NEGATIVE arm: a refused
    write only ever protects an existing POSITIVE row (that's what the
    P0-5 guard blocks), so there's nothing to rescue on the negative side.
    """

    async def test_binds_the_refusal_cutoff_as_a_default_kwarg(self):
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(return_value=None)

        await get_cached_artist_wikipedia_bio(pg, discogs_artist_id=_ARTIST_ID, now=_NOW)

        refusal_cutoff = pg.fetchone.await_args.args[4]
        assert refusal_cutoff == _NOW - DEFAULT_REFUSAL_COOLDOWN

    async def test_a_custom_refusal_cooldown_is_honored(self):
        from datetime import timedelta

        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(return_value=None)
        custom_cooldown = timedelta(days=3)

        await get_cached_artist_wikipedia_bio(
            pg, discogs_artist_id=_ARTIST_ID, now=_NOW, refusal_cooldown=custom_cooldown
        )

        refusal_cutoff = pg.fetchone.await_args.args[4]
        assert refusal_cutoff == _NOW - custom_cooldown

    async def test_upsert_also_advances_last_attempted_at(self):
        # LML#1192 review round 3/4, finding 13: fetched_at describes
        # CONTENT age (the TTL clock get_cached_artist_wikipedia_bio
        # reads); last_attempted_at is the offline drain's own progress
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
        assert "last_attempted_at" in sql

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
    """LML#1192 review round 4, P0-1: last_attempted_at was introduced only
    in #1196 (the downstream drain PR), not #1194 (the PR that actually
    creates lml_cache.artist_wikipedia_bio via CREATE TABLE IF NOT
    EXISTS) -- so #1194 alone deploys a table missing the column
    entirely, and #1196's later CREATE TABLE IF NOT EXISTS is then a
    no-op forever. Both the CREATE TABLE (fresh deploys) and an idempotent
    ALTER TABLE ADD COLUMN IF NOT EXISTS (a table #1194 already created
    without it) must exist here, mirroring
    entity/streaming_url_cache.py's is_error column precedent."""

    async def test_bootstrap_issues_an_add_column_statement_for_last_attempted_at(self):
        pg = _FakePgSource()

        await set_up_artist_wikipedia_bio_schema(pg)

        assert any("CREATE TABLE" in s and "last_attempted_at" in s for s in pg.executed)
        assert any(
            "ADD COLUMN IF NOT EXISTS" in s and "last_attempted_at" in s for s in pg.executed
        )

    async def test_the_add_column_alter_runs_as_its_own_bootstrap_call(self):
        # LML#1121 FIX 5 precedent: PG16 still takes an AccessExclusiveLock
        # on ADD COLUMN IF NOT EXISTS even on the no-op path, so it must
        # not share a transaction with schema/table creation -- a
        # lock_timeout there must not roll back the already-succeeded
        # CREATE TABLE too.
        pg = _FakePgSource()

        await set_up_artist_wikipedia_bio_schema(pg)

        assert pg.acquire_count >= 2


@pytest.mark.asyncio
class TestSchemaCarriesLastRefusedAtColumn:
    """LML#1192 review round 6, C1-1: same P0-1 treatment as
    last_attempted_at above, one column later -- last_refused_at was
    introduced after #1194 may already have deployed a table without it,
    so both the CREATE TABLE (fresh installs) and an idempotent
    ADD COLUMN IF NOT EXISTS (an already-existing table) must exist, and
    the ALTER must run as its own bootstrap call, not share a transaction
    with CREATE TABLE/SCHEMA.
    """

    async def test_bootstrap_issues_an_add_column_statement_for_last_refused_at(self):
        pg = _FakePgSource()

        await set_up_artist_wikipedia_bio_schema(pg)

        assert any("CREATE TABLE" in s and "last_refused_at" in s for s in pg.executed)
        assert any("ADD COLUMN IF NOT EXISTS" in s and "last_refused_at" in s for s in pg.executed)

    async def test_the_add_column_alter_runs_as_its_own_bootstrap_call(self):
        pg = _FakePgSource()

        await set_up_artist_wikipedia_bio_schema(pg)

        assert pg.acquire_count >= 3  # schema+table, last_attempted_at ALTER, last_refused_at ALTER
