"""pg integration tests for the LML#804/#815 search-arm statement_timeout bound.

Every trigram search arm on the discogs-cache pool runs its query inside a
``SET LOCAL`` transaction pinning ``statement_timeout`` + ``work_mem``, via the
shared ``DiscogsCacheService._bounded_fetch`` helper (LML#815). LML#804 bounded
the two incident arms (``search_releases`` / ``search_releases_by_track``);
LML#815 extends the bound to ``search_artists_by_name``,
``search_releases_by_title``, ``search_tracks_by_title``, ``autocomplete_tracks``,
and ``artist_trigram_candidates`` (whose ``SET LOCAL`` previously pinned only the
pg_trgm floor). This suite pins the load-bearing behaviors against real
PostgreSQL:

* A query that overruns ``statement_timeout`` is cancelled by the server as
  ``asyncpg.QueryCanceledError`` and the search arm maps it into
  ``CacheUnavailableError`` — the fallthrough seam's cache-only degrade
  boundary — never a 500 (LML#755's "couldn't ask" principle).
* The pool slot is released: the pool is immediately usable afterward.
* ``SET LOCAL`` scoping does not leak to the write-serving pool session — a
  write on the same pool is unbounded by the search arm's tiny timeout.

The timeout is provoked deterministically by installing a *slow* ``f_unaccent``
(the wrapper every trigram predicate calls) that ``pg_sleep``s, so a search
with a tiny ``statement_timeout`` always overruns. The fixture restores the
standard wrapper on teardown.

Data safety: the fixture **skips** when the tables it seeds already exist (a
live discogs-cache) rather than writing into real cache data — the same posture
as ``test_search_releases_credits.py``.

Run with: ``pytest -m pg -v tests/integration/test_search_arm_statement_timeout.py``
"""

from __future__ import annotations

import asyncpg
import pytest
import pytest_asyncio

from discogs.cache_service import CacheUnavailableError, DiscogsCacheService
from tests.integration.conftest import (
    F_UNACCENT_WRAPPER_SQL,
    skip_if_drop_targets_populated,
)

pytestmark = pytest.mark.pg

# A tiny per-transaction ceiling; the slow f_unaccent sleeps far longer, so the
# search-arm query always overruns it.
_TINY_TIMEOUT_MS = 100

# f_unaccent that sleeps 0.5s per evaluation, guaranteeing the trigram scan
# overruns the 100ms statement_timeout. VOLATILE so PG can't fold it away.
_SLOW_F_UNACCENT_SQL = (
    "CREATE OR REPLACE FUNCTION f_unaccent(text) RETURNS text "
    "AS $$ SELECT pg_sleep(0.5); SELECT $1 $$ LANGUAGE sql VOLATILE"
)


@pytest_asyncio.fixture
async def slow_cache(pg_pool):
    """A ``DiscogsCacheService`` whose search arms always overrun a tiny timeout.

    Seeds the tables every trigram arm touches (release / release_artist /
    release_track / release_track_artist for the release + track arms; artist /
    artist_name_variation for ``search_artists_by_name``), installs the slow
    ``f_unaccent``, and yields a service pinned to a 100ms ``statement_timeout``.
    Restores the standard ``f_unaccent`` wrapper and drops the tables on teardown.
    """
    async with pg_pool.acquire() as conn:
        await skip_if_drop_targets_populated(
            conn,
            (
                "release",
                "release_artist",
                "release_track",
                "release_track_artist",
                "artist",
                "artist_name_variation",
            ),
        )
        try:
            await conn.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
            await conn.execute("CREATE EXTENSION IF NOT EXISTS unaccent")
        except Exception as e:
            pytest.skip(f"pg_trgm/unaccent extensions unavailable: {e}")

        for table in (
            "release_track_artist",
            "release_track",
            "release_artist",
            "release",
            "artist_name_variation",
            "artist",
        ):
            await conn.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
        await conn.execute(
            "CREATE TABLE release (id integer PRIMARY KEY, title text NOT NULL, artwork_url text)"
        )
        await conn.execute(
            "CREATE TABLE release_artist ("
            "release_id integer NOT NULL REFERENCES release(id) ON DELETE CASCADE, "
            "artist_id integer, artist_name text NOT NULL, extra integer DEFAULT 0, role text)"
        )
        await conn.execute(
            "CREATE TABLE release_track ("
            "release_id integer NOT NULL REFERENCES release(id) ON DELETE CASCADE, "
            "sequence integer, position text, title text NOT NULL, duration text)"
        )
        await conn.execute(
            "CREATE TABLE release_track_artist ("
            "release_id integer NOT NULL REFERENCES release(id) ON DELETE CASCADE, "
            "track_sequence integer, artist_name text, extra integer DEFAULT 0)"
        )
        await conn.execute("INSERT INTO release (id, title) VALUES (1, 'Dots and Loops')")
        # artist_id must be non-NULL: artist_trigram_candidates filters
        # ``ra.artist_id IS NOT NULL`` first, and PG would short-circuit the row
        # out before the slow f_unaccent runs (masking the timeout) if it were
        # NULL. 1 matches the seeded ``artist`` row.
        await conn.execute(
            "INSERT INTO release_artist (release_id, artist_id, artist_name, extra) "
            "VALUES (1, 1, 'Stereolab', 0)"
        )
        await conn.execute(
            "INSERT INTO release_track (release_id, sequence, title) VALUES (1, 1, 'Brakhage')"
        )
        # Artist tables for search_artists_by_name (LML#815).
        await conn.execute("CREATE TABLE artist (id integer PRIMARY KEY, name text NOT NULL)")
        await conn.execute(
            "CREATE TABLE artist_name_variation ("
            "artist_id integer NOT NULL REFERENCES artist(id) ON DELETE CASCADE, name text NOT NULL)"
        )
        await conn.execute("INSERT INTO artist (id, name) VALUES (1, 'Stereolab')")
        await conn.execute(
            "INSERT INTO artist_name_variation (artist_id, name) VALUES (1, 'Stereolab')"
        )
        # Install the slow wrapper LAST so setup DDL/seeding isn't slowed.
        await conn.execute(_SLOW_F_UNACCENT_SQL)

    yield DiscogsCacheService(
        pg_pool, search_statement_timeout_ms=_TINY_TIMEOUT_MS, search_work_mem="64MB"
    )

    async with pg_pool.acquire() as conn:
        # Restore the standard (fast) wrapper so a leaked slow function can't
        # poison sibling pg suites, then drop the seeded tables.
        try:
            await conn.execute(F_UNACCENT_WRAPPER_SQL)
        except Exception:
            pass
        for table in (
            "release_track_artist",
            "release_track",
            "release_artist",
            "release",
            "artist_name_variation",
            "artist",
        ):
            await conn.execute(f"DROP TABLE IF EXISTS {table} CASCADE")


class TestSearchArmStatementTimeout:
    @pytest.mark.asyncio
    async def test_search_releases_timeout_degrades_to_cache_unavailable(self, slow_cache):
        """A trigram query overrunning statement_timeout raises
        CacheUnavailableError (degrade to cache-only, no 500), with the
        server-side QueryCanceledError preserved as the cause."""
        with pytest.raises(CacheUnavailableError) as exc_info:
            await slow_cache.search_releases(artist="Stereolab")
        assert isinstance(exc_info.value.__cause__, asyncpg.exceptions.QueryCanceledError)

    @pytest.mark.asyncio
    async def test_search_releases_by_track_timeout_degrades(self, slow_cache):
        with pytest.raises(CacheUnavailableError) as exc_info:
            await slow_cache.search_releases_by_track("Brakhage", "Stereolab")
        assert isinstance(exc_info.value.__cause__, asyncpg.exceptions.QueryCanceledError)

    @pytest.mark.asyncio
    async def test_search_artists_by_name_timeout_degrades(self, slow_cache):
        """LML#815: the #359 dominant chokepoint now degrades on timeout too."""
        with pytest.raises(CacheUnavailableError) as exc_info:
            await slow_cache.search_artists_by_name("Stereolab")
        assert isinstance(exc_info.value.__cause__, asyncpg.exceptions.QueryCanceledError)

    @pytest.mark.asyncio
    async def test_search_releases_by_title_timeout_degrades(self, slow_cache):
        with pytest.raises(CacheUnavailableError) as exc_info:
            await slow_cache.search_releases_by_title("Dots and Loops")
        assert isinstance(exc_info.value.__cause__, asyncpg.exceptions.QueryCanceledError)

    @pytest.mark.asyncio
    async def test_search_tracks_by_title_timeout_degrades(self, slow_cache):
        with pytest.raises(CacheUnavailableError) as exc_info:
            await slow_cache.search_tracks_by_title("Brakhage")
        assert isinstance(exc_info.value.__cause__, asyncpg.exceptions.QueryCanceledError)

    @pytest.mark.asyncio
    async def test_autocomplete_tracks_timeout_degrades(self, slow_cache):
        with pytest.raises(CacheUnavailableError) as exc_info:
            await slow_cache.autocomplete_tracks("Stereolab", "Bra")
        assert isinstance(exc_info.value.__cause__, asyncpg.exceptions.QueryCanceledError)

    @pytest.mark.asyncio
    async def test_artist_trigram_candidates_timeout_degrades(self, slow_cache):
        """LML#815: reconciled — its SET LOCAL now carries statement_timeout +
        work_mem (not just the pg_trgm floor), so it degrades like the rest."""
        with pytest.raises(CacheUnavailableError) as exc_info:
            await slow_cache.artist_trigram_candidates(["Stereolab"])
        assert isinstance(exc_info.value.__cause__, asyncpg.exceptions.QueryCanceledError)

    @pytest.mark.asyncio
    async def test_pool_slot_released_after_timeout(self, slow_cache, pg_pool):
        """The acquire/transaction exit releases the connection on cancel, so
        the pool is immediately usable — no leaked slot behind a long query."""
        with pytest.raises(CacheUnavailableError):
            await slow_cache.search_releases(artist="Stereolab")
        # If the slot had leaked, this would hang until the pool timed out.
        assert await pg_pool.fetchval("SELECT 1") == 1

    @pytest.mark.asyncio
    async def test_set_local_does_not_leak_to_write_path(self, slow_cache, pg_pool):
        """SET LOCAL is transaction-scoped: a write on the same pool is NOT
        bounded by the search arm's 100ms timeout. A 250ms write that would be
        cancelled under a leaked bound completes cleanly."""
        with pytest.raises(CacheUnavailableError):
            await slow_cache.search_releases(artist="Stereolab")

        async with pg_pool.acquire() as conn:
            # The pooled session carries the server default, not the 100ms LOCAL.
            assert await conn.fetchval("SHOW statement_timeout") == "0"
            async with conn.transaction():
                await conn.execute("CREATE TEMP TABLE _leak_probe (v int) ON COMMIT DROP")
                # 250ms > the 100ms search timeout; completes because SET LOCAL
                # reverted at the search arm's transaction end.
                await conn.execute("INSERT INTO _leak_probe SELECT 1 FROM pg_sleep(0.25)")
                assert await conn.fetchval("SELECT count(*) FROM _leak_probe") == 1
