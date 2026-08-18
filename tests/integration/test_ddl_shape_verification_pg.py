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

# Two statements that both parse as the `album` column expectation but that
# PostgreSQL rejects, standing in for the observed lock timeout without making
# the test hold a conflicting lock for the bootstrap's full 10s lock_timeout.
#
# They fail for different reasons on purpose, because PG's IF NOT EXISTS
# short-circuits BEFORE resolving the column type: the unresolvable-type form
# fails only while the column is absent, and quietly succeeds once it exists.
# So the "column absent" case needs the first form and the "column already
# present" case needs the second, whose trailing clause fails either way.
_DDL_ADD_COLUMN_FAILS_WHEN_ABSENT = (
    f"ALTER TABLE lml_cache.{_TABLE} ADD COLUMN IF NOT EXISTS album no_such_type"
)
_DDL_ADD_COLUMN_FAILS_ALWAYS = (
    f"ALTER TABLE lml_cache.{_TABLE} ADD COLUMN IF NOT EXISTS album TEXT, "
    "ADD COLUMN artist TEXT"  # artist already exists, and there is no IF NOT EXISTS
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

    async def test_a_failed_alter_whose_column_already_exists_is_a_no_op(self, pg_source, caplog):
        """The incident as it actually happened: the statement failed against a
        table that already had the column, so nothing was degraded. It must warn
        and return, leaving the caller free to run its remaining bootstraps."""
        with verifying_lml_cache_shape():
            await bootstrap_lml_cache_table(pg_source, _DDL_SCHEMA, _DDL_TABLE, _DDL_ADD_COLUMN)

            with caplog.at_level("WARNING"):
                await bootstrap_lml_cache_table(pg_source, _DDL_ADD_COLUMN_FAILS_ALWAYS)

        assert any("already satisfies" in rec.getMessage() for rec in caplog.records)
