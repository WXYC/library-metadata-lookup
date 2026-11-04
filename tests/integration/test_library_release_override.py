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
* ``get_library_release_pins_strict`` (LML#1503) keys on ``library_id`` (the
  legacy release id space), returns ``(discogs_release_id, source)`` and omits
  ids with no pin.

Run with: pytest -m pg -v tests/integration/test_library_release_override.py
"""

from __future__ import annotations

import asyncpg
import pytest
import pytest_asyncio

from entity.library_release_override import (
    get_library_release_overrides,
    get_library_release_pins_strict,
    set_up_library_release_override_schema,
)
from tests.integration.conftest import skip_if_named_tables_populated

_INSERT_SQL = (
    "INSERT INTO lml_cache.library_release_override "
    "(library_id, discogs_release_id) VALUES ($1, $2)"
)
_INSERT_WITH_SOURCE_SQL = (
    "INSERT INTO lml_cache.library_release_override "
    "(library_id, discogs_release_id, source) VALUES ($1, $2, $3)"
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


@pytest.mark.pg
class TestPinsReader:
    """The pinned-tracklists route's reader (LML#1503), against the real table."""

    @pytest.mark.asyncio
    async def test_returns_release_and_source_keyed_on_library_id(self, pg_source, pg_pool):
        async with pg_pool.acquire() as conn:
            await conn.execute(_INSERT_WITH_SOURCE_SQL, 17, 437503, "alex-l-2026")
            await conn.execute(_INSERT_WITH_SOURCE_SQL, 28, 9590, "other-batch")
            # Not requested: must not leak into the answer.
            await conn.execute(_INSERT_WITH_SOURCE_SQL, 99, 1, "alex-l-2026")

        result = await get_library_release_pins_strict(pg_source, [17, 28])
        assert result == {17: (437503, "alex-l-2026"), 28: (9590, "other-batch")}

    @pytest.mark.asyncio
    async def test_missing_ids_are_absent(self, pg_source, pg_pool):
        async with pg_pool.acquire() as conn:
            await conn.execute(_INSERT_WITH_SOURCE_SQL, 17, 437503, "alex-l-2026")

        result = await get_library_release_pins_strict(pg_source, [17, 999, 1000])
        assert result == {17: (437503, "alex-l-2026")}

    @pytest.mark.asyncio
    async def test_source_defaults_to_the_table_default(self, pg_source, pg_pool):
        async with pg_pool.acquire() as conn:
            await conn.execute(_INSERT_SQL, 17, 437503)

        assert await get_library_release_pins_strict(pg_source, [17]) == {
            17: (437503, "alex-l-2026")
        }

    @pytest.mark.asyncio
    async def test_int4_range_edges_round_trip(self, pg_source, pg_pool):
        async with pg_pool.acquire() as conn:
            await conn.execute(_INSERT_WITH_SOURCE_SQL, 2**31 - 1, 5, "edge")
            await conn.execute(_INSERT_WITH_SOURCE_SQL, -(2**31), 6, "edge")

        result = await get_library_release_pins_strict(pg_source, [2**31 - 1, -(2**31)])
        assert result == {2**31 - 1: (5, "edge"), -(2**31): (6, "edge")}
