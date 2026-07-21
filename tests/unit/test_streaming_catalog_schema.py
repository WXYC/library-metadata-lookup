"""Unit tests for the streaming-catalog schema bootstrap (LML#842 PR A).

Two surfaces, mirroring ``test_streaming_url_cache_schema.py``:

* ``entity/streaming_catalog.sql`` — the canonical-DDL reference file. It must
  describe the four ``lml_cache`` tables (``streaming_album``,
  ``streaming_album_service``, ``streaming_track_result``,
  ``streaming_coverage_baseline``), the named service CHECK, and the
  no-regress guard functions + triggers, so a reviewer / operator has the DDL
  inline. A normalized-containment check pins every runtime statement —
  including the PL/pgSQL guard bodies — to the reference verbatim.
* ``set_up_streaming_catalog_schema`` — the lifespan/DAO bootstrap helper. It
  must issue pure, byte-stable DDL (CREATE/ALTER only — never a row
  mutation) inside one transaction on one connection, after a preamble that
  bounds lock waits and serializes concurrent boots. Note this module
  deliberately extends the ``lml_cache.*`` bootstrap convention beyond
  CREATE-TABLE-only: triggers and PL/pgSQL functions have no ``IF NOT
  EXISTS``, so their idempotent form is ``CREATE OR REPLACE`` (PG14+; the
  discogs-cache runs PG17).

PG is faked with a recording double (``AsyncMock(spec=PgSource)`` can't model
``async with pg.acquire()``); the integration layer
(``tests/integration/test_streaming_catalog.py``) drives the real DDL and the
trigger semantics matrix against PostgreSQL.
"""

from __future__ import annotations

import re
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from entity.streaming_catalog import (
    _BOOTSTRAP_ADVISORY_LOCK,
    _BOOTSTRAP_LOCK_TIMEOUT,
    _DDL_STATEMENTS,
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


def _normalize_sql(text: str) -> str:
    """Strip ``--`` comments and collapse all whitespace to single spaces.

    Safe here because no statement in either file embeds a literal ``--``
    inside a string (the RAISE messages use an em-dash).
    """
    lines = [line.split("--", 1)[0] for line in text.splitlines()]
    return " ".join(" ".join(lines).split())


class _FakeTransaction:
    def __init__(self, source: _FakePgSource) -> None:
        self._source = source

    async def __aenter__(self) -> _FakeTransaction:
        self._source.transaction_starts += 1
        self._source.in_transaction = True
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        self._source.in_transaction = False


class _FakeConnection:
    def __init__(self, source: _FakePgSource) -> None:
        self._source = source

    def transaction(self) -> _FakeTransaction:
        return _FakeTransaction(self._source)

    async def execute(self, sql: str, *args: object) -> str:
        self._source.executed.append((sql, self._source.in_transaction))
        return "CREATE"


class _FakePgSource:
    """Records every statement the bootstrap issues and whether it ran inside
    the wrapping transaction. Deliberately defines only ``acquire`` — any
    other attribute access (pool-level ``execute``/``fetchone``) is an
    ``AttributeError``, which doubles as the only-one-connection assertion."""

    def __init__(self) -> None:
        self.executed: list[tuple[str, bool]] = []
        self.acquire_count = 0
        self.transaction_starts = 0
        self.in_transaction = False

    @property
    def statements(self) -> list[str]:
        return [sql for sql, _ in self.executed]

    @asynccontextmanager
    async def acquire(self):
        self.acquire_count += 1
        yield _FakeConnection(self)


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
        # 3 functions (album-service row guard, track row guard, shared
        # TRUNCATE guard) wired through 4 triggers (row + TRUNCATE per table).
        ddl = _SQL_REFERENCE.read_text()
        assert ddl.count("CREATE OR REPLACE FUNCTION") == 3
        assert ddl.count("CREATE OR REPLACE TRIGGER") == 4
        assert ALLOW_URL_REMOVAL_GUC in ddl

    def test_declares_the_key_constraints(self):
        ddl = _SQL_REFERENCE.read_text()
        assert "UNIQUE (normalized_artist, normalized_title)" in ddl
        assert "PRIMARY KEY (album_id, service)" in ddl
        assert "UNIQUE (album_id, artist, title)" in ddl

    def test_reference_contains_every_runtime_statement_verbatim(self):
        # Substring checks above can't see a drifted PL/pgSQL body (guard
        # predicates live inside the $$...$$ blocks). Normalize both sides and
        # require every runtime statement to appear verbatim in the reference.
        reference = _normalize_sql(_SQL_REFERENCE.read_text())
        for statement in _DDL_STATEMENTS:
            normalized = _normalize_sql(statement)
            assert normalized in reference, (
                "runtime DDL statement missing from (or drifted in) "
                f"entity/streaming_catalog.sql: {statement.lstrip()[:80]!r}"
            )


@pytest.mark.asyncio
class TestSetUpStreamingCatalogSchema:
    """``set_up_streaming_catalog_schema`` runs exactly the idempotent DDL."""

    async def test_creates_schema_first_then_everything_else(self):
        pg = _FakePgSource()

        await set_up_streaming_catalog_schema(pg)

        ddl = pg.statements[2:]  # after the two-preamble
        assert "CREATE SCHEMA IF NOT EXISTS lml_cache" in ddl[0]
        joined = "\n".join(ddl)
        for table in _TABLES:
            assert f"CREATE TABLE IF NOT EXISTS lml_cache.{table}" in joined
        assert "DROP CONSTRAINT IF EXISTS streaming_album_service_valid" in joined
        assert "ADD CONSTRAINT streaming_album_service_valid CHECK" in joined
        assert joined.count("CREATE OR REPLACE FUNCTION") == 3
        assert joined.count("CREATE OR REPLACE TRIGGER") == 4
        assert joined.count("BEFORE TRUNCATE ON") == 2
        assert "CREATE INDEX IF NOT EXISTS idx_streaming_album_service_status" in joined
        assert "CREATE INDEX IF NOT EXISTS idx_streaming_track_result_status" in joined

    async def test_runs_as_one_transaction_on_one_connection(self):
        # All-or-nothing: a mid-boot failure must never leave tables standing
        # without their guard triggers (every statement is transactional DDL).
        pg = _FakePgSource()

        await set_up_streaming_catalog_schema(pg)

        assert pg.acquire_count == 1
        assert pg.transaction_starts == 1
        assert all(in_txn for _, in_txn in pg.executed)

    async def test_preamble_bounds_lock_waits_and_serializes_boots(self):
        pg = _FakePgSource()

        await set_up_streaming_catalog_schema(pg)

        assert pg.statements[0] == _BOOTSTRAP_LOCK_TIMEOUT
        assert pg.statements[1] == _BOOTSTRAP_ADVISORY_LOCK
        assert pg.statements[2:] == list(_DDL_STATEMENTS)

    async def test_alter_check_admits_the_full_service_set(self):
        pg = _FakePgSource()

        await set_up_streaming_catalog_schema(pg)

        joined = "\n".join(pg.statements)
        in_list = re.search(r"ADD CONSTRAINT.*?service\s+IN\s*\(([^)]*)\)", joined, re.DOTALL)
        assert in_list is not None, "ADD CONSTRAINT IN-list not found in the ALTER"
        alter_services = set(re.findall(r"'([^']+)'", in_list.group(1)))
        assert alter_services == set(_SERVICES)

    async def test_guards_honor_the_opt_in_guc(self):
        # All three guard functions must consult the documented escape-hatch
        # GUC — it is what makes targeted manual revocation (docs/scripts.md
        # runbook) possible without superuser DISABLE TRIGGER gymnastics.
        pg = _FakePgSource()

        await set_up_streaming_catalog_schema(pg)

        functions = [sql for sql in pg.statements if "CREATE OR REPLACE FUNCTION" in sql]
        assert len(functions) == 3
        for fn_sql in functions:
            assert ALLOW_URL_REMOVAL_GUC in fn_sql

    async def test_bootstrap_is_idempotent(self):
        # Re-running the bootstrap issues the byte-identical DDL each time.
        first_pg = _FakePgSource()
        second_pg = _FakePgSource()

        await set_up_streaming_catalog_schema(first_pg)
        await set_up_streaming_catalog_schema(second_pg)

        assert first_pg.statements == second_pg.statements

    async def test_bootstrap_is_pure_ddl_after_the_preamble(self):
        # The bootstrap must never mutate rows. The sibling tests grep for
        # INSERT/UPDATE/DELETE substrings, but the guard-trigger DDL here
        # legitimately contains "BEFORE UPDATE OR DELETE" — so assert on the
        # statement head instead: after the two-statement preamble (SET LOCAL
        # + advisory lock SELECT, both row-neutral), every statement is CREATE
        # or ALTER.
        pg = _FakePgSource()

        await set_up_streaming_catalog_schema(pg)

        assert pg.statements[:2] == [_BOOTSTRAP_LOCK_TIMEOUT, _BOOTSTRAP_ADVISORY_LOCK]
        ddl = pg.statements[2:]
        assert ddl, "bootstrap issued no DDL"
        for sql in ddl:
            head = sql.lstrip().split()[0]
            assert head in {"CREATE", "ALTER"}, f"non-DDL statement in bootstrap: {sql[:80]!r}"
