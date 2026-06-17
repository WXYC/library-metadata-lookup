"""Unit tests for the streaming-URL cache schema bootstrap (LML#573).

Two surfaces:

* ``entity/streaming_url_cache.sql`` — the canonical-DDL reference file
  (mirrors ``entity/release_identity.sql``). It must describe the
  ``lml_cache.album_streaming_url_cache`` table with the named CHECK
  constraint and composite PK, so a reviewer / operator has the DDL inline.
* ``set_up_streaming_url_cache_schema`` — the lifespan bootstrap helper. It
  must issue ``CREATE SCHEMA``, ``CREATE TABLE`` (named CHECK), and the
  ``to_regclass``-guarded cross-schema backfill that preserves each
  backfilled row's ``last_checked_at`` (the TTL-preservation AC).

PG is mocked; the integration layer
(``tests/integration/test_streaming_url_persistent_lookup.py``) drives the
real DDL + backfill against PostgreSQL.
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

    def test_has_composite_primary_key(self):
        ddl = _SQL_REFERENCE.read_text()
        assert "PRIMARY KEY (service, artist_normalized, album_normalized)" in ddl


@pytest.mark.asyncio
class TestSetUpStreamingUrlCacheSchema:
    """``set_up_streaming_url_cache_schema`` runs idempotent DDL + guarded backfill."""

    async def test_creates_schema_then_table(self):
        pg = AsyncMock(spec=PgSource)
        pg.execute = AsyncMock(return_value="CREATE")
        # No grandfathered table → backfill is a no-op.
        pg.fetchone = AsyncMock(return_value={"source_exists": False})

        await set_up_streaming_url_cache_schema(pg)

        # First two executes: schema, then table.
        schema_sql = pg.execute.await_args_list[0].args[0]
        table_sql = pg.execute.await_args_list[1].args[0]
        assert "CREATE SCHEMA IF NOT EXISTS lml_cache" in schema_sql
        assert "CREATE TABLE IF NOT EXISTS lml_cache.album_streaming_url_cache" in table_sql
        assert "CONSTRAINT album_streaming_url_cache_service_valid CHECK" in table_sql
        assert "PRIMARY KEY (service, artist_normalized, album_normalized)" in table_sql

    async def test_skips_backfill_when_old_table_absent(self):
        pg = AsyncMock(spec=PgSource)
        pg.execute = AsyncMock(return_value="CREATE")
        pg.fetchone = AsyncMock(return_value={"source_exists": False})

        await set_up_streaming_url_cache_schema(pg)

        # Exactly two executes (schema + table); no backfill INSERT.
        assert pg.execute.await_count == 2
        # The guard probed to_regclass first.
        probe_sql = pg.fetchone.await_args.args[0]
        assert "to_regclass('entity.album_apple_music_lookup_cache')" in probe_sql

    async def test_runs_backfill_when_old_table_present(self):
        pg = AsyncMock(spec=PgSource)
        pg.execute = AsyncMock(return_value="CREATE")
        pg.fetchone = AsyncMock(return_value={"source_exists": True})

        await set_up_streaming_url_cache_schema(pg)

        # Schema, table, then the backfill INSERT.
        assert pg.execute.await_count == 3
        backfill_sql = pg.execute.await_args_list[2].args[0]
        assert "INSERT INTO lml_cache.album_streaming_url_cache" in backfill_sql
        assert "FROM entity.album_apple_music_lookup_cache" in backfill_sql
        assert "'apple_music_album'" in backfill_sql
        assert (
            "ON CONFLICT (service, artist_normalized, album_normalized) DO NOTHING" in backfill_sql
        )
        # TTL-preservation AC: last_checked_at is projected EXPLICITLY in the
        # SELECT, not left to the column default (which would extend every
        # backfilled known-miss row's TTL by up to 7 days).
        assert "last_checked_at" in backfill_sql
        assert "now()" not in backfill_sql, (
            "backfill must project the source row's last_checked_at, not now()"
        )
