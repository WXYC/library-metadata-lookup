"""Unit tests for the streaming-catalog schema bootstrap (LML#842 PR A).

Two surfaces, mirroring ``test_streaming_url_cache_schema.py``:

* ``entity/streaming_catalog.sql`` — the canonical-DDL reference file. It must
  describe the four ``lml_cache`` tables (``streaming_album``,
  ``streaming_album_service``, ``streaming_track_result``,
  ``streaming_coverage_baseline``), the named service CHECK, and the
  no-regress guard functions + triggers, so a reviewer / operator has the DDL
  inline.
* ``set_up_streaming_catalog_schema`` — the lifespan/DAO bootstrap helper. It
  must issue pure, byte-stable DDL (CREATE/ALTER only — never a row
  mutation), idempotently. Note this module deliberately extends the
  ``lml_cache.*`` bootstrap convention beyond CREATE-TABLE-only: triggers and
  PL/pgSQL functions have no ``IF NOT EXISTS``, so their idempotent form is
  ``CREATE OR REPLACE`` (PG14+; the discogs-cache runs PG17).

PG is mocked; the integration layer
(``tests/integration/test_streaming_catalog.py``) drives the real DDL and the
trigger semantics matrix against PostgreSQL.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from entity.sources import PgSource
from entity.streaming_catalog import (
    _SERVICES,
    ALLOW_URL_REMOVAL_GUC,
    set_up_streaming_catalog_schema,
)

_SQL_REFERENCE = Path(__file__).resolve().parent.parent.parent / "entity" / "streaming_catalog.sql"

_TABLES = (
    "streaming_album",
    "streaming_album_service",
    "streaming_track_result",
    "streaming_coverage_baseline",
)


class TestCanonicalDDLReference:
    """``entity/streaming_catalog.sql`` is the inline DDL reference."""

    def test_reference_file_exists(self):
        assert _SQL_REFERENCE.is_file()

    def test_targets_lml_cache_schema_and_all_four_tables(self):
        ddl = _SQL_REFERENCE.read_text()
        assert "CREATE SCHEMA IF NOT EXISTS lml_cache" in ddl
        for table in _TABLES:
            assert f"lml_cache.{table}" in ddl, f"missing table {table} in the .sql reference"

    def test_has_named_service_check_constraint(self):
        ddl = _SQL_REFERENCE.read_text()
        assert "CONSTRAINT streaming_album_service_valid CHECK" in ddl

    def test_check_constraint_allowlist_matches_services(self):
        # The .sql is a hand-maintained mirror of the runtime DDL, which
        # generates its IN-list from ``_SERVICES``. Parse the first IN-list and
        # assert exact-set equality so the mirror can't drift.
        ddl = _SQL_REFERENCE.read_text()
        match = re.search(r"service\s+IN\s*\(([^)]*)\)", ddl)
        assert match is not None, "CHECK constraint IN-list not found in the .sql reference"
        sql_services = set(re.findall(r"'([^']+)'", match.group(1)))
        assert sql_services == set(_SERVICES), (
            f".sql CHECK allowlist {sql_services} drifted from _SERVICES "
            f"{set(_SERVICES)} — update entity/streaming_catalog.sql"
        )

    def test_covers_drift_services_from_the_real_prod_file(self):
        # tidal/youtube_music/soundcloud exist as drift columns in the real
        # prod streaming_availability.db; the normalized design admits them as
        # ordinary service values so the seed can map them without DDL churn.
        for service in ("tidal", "youtube_music", "soundcloud"):
            assert service in _SERVICES

    def test_describes_the_no_regress_guards(self):
        ddl = _SQL_REFERENCE.read_text()
        assert ddl.count("CREATE OR REPLACE FUNCTION") == 2
        assert ddl.count("CREATE OR REPLACE TRIGGER") == 2
        assert ALLOW_URL_REMOVAL_GUC in ddl

    def test_declares_the_key_constraints(self):
        ddl = _SQL_REFERENCE.read_text()
        assert "UNIQUE (normalized_artist, normalized_title)" in ddl
        assert "PRIMARY KEY (album_id, service)" in ddl
        assert "UNIQUE (album_id, artist, title)" in ddl


@pytest.mark.asyncio
class TestSetUpStreamingCatalogSchema:
    """``set_up_streaming_catalog_schema`` runs exactly the idempotent DDL."""

    async def test_creates_schema_first_then_everything_else(self):
        pg = AsyncMock(spec=PgSource)
        pg.execute = AsyncMock(return_value="CREATE")

        await set_up_streaming_catalog_schema(pg)

        executed = [call.args[0] for call in pg.execute.await_args_list]
        assert "CREATE SCHEMA IF NOT EXISTS lml_cache" in executed[0]
        joined = "\n".join(executed)
        for table in _TABLES:
            assert f"CREATE TABLE IF NOT EXISTS lml_cache.{table}" in joined
        assert "DROP CONSTRAINT IF EXISTS streaming_album_service_valid" in joined
        assert "ADD CONSTRAINT streaming_album_service_valid CHECK" in joined
        assert joined.count("CREATE OR REPLACE FUNCTION") == 2
        assert joined.count("CREATE OR REPLACE TRIGGER") == 2
        assert "CREATE INDEX IF NOT EXISTS idx_streaming_album_service_status" in joined
        assert "CREATE INDEX IF NOT EXISTS idx_streaming_track_result_status" in joined

    async def test_alter_check_admits_the_full_service_set(self):
        pg = AsyncMock(spec=PgSource)
        pg.execute = AsyncMock(return_value="ALTER")

        await set_up_streaming_catalog_schema(pg)

        joined = "\n".join(call.args[0] for call in pg.execute.await_args_list)
        in_list = re.search(r"ADD CONSTRAINT.*?service\s+IN\s*\(([^)]*)\)", joined, re.DOTALL)
        assert in_list is not None, "ADD CONSTRAINT IN-list not found in the ALTER"
        alter_services = set(re.findall(r"'([^']+)'", in_list.group(1)))
        assert alter_services == set(_SERVICES)

    async def test_guards_honor_the_opt_in_guc(self):
        # Both guard functions must consult the documented escape-hatch GUC —
        # it is what makes targeted manual revocation (docs/scripts.md runbook)
        # possible without superuser DISABLE TRIGGER gymnastics.
        pg = AsyncMock(spec=PgSource)
        pg.execute = AsyncMock(return_value="CREATE")

        await set_up_streaming_catalog_schema(pg)

        functions = [
            call.args[0]
            for call in pg.execute.await_args_list
            if "CREATE OR REPLACE FUNCTION" in call.args[0]
        ]
        assert len(functions) == 2
        for fn_sql in functions:
            assert ALLOW_URL_REMOVAL_GUC in fn_sql

    async def test_bootstrap_is_idempotent(self):
        # Re-running the bootstrap issues the byte-identical DDL each time.
        pg = AsyncMock(spec=PgSource)
        pg.execute = AsyncMock(return_value="CREATE")

        await set_up_streaming_catalog_schema(pg)
        first = [call.args[0] for call in pg.execute.await_args_list]
        pg.execute.reset_mock()
        await set_up_streaming_catalog_schema(pg)
        second = [call.args[0] for call in pg.execute.await_args_list]

        assert first == second

    async def test_bootstrap_is_pure_ddl(self):
        # The bootstrap must never mutate rows. The sibling tests grep for
        # INSERT/UPDATE/DELETE substrings, but the guard-trigger DDL here
        # legitimately contains "BEFORE UPDATE OR DELETE" — so assert on the
        # statement head instead: every statement is CREATE or ALTER.
        pg = AsyncMock(spec=PgSource)
        pg.execute = AsyncMock(return_value="CREATE")

        await set_up_streaming_catalog_schema(pg)

        executed = [call.args[0] for call in pg.execute.await_args_list]
        assert executed, "bootstrap issued no DDL"
        for sql in executed:
            head = sql.lstrip().split()[0]
            assert head in {"CREATE", "ALTER"}, f"non-DDL statement in bootstrap: {sql[:80]!r}"
        pg.fetchone.assert_not_awaited()
