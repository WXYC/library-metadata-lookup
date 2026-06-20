"""Unit tests for the streaming-URL cache schema bootstrap (LML#573).

Two surfaces:

* ``entity/streaming_url_cache.sql`` — the canonical-DDL reference file
  (mirrors ``entity/release_identity.sql``). It must describe the
  ``lml_cache.album_streaming_url_cache`` table with the named CHECK
  constraint and composite PK, so a reviewer / operator has the DDL inline.
* ``set_up_streaming_url_cache_schema`` — the lifespan bootstrap helper. It
  must issue ``CREATE SCHEMA`` then ``CREATE TABLE`` (named CHECK) and nothing
  else (the LML#571→#573 apple-table backfill was removed in #573's PR-2).

PG is mocked; the integration layer
(``tests/integration/test_streaming_url_persistent_lookup.py``) drives the
real DDL against PostgreSQL.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from entity.sources import PgSource
from entity.streaming_url_cache import set_up_streaming_url_cache_schema

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

    async def test_creates_schema_table_then_alters_check(self):
        pg = AsyncMock(spec=PgSource)
        pg.execute = AsyncMock(return_value="CREATE")

        await set_up_streaming_url_cache_schema(pg)

        # Exactly three executes — schema, table, then the idempotent CHECK
        # ALTER (so a pre-existing prod table picks up new service values that
        # CREATE TABLE IF NOT EXISTS cannot add). No backfill.
        assert pg.execute.await_count == 3
        schema_sql = pg.execute.await_args_list[0].args[0]
        table_sql = pg.execute.await_args_list[1].args[0]
        alter_sql = pg.execute.await_args_list[2].args[0]
        assert "CREATE SCHEMA IF NOT EXISTS lml_cache" in schema_sql
        assert "CREATE TABLE IF NOT EXISTS lml_cache.album_streaming_url_cache" in table_sql
        assert "CONSTRAINT album_streaming_url_cache_service_valid CHECK" in table_sql
        assert "PRIMARY KEY (service, artist_normalized, album_normalized)" in table_sql
        assert "ALTER TABLE lml_cache.album_streaming_url_cache" in alter_sql
        assert "DROP CONSTRAINT IF EXISTS album_streaming_url_cache_service_valid" in alter_sql
        assert "ADD CONSTRAINT album_streaming_url_cache_service_valid CHECK" in alter_sql

    async def test_alter_check_admits_the_full_service_set(self):
        # The ALTER's IN-list must carry every ``_SERVICES`` value (including
        # bandcamp) so an existing prod table gets the widened allowlist.
        import re

        from entity.streaming_url_cache import _SERVICES

        pg = AsyncMock(spec=PgSource)
        pg.execute = AsyncMock(return_value="ALTER")

        await set_up_streaming_url_cache_schema(pg)

        alter_sql = pg.execute.await_args_list[2].args[0]
        in_list = re.search(r"ADD CONSTRAINT.*?service\s+IN\s*\(([^)]*)\)", alter_sql, re.DOTALL)
        assert in_list is not None, "ADD CONSTRAINT IN-list not found in the ALTER"
        alter_services = set(re.findall(r"'([^']+)'", in_list.group(1)))
        assert alter_services == set(_SERVICES)
        assert "bandcamp" in alter_services

    async def test_bootstrap_is_idempotent(self):
        # Re-running the bootstrap issues the byte-identical DDL each time: the
        # DROP CONSTRAINT IF EXISTS + ADD CONSTRAINT pair is a safe no-op on a
        # table whose constraint already matches.
        pg = AsyncMock(spec=PgSource)
        pg.execute = AsyncMock(return_value="ALTER")

        await set_up_streaming_url_cache_schema(pg)
        first = [call.args[0] for call in pg.execute.await_args_list]
        pg.execute.reset_mock()
        await set_up_streaming_url_cache_schema(pg)
        second = [call.args[0] for call in pg.execute.await_args_list]

        assert first == second

    async def test_does_not_probe_or_backfill_the_old_apple_table(self):
        # Regression guard for #573 PR-2: the grandfathered
        # ``entity.album_apple_music_lookup_cache`` backfill is gone — the
        # bootstrap must not probe ``to_regclass`` or run any INSERT.
        pg = AsyncMock(spec=PgSource)
        pg.execute = AsyncMock(return_value="CREATE")

        await set_up_streaming_url_cache_schema(pg)

        pg.fetchone.assert_not_awaited()
        executed = [call.args[0] for call in pg.execute.await_args_list]
        assert not any("INSERT" in sql for sql in executed)
        assert not any("album_apple_music_lookup_cache" in sql for sql in executed)
