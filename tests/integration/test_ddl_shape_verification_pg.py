"""Integration coverage for the LML#1210 shape-verification catalog queries.

``tests/unit/test_ddl_shape_verification.py`` drives the policy through a fake
catalog. That proves the decision logic but not the SQL: a typo in a
``pg_catalog`` query, or a column alias that doesn't match what the reader
expects, would surface only at boot -- in the exact code path whose whole job
is to notice problems at boot. Worse, both mistakes fail *open* in the
"present" direction and *closed* in the "absent" one, so a broken query looks
like a healthy schema right up until it looks like a catastrophic one.

This file runs the real queries against a real PostgreSQL and asserts each
object kind is detected both present and absent. It works on a scratch table of
its own rather than any live ``lml_cache.*`` table, so it can drop and recreate
freely.

Run with: pytest -m pg -v tests/integration/test_ddl_shape_verification_pg.py
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from entity.ddl import (
    ExpectedShape,
    LmlCacheSchemaMismatchError,
    _missing_objects,
    bootstrap_lml_cache_table,
    parse_expected_shape,
    verifying_lml_cache_shape,
)
from tests.integration.conftest import skip_if_named_tables_populated

_TABLE = "ddl_shape_verification_scratch"
_INDEX = f"idx_{_TABLE}_artist"
_DDL_SCHEMA = "CREATE SCHEMA IF NOT EXISTS lml_cache"
_DDL_TABLE = f"""\
CREATE TABLE IF NOT EXISTS lml_cache.{_TABLE} (
    id BIGINT PRIMARY KEY,
    artist TEXT NOT NULL
)"""
_DDL_ADD_COLUMN = f"ALTER TABLE lml_cache.{_TABLE} ADD COLUMN IF NOT EXISTS album TEXT"
_DDL_INDEX = f"CREATE INDEX IF NOT EXISTS {_INDEX} ON lml_cache.{_TABLE} (artist)"

# Parses as the `album` column expectation and fails only while the column is
# absent: PG's IF NOT EXISTS short-circuits BEFORE resolving the column type, so
# once `album` exists this statement quietly succeeds. That makes it exactly the
# "the ALTER failed and the column really is missing" fixture and nothing else.
_DDL_ADD_COLUMN_FAILS_WHEN_ABSENT = (
    f"ALTER TABLE lml_cache.{_TABLE} ADD COLUMN IF NOT EXISTS album no_such_type"
)


@pytest_asyncio.fixture(autouse=True)
async def scratch_table(pg_pool):
    """Drop the scratch table before and after; refuse to run if it has rows."""
    async with pg_pool.acquire() as conn:
        await skip_if_named_tables_populated(conn, (("lml_cache", _TABLE),))
        await conn.execute(f"DROP TABLE IF EXISTS lml_cache.{_TABLE}")
    yield
    async with pg_pool.acquire() as conn:
        await conn.execute(f"DROP TABLE IF EXISTS lml_cache.{_TABLE}")


@pytest.mark.pg
@pytest.mark.asyncio
class TestCatalogQueriesAgainstRealPostgres:
    """Each object kind, detected both present and absent."""

    async def test_a_freshly_bootstrapped_table_verifies_clean(self, pg_source):
        """The success path end to end: real DDL, real catalog, no complaint."""
        with verifying_lml_cache_shape():
            await bootstrap_lml_cache_table(
                pg_source, _DDL_SCHEMA, _DDL_TABLE, _DDL_ADD_COLUMN, _DDL_INDEX
            )

        expected = parse_expected_shape((_DDL_SCHEMA, _DDL_TABLE, _DDL_ADD_COLUMN, _DDL_INDEX))
        assert await _missing_objects(pg_source, expected) == []

    async def test_an_absent_table_and_index_are_reported(self, pg_source):
        expected = parse_expected_shape((_DDL_SCHEMA, _DDL_TABLE, _DDL_INDEX))

        missing = await _missing_objects(pg_source, expected)

        assert f"table lml_cache.{_TABLE}" in missing
        assert f"index lml_cache.{_INDEX}" in missing

    async def test_an_absent_column_on_a_present_table_is_reported(self, pg_source):
        """The observed incident's dangerous twin: table fine, column short."""
        with verifying_lml_cache_shape():
            await bootstrap_lml_cache_table(pg_source, _DDL_SCHEMA, _DDL_TABLE)

        missing = await _missing_objects(pg_source, parse_expected_shape((_DDL_ADD_COLUMN,)))

        assert missing == [f"column lml_cache.{_TABLE}.album"]

    async def test_a_column_on_an_absent_table_reports_the_table_not_the_column(self, pg_source):
        """An ALTER-only call expects no tables of its own, so table presence is
        queried directly. Inferring it from the column rows would let a missing
        table pass as "no columns to check"."""
        missing = await _missing_objects(pg_source, parse_expected_shape((_DDL_ADD_COLUMN,)))

        assert missing == [f"table lml_cache.{_TABLE}"]

    async def test_an_absent_schema_is_reported(self, pg_source):
        missing = await _missing_objects(
            pg_source, ExpectedShape(schemas={"lml_cache_does_not_exist"})
        )

        assert missing == ["schema lml_cache_does_not_exist"]

    async def test_an_unquoted_mixed_case_identifier_matches_the_folded_catalog(self, pg_source):
        """Only PostgreSQL can prove this one: it folds unquoted identifiers to
        lowercase and the catalog stores them folded, so an as-written capture
        would read a perfectly healthy object as absent. The unit suite's fake
        catalog cannot distinguish the two -- it stores whatever it is handed."""
        with verifying_lml_cache_shape():
            await bootstrap_lml_cache_table(pg_source, _DDL_SCHEMA, _DDL_TABLE, _DDL_ADD_COLUMN)

        mixed_case = f"ALTER TABLE lml_cache.{_TABLE.upper()} ADD COLUMN IF NOT EXISTS ALBUM TEXT"

        assert await _missing_objects(pg_source, parse_expected_shape((mixed_case,))) == []

    async def test_the_live_lml_cache_schema_is_present(self, pg_source):
        """The schema query's positive case. Without it, a query that returned
        nothing for everything would still pass the test above."""
        assert await _missing_objects(pg_source, ExpectedShape(schemas={"lml_cache"})) == []


@pytest.mark.pg
@pytest.mark.asyncio
class TestFailedDdlAgainstRealPostgres:
    """The two outcomes a failed bootstrap statement can have, for real."""

    async def test_a_failed_alter_whose_column_is_absent_escalates(self, pg_source):
        """The case the 2026-08-17 incident was one column away from: the ALTER
        fails and the column genuinely is not there, so the cache would degrade
        to silent misses. The boot must refuse to call this ready."""
        with verifying_lml_cache_shape():
            await bootstrap_lml_cache_table(pg_source, _DDL_SCHEMA, _DDL_TABLE)

            with pytest.raises(LmlCacheSchemaMismatchError) as excinfo:
                await bootstrap_lml_cache_table(pg_source, _DDL_ADD_COLUMN_FAILS_WHEN_ABSENT)

        assert f"column lml_cache.{_TABLE}.album" in str(excinfo.value)
        assert excinfo.value.__cause__ is not None, "the PG error must stay in the traceback"

    async def test_a_failed_alter_whose_column_already_exists_is_a_no_op(
        self, pg_pool, pg_source, caplog
    ):
        """The incident as it actually happened: the statement failed against a
        table that already had the column, so nothing was degraded. It must warn
        and return, leaving the caller free to run its remaining bootstraps.

        Provoked with a genuine lock timeout, holding ACCESS EXCLUSIVE from a
        second connection, because there is no cheaper way to reach this state
        honestly: all four recognized DDL forms are idempotent by construction,
        so once a statement's objects are deployed the statement cannot fail on
        its own. A rejected statement whose expectation is nonetheless satisfied
        therefore needs an EXTERNAL cause -- which is what the 2026-08-17 boot
        hit. Costs the bootstrap's full 10s ``lock_timeout``; it is the only
        slow test here and it buys the one path where a real failure is
        provably harmless.
        """
        with verifying_lml_cache_shape():
            await bootstrap_lml_cache_table(pg_source, _DDL_SCHEMA, _DDL_TABLE, _DDL_ADD_COLUMN)

            async with pg_pool.acquire() as blocker:
                async with blocker.transaction():
                    await blocker.execute(f"LOCK TABLE lml_cache.{_TABLE} IN ACCESS EXCLUSIVE MODE")

                    with caplog.at_level("WARNING"):
                        await bootstrap_lml_cache_table(pg_source, _DDL_ADD_COLUMN)

        assert any("already satisfies" in rec.getMessage() for rec in caplog.records)
