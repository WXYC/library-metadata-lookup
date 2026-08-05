"""Unit tests for the streaming-URL cache schema bootstrap (LML#573, LML#1038).

Two surfaces:

* ``entity/streaming_url_cache.sql`` — the canonical-DDL reference file
  (mirrors ``entity/release_identity.sql``). It must describe the
  ``lml_cache.album_streaming_url_cache`` table with the named CHECK
  constraint and composite PK, so a reviewer / operator has the DDL inline.
* ``set_up_streaming_url_cache_schema`` — the lifespan bootstrap helper. It
  must issue ``CREATE SCHEMA`` then ``CREATE TABLE`` (named CHECK), then the
  widen-only DO block (``entity.ddl.widen_service_check`` — LML#1038
  generalized this table onto the same LML#890 generation
  ``entity/streaming_catalog.py`` pioneered, retiring this module's own
  ~33-line LML#886 port), all inside one transaction on one connection behind
  a ``lock_timeout`` + advisory-lock preamble. No row mutation (the
  LML#571→#573 apple-table backfill was removed in #573's PR-2).

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

from entity.streaming_url_cache import set_up_streaming_url_cache_schema


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

    def test_has_error_column_alter(self):
        # LML#1121: the additive upgrade for a table that predates the
        # `is_error` column, mirroring LML#824's `crowd_out` ALTER.
        ddl = _SQL_REFERENCE.read_text()
        assert "ADD COLUMN IF NOT EXISTS is_error BOOLEAN NOT NULL DEFAULT false" in ddl


class TestStreamingServiceParity:
    """LML#1037 hard constraint: the enum-derived storage keys must equal
    ``_SERVICES`` -- the CHECK-list parity proof for the code-side allowlist.
    The LIVE deployed-constraint counterpart (against a real PostgreSQL
    connection) is
    ``tests/integration/test_streaming_url_persistent_lookup.py::
    TestStreamingServiceCheckParity``.
    """

    def test_services_equals_enum_album_cache_keys(self):
        from entity.streaming_url_cache import _SERVICES
        from streaming.service import StreamingService

        assert set(_SERVICES) == {
            s.album_cache_key for s in StreamingService if s.album_cache_key is not None
        }

    def test_services_is_exactly_apple_spotify_bandcamp(self):
        from entity.streaming_url_cache import _SERVICES

        assert set(_SERVICES) == {"apple_music_album", "spotify_album", "bandcamp"}


@pytest.mark.asyncio
class TestSetUpStreamingUrlCacheSchema:
    """``set_up_streaming_url_cache_schema`` runs exactly the idempotent DDL."""

    async def test_creates_schema_table_then_widens_check(self):
        pg = _FakePgSource()

        await set_up_streaming_url_cache_schema(pg)

        # One-statement lock_timeout preamble (LML#1038 PR-2 dropped the
        # inner advisory lock -- see the module docstring), then schema,
        # table, the LML#1121 additive `is_error` column ALTER, then the
        # widen-only DO block (so a pre-existing prod table picks up new
        # service values that CREATE TABLE IF NOT EXISTS cannot add). No
        # backfill.
        ddl = pg.statements[1:]
        assert len(ddl) == 4
        schema_sql, table_sql, error_column_sql, widen_sql = ddl
        assert "CREATE SCHEMA IF NOT EXISTS lml_cache" in schema_sql
        assert "CREATE TABLE IF NOT EXISTS lml_cache.album_streaming_url_cache" in table_sql
        assert "CONSTRAINT album_streaming_url_cache_service_valid CHECK" in table_sql
        assert "PRIMARY KEY (service, artist_normalized, album_normalized)" in table_sql
        # LML#1121: upgrades a table created before `is_error` existed.
        assert "ALTER TABLE lml_cache.album_streaming_url_cache" in error_column_sql
        assert (
            "ADD COLUMN IF NOT EXISTS is_error BOOLEAN NOT NULL DEFAULT false" in error_column_sql
        )
        # LML#1038: the widen block now comes from the shared
        # entity.ddl.build_widen_service_check_sql -- same widen-only shape,
        # generic dollar-quote tag (not this table's own).
        assert widen_sql.startswith("DO $")
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

    async def test_preamble_bounds_lock_waits(self):
        pg = _FakePgSource()

        await set_up_streaming_url_cache_schema(pg)

        assert pg.statements[0] == "SET LOCAL lock_timeout = '10s'"

    async def test_no_inner_advisory_lock(self):
        # LML#1038 PR-2: every caller of this bootstrap goes through
        # main.py's lifespan, which already wraps every bootstrap call in
        # one session-scoped advisory lock -- an inner xact lock here (the
        # pre-PR-2 posture, key 886001) serialized nothing an outer caller
        # wasn't already serializing. See the module docstring.
        pg = _FakePgSource()

        await set_up_streaming_url_cache_schema(pg)

        assert not any("pg_advisory" in sql for sql in pg.statements)
