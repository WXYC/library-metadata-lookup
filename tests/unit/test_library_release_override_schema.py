"""Unit tests for the verified library-release override schema bootstrap (LML#850).

Two surfaces, mirroring ``test_streaming_url_cache_schema.py``:

* ``entity/library_release_override.sql`` — the canonical-DDL reference file.
  It must describe the ``lml_cache.library_release_override`` table with the
  ``library_id`` primary key and the ``discogs_release_id > 0`` CHECK, so a
  reviewer / operator has the DDL inline.
* ``set_up_library_release_override_schema`` — the lifespan bootstrap helper.
  It must issue ``CREATE SCHEMA`` then ``CREATE TABLE`` (and nothing that
  mutates existing rows), idempotently.

PG is mocked; the integration layer
(``tests/integration/test_library_release_override.py``) drives the real DDL
against PostgreSQL.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from entity.library_release_override import (
    _DDL_TABLE,
    set_up_library_release_override_schema,
)
from tests.unit.conftest import extract_create_table as _extract_create_table
from tests.unit.conftest import strip_sql_comments as _strip_sql_comments

_SQL_REFERENCE = (
    Path(__file__).resolve().parent.parent.parent / "entity" / "library_release_override.sql"
)


class TestCanonicalDDLReference:
    """``entity/library_release_override.sql`` is the inline DDL reference."""

    def test_reference_file_exists(self):
        assert _SQL_REFERENCE.is_file()

    def test_targets_lml_cache_schema_and_table(self):
        ddl = _SQL_REFERENCE.read_text()
        assert "CREATE SCHEMA IF NOT EXISTS lml_cache" in ddl
        assert "lml_cache.library_release_override" in ddl

    def test_library_id_is_primary_key(self):
        ddl = _SQL_REFERENCE.read_text()
        assert "library_id" in ddl
        assert "PRIMARY KEY" in ddl

    def test_has_positive_release_id_check(self):
        ddl = _SQL_REFERENCE.read_text()
        assert "discogs_release_id" in ddl
        assert "CHECK (discogs_release_id > 0)" in ddl

    def test_carries_source_column_for_reversibility(self):
        # The seed is reversible by ``DELETE WHERE source = 'alex-l-2026'``;
        # the source column is the load-bearing rollback handle.
        ddl = _SQL_REFERENCE.read_text()
        assert "source" in ddl

    def test_module_create_table_matches_sql_reference(self):
        # The .sql file is the hand-maintained mirror of the runtime DDL, and its
        # header promises "update both places (and the parity assertions in
        # tests/unit/test_library_release_override_schema.py)". Pin the module's
        # CREATE TABLE (modulo comments + trailing semicolon) to the reference so
        # the two can't silently drift — the same contract the sibling
        # release-resolution / streaming-URL cache schema tests enforce.
        ddl = _SQL_REFERENCE.read_text()
        sql_create = _extract_create_table(ddl)
        module_create = _strip_sql_comments(_DDL_TABLE).strip().rstrip(";")
        assert sql_create == module_create, (
            "entity/library_release_override.sql CREATE TABLE drifted from the "
            "_DDL_TABLE in entity/library_release_override.py — update both."
        )


@pytest.mark.asyncio
class TestSetUpLibraryReleaseOverrideSchema:
    """``set_up_library_release_override_schema`` runs exactly the idempotent DDL.

    LML#1038 PR-2: runs through ``entity.ddl.bootstrap_lml_cache_table`` --
    one transaction on one acquired connection, behind a ``lock_timeout``
    preamble. ``mock_pg_tx`` (``tests/unit/conftest.py``) is the shared fake
    for this shape.
    """

    async def test_creates_schema_then_table(self, mock_pg_tx):
        conn = mock_pg_tx._mock_conn

        await set_up_library_release_override_schema(mock_pg_tx)

        # [0] is the lock_timeout preamble; [1]/[2] are schema then table. No
        # mutation of existing rows (no UPDATE/DELETE/INSERT on bootstrap).
        assert conn.execute.await_count == 3
        schema_sql = conn.execute.await_args_list[1].args[0]
        table_sql = conn.execute.await_args_list[2].args[0]
        assert "CREATE SCHEMA IF NOT EXISTS lml_cache" in schema_sql
        assert "CREATE TABLE IF NOT EXISTS lml_cache.library_release_override" in table_sql
        assert "PRIMARY KEY" in table_sql
        assert "CHECK (discogs_release_id > 0)" in table_sql

    async def test_runs_as_one_transaction_on_one_connection(self, mock_pg_tx):
        conn = mock_pg_tx._mock_conn

        await set_up_library_release_override_schema(mock_pg_tx)

        mock_pg_tx.acquire.assert_called_once()
        conn.transaction.assert_called_once()
        conn._mock_tx_ctx.__aenter__.assert_awaited_once()
        conn._mock_tx_ctx.__aexit__.assert_awaited_once()

    async def test_bootstrap_is_idempotent(self, mock_pg_tx):
        # Re-running the bootstrap issues the byte-identical DDL each time.
        conn = mock_pg_tx._mock_conn

        await set_up_library_release_override_schema(mock_pg_tx)
        first = [call.args[0] for call in conn.execute.await_args_list]
        conn.execute.reset_mock()
        await set_up_library_release_override_schema(mock_pg_tx)
        second = [call.args[0] for call in conn.execute.await_args_list]

        assert first == second

    async def test_bootstrap_does_not_mutate_rows(self, mock_pg_tx):
        # Regression guard: the bootstrap is pure DDL — never an INSERT/UPDATE/
        # DELETE that could touch seeded override rows on every boot.
        conn = mock_pg_tx._mock_conn

        await set_up_library_release_override_schema(mock_pg_tx)

        executed = [call.args[0] for call in conn.execute.await_args_list]
        assert not any("INSERT" in sql for sql in executed)
        assert not any("UPDATE" in sql for sql in executed)
        assert not any("DELETE" in sql for sql in executed)
