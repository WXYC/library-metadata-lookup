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
        # PR-1 ships exactly Apple + Spotify in the constraint.
        assert "'apple_music_album'" in ddl
        assert "'spotify_album'" in ddl

    def test_check_constraint_allowlist_matches_pr1_services(self):
        # The .sql is a hand-maintained mirror of the runtime DDL, which
        # generates its IN-list from ``_PR1_SERVICES``. A substring check would
        # stay green if PR-3 edited ``_PR1_SERVICES`` but forgot the .sql; parse
        # the IN-list and assert exact-set equality so the mirror can't drift.
        import re

        from entity.streaming_url_cache import _PR1_SERVICES

        ddl = _SQL_REFERENCE.read_text()
        match = re.search(r"service\s+IN\s*\(([^)]*)\)", ddl)
        assert match is not None, "CHECK constraint IN-list not found in the .sql reference"
        sql_services = set(re.findall(r"'([^']+)'", match.group(1)))
        assert sql_services == set(_PR1_SERVICES), (
            f".sql CHECK allowlist {sql_services} drifted from _PR1_SERVICES "
            f"{set(_PR1_SERVICES)} — update entity/streaming_url_cache.sql"
        )

    def test_has_composite_primary_key(self):
        ddl = _SQL_REFERENCE.read_text()
        assert "PRIMARY KEY (service, artist_normalized, album_normalized)" in ddl


@pytest.mark.asyncio
class TestSetUpStreamingUrlCacheSchema:
    """``set_up_streaming_url_cache_schema`` runs exactly the idempotent DDL."""

    async def test_creates_schema_then_table(self):
        pg = AsyncMock(spec=PgSource)
        pg.execute = AsyncMock(return_value="CREATE")

        await set_up_streaming_url_cache_schema(pg)

        # Exactly two executes — schema, then table. No backfill.
        assert pg.execute.await_count == 2
        schema_sql = pg.execute.await_args_list[0].args[0]
        table_sql = pg.execute.await_args_list[1].args[0]
        assert "CREATE SCHEMA IF NOT EXISTS lml_cache" in schema_sql
        assert "CREATE TABLE IF NOT EXISTS lml_cache.album_streaming_url_cache" in table_sql
        assert "CONSTRAINT album_streaming_url_cache_service_valid CHECK" in table_sql
        assert "PRIMARY KEY (service, artist_normalized, album_normalized)" in table_sql

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
