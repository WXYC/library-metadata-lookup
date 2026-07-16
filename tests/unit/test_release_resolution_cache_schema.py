"""Unit tests for the release-resolution cache schema bootstrap (LML#632).

Mirrors ``tests/unit/test_streaming_url_cache_schema.py``. Two surfaces:

* ``entity/release_resolution_cache.sql`` — the canonical-DDL reference file
  (mirrors ``entity/streaming_url_cache.sql``). It must describe the
  ``lml_cache.release_resolution_cache`` table with the named CHECK constraint
  and composite PK, so a reviewer / operator has the DDL inline.
* ``set_up_release_resolution_cache_schema`` — the lifespan bootstrap helper.
  It must issue ``CREATE SCHEMA`` then ``CREATE TABLE`` (named CHECK) and
  nothing else.

The parity assertion pins the module's runtime ``CREATE TABLE`` text to the
``.sql`` reference so the two cannot silently drift. PG is mocked; the
integration layer (``tests/integration/test_release_resolution_cache.py``)
drives the real DDL and TTL math against PostgreSQL.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from entity.release_resolution_cache import (
    _DDL_TABLE,
    set_up_release_resolution_cache_schema,
)
from entity.sources import PgSource

_SQL_REFERENCE = (
    Path(__file__).resolve().parent.parent.parent / "entity" / "release_resolution_cache.sql"
)


class TestCanonicalDDLReference:
    """``entity/release_resolution_cache.sql`` is the inline DDL reference."""

    def test_reference_file_exists(self):
        assert _SQL_REFERENCE.is_file()

    def test_targets_lml_cache_schema_and_table(self):
        ddl = _SQL_REFERENCE.read_text()
        assert "CREATE SCHEMA IF NOT EXISTS lml_cache" in ddl
        assert "lml_cache.release_resolution_cache" in ddl

    def test_has_named_check_constraint(self):
        ddl = _SQL_REFERENCE.read_text()
        assert "CONSTRAINT release_id_validity CHECK" in ddl
        assert "release_id IS NULL OR release_id > 0" in ddl

    def test_has_composite_primary_key(self):
        ddl = _SQL_REFERENCE.read_text()
        assert "PRIMARY KEY (artist_normalized, title_normalized, is_track)" in ddl

    def test_has_crowd_out_marker_column(self):
        # LML#824: the crowd-out marker distinguishes a short-TTL crowd-out miss
        # from a full-7-day exhausted miss. NOT NULL DEFAULT false so a
        # pre-existing row (and every positive) reads as a normal miss.
        ddl = _SQL_REFERENCE.read_text()
        assert "crowd_out BOOLEAN NOT NULL DEFAULT false" in ddl

    def test_module_create_table_matches_sql_reference(self):
        # The .sql file is the hand-maintained mirror of the runtime DDL. Pin
        # the module's ``CREATE TABLE ...`` statement (modulo SQL comments and
        # the trailing semicolon) to the reference so the mirror can't drift —
        # the same contract ``test_streaming_url_cache_schema`` enforces, but
        # via a full text comparison since this DDL carries no generated list.
        ddl = _SQL_REFERENCE.read_text()
        sql_create = _extract_create_table(ddl)
        module_create = _strip_sql_comments(_DDL_TABLE).strip().rstrip(";")
        assert sql_create == module_create, (
            "entity/release_resolution_cache.sql CREATE TABLE drifted from the "
            "_DDL_TABLE in entity/release_resolution_cache.py — update both."
        )


def _strip_sql_comments(sql: str) -> str:
    """Drop ``--`` line comments and collapse incidental whitespace.

    Lets the parity check compare the *shape* of the two DDL statements without
    tripping on the explanatory comments the ``.sql`` reference carries inline.
    """
    lines = []
    for raw in sql.splitlines():
        line = raw.split("--", 1)[0].rstrip()
        if line.strip():
            lines.append(line.strip())
    return " ".join(lines)


def _extract_create_table(ddl: str) -> str:
    """Pull the single ``CREATE TABLE ...);`` statement out of the .sql file."""
    start = ddl.index("CREATE TABLE")
    end = ddl.index(";", start)
    return _strip_sql_comments(ddl[start:end]).strip().rstrip(";")


@pytest.mark.asyncio
class TestSetUpReleaseResolutionCacheSchema:
    """``set_up_release_resolution_cache_schema`` runs exactly the idempotent DDL."""

    async def test_creates_schema_then_table_then_alters(self):
        pg = AsyncMock(spec=PgSource)
        pg.execute = AsyncMock(return_value="CREATE")

        await set_up_release_resolution_cache_schema(pg)

        # Three executes — schema, then table, then the additive crowd_out column
        # ALTER (LML#824) that upgrades a pre-existing prod table in place (the
        # CREATE TABLE IF NOT EXISTS above is a no-op there). All idempotent.
        assert pg.execute.await_count == 3
        schema_sql = pg.execute.await_args_list[0].args[0]
        table_sql = pg.execute.await_args_list[1].args[0]
        alter_sql = pg.execute.await_args_list[2].args[0]
        assert "CREATE SCHEMA IF NOT EXISTS lml_cache" in schema_sql
        assert "CREATE TABLE IF NOT EXISTS lml_cache.release_resolution_cache" in table_sql
        assert "CONSTRAINT release_id_validity CHECK" in table_sql
        assert "PRIMARY KEY (artist_normalized, title_normalized, is_track)" in table_sql
        assert "ALTER TABLE lml_cache.release_resolution_cache" in alter_sql
        assert "ADD COLUMN IF NOT EXISTS crowd_out BOOLEAN NOT NULL DEFAULT false" in alter_sql

    async def test_does_not_read_or_backfill(self):
        # The bootstrap is pure DDL: no probe, no data INSERT. The crowd_out
        # ALTER (LML#824) is an idempotent DDL column add, not a row backfill.
        pg = AsyncMock(spec=PgSource)
        pg.execute = AsyncMock(return_value="CREATE")

        await set_up_release_resolution_cache_schema(pg)

        pg.fetchone.assert_not_awaited()
        executed = [call.args[0] for call in pg.execute.await_args_list]
        assert not any("INSERT" in sql for sql in executed)
