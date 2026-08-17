"""Unit tests for the artist Wikipedia bio cache schema bootstrap (LML#513/#1192).

Mirrors ``tests/unit/test_release_resolution_cache_schema.py``. Two surfaces:

* ``entity/artist_wikipedia_bio.sql`` -- the canonical-DDL reference file.
* ``set_up_artist_wikipedia_bio_schema`` -- the lifespan bootstrap helper. It
  issues ``CREATE SCHEMA``/``CREATE TABLE`` as one ``bootstrap_lml_cache_table``
  transaction, THEN the ``last_attempted_at`` additive
  ``ALTER TABLE ... ADD COLUMN IF NOT EXISTS`` as its OWN, separate
  ``bootstrap_lml_cache_table`` call (LML#1192 review round 4, P0-1 --
  this table's CREATE TABLE alone shipped in #1194 before last_attempted_at
  existed at all; the ALTER upgrades a table #1194 already created without
  it, mirroring ``entity/streaming_url_cache.py``'s ``is_error`` column).

The parity assertion pins the module's runtime ``CREATE TABLE`` text to the
``.sql`` reference so the two cannot silently drift. PG is mocked; the
integration layer (``tests/integration/test_artist_wikipedia_bio.py``) drives
the real DDL, UPSERT, and read-side TTL/self-healing semantics against
PostgreSQL.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from entity.artist_wikipedia_bio import (
    _DDL_TABLE,
    set_up_artist_wikipedia_bio_schema,
)
from tests.unit.conftest import extract_create_table as _extract_create_table
from tests.unit.conftest import strip_sql_comments as _strip_sql_comments

_SQL_REFERENCE = (
    Path(__file__).resolve().parent.parent.parent / "entity" / "artist_wikipedia_bio.sql"
)


class TestCanonicalDDLReference:
    def test_reference_file_exists(self):
        assert _SQL_REFERENCE.is_file()

    def test_targets_lml_cache_schema_and_table(self):
        ddl = _SQL_REFERENCE.read_text()
        assert "CREATE SCHEMA IF NOT EXISTS lml_cache" in ddl
        assert "lml_cache.artist_wikipedia_bio" in ddl

    def test_has_primary_key_on_discogs_artist_id(self):
        ddl = _SQL_REFERENCE.read_text()
        assert "discogs_artist_id BIGINT PRIMARY KEY" in ddl

    def test_extract_column_is_nullable(self):
        # NULL extract records a negative result -- must NOT be NOT NULL.
        ddl = _SQL_REFERENCE.read_text()
        create_table = _extract_create_table(ddl)
        assert "extract TEXT" in create_table
        assert "extract TEXT NOT NULL" not in create_table

    def test_module_create_table_matches_sql_reference(self):
        ddl = _SQL_REFERENCE.read_text()
        sql_create = _extract_create_table(ddl)
        module_create = _strip_sql_comments(_DDL_TABLE).strip().rstrip(";")
        assert sql_create == module_create, (
            "entity/artist_wikipedia_bio.sql CREATE TABLE drifted from the "
            "_DDL_TABLE in entity/artist_wikipedia_bio.py -- update both."
        )


@pytest.mark.asyncio
class TestSetUpArtistWikipediaBioSchema:
    async def test_creates_schema_then_table(self, mock_pg_tx):
        conn = mock_pg_tx._mock_conn

        await set_up_artist_wikipedia_bio_schema(mock_pg_tx)

        # [0] is the lock_timeout preamble; [1]-[2] are schema then table
        # (the first bootstrap_lml_cache_table call).
        assert conn.execute.await_count >= 3
        schema_sql = conn.execute.await_args_list[1].args[0]
        table_sql = conn.execute.await_args_list[2].args[0]
        assert "CREATE SCHEMA IF NOT EXISTS lml_cache" in schema_sql
        assert "CREATE TABLE IF NOT EXISTS lml_cache.artist_wikipedia_bio" in table_sql
        assert "discogs_artist_id BIGINT PRIMARY KEY" in table_sql
        assert "last_attempted_at" in table_sql

    async def test_last_attempted_at_column_is_added_via_its_own_bootstrap_call(self, mock_pg_tx):
        # LML#1192 review round 4, P0-1: a SECOND, SEPARATE
        # bootstrap_lml_cache_table call -- its own connection acquire and
        # its own transaction -- carries the additive ALTER TABLE, so a
        # lock_timeout there can't roll back the (already-succeeded)
        # schema/table step (mirrors entity/streaming_url_cache.py's
        # LML#1121 FIX 5 lock-isolation rationale).
        conn = mock_pg_tx._mock_conn

        await set_up_artist_wikipedia_bio_schema(mock_pg_tx)

        executed = [call.args[0] for call in conn.execute.await_args_list]
        assert any(
            "ALTER TABLE" in sql
            and "ADD COLUMN IF NOT EXISTS" in sql
            and "last_attempted_at" in sql
            for sql in executed
        )

    async def test_last_refused_at_column_is_added_via_its_own_bootstrap_call(self, mock_pg_tx):
        # LML#1192 review round 6, C1-1: same P0-1 treatment, one column
        # later -- its own bootstrap_lml_cache_table call, separate from
        # both the CREATE TABLE step and the last_attempted_at ALTER.
        conn = mock_pg_tx._mock_conn

        await set_up_artist_wikipedia_bio_schema(mock_pg_tx)

        executed = [call.args[0] for call in conn.execute.await_args_list]
        assert any(
            "ALTER TABLE" in sql and "ADD COLUMN IF NOT EXISTS" in sql and "last_refused_at" in sql
            for sql in executed
        )

    async def test_four_separate_bootstrap_calls_not_one_shared_transaction(self, mock_pg_tx):
        # CREATE TABLE, the round-7 conditional rename, the
        # last_attempted_at ALTER, and the last_refused_at ALTER each
        # acquire their own connection/transaction -- a lock_timeout on any
        # one must not roll back an already-succeeded earlier step.
        await set_up_artist_wikipedia_bio_schema(mock_pg_tx)

        assert mock_pg_tx.acquire.call_count == 4

    async def test_does_not_read_or_backfill(self, mock_pg_tx):
        conn = mock_pg_tx._mock_conn

        await set_up_artist_wikipedia_bio_schema(mock_pg_tx)

        mock_pg_tx.fetchone.assert_not_awaited()
        conn.fetchrow.assert_not_awaited()
        executed = [call.args[0] for call in conn.execute.await_args_list]
        assert not any("INSERT" in sql for sql in executed)


class TestLastAttemptedAtRename:
    """LML#1192 cross-PR review, round 7: ``last_checked_at`` renamed to
    ``last_attempted_at``. The old name was a cross-schema homonym: on the
    streaming URL caches ``last_checked_at`` is the TTL gate ("when did we
    last successfully check upstream"), while here it was the drain's
    progress cursor -- it advances on refusals and never gates the read.
    The ``attempted_at`` family (``artist_wikipedia_bio_attempt``,
    ``compilation_track_identity``) already names exactly those semantics.
    Renamed while the table has zero prod rows; the conditional-rename
    migration below covers any environment (staging) whose lifespan already
    created the old shape."""

    def test_table_ddl_names_last_attempted_at(self):
        assert "last_attempted_at" in _DDL_TABLE
        assert "last_checked_at" not in _DDL_TABLE

    @pytest.mark.asyncio
    async def test_conditional_rename_runs_between_create_and_the_alters(self, mock_pg_tx):
        # The rename must be guarded (no IF EXISTS form exists for RENAME
        # COLUMN) and must run BEFORE the additive last_attempted_at ALTER,
        # so an old-shape table renames instead of ending up with both
        # columns.
        conn = mock_pg_tx._mock_conn

        await set_up_artist_wikipedia_bio_schema(mock_pg_tx)

        executed = [call.args[0] for call in conn.execute.await_args_list]
        rename_idx = next(
            i
            for i, sql in enumerate(executed)
            if "RENAME COLUMN last_checked_at TO last_attempted_at" in sql
        )
        rename_sql = executed[rename_idx]
        # Guarded both ways: only when the old column exists AND the new one
        # doesn't (a double-run or a half-migrated table must no-op).
        assert "information_schema.columns" in rename_sql
        assert rename_sql.count("column_name") >= 2
        add_idx = next(
            i
            for i, sql in enumerate(executed)
            if "ADD COLUMN IF NOT EXISTS last_attempted_at" in sql
        )
        assert rename_idx < add_idx
