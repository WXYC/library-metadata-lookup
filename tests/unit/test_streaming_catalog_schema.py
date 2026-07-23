"""Unit tests for the streaming-catalog schema bootstrap (LML#842 PR A).

Two surfaces, mirroring ``test_streaming_url_cache_schema.py``:

* ``entity/streaming_catalog.sql`` — the canonical-DDL reference file. It must
  describe the four ``lml_cache`` tables (``streaming_album``,
  ``streaming_album_service``, ``streaming_track_result``,
  ``streaming_coverage_baseline``), the named service CHECK, and the
  no-regress guard functions + triggers, so a reviewer / operator has the DDL
  inline. A normalized exact-equality check pins the reference to the runtime
  statements — including the PL/pgSQL guard bodies — same statements, same
  order, nothing stale; a per-statement containment loop keeps the diagnostics
  readable when one statement drifts.
* ``set_up_streaming_catalog_schema`` — the lifespan/DAO bootstrap helper. It
  must issue pure, byte-stable DDL (CREATE/ALTER only — never a row
  mutation) inside one transaction on one connection, after a preamble that
  bounds lock waits and serializes concurrent boots. Note this module
  deliberately extends the ``lml_cache.*`` bootstrap convention beyond
  CREATE-TABLE-only: triggers and PL/pgSQL functions have no ``IF NOT
  EXISTS``, so their idempotent form is ``CREATE OR REPLACE`` (PG14+; the
  discogs-cache runs PG17).

PG is faked with a recording double: ``AsyncMock`` can model ``async with
pg.acquire()``, but the nested ``conn.transaction()`` state tracking, the
per-statement ``(sql, in_transaction)`` record, and ordered failure injection
aren't expressible with the conftest AsyncMock helpers. The integration layer
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
    _TABLES,
    ALLOW_URL_REMOVAL_GUC,
    set_up_streaming_catalog_schema,
)

_SQL_REFERENCE = Path(__file__).resolve().parent.parent.parent / "entity" / "streaming_catalog.sql"


def _normalize_sql(text: str) -> str:
    """Strip ``--`` comments, drop semicolons, collapse whitespace.

    Semicolons are replaced on BOTH sides of every comparison (the .sql
    carries statement terminators, the runtime statements don't; the ones
    inside the PL/pgSQL bodies are dropped symmetrically, so equality is
    unaffected). A ``--`` inside a runtime statement would silently truncate
    it — ``test_runtime_statements_carry_no_line_comments`` guards that side;
    the .sql's ``--`` prose is the point of the reference file. The RAISE
    messages use an em-dash, never ``--``.
    """
    lines = [line.split("--", 1)[0] for line in text.splitlines()]
    return " ".join(" ".join(lines).replace(";", " ").split())


class _FakeTransaction:
    def __init__(self, source: _FakePgSource) -> None:
        self._source = source

    async def __aenter__(self) -> _FakeTransaction:
        self._source.transaction_starts += 1
        self._source.in_transaction = True
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        self._source.transaction_ends += 1
        self._source.in_transaction = False


class _FakeConnection:
    def __init__(self, source: _FakePgSource) -> None:
        self._source = source

    def transaction(self) -> _FakeTransaction:
        return _FakeTransaction(self._source)

    async def execute(self, sql: str, *args: object) -> str:
        self._source.executed.append((sql, self._source.in_transaction))
        fail_on = self._source.fail_on
        if fail_on is not None and fail_on in sql:
            raise RuntimeError(f"injected failure on {fail_on!r}")
        return "CREATE"


class _FakePgSource:
    """Records every statement the bootstrap issues and whether it ran inside
    the wrapping transaction. Deliberately defines only ``acquire`` — any
    other attribute access (pool-level ``execute``/``fetchone``) is an
    ``AttributeError``, which doubles as the only-one-connection assertion.
    ``fail_on`` injects a mid-boot failure on the first statement containing
    that substring, for the error-propagation test."""

    def __init__(self, fail_on: str | None = None) -> None:
        self.executed: list[tuple[str, bool]] = []
        self.acquire_count = 0
        self.transaction_starts = 0
        self.transaction_ends = 0
        self.in_transaction = False
        self.fail_on = fail_on

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
        ddl = _SQL_REFERENCE.read_text(encoding="utf-8")
        assert "CREATE SCHEMA IF NOT EXISTS lml_cache" in ddl
        for table in _TABLES:
            assert f"lml_cache.{table}" in ddl, f"missing table {table} in the .sql reference"

    def test_has_named_service_check_constraint(self):
        ddl = _SQL_REFERENCE.read_text(encoding="utf-8")
        assert "CONSTRAINT streaming_album_service_valid CHECK" in ddl

    def test_check_constraint_allowlist_matches_services(self):
        # The .sql mirrors the runtime DDL (regenerated via
        # scripts/regenerate_streaming_catalog_sql.py), which generates its
        # IN-list from ``_SERVICES``. Anchor on the named constraint (the
        # widen DO block contains other ``service IN`` fragments) and assert
        # exact-set equality so the mirror can't drift.
        ddl = _SQL_REFERENCE.read_text(encoding="utf-8")
        match = re.search(
            r"CONSTRAINT streaming_album_service_valid CHECK\s*\(\s*service\s+IN\s*\(([^)]*)\)",
            ddl,
        )
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
        # 5 functions (album, album-service, track, coverage-baseline row
        # guards + the shared TRUNCATE guard) wired through 8 triggers (a row
        # guard and a TRUNCATE guard per table).
        ddl = _SQL_REFERENCE.read_text(encoding="utf-8")
        assert ddl.count("CREATE OR REPLACE FUNCTION") == 5
        assert ddl.count("CREATE OR REPLACE TRIGGER") == 8
        assert ALLOW_URL_REMOVAL_GUC in ddl

    def test_declares_the_key_constraints(self):
        ddl = _SQL_REFERENCE.read_text(encoding="utf-8")
        assert "UNIQUE (normalized_artist, normalized_title)" in ddl
        assert "PRIMARY KEY (album_id, service)" in ddl
        assert "UNIQUE (album_id, artist, title)" in ddl

    def test_reference_contains_every_runtime_statement_verbatim(self):
        # Substring checks above can't see a drifted PL/pgSQL body (guard
        # predicates live inside the $$...$$ blocks). Normalize both sides and
        # require every runtime statement to appear verbatim in the reference —
        # kept alongside the exact-equality test below because a per-statement
        # loop names WHICH statement drifted.
        reference = _normalize_sql(_SQL_REFERENCE.read_text(encoding="utf-8"))
        for statement in _DDL_STATEMENTS:
            normalized = _normalize_sql(statement)
            assert normalized in reference, (
                "runtime DDL statement missing from (or drifted in) "
                f"entity/streaming_catalog.sql: {statement.lstrip()[:80]!r}"
            )

    def test_reference_equals_the_runtime_statements_exactly(self):
        # Containment alone lets the reference carry stale extra statements
        # (or reorder them) without failing. The whole normalized file must
        # equal the normalized runtime sequence: same statements, same order,
        # nothing extra.
        reference = _normalize_sql(_SQL_REFERENCE.read_text(encoding="utf-8"))
        runtime = _normalize_sql(" ".join(_DDL_STATEMENTS))
        assert reference == runtime

    def test_tables_constant_matches_the_create_table_statements(self):
        # ``_TABLES`` is the one authoritative table list (tests derive
        # FK-safe drop order by reversing it), so it must track the CREATE
        # TABLE statements exactly — names AND creation order.
        created = re.findall(
            r"CREATE TABLE IF NOT EXISTS lml_cache\.([a-z_]+) \(",
            "\n".join(_DDL_STATEMENTS),
        )
        assert created == list(_TABLES)

    def test_reference_equals_the_generator_output_byte_for_byte(self):
        # The normalized-equality tests above pin the STATEMENTS; this pins
        # the whole file — prose included — to the generator, so a hand edit
        # to the .sql (or an unregenerated statement change) fails loudly
        # with the fix being "run the generator".
        from scripts.regenerate_streaming_catalog_sql import build_reference

        assert _SQL_REFERENCE.read_text(encoding="utf-8") == build_reference(), (
            "entity/streaming_catalog.sql is stale — regenerate via "
            "`uv run python -m scripts.regenerate_streaming_catalog_sql`"
        )

    def test_runtime_statements_carry_no_line_comments(self):
        # ``_normalize_sql`` strips ``--`` to end-of-line; a comment inside a
        # runtime statement would make the parity tests compare a silently
        # truncated statement. Keep prose in the .sql reference and in Python
        # comments around the constants — never inside the statements.
        for statement in _DDL_STATEMENTS:
            assert "--" not in statement, (
                f"line comment inside runtime DDL: {statement.lstrip()[:80]!r}"
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
        assert joined.count("CREATE OR REPLACE FUNCTION") == 5
        assert joined.count("CREATE OR REPLACE TRIGGER") == 8
        assert joined.count("BEFORE TRUNCATE ON") == 4
        assert "CREATE INDEX IF NOT EXISTS idx_streaming_album_service_status" in joined
        assert (
            "CREATE INDEX IF NOT EXISTS idx_streaming_track_result_status_id\n"
            "    ON lml_cache.streaming_track_result (resolution_status, id)" in joined
        )
        assert "DROP INDEX IF EXISTS lml_cache.idx_streaming_track_result_status" in joined

    async def test_runs_as_one_transaction_on_one_connection(self):
        # All-or-nothing: a mid-boot failure must never leave tables standing
        # without their guard triggers (every statement is transactional DDL).
        pg = _FakePgSource()

        await set_up_streaming_catalog_schema(pg)

        assert pg.acquire_count == 1
        assert pg.transaction_starts == 1
        assert pg.transaction_ends == 1
        assert pg.in_transaction is False
        assert all(in_txn for _, in_txn in pg.executed)

    async def test_mid_boot_failure_propagates_and_closes_the_transaction(self):
        # The bootstrap must not swallow a mid-boot error (the lifespan caller
        # decides how to degrade), and the transaction context must still be
        # exited so the real asyncpg transaction rolls back cleanly.
        pg = _FakePgSource(fail_on="streaming_track_result")

        with pytest.raises(RuntimeError, match="injected failure"):
            await set_up_streaming_catalog_schema(pg)

        assert pg.transaction_starts == 1
        assert pg.transaction_ends == 1
        assert pg.in_transaction is False

    async def test_preamble_bounds_lock_waits_and_serializes_boots(self):
        pg = _FakePgSource()

        await set_up_streaming_catalog_schema(pg)

        assert pg.statements[0] == _BOOTSTRAP_LOCK_TIMEOUT
        assert pg.statements[1] == _BOOTSTRAP_ADVISORY_LOCK
        assert pg.statements[2:] == list(_DDL_STATEMENTS)

    async def test_widen_block_admits_the_full_service_set(self):
        # The widen-only DO block carries the shipped set as a PL/pgSQL array
        # (``code_services``); the merged IN-list is computed at runtime, so
        # this is the one place the code-side set is statically visible.
        pg = _FakePgSource()

        await set_up_streaming_catalog_schema(pg)

        joined = "\n".join(pg.statements)
        in_list = re.search(r"code_services\s+text\[\]\s*:=\s*ARRAY\[([^\]]*)\]", joined)
        assert in_list is not None, "code_services array not found in the widen DO block"
        widen_services = set(re.findall(r"'([^']+)'", in_list.group(1)))
        assert widen_services == set(_SERVICES)

    async def test_guards_honor_the_opt_in_guc(self):
        # All five guard functions must CONSULT the documented escape-hatch
        # GUC — it is what makes targeted manual revocation possible without
        # superuser DISABLE TRIGGER gymnastics. Assert the current_setting()
        # read, not the bare GUC name: every RAISE hint also contains the
        # name, so a substring check alone is satisfied by a guard that
        # mentions the opt-in without ever honoring it.
        pg = _FakePgSource()

        await set_up_streaming_catalog_schema(pg)

        functions = [sql for sql in pg.statements if "CREATE OR REPLACE FUNCTION" in sql]
        assert len(functions) == 5
        for fn_sql in functions:
            assert f"current_setting('{ALLOW_URL_REMOVAL_GUC}'" in fn_sql

    async def test_every_raise_carries_the_one_opt_in_hint(self):
        # 20 RAISE sites across the five guard functions; each renders the
        # same hoisted hint literal as its final message line, so the runbook
        # pointer can't drift per-site.
        pg = _FakePgSource()

        await set_up_streaming_catalog_schema(pg)

        functions = "\n".join(sql for sql in pg.statements if "CREATE OR REPLACE FUNCTION" in sql)
        hint = (
            f"opt in via set_config(''{ALLOW_URL_REMOVAL_GUC}'', ''on'', true) in this transaction"
        )
        assert functions.count("RAISE EXCEPTION") == 20
        assert functions.count(hint) == 20

    async def test_bootstrap_is_pure_ddl_after_the_preamble(self):
        # The bootstrap must never mutate rows. The sibling tests grep for
        # INSERT/UPDATE/DELETE substrings, but the guard-trigger DDL here
        # legitimately contains "BEFORE UPDATE OR DELETE" — so assert on the
        # statement head instead: after the two-statement preamble (SET LOCAL
        # + advisory lock SELECT, both row-neutral), every statement is CREATE,
        # ALTER, the widen-only DO block (which only ever EXECUTEs an ALTER), or
        # a DROP INDEX IF EXISTS (row-neutral DDL: the track status-index
        # reshape drops the superseded single-column form).
        pg = _FakePgSource()

        await set_up_streaming_catalog_schema(pg)

        assert pg.statements[:2] == [_BOOTSTRAP_LOCK_TIMEOUT, _BOOTSTRAP_ADVISORY_LOCK]
        ddl = pg.statements[2:]
        assert ddl, "bootstrap issued no DDL"
        for sql in ddl:
            head = sql.lstrip().split()[0]
            assert head in {"CREATE", "ALTER", "DO", "DROP"}, (
                f"non-DDL statement in bootstrap: {sql[:80]!r}"
            )
