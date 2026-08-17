"""Integration tests for the artist Wikipedia bio cache (LML#513/#1192).

Mirrors ``tests/integration/test_release_resolution_cache.py``. All unit
coverage mocks ``PgSource``; this file is the matching ``@pytest.mark.pg``
layer driving the real schema, the real UPSERT, the real dual-TTL math, and
(LML#1192 review round 5) the row-is-authoritative read against an actual
PostgreSQL connection.

Concrete production risks the unit tests cannot catch:

* TIMESTAMPTZ codec drift -- asyncpg returns aware ``datetime``; a naive
  value would break the ``fetched_at`` cutoff comparison.
* UPSERT semantics -- the single-column-PK ``ON CONFLICT`` updates in place,
  including ``wikipedia_url`` itself.
* The round-5 read predicate: a row is now readable by ``discogs_artist_id``
  alone, regardless of which ``wikipedia_url`` it was written under --
  through round 4 a row stored under a DIFFERENT url than the caller's
  current pick was invisible, which made a fetch-validated warm's own write
  permanently unreadable by a caller whose sync pick never changes (see
  ``TestRowIsAuthoritative``, and ``entity/artist_wikipedia_bio.py``'s
  module docstring for the full history).
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
    async def test_set_then_get_returns_extract_and_url(self, pg_source):
        await set_cached_artist_wikipedia_bio(
            pg_source,
            discogs_artist_id=99,
            wikipedia_url=_URL,
            slug_score=97.6,
            lang="en",
            extract="Stereolab are a band.",
        )
        result = await get_cached_artist_wikipedia_bio(pg_source, discogs_artist_id=99)
        assert result.was_present is True
        assert result.value.extract == "Stereolab are a band."
        assert result.value.wikipedia_url == _URL

    @pytest.mark.asyncio
    async def test_negative_result_round_trips_as_present_with_none_extract(self, pg_source):
        await set_cached_artist_wikipedia_bio(
            pg_source,
            discogs_artist_id=99,
            wikipedia_url=_URL,
            slug_score=85.0,
            lang="en",
            extract=None,
        )
        result = await get_cached_artist_wikipedia_bio(pg_source, discogs_artist_id=99)
        assert result.was_present is True
        assert result.value.extract is None
        assert result.value.wikipedia_url == _URL

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
        result = await get_cached_artist_wikipedia_bio(pg_source, discogs_artist_id=99)
        assert result.value.extract == "Newly-found biography."


@pytest.mark.pg
class TestRefusedRefreshDoesNotWedgeTheRow:
    """LML#1192 review round 6, C1-1: a positive row that ages past its TTL,
    then has its warm's negative re-check refused by the P0-5 null-overwrite
    guard, must not become permanently unreadable.

    Sequence: a positive row ages past ``DEFAULT_SUCCESS_TTL`` (so the NEXT
    read is a genuine miss and a warm gets scheduled in production) -> the
    warm re-fetches, finds nothing (the page was deleted/renamed), and
    writes a negative -- the P0-5 guard at ``_UPSERT_SQL`` correctly refuses
    that write outright (``WHERE NOT (existing.extract IS NOT NULL AND
    EXCLUDED.extract IS NULL)`` is false), so the row's ``extract`` and
    ``fetched_at`` are untouched -- but nothing else records that a refusal
    JUST happened. A THIRD read, with no fix, re-evaluates the exact same
    stale predicate against the exact same untouched ``fetched_at`` and
    reports a miss again, forever: every subsequent request repeats the
    refused re-check, and no offline drain mode can rescue it either
    (``incremental`` skips it because the row exists; ``--retry-misses``
    skips it because ``extract IS NULL`` is false; ``--repick``/
    ``--refresh-stale`` both hit the same P0-5 refusal on their own retry).

    This is the wedge, reproduced against real Postgres with no mocking:
    the row genuinely holds a usable, if aging, extract the whole time, yet
    is reported absent on every read once the refusal has happened once.
    """

    @pytest.mark.asyncio
    async def test_row_survives_a_refused_post_ttl_refresh_and_stays_readable(
        self, pg_source, pg_pool
    ):
        await set_cached_artist_wikipedia_bio(
            pg_source,
            discogs_artist_id=99,
            wikipedia_url=_URL,
            slug_score=95.0,
            lang="en",
            extract="Stereolab are a band.",
        )
        now = datetime(2026, 8, 14, tzinfo=UTC)
        ancient = now - timedelta(days=31)
        async with pg_pool.acquire() as conn:
            await conn.execute(
                "UPDATE lml_cache.artist_wikipedia_bio SET fetched_at = $1 "
                "WHERE discogs_artist_id = 99",
                ancient,
            )

        # First read past TTL: a genuine miss, as designed -- this is what
        # schedules the warm in production. Not the bug by itself.
        first = await get_cached_artist_wikipedia_bio(pg_source, discogs_artist_id=99, now=now)
        assert first.was_present is False

        # The warm re-fetches live, finds nothing (deleted/renamed page),
        # and attempts to write a negative -- the P0-5 guard refuses this
        # at the database level, so extract/fetched_at are untouched.
        await set_cached_artist_wikipedia_bio(
            pg_source,
            discogs_artist_id=99,
            wikipedia_url=_URL,
            slug_score=0.0,
            lang="en",
            extract=None,
        )
        async with pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT extract, fetched_at FROM lml_cache.artist_wikipedia_bio "
                "WHERE discogs_artist_id = 99"
            )
        assert row["extract"] == "Stereolab are a band.", "the P0-5 guard must have refused"
        assert row["fetched_at"] == ancient, "a refused write must not touch fetched_at"

        # The wedge: a third read, moments later, with nothing else having
        # changed. The row still genuinely holds a usable extract -- it
        # must be served as a hit, not reported absent forever.
        third = await get_cached_artist_wikipedia_bio(pg_source, discogs_artist_id=99, now=now)
        assert third.was_present is True, (
            "a row whose refresh was refused must stay readable -- reporting it "
            "absent forever re-triggers a live warm on every future request with "
            "no drain mode able to rescue it"
        )
        assert third.value.extract == "Stereolab are a band."


@pytest.mark.pg
class TestRowIsAuthoritative:
    """LML#1192 review round 5: through round 4, a row stored under a
    DIFFERENT ``wikipedia_url`` than the caller's current pick was
    invisible to ``get_cached_artist_wikipedia_bio`` -- framed at the time
    as "self-healing" a stale Phase-A pick. That predicate made a
    fetch-validated warm's own write (round 4, P0-2 -- which can
    legitimately choose a DIFFERENT, better URL than the request path's
    unvalidated sync pick, the canonical case being Low/Sade's bare
    disambiguation-page URL vs. their real qualified ``_(band)`` article)
    permanently unreadable by a caller whose sync pick never changes.
    Reproduced live against real Postgres before this fix (see the round-5
    coordinator report): writing under a qualified URL and reading back
    with the original bare URL returned ``was_present=False`` forever. The
    row is now read by ``discogs_artist_id`` alone and reports its own
    ``wikipedia_url`` on a hit -- these tests pin that against a real
    round-trip, not just the mocked unit layer.
    """

    @pytest.mark.asyncio
    async def test_a_row_written_under_one_url_is_still_a_hit_regardless_of_which_url_written(
        self, pg_source
    ):
        # No "caller's current pick" is compared against at all anymore --
        # any wikipedia_url the row was written under round-trips as a hit.
        await set_cached_artist_wikipedia_bio(
            pg_source,
            discogs_artist_id=99,
            wikipedia_url="https://en.wikipedia.org/wiki/Low_(band)",
            slug_score=100.0,
            lang="en",
            extract="Low are a band.",
        )
        result = await get_cached_artist_wikipedia_bio(pg_source, discogs_artist_id=99)
        assert result.was_present is True
        assert result.value.extract == "Low are a band."
        assert result.value.wikipedia_url == "https://en.wikipedia.org/wiki/Low_(band)"

    @pytest.mark.asyncio
    async def test_a_negative_row_still_reports_its_own_url_on_a_negative_hit(self, pg_source):
        await set_cached_artist_wikipedia_bio(
            pg_source,
            discogs_artist_id=99,
            wikipedia_url="https://en.wikipedia.org/wiki/A_404_Page",
            slug_score=82.0,
            lang="en",
            extract=None,
        )
        result = await get_cached_artist_wikipedia_bio(pg_source, discogs_artist_id=99)
        assert result.was_present is True
        assert result.value.extract is None
        assert result.value.wikipedia_url == "https://en.wikipedia.org/wiki/A_404_Page"

    @pytest.mark.asyncio
    async def test_replacing_the_stored_url_via_upsert_is_visible_on_the_very_next_read(
        self, pg_source
    ):
        # Direct simulation of the warm-rewiring scenario: a first write
        # under the bare url, then a later write (the validated warm)
        # replaces it with the qualified url -- the read must see the NEW
        # url immediately, keyed on discogs_artist_id alone.
        await set_cached_artist_wikipedia_bio(
            pg_source,
            discogs_artist_id=99,
            wikipedia_url="https://en.wikipedia.org/wiki/Low",
            slug_score=75.0,
            lang="en",
            extract=None,
        )
        await set_cached_artist_wikipedia_bio(
            pg_source,
            discogs_artist_id=99,
            wikipedia_url="https://en.wikipedia.org/wiki/Low_(band)",
            slug_score=100.0,
            lang="en",
            extract="Low are a band.",
        )
        result = await get_cached_artist_wikipedia_bio(pg_source, discogs_artist_id=99)
        assert result.was_present is True
        assert result.value.extract == "Low are a band."
        assert result.value.wikipedia_url == "https://en.wikipedia.org/wiki/Low_(band)"


@pytest.mark.pg
class TestDualTTL:
    @pytest.mark.asyncio
    async def test_absent_entry_reads_not_present(self, pg_source):
        result = await get_cached_artist_wikipedia_bio(pg_source, discogs_artist_id=99)
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
        result = await get_cached_artist_wikipedia_bio(pg_source, discogs_artist_id=99, now=now)
        assert result.was_present is True
        assert result.value.extract == "Fresh text."

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
        result = await get_cached_artist_wikipedia_bio(pg_source, discogs_artist_id=99, now=now)
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
        result = await get_cached_artist_wikipedia_bio(pg_source, discogs_artist_id=99, now=now)
        assert result.was_present is True
        assert result.value.extract is None

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
        result = await get_cached_artist_wikipedia_bio(pg_source, discogs_artist_id=99, now=now)
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
        negative = await get_cached_artist_wikipedia_bio(pg_source, discogs_artist_id=1, now=now)
        positive = await get_cached_artist_wikipedia_bio(pg_source, discogs_artist_id=2, now=now)
        assert negative.was_present is False
        assert positive.was_present is True
        assert positive.value.extract == "Still fresh."
