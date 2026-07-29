"""Unit tests for the streaming-URL cache schema bootstrap (LML#573, LML#886).

Two surfaces:

* ``entity/streaming_url_cache.sql`` — the canonical-DDL reference file
  (mirrors ``entity/release_identity.sql``). It must describe the
  ``lml_cache.album_streaming_url_cache`` table with the named CHECK
  constraint and composite PK, so a reviewer / operator has the DDL inline.
* ``set_up_streaming_url_cache_schema`` — the lifespan bootstrap helper. It
  must issue ``CREATE SCHEMA`` then ``CREATE TABLE`` (named CHECK), then the
  widen-only DO block (LML#886 — ported from ``entity/streaming_catalog.py``'s
  ``_DDL_ALBUM_SERVICE_WIDEN_CHECK``), all inside one transaction on one
  connection behind a ``lock_timeout`` + advisory-lock preamble. No row
  mutation (the LML#571→#573 apple-table backfill was removed in #573's
  PR-2).

PG is faked with a recording double (mirrors
``test_streaming_catalog_schema.py``'s ``_FakePgSource``): ``AsyncMock`` can
model ``async with pg.acquire()``, but the nested ``conn.transaction()``
bookkeeping needed to assert the wrapping-transaction shape isn't expressible
with the conftest AsyncMock helpers. The integration layer
(``tests/integration/test_streaming_url_persistent_lookup.py``) drives the
real DDL — including the widen-only rewrite semantics — against PostgreSQL.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from entity.streaming_url_cache import (
    _BOOTSTRAP_ADVISORY_LOCK,
    _BOOTSTRAP_LOCK_TIMEOUT,
    set_up_streaming_url_cache_schema,
)


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
    """Records every statement the bootstrap issues and whether it ran inside
    the wrapping transaction. Deliberately defines only ``acquire`` — any
    other attribute access (pool-level ``execute``/``fetchone``) is an
    ``AttributeError``, which doubles as the only-one-connection assertion."""

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


_SQL_REFERENCE = (
    Path(__file__).resolve().parent.parent.parent / "entity" / "streaming_url_cache.sql"
)


class TestCanonicalDDLReference:
    """``entity/streaming_url_cache.sql`` is the inline DDL reference."""

    def test_reference_file_exists(self):
        assert _SQL_REFERENCE.is_file()

    def test_targets_lml_cache_schema_and_table(self):
        ddl = _SQL_REFERENCE.read_text()
        assert "CREATE SCHEMA IF NOT EXISTS lml_cache" in ddl
        assert "lml_cache.album_streaming_url_cache" in ddl

    def test_has_named_check_constraint(self):
        ddl = _SQL_REFERENCE.read_text()
        assert "CONSTRAINT album_streaming_url_cache_service_valid CHECK" in ddl
        # PR-3 ships Apple + Spotify + Bandcamp in the constraint.
        assert "'apple_music_album'" in ddl
        assert "'spotify_album'" in ddl
        assert "'bandcamp'" in ddl

    def test_check_constraint_allowlist_matches_services(self):
        # The .sql is a hand-maintained mirror of the runtime DDL, which
        # generates its IN-list from ``_SERVICES``. A substring check would stay
        # green if a future PR edited ``_SERVICES`` but forgot the .sql; parse
        # the first IN-list (the CREATE TABLE CHECK) and assert exact-set
        # equality so the mirror can't drift.
        import re

        from entity.streaming_url_cache import _SERVICES

        ddl = _SQL_REFERENCE.read_text()
        match = re.search(r"service\s+IN\s*\(([^)]*)\)", ddl)
        assert match is not None, "CHECK constraint IN-list not found in the .sql reference"
        sql_services = set(re.findall(r"'([^']+)'", match.group(1)))
        assert sql_services == set(_SERVICES), (
            f".sql CHECK allowlist {sql_services} drifted from _SERVICES "
            f"{set(_SERVICES)} — update entity/streaming_url_cache.sql"
        )

    def test_has_composite_primary_key(self):
        ddl = _SQL_REFERENCE.read_text()
        assert "PRIMARY KEY (service, artist_normalized, album_normalized)" in ddl


@pytest.mark.asyncio
class TestSetUpStreamingUrlCacheSchema:
    """``set_up_streaming_url_cache_schema`` runs exactly the idempotent DDL."""

    async def test_creates_schema_table_then_widens_check(self):
        pg = _FakePgSource()

        await set_up_streaming_url_cache_schema(pg)

        # Two-statement preamble, then schema, table, then the widen-only DO
        # block (so a pre-existing prod table picks up new service values
        # that CREATE TABLE IF NOT EXISTS cannot add). No backfill.
        ddl = pg.statements[2:]
        assert len(ddl) == 3
        schema_sql, table_sql, widen_sql = ddl
        assert "CREATE SCHEMA IF NOT EXISTS lml_cache" in schema_sql
        assert "CREATE TABLE IF NOT EXISTS lml_cache.album_streaming_url_cache" in table_sql
        assert "CONSTRAINT album_streaming_url_cache_service_valid CHECK" in table_sql
        assert "PRIMARY KEY (service, artist_normalized, album_normalized)" in table_sql
        assert "DO $cache_check$" in widen_sql
        assert "album_streaming_url_cache_service_valid" in widen_sql

    async def test_widen_block_admits_the_full_service_set(self):
        # The widen-only DO block carries the shipped set as a PL/pgSQL array
        # (``code_services``); the merged IN-list is computed at runtime, so
        # this is the one place the code-side set is statically visible.
        import re

        from entity.streaming_url_cache import _SERVICES

        pg = _FakePgSource()

        await set_up_streaming_url_cache_schema(pg)

        widen_sql = pg.statements[-1]
        in_list = re.search(r"code_services\s+text\[\]\s*:=\s*ARRAY\[([^\]]*)\]", widen_sql)
        assert in_list is not None, "code_services array not found in the widen DO block"
        widen_services = set(re.findall(r"'([^']+)'", in_list.group(1)))
        assert widen_services == set(_SERVICES)
        assert "bandcamp" in widen_services

    async def test_bootstrap_is_idempotent(self):
        # Re-running the bootstrap issues the byte-identical DDL each time —
        # whether the widen block actually rewrites the constraint at runtime
        # is a PG-side decision the mock can't model (see the integration
        # layer), but the statements the bootstrap *issues* never change.
        pg = _FakePgSource()

        await set_up_streaming_url_cache_schema(pg)
        first = list(pg.statements)
        pg2 = _FakePgSource()
        await set_up_streaming_url_cache_schema(pg2)
        second = list(pg2.statements)

        assert first == second

    async def test_does_not_probe_or_backfill_the_old_apple_table(self):
        # Regression guard for #573 PR-2: the grandfathered
        # ``entity.album_apple_music_lookup_cache`` backfill is gone — the
        # bootstrap must not run any INSERT.
        pg = _FakePgSource()

        await set_up_streaming_url_cache_schema(pg)

        assert not any("INSERT" in sql for sql in pg.statements)
        assert not any("album_apple_music_lookup_cache" in sql for sql in pg.statements)

    async def test_runs_as_one_transaction_on_one_connection(self):
        # All-or-nothing: a mid-boot failure must never leave the table
        # standing without its service CHECK.
        pg = _FakePgSource()

        await set_up_streaming_url_cache_schema(pg)

        assert pg.acquire_count == 1
        assert pg.transaction_starts == 1
        assert pg.transaction_ends == 1
        assert pg.in_transaction is False
        assert all(in_txn for _, in_txn in pg.executed)

    async def test_preamble_bounds_lock_waits_and_serializes_boots(self):
        pg = _FakePgSource()

        await set_up_streaming_url_cache_schema(pg)

        assert pg.statements[0] == _BOOTSTRAP_LOCK_TIMEOUT
        assert pg.statements[1] == _BOOTSTRAP_ADVISORY_LOCK
