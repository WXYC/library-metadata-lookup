"""Unit tests for the per-track identity schema bootstrap (LML#1020).

Two surfaces, mirroring ``test_compilation_track_location_schema.py``:

* ``entity/compilation_track_identity.sql`` -- the generated canonical-DDL
  reference. It must describe ``lml_cache.compilation_track_identity`` with
  its credit-shaped composite primary key, the ``source`` CHECK, the
  verdict-coherence CHECK, and the partial retry-cohort index.
* ``set_up_compilation_track_identity_schema`` -- the bootstrap helper called
  from both the FastAPI lifespan and the standalone backfill script. It must
  issue ``CREATE SCHEMA`` then ``CREATE TABLE`` then ``CREATE INDEX`` (and
  nothing that mutates existing rows), idempotently.

PG is mocked; the integration layer
(``tests/integration/test_compilation_track_identity_schema.py``) drives the
real DDL -- including the EXPLAIN index-use assertion -- against PostgreSQL.

Plan: ``docs/plans/lml-1020-per-track-identity-matcher.md``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from entity.compilation_track_identity import (
    _DDL_TABLE,
    set_up_compilation_track_identity_schema,
)
from tests.unit.conftest import extract_create_table as _extract_create_table
from tests.unit.conftest import strip_sql_comments as _strip_sql_comments

_SQL_REFERENCE = (
    Path(__file__).resolve().parent.parent.parent / "entity" / "compilation_track_identity.sql"
)


class TestCanonicalDDLReference:
    """``entity/compilation_track_identity.sql`` is the generated DDL reference."""

    def test_reference_file_exists(self):
        assert _SQL_REFERENCE.is_file()

    def test_targets_lml_cache_schema_and_table(self):
        ddl = _SQL_REFERENCE.read_text()
        assert "CREATE SCHEMA IF NOT EXISTS lml_cache" in ddl
        assert "lml_cache.compilation_track_identity" in ddl

    def test_primary_key_is_credit_shaped_not_position_shaped(self):
        # The plan's D7/F1 finding: track_position is absent from library.db's
        # CTA export entirely, and a 66-performer track legitimately shares one
        # position -- so a position-keyed table collides by construction. The
        # key is one row per credited artist per track per source.
        ddl = _SQL_REFERENCE.read_text()
        assert "PRIMARY KEY (library_id, track_artist, track_title, source)" in ddl
        assert "PRIMARY KEY (library_id, track_position" not in ddl

    def test_has_source_check(self):
        ddl = _SQL_REFERENCE.read_text()
        assert "CHECK (source IN ('discogs', 'musicbrainz'))" in ddl

    def test_verdict_coherence_check_binds_confidence_and_method(self):
        ddl = _SQL_REFERENCE.read_text()
        assert "(external_id IS NULL) = (confidence IS NULL)" in ddl
        assert "(external_id IS NULL) = (method IS NULL)" in ddl

    def test_verdict_coherence_check_excludes_resolved_artist_name(self):
        # A tier-1 `identity_store` hit resolves with a non-NULL Discogs id and
        # canonical_name=None (artists/resolver.py -- entity.identity stores no
        # Discogs title). That is the COMMON resolved shape on a warm store, so
        # binding it into the coherence rule would abort most inserts.
        ddl = _SQL_REFERENCE.read_text()
        assert "(external_id IS NULL) = (resolved_artist_name IS NULL)" not in ddl

    def test_track_position_is_nullable(self):
        # 100% of positions are recovered from the Discogs tracklist join, and
        # the join legitimately misses -- NULL must be representable.
        ddl = _SQL_REFERENCE.read_text()
        assert "track_position       TEXT," in ddl
        assert "track_position       TEXT NOT NULL" not in ddl

    def test_retry_cohort_index_is_partial(self):
        # A plain (library_id) index would be dead weight -- library_id is the
        # PK's leading column. The retry cohort is the one access path the PK
        # does not already serve.
        ddl = _SQL_REFERENCE.read_text()
        assert "CREATE INDEX IF NOT EXISTS idx_compilation_track_identity_misses" in ddl
        assert "WHERE external_id IS NULL" in ddl

    def test_documents_the_legacy_id_key_space(self):
        # F2: library.db's library.id is the legacy MySQL LIBRARY_RELEASE_ID,
        # NOT Backend's wxyc_schema.library.id. A naive id join in #1021
        # silently returns zero rows, so the warning must survive in the
        # generated sidecar -- not only in the module docstring.
        ddl = _SQL_REFERENCE.read_text()
        assert "legacy" in ddl.lower()
        assert "wxyc_schema.library.id" in ddl

    def test_module_create_table_matches_sql_reference(self):
        ddl = _SQL_REFERENCE.read_text()
        sql_create = _extract_create_table(ddl)
        module_create = _strip_sql_comments(_DDL_TABLE).strip().rstrip(";")
        assert sql_create == module_create, (
            "entity/compilation_track_identity.sql CREATE TABLE drifted from "
            "the _DDL_TABLE in entity/compilation_track_identity.py -- "
            "regenerate via `uv run python -m scripts.regenerate_lml_cache_sql`."
        )

    def test_ddl_constant_carries_no_inline_comments(self):
        # scripts/regenerate_lml_cache_sql.py's header: inline per-column `--`
        # comments were deliberately replaced with above-statement prose,
        # because the parity tests strip comments before comparing -- so an
        # inline comment lives in an unverified duplicate.
        assert "--" not in _DDL_TABLE


@pytest.mark.asyncio
class TestSetUpCompilationTrackIdentitySchema:
    """``set_up_compilation_track_identity_schema`` runs exactly the idempotent DDL."""

    async def test_creates_schema_table_then_index(self, mock_pg_tx):
        conn = mock_pg_tx._mock_conn

        await set_up_compilation_track_identity_schema(mock_pg_tx)

        # [0] is the lock_timeout preamble.
        assert conn.execute.await_count == 4
        schema_sql = conn.execute.await_args_list[1].args[0]
        table_sql = conn.execute.await_args_list[2].args[0]
        index_sql = conn.execute.await_args_list[3].args[0]
        assert "CREATE SCHEMA IF NOT EXISTS lml_cache" in schema_sql
        assert "CREATE TABLE IF NOT EXISTS lml_cache.compilation_track_identity" in table_sql
        assert "PRIMARY KEY (library_id, track_artist, track_title, source)" in table_sql
        assert "CHECK (source IN ('discogs', 'musicbrainz'))" in table_sql
        assert "CREATE INDEX IF NOT EXISTS idx_compilation_track_identity_misses" in index_sql

    async def test_runs_as_one_transaction_on_one_connection(self, mock_pg_tx):
        conn = mock_pg_tx._mock_conn

        await set_up_compilation_track_identity_schema(mock_pg_tx)

        mock_pg_tx.acquire.assert_called_once()
        conn.transaction.assert_called_once()
        conn._mock_tx_ctx.__aenter__.assert_awaited_once()
        conn._mock_tx_ctx.__aexit__.assert_awaited_once()

    async def test_bootstrap_is_idempotent(self, mock_pg_tx):
        conn = mock_pg_tx._mock_conn

        await set_up_compilation_track_identity_schema(mock_pg_tx)
        first = [call.args[0] for call in conn.execute.await_args_list]
        conn.execute.reset_mock()
        await set_up_compilation_track_identity_schema(mock_pg_tx)
        second = [call.args[0] for call in conn.execute.await_args_list]

        assert first == second

    async def test_bootstrap_does_not_mutate_rows(self, mock_pg_tx):
        conn = mock_pg_tx._mock_conn

        await set_up_compilation_track_identity_schema(mock_pg_tx)

        executed = [call.args[0] for call in conn.execute.await_args_list]
        assert not any("INSERT" in sql for sql in executed)
        assert not any("UPDATE" in sql for sql in executed)
        assert not any("DELETE" in sql for sql in executed)
