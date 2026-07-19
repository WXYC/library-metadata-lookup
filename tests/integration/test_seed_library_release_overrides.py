"""Integration (``@pytest.mark.pg``) tests for the override seeder (LML#850).

Drives the seeder's real ``ON CONFLICT (library_id) DO UPDATE`` upsert against
an actual PostgreSQL connection and reads back through the production prefetch
helper. Unit coverage (``tests/unit/test_seed_library_release_overrides.py``)
mocks PG for the CSV-parse + dry-run logic; this proves the SQL itself.

Concrete risks the unit tests cannot catch:

* Upsert semantics — a re-seed with a corrected release updates in place
  (idempotent), never duplicates a ``library_id``.
* The dry-run path truly writes nothing.
* The reversibility contract: a scoped ``DELETE ... WHERE source = ...`` removes
  exactly the seeded rows.

Run with: pytest -m pg -v tests/integration/test_seed_library_release_overrides.py
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from entity.library_release_override import (
    get_library_release_overrides,
    set_up_library_release_override_schema,
)
from entity.sources import PgSource
from scripts.seed_library_release_overrides import OverrideRow, seed_overrides

_SOURCE = "alex-l-2026"


@pytest_asyncio.fixture
async def pg_source(pg_pool):
    """A ``PgSource`` borrowing the test pool (no-op close)."""
    return PgSource(pool=pg_pool)


@pytest_asyncio.fixture(autouse=True)
async def set_up_override_schema(pg_pool, pg_source):
    """Reset just the override table, then apply its DDL (surgical — see the
    read-path integration file for the rationale)."""
    async with pg_pool.acquire() as conn:
        await conn.execute("DROP TABLE IF EXISTS lml_cache.library_release_override")
    await set_up_library_release_override_schema(pg_source)
    yield
    async with pg_pool.acquire() as conn:
        await conn.execute("DROP TABLE IF EXISTS lml_cache.library_release_override")


@pytest.mark.pg
class TestSeedRoundTrip:
    @pytest.mark.asyncio
    async def test_seed_then_prefetch_returns_pins(self, pg_source, pg_pool):
        rows = [
            OverrideRow(library_id=17, discogs_release_id=437503),
            OverrideRow(library_id=28, discogs_release_id=9590),
        ]
        async with pg_pool.acquire() as conn:
            written = await seed_overrides(conn, rows, execute=True, source=_SOURCE)
        assert written == 2

        result = await get_library_release_overrides(pg_source, [17, 28, 999])
        assert result == {17: 437503, 28: 9590}

    @pytest.mark.asyncio
    async def test_reseed_updates_in_place_and_is_idempotent(self, pg_source, pg_pool):
        async with pg_pool.acquire() as conn:
            await seed_overrides(
                conn,
                [OverrideRow(library_id=17, discogs_release_id=437503)],
                execute=True,
                source=_SOURCE,
            )
            # Re-seed the same library id with a CORRECTED release — upsert
            # updates the existing row in place (one row, new value).
            await seed_overrides(
                conn,
                [OverrideRow(library_id=17, discogs_release_id=111111)],
                execute=True,
                source=_SOURCE,
            )
            count = await conn.fetchval(
                "SELECT count(*)::int FROM lml_cache.library_release_override WHERE library_id = 17"
            )
        assert count == 1
        result = await get_library_release_overrides(pg_source, [17])
        assert result == {17: 111111}

    @pytest.mark.asyncio
    async def test_reseed_does_not_clobber_a_different_source_pin(self, pg_source, pg_pool):
        # Data safety: a pin later hand-corrected under a DIFFERENT source (e.g. a
        # Music-Director fix) must survive a re-applied CSV. The source-guarded
        # ON CONFLICT ... WHERE keeps it untouched.
        async with pg_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO lml_cache.library_release_override "
                "(library_id, discogs_release_id, source) VALUES (17, 222222, 'md-fix')"
            )
            # Re-run Alex's seeder over the same library_id with a different value.
            await seed_overrides(
                conn,
                [OverrideRow(library_id=17, discogs_release_id=437503)],
                execute=True,
                source=_SOURCE,
            )
            row = await conn.fetchrow(
                "SELECT discogs_release_id, source FROM lml_cache.library_release_override "
                "WHERE library_id = 17"
            )
        # The MD fix is preserved — NOT reverted to Alex's value or re-stamped.
        assert row["discogs_release_id"] == 222222
        assert row["source"] == "md-fix"

    @pytest.mark.asyncio
    async def test_dry_run_writes_nothing(self, pg_source, pg_pool):
        async with pg_pool.acquire() as conn:
            written = await seed_overrides(
                conn,
                [OverrideRow(library_id=17, discogs_release_id=437503)],
                execute=False,
                source=_SOURCE,
            )
        assert written == 0
        result = await get_library_release_overrides(pg_source, [17])
        assert result == {}

    @pytest.mark.asyncio
    async def test_rollback_delete_by_source(self, pg_source, pg_pool):
        # The reversibility contract: a scoped DELETE by source removes exactly
        # the seeded rows.
        async with pg_pool.acquire() as conn:
            await seed_overrides(
                conn,
                [OverrideRow(library_id=17, discogs_release_id=437503)],
                execute=True,
                source=_SOURCE,
            )
            await conn.execute(
                "DELETE FROM lml_cache.library_release_override WHERE source = $1", _SOURCE
            )
        result = await get_library_release_overrides(pg_source, [17])
        assert result == {}
