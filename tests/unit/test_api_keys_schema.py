"""Unit tests for the per-consumer API-key schema (LML per-consumer API keys plan).

Two surfaces, mirroring ``test_track_streaming_url_cache_schema.py``:

* ``entity/api_keys.sql`` -- the canonical-DDL reference file. It must
  describe the ``lml_cache.api_keys`` table with no ``UNIQUE(caller_name)``
  (a rotation in progress means two live rows for one caller, by design).
* ``set_up_api_keys_schema`` -- the lifespan bootstrap helper. It must issue
  ``CREATE SCHEMA`` then ``CREATE TABLE`` and nothing else (no CHECK/ALTER,
  unlike the track-streaming-url cache -- this table has no allowlist).

PG is mocked; ``tests/integration/test_api_keys_pg.py`` drives the real DDL
against PostgreSQL.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from entity.api_keys import set_up_api_keys_schema

_SQL_REFERENCE = Path(__file__).resolve().parent.parent.parent / "entity" / "api_keys.sql"


class TestCanonicalDDLReference:
    def test_reference_file_exists(self):
        assert _SQL_REFERENCE.is_file()

    def test_targets_lml_cache_schema_and_api_keys_table(self):
        ddl = _SQL_REFERENCE.read_text()
        assert "CREATE SCHEMA IF NOT EXISTS lml_cache" in ddl
        assert "lml_cache.api_keys" in ddl

    def test_has_no_unique_constraint_on_caller_name(self):
        # Deliberate: a rotation in progress means two live rows for the same
        # caller (old not-yet-revoked, new not-yet-confirmed).
        ddl = _SQL_REFERENCE.read_text()
        assert "UNIQUE(caller_name)" not in ddl
        assert "UNIQUE (caller_name)" not in ddl

    def test_key_hash_is_unique(self):
        ddl = _SQL_REFERENCE.read_text()
        assert "key_hash TEXT NOT NULL UNIQUE" in ddl

    def test_has_the_expected_columns(self):
        ddl = _SQL_REFERENCE.read_text()
        for column in (
            "caller_name",
            "key_hash",
            "created_at",
            "last_used_at",
            "revoked_at",
            "note",
        ):
            assert column in ddl


class TestCanonicalDDLMatchesRuntime:
    """entity/api_keys.sql and the runtime ``_DDL_*`` constants are two copies
    of the same DDL -- the .sql file itself says "update both places". Nothing
    else enforces that they actually agree, so assert parity: a change to one
    that forgets the other fails loudly here instead of silently drifting (the
    reviewer finding this test guards).
    """

    @staticmethod
    def _strip_line_comments(sql: str) -> str:
        # Drop ``--`` line comments. Done BEFORE splitting on ``;`` because the
        # .sql prose comments themselves contain semicolons (e.g. "persisted
        # anywhere; it is shown once"), which would otherwise corrupt the
        # statement split.
        out = []
        for line in sql.splitlines():
            idx = line.find("--")
            if idx != -1:
                line = line[:idx]
            out.append(line)
        return "\n".join(out)

    @staticmethod
    def _collapse(sql: str) -> str:
        # Collapse all whitespace to single spaces and drop ``;`` so the
        # semicolon-terminated .sql statements compare equal to the
        # semicolon-free Python constants.
        return " ".join(sql.split()).replace(";", "").strip()

    def test_sql_schema_statement_matches_runtime_constant(self):
        from entity.api_keys import _DDL_SCHEMA

        decommented = self._strip_line_comments(_SQL_REFERENCE.read_text())
        # The CREATE SCHEMA is the first statement in the file.
        schema_stmt = decommented.split(";", 1)[0]
        assert self._collapse(schema_stmt) == self._collapse(_DDL_SCHEMA)

    def test_sql_table_statement_matches_runtime_constant(self):
        from entity.api_keys import _DDL_TABLE

        decommented = self._strip_line_comments(_SQL_REFERENCE.read_text())
        statements = [s for s in decommented.split(";") if s.strip()]
        table_stmt = next(s for s in statements if "CREATE TABLE" in s)
        assert self._collapse(table_stmt) == self._collapse(_DDL_TABLE)


@pytest.mark.asyncio
class TestSetUpApiKeysSchema:
    """LML#1038 PR-2: runs through ``entity.ddl.bootstrap_lml_cache_table`` --
    one transaction on one acquired connection, behind a ``lock_timeout``
    preamble. ``mock_pg_tx`` (``tests/unit/conftest.py``) is the shared fake
    for this shape, reused across every ``lml_cache.*`` bootstrap test.
    """

    async def test_creates_schema_then_table(self, mock_pg_tx):
        conn = mock_pg_tx._mock_conn

        await set_up_api_keys_schema(mock_pg_tx)

        # [0] is the lock_timeout preamble; [1]/[2] are schema then table.
        assert conn.execute.await_count == 3
        schema_sql = conn.execute.await_args_list[1].args[0]
        table_sql = conn.execute.await_args_list[2].args[0]
        assert "CREATE SCHEMA IF NOT EXISTS lml_cache" in schema_sql
        assert "CREATE TABLE IF NOT EXISTS lml_cache.api_keys" in table_sql
        assert "key_hash TEXT NOT NULL UNIQUE" in table_sql
        # No table-level UNIQUE(caller_name) -- a rotation in progress means
        # two live rows for the same caller, by design.
        assert "UNIQUE(caller_name)" not in table_sql
        assert "UNIQUE (caller_name)" not in table_sql

    async def test_runs_as_one_transaction_on_one_connection(self, mock_pg_tx):
        conn = mock_pg_tx._mock_conn

        await set_up_api_keys_schema(mock_pg_tx)

        mock_pg_tx.acquire.assert_called_once()
        conn.transaction.assert_called_once()
        conn._mock_tx_ctx.__aenter__.assert_awaited_once()
        conn._mock_tx_ctx.__aexit__.assert_awaited_once()

    async def test_bootstrap_is_idempotent(self, mock_pg_tx):
        conn = mock_pg_tx._mock_conn

        await set_up_api_keys_schema(mock_pg_tx)
        first = [call.args[0] for call in conn.execute.await_args_list]
        conn.execute.reset_mock()
        await set_up_api_keys_schema(mock_pg_tx)
        second = [call.args[0] for call in conn.execute.await_args_list]

        assert first == second

    async def test_does_not_touch_other_lml_cache_tables(self, mock_pg_tx):
        conn = mock_pg_tx._mock_conn

        await set_up_api_keys_schema(mock_pg_tx)

        executed = [call.args[0] for call in conn.execute.await_args_list]
        assert not any("streaming_url_cache" in sql for sql in executed)
        assert not any("discogs_rate_bucket" in sql for sql in executed)
