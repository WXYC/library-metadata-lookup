"""Unit tests for the Wikipedia bio program's write-nothing attempt schema (LML#1192 review round 4, P0-8).

Two surfaces, mirroring ``test_discogs_rate_bucket_schema.py``:

* ``entity/artist_wikipedia_bio_attempt.sql`` — the canonical-DDL reference
  file. It must describe the ``lml_cache.artist_wikipedia_bio_attempt``
  table, so a reviewer / operator has the DDL inline.
* ``set_up_artist_wikipedia_bio_attempt_schema`` — the bootstrap helper. It
  must issue ``CREATE SCHEMA`` then ``CREATE TABLE`` and nothing else (no
  seed -- unlike ``discogs_rate_bucket``, there is no singleton row to
  pre-populate).

PG is mocked; ``tests/integration/test_artist_wikipedia_bio_attempt.py``
drives the real DDL + the incremental/retry_misses JOIN behavior against
PostgreSQL.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from entity.artist_wikipedia_bio_attempt import (
    _DDL_TABLE,
    set_up_artist_wikipedia_bio_attempt_schema,
)
from tests.unit.conftest import extract_create_table as _extract_create_table
from tests.unit.conftest import strip_sql_comments as _strip_sql_comments

_SQL_REFERENCE = (
    Path(__file__).resolve().parent.parent.parent / "entity" / "artist_wikipedia_bio_attempt.sql"
)


class TestCanonicalDDLReference:
    """``entity/artist_wikipedia_bio_attempt.sql`` is the inline DDL reference."""

    def test_reference_file_exists(self):
        assert _SQL_REFERENCE.is_file()

    def test_targets_lml_cache_schema_and_table(self):
        ddl = _SQL_REFERENCE.read_text()
        assert "CREATE SCHEMA IF NOT EXISTS lml_cache" in ddl
        assert "lml_cache.artist_wikipedia_bio_attempt" in ddl

    def test_discogs_artist_id_is_primary_key(self):
        ddl = _SQL_REFERENCE.read_text()
        assert "discogs_artist_id INTEGER PRIMARY KEY" in ddl

    def test_has_the_expected_columns(self):
        ddl = _SQL_REFERENCE.read_text()
        for column in ("discogs_artist_id", "attempted_at", "outcome"):
            assert column in ddl

    def test_module_create_table_matches_sql_reference(self):
        # Structural parity: the generated sidecar's CREATE TABLE (modulo
        # comments + trailing semicolon) must match the runtime _DDL_TABLE
        # constant byte-for-byte after normalization -- the same contract the
        # sibling schema tests enforce, so a change to one that forgets the
        # other (or a stale, un-regenerated sidecar) fails loudly here.
        ddl = _SQL_REFERENCE.read_text()
        sql_create = _extract_create_table(ddl)
        module_create = _strip_sql_comments(_DDL_TABLE).strip().rstrip(";")
        assert sql_create == module_create, (
            "entity/artist_wikipedia_bio_attempt.sql CREATE TABLE drifted from "
            "the _DDL_TABLE in entity/artist_wikipedia_bio_attempt.py -- "
            "regenerate via scripts/regenerate_lml_cache_sql.py."
        )


@pytest.mark.asyncio
class TestSetUpArtistWikipediaBioAttemptSchema:
    """``set_up_artist_wikipedia_bio_attempt_schema`` runs exactly the idempotent DDL.

    Runs through ``entity.ddl.bootstrap_lml_cache_table`` -- one transaction
    on one acquired connection, behind a ``lock_timeout`` preamble.
    ``mock_pg_tx`` (``tests/unit/conftest.py``) is the shared fake for this
    shape.
    """

    async def test_creates_schema_then_table(self, mock_pg_tx):
        conn = mock_pg_tx._mock_conn

        await set_up_artist_wikipedia_bio_attempt_schema(mock_pg_tx)

        # [0] is the lock_timeout preamble.
        assert conn.execute.await_count == 3
        schema_sql = conn.execute.await_args_list[1].args[0]
        table_sql = conn.execute.await_args_list[2].args[0]
        assert "CREATE SCHEMA IF NOT EXISTS lml_cache" in schema_sql
        assert "CREATE TABLE IF NOT EXISTS lml_cache.artist_wikipedia_bio_attempt" in table_sql
        assert "discogs_artist_id INTEGER PRIMARY KEY" in table_sql

    async def test_runs_as_one_transaction_on_one_connection(self, mock_pg_tx):
        conn = mock_pg_tx._mock_conn

        await set_up_artist_wikipedia_bio_attempt_schema(mock_pg_tx)

        mock_pg_tx.acquire.assert_called_once()
        conn.transaction.assert_called_once()
        conn._mock_tx_ctx.__aenter__.assert_awaited_once()
        conn._mock_tx_ctx.__aexit__.assert_awaited_once()

    async def test_bootstrap_is_idempotent(self, mock_pg_tx):
        conn = mock_pg_tx._mock_conn

        await set_up_artist_wikipedia_bio_attempt_schema(mock_pg_tx)
        first = [call.args for call in conn.execute.await_args_list]
        conn.execute.reset_mock()
        await set_up_artist_wikipedia_bio_attempt_schema(mock_pg_tx)
        second = [call.args for call in conn.execute.await_args_list]

        assert first == second

    async def test_does_not_touch_other_lml_cache_tables(self, mock_pg_tx):
        conn = mock_pg_tx._mock_conn

        await set_up_artist_wikipedia_bio_attempt_schema(mock_pg_tx)

        executed = [call.args[0] for call in conn.execute.await_args_list]
        assert not any("artist_wikipedia_bio (" in sql for sql in executed)
        assert not any("discogs_rate_bucket" in sql for sql in executed)
