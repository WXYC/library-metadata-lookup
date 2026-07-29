"""Integration (``@pytest.mark.pg``) tests for the library-release override read path (LML#850).

All unit coverage mocks ``PgSource``; this file is the matching layer driving
the real DDL, the real ``CHECK (discogs_release_id > 0)`` constraint, and the
real ``get_library_release_overrides`` prefetch against an actual PostgreSQL
connection. Rows are inserted with raw SQL here (the seeder — and its own
round-trip integration coverage — lives in a separate change).

Concrete production risks the unit tests cannot catch:

* The CHECK constraint rejects a non-positive ``discogs_release_id``.
* The ``library_id`` primary key enforces one pin per library row.
* ``get_library_release_overrides`` returns exactly the subset of queried ids
  that carry a pin, keyed correctly.

Run with: pytest -m pg -v tests/integration/test_library_release_override.py
"""

from __future__ import annotations

import asyncpg
import pytest
import pytest_asyncio

from entity.library_release_override import (
    get_library_release_overrides,
    set_up_library_release_override_schema,
)
from tests.integration.conftest import skip_if_named_tables_populated

_INSERT_SQL = (
    "INSERT INTO lml_cache.library_release_override "
    "(library_id, discogs_release_id) VALUES ($1, $2)"
)


@pytest_asyncio.fixture(autouse=True)
async def set_up_override_schema(pg_pool, pg_source):
    """Reset just the override table, then apply its DDL.

    Surgical: drops only ``lml_cache.library_release_override`` (not the whole
    ``lml_cache`` schema), so sibling LML caches present in the shared test PG
    stay intact. ``CREATE SCHEMA IF NOT EXISTS`` inside the bootstrap re-creates
    the schema if a prior suite dropped it. The populated-table veto runs
    FIRST: a mispointed ``DATABASE_URL_TEST`` at the shared discogs-cache PG
    must skip, not drop collected override pins.
    """
    async with pg_pool.acquire() as conn:
        await skip_if_named_tables_populated(conn, (("lml_cache", "library_release_override"),))
        await conn.execute("DROP TABLE IF EXISTS lml_cache.library_release_override")
    await set_up_library_release_override_schema(pg_source)
    yield
    async with pg_pool.acquire() as conn:
        await conn.execute("DROP TABLE IF EXISTS lml_cache.library_release_override")


@pytest.mark.pg
class TestSchemaBootstrap:
    @pytest.mark.asyncio
    async def test_second_boot_is_a_no_op(self, pg_source, pg_pool):
        await set_up_library_release_override_schema(pg_source)
        async with pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT count(*)::int AS n FROM information_schema.tables "
                "WHERE table_schema = 'lml_cache' "
                "AND table_name = 'library_release_override'"
            )
        assert row["n"] == 1

    @pytest.mark.asyncio
    async def test_check_constraint_rejects_non_positive_release_id(self, pg_pool):
        with pytest.raises(asyncpg.PostgresError):
            async with pg_pool.acquire() as conn:
                await conn.execute(_INSERT_SQL, 1, 0)

    @pytest.mark.asyncio
    async def test_library_id_primary_key_rejects_duplicate(self, pg_pool):
        async with pg_pool.acquire() as conn:
            await conn.execute(_INSERT_SQL, 17, 437503)
            with pytest.raises(asyncpg.PostgresError):
                await conn.execute(_INSERT_SQL, 17, 999999)


@pytest.mark.pg
class TestPrefetchReadHelper:
    @pytest.mark.asyncio
    async def test_prefetch_returns_only_pinned_subset(self, pg_source, pg_pool):
        async with pg_pool.acquire() as conn:
            await conn.execute(_INSERT_SQL, 17, 437503)
            await conn.execute(_INSERT_SQL, 28, 9590)

        # Query a superset of ids: only the pinned ones come back, keyed right.
        result = await get_library_release_overrides(pg_source, [17, 28, 999])
        assert result == {17: 437503, 28: 9590}

    @pytest.mark.asyncio
    async def test_prefetch_empty_when_no_pins(self, pg_source):
        result = await get_library_release_overrides(pg_source, [17, 28])
        assert result == {}
