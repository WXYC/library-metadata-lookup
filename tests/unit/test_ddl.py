"""Unit tests for the shared ``lml_cache.*`` DDL primitives (LML#1038).

Two surfaces this module owns, previously duplicated across all 8 ``entity/``
store modules:

* ``LML_CACHE_SCHEMA_DDL`` -- the single ``CREATE SCHEMA IF NOT EXISTS
  lml_cache`` statement every bootstrap issues first.
* ``widen_service_check`` / ``build_widen_service_check_sql`` -- the newest
  widen-only named-CHECK maintenance generation (LML#890, originally
  ``entity/streaming_catalog.py``'s ``_DDL_ALBUM_SERVICE_WIDEN_CHECK``),
  generalized to any ``(table, constraint, services)`` triple so the two
  older, narrower ports (a static DROP/ADD in
  ``entity/track_streaming_url_cache.py``, an un-validated 33-line deparse in
  ``entity/streaming_url_cache.py``) can retire in its favor.
"""

from __future__ import annotations

import re
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from entity.ddl import (
    LML_CACHE_SCHEMA_DDL,
    bootstrap_lml_cache_table,
    build_widen_service_check_sql,
    widen_service_check,
)


class TestLmlCacheSchemaDdl:
    def test_is_create_schema_if_not_exists_lml_cache(self):
        assert LML_CACHE_SCHEMA_DDL == "CREATE SCHEMA IF NOT EXISTS lml_cache"


class TestBuildWidenServiceCheckSql:
    """Pure SQL-text builder -- no PG access, so these run with no mocks."""

    def test_targets_the_given_table_and_constraint(self):
        sql = build_widen_service_check_sql(
            table="album_streaming_url_cache",
            constraint="album_streaming_url_cache_service_valid",
            services=("apple_music_album", "spotify_album", "bandcamp"),
        )
        assert "lml_cache.album_streaming_url_cache" in sql
        assert "album_streaming_url_cache_service_valid" in sql

    def test_is_a_do_block(self):
        sql = build_widen_service_check_sql(
            table="track_streaming_url_cache",
            constraint="track_streaming_url_cache_service_valid",
            services=("apple_music_track",),
        )
        assert sql.startswith("DO $")
        assert sql.rstrip().endswith("$")

    def test_carries_the_full_service_set_as_a_code_side_array(self):
        # Mirrors test_streaming_url_cache_schema.py's
        # test_widen_block_admits_the_full_service_set: the shipped set is
        # statically visible in the DO block as a PL/pgSQL array literal.
        services = ("apple_music_album", "spotify_album", "bandcamp")
        sql = build_widen_service_check_sql(
            table="album_streaming_url_cache",
            constraint="album_streaming_url_cache_service_valid",
            services=services,
        )
        match = re.search(r"code_services\s+text\[\]\s*:=\s*ARRAY\[([^\]]*)\]", sql)
        assert match is not None
        assert set(re.findall(r"'([^']+)'", match.group(1))) == set(services)

    def test_merges_never_narrows_is_documented_by_the_widen_only_shape(self):
        # Structural smoke: the block must EXECUTE an ALTER that DROP+ADDs the
        # named constraint (widen-only maintenance), never a bare unconditional
        # ALTER -- that's the whole point of the newest generation over the
        # deleted static-DROP/ADD port.
        sql = build_widen_service_check_sql(
            table="t", constraint="t_service_valid", services=("a",)
        )
        assert "DROP CONSTRAINT IF EXISTS t_service_valid" in sql
        assert "ADD CONSTRAINT t_service_valid CHECK (service IN (" in sql
        assert "EXECUTE" in sql

    def test_two_calls_with_different_tables_produce_distinct_sql(self):
        sql_a = build_widen_service_check_sql(
            table="album_streaming_url_cache",
            constraint="album_streaming_url_cache_service_valid",
            services=("apple_music_album",),
        )
        sql_b = build_widen_service_check_sql(
            table="track_streaming_url_cache",
            constraint="track_streaming_url_cache_service_valid",
            services=("apple_music_track",),
        )
        assert sql_a != sql_b
        assert "album_streaming_url_cache" not in sql_b
        assert "track_streaming_url_cache" not in sql_a

    def test_is_deterministic(self):
        kwargs = {
            "table": "streaming_album_service",
            "constraint": "streaming_album_service_valid",
            "services": ("apple_music", "spotify"),
        }
        assert build_widen_service_check_sql(**kwargs) == build_widen_service_check_sql(**kwargs)


@pytest.mark.asyncio
class TestWidenServiceCheck:
    """The async convenience wrapper: build the SQL, execute it on ``pg``."""

    async def test_executes_the_built_sql_exactly_once(self):
        pg = AsyncMock()
        pg.execute = AsyncMock(return_value="DO")

        await widen_service_check(
            pg,
            table="album_streaming_url_cache",
            constraint="album_streaming_url_cache_service_valid",
            services=("apple_music_album", "spotify_album"),
        )

        assert pg.execute.await_count == 1
        executed_sql = pg.execute.await_args_list[0].args[0]
        assert executed_sql == build_widen_service_check_sql(
            table="album_streaming_url_cache",
            constraint="album_streaming_url_cache_service_valid",
            services=("apple_music_album", "spotify_album"),
        )

    async def test_works_against_a_bare_execute_only_double(self):
        # Call sites pass either an entity.sources.PgSource (autocommit) or a
        # raw asyncpg.Connection already inside a transaction (the heavy-tier
        # bootstraps) -- widen_service_check must not assume anything beyond
        # an async ``execute(sql, *args)``.
        class _ExecuteOnly:
            def __init__(self) -> None:
                self.calls: list[str] = []

            async def execute(self, sql: str, *args: object) -> str:
                self.calls.append(sql)
                return "DO"

        conn = _ExecuteOnly()

        await widen_service_check(
            conn,
            table="track_streaming_url_cache",
            constraint="track_streaming_url_cache_service_valid",
            services=("apple_music_track",),
        )

        assert len(conn.calls) == 1
        assert "lml_cache.track_streaming_url_cache" in conn.calls[0]


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
        return "CREATE"


class _FakePgSource:
    """Records every statement issued and whether it ran inside the wrapping
    transaction. Mirrors ``test_streaming_url_cache_schema.py``'s fake so a
    reader who has seen one recognizes the other -- deliberately defines only
    ``acquire``; any pool-level ``execute``/``fetchone`` call is an
    ``AttributeError``, doubling as a not-pool-level-called assertion."""

    def __init__(self) -> None:
        self.executed: list[tuple[str, bool]] = []
        self.acquire_count = 0
        self.transaction_starts = 0
        self.transaction_ends = 0
        self.in_transaction = False

    @property
    def statements(self) -> list[str]:
        return [sql for sql, _ in self.executed]

    @asynccontextmanager
    async def acquire(self):
        self.acquire_count += 1
        yield _FakeConnection(self)


@pytest.mark.asyncio
class TestBootstrapLmlCacheTable:
    """PR-2: the shared transactional posture for every ``lml_cache.*`` bootstrap.

    Gives all 8 bootstraps (only 2 have it today: ``streaming_url_cache.py``,
    ``streaming_catalog.py``) one transaction on one connection behind a
    ``SET LOCAL lock_timeout`` preamble, plus an opt-in
    ``pg_advisory_xact_lock`` for the (now rare) bootstrap invoked from
    somewhere other than ``main.py``'s lifespan -- see the PR-2 body for the
    per-table advisory-key decision.
    """

    async def test_runs_every_statement_inside_one_transaction(self):
        pg = _FakePgSource()

        await bootstrap_lml_cache_table(
            pg, "CREATE SCHEMA IF NOT EXISTS lml_cache", "CREATE TABLE t ()"
        )

        assert pg.acquire_count == 1
        assert pg.transaction_starts == 1
        assert pg.transaction_ends == 1
        assert pg.in_transaction is False
        assert all(in_txn for _, in_txn in pg.executed[1:])  # [0] is the lock_timeout preamble

    async def test_issues_the_lock_timeout_preamble_first(self):
        pg = _FakePgSource()

        await bootstrap_lml_cache_table(pg, "CREATE SCHEMA IF NOT EXISTS lml_cache")

        assert pg.statements[0] == "SET LOCAL lock_timeout = '10s'"

    async def test_statements_run_in_order_after_the_preamble(self):
        pg = _FakePgSource()

        await bootstrap_lml_cache_table(pg, "STATEMENT A", "STATEMENT B", "STATEMENT C")

        assert pg.statements[1:] == ["STATEMENT A", "STATEMENT B", "STATEMENT C"]

    async def test_no_advisory_lock_by_default(self):
        pg = _FakePgSource()

        await bootstrap_lml_cache_table(pg, "CREATE SCHEMA IF NOT EXISTS lml_cache")

        assert not any("pg_advisory" in sql for sql in pg.statements)

    async def test_advisory_key_issues_the_xact_lock_right_after_the_preamble(self):
        pg = _FakePgSource()

        await bootstrap_lml_cache_table(pg, "CREATE TABLE t ()", advisory_key=123456)

        assert pg.statements[0] == "SET LOCAL lock_timeout = '10s'"
        assert pg.statements[1] == "SELECT pg_advisory_xact_lock(123456)"
        assert pg.statements[2] == "CREATE TABLE t ()"

    async def test_accepts_a_callable_statement_for_custom_per_statement_logic(self):
        # entity/streaming_catalog.py's CREATE-OR-REPLACE-skip-if-unchanged
        # functions/triggers, and every widen_service_check call site, need
        # more than "execute this literal string" -- a callable receives the
        # live connection and decides for itself.
        pg = _FakePgSource()
        calls: list[str] = []

        async def _custom(conn: object) -> None:
            calls.append("ran")
            await conn.execute("CUSTOM STATEMENT")  # type: ignore[attr-defined]

        await bootstrap_lml_cache_table(pg, "CREATE SCHEMA IF NOT EXISTS lml_cache", _custom)

        assert calls == ["ran"]
        assert pg.statements[2:] == ["CUSTOM STATEMENT"]

    async def test_widen_service_check_composes_as_a_callable_statement(self):
        pg = _FakePgSource()

        await bootstrap_lml_cache_table(
            pg,
            "CREATE SCHEMA IF NOT EXISTS lml_cache",
            "CREATE TABLE lml_cache.t (service TEXT)",
            lambda conn: widen_service_check(
                conn, table="t", constraint="t_service_valid", services=("a",)
            ),
        )

        assert pg.statements[-1].startswith("DO $")
        assert "t_service_valid" in pg.statements[-1]

    async def test_mid_boot_failure_propagates_and_closes_the_transaction(self):
        pg = _FakePgSource()

        async def _boom(conn: object) -> None:
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError, match="boom"):
            await bootstrap_lml_cache_table(pg, "CREATE SCHEMA IF NOT EXISTS lml_cache", _boom)

        assert pg.transaction_starts == 1
        assert pg.transaction_ends == 1  # __aexit__ still runs (rollback), even on raise
