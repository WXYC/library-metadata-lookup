"""Unit tests for the V/A recall-index schema bootstrap (LML#1019).

Two surfaces, mirroring ``test_library_release_override_schema.py``:

* ``entity/compilation_track_location.sql`` -- the canonical-DDL reference
  file. It must describe the ``lml_cache.compilation_track_location`` table
  with its composite primary key, the ``credit_role`` / ``discogs_release_id``
  CHECKs, and the reverse ``(track_artist, track_title)`` index.
* ``set_up_compilation_track_location_schema`` -- the bootstrap helper called
  from both the FastAPI lifespan and the standalone build script. It must
  issue ``CREATE SCHEMA`` then ``CREATE TABLE`` then ``CREATE INDEX`` (and
  nothing that mutates existing rows), idempotently.

PG is mocked; the integration layer
(``tests/integration/test_compilation_track_location_schema.py``) drives the
real DDL -- including the EXPLAIN index-use assertion -- against PostgreSQL.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from entity.compilation_track_location import (
    _DDL_TABLE,
    set_up_compilation_track_location_schema,
)
from entity.sources import PgSource
from tests.unit.conftest import extract_create_table as _extract_create_table
from tests.unit.conftest import strip_sql_comments as _strip_sql_comments

_SQL_REFERENCE = (
    Path(__file__).resolve().parent.parent.parent / "entity" / "compilation_track_location.sql"
)


class TestCanonicalDDLReference:
    """``entity/compilation_track_location.sql`` is the inline DDL reference."""

    def test_reference_file_exists(self):
        assert _SQL_REFERENCE.is_file()

    def test_targets_lml_cache_schema_and_table(self):
        ddl = _SQL_REFERENCE.read_text()
        assert "CREATE SCHEMA IF NOT EXISTS lml_cache" in ddl
        assert "lml_cache.compilation_track_location" in ddl

    def test_has_composite_primary_key(self):
        ddl = _SQL_REFERENCE.read_text()
        assert "PRIMARY KEY (library_id, track_position, track_artist)" in ddl

    def test_has_credit_role_check(self):
        ddl = _SQL_REFERENCE.read_text()
        assert "CHECK (credit_role IN ('primary', 'featured', 'extra'))" in ddl

    def test_has_positive_release_id_check(self):
        ddl = _SQL_REFERENCE.read_text()
        assert "discogs_release_id" in ddl
        assert "CHECK (discogs_release_id > 0)" in ddl

    def test_has_reverse_index_on_artist_and_title(self):
        ddl = _SQL_REFERENCE.read_text()
        assert "CREATE INDEX IF NOT EXISTS idx_compilation_track_location_reverse" in ddl
        assert "(track_artist, track_title)" in ddl

    def test_module_create_table_matches_sql_reference(self):
        # The .sql file is the hand-maintained mirror of the runtime DDL, and
        # its header promises "update both places". Pin the module's CREATE
        # TABLE (modulo comments + trailing semicolon) to the reference so the
        # two can't silently drift.
        ddl = _SQL_REFERENCE.read_text()
        sql_create = _extract_create_table(ddl)
        module_create = _strip_sql_comments(_DDL_TABLE).strip().rstrip(";")
        assert sql_create == module_create, (
            "entity/compilation_track_location.sql CREATE TABLE drifted from "
            "the _DDL_TABLE in entity/compilation_track_location.py -- update both."
        )


@pytest.mark.asyncio
class TestSetUpCompilationTrackLocationSchema:
    """``set_up_compilation_track_location_schema`` runs exactly the idempotent DDL."""

    async def test_creates_schema_table_then_index(self):
        pg = AsyncMock(spec=PgSource)
        pg.execute = AsyncMock(return_value="CREATE")

        await set_up_compilation_track_location_schema(pg)

        assert pg.execute.await_count == 3
        schema_sql = pg.execute.await_args_list[0].args[0]
        table_sql = pg.execute.await_args_list[1].args[0]
        index_sql = pg.execute.await_args_list[2].args[0]
        assert "CREATE SCHEMA IF NOT EXISTS lml_cache" in schema_sql
        assert "CREATE TABLE IF NOT EXISTS lml_cache.compilation_track_location" in table_sql
        assert "PRIMARY KEY (library_id, track_position, track_artist)" in table_sql
        assert "CHECK (credit_role IN ('primary', 'featured', 'extra'))" in table_sql
        assert "CHECK (discogs_release_id > 0)" in table_sql
        assert "CREATE INDEX IF NOT EXISTS idx_compilation_track_location_reverse" in index_sql

    async def test_bootstrap_is_idempotent(self):
        pg = AsyncMock(spec=PgSource)
        pg.execute = AsyncMock(return_value="CREATE")

        await set_up_compilation_track_location_schema(pg)
        first = [call.args[0] for call in pg.execute.await_args_list]
        pg.execute.reset_mock()
        await set_up_compilation_track_location_schema(pg)
        second = [call.args[0] for call in pg.execute.await_args_list]

        assert first == second

    async def test_bootstrap_does_not_mutate_rows(self):
        pg = AsyncMock(spec=PgSource)
        pg.execute = AsyncMock(return_value="CREATE")

        await set_up_compilation_track_location_schema(pg)

        executed = [call.args[0] for call in pg.execute.await_args_list]
        assert not any("INSERT" in sql for sql in executed)
        assert not any("UPDATE" in sql for sql in executed)
        assert not any("DELETE" in sql for sql in executed)
