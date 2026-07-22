"""Unit tests for the track-scoped streaming-URL cache schema (LML#893, L1).

Two surfaces, mirroring ``test_streaming_url_cache_schema.py``:

* ``entity/track_streaming_url_cache.sql`` — the canonical-DDL reference file.
  It must describe the ``lml_cache.track_streaming_url_cache`` table with the
  named CHECK constraint and the composite PK carrying the song axis.
* ``set_up_track_streaming_url_cache_schema`` — the lifespan bootstrap helper.
  It must issue ``CREATE SCHEMA`` then ``CREATE TABLE`` (named CHECK) then the
  idempotent ALTER, and nothing else.

PG is mocked; ``tests/integration/test_track_streaming_url_cache_pg.py`` drives
the real DDL against PostgreSQL.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from entity.sources import PgSource
from entity.track_streaming_url_cache import (
    _SERVICES,
    set_up_track_streaming_url_cache_schema,
)

_SQL_REFERENCE = (
    Path(__file__).resolve().parent.parent.parent / "entity" / "track_streaming_url_cache.sql"
)


class TestCanonicalDDLReference:
    def test_reference_file_exists(self):
        assert _SQL_REFERENCE.is_file()

    def test_targets_lml_cache_schema_and_track_table(self):
        ddl = _SQL_REFERENCE.read_text()
        assert "CREATE SCHEMA IF NOT EXISTS lml_cache" in ddl
        assert "lml_cache.track_streaming_url_cache" in ddl

    def test_has_named_check_constraint_with_track_service(self):
        ddl = _SQL_REFERENCE.read_text()
        assert "CONSTRAINT track_streaming_url_cache_service_valid CHECK" in ddl
        assert "'apple_music_track'" in ddl

    def test_check_constraint_allowlist_matches_services(self):
        ddl = _SQL_REFERENCE.read_text()
        match = re.search(r"service\s+IN\s*\(([^)]*)\)", ddl)
        assert match is not None, "CHECK constraint IN-list not found in the .sql reference"
        sql_services = set(re.findall(r"'([^']+)'", match.group(1)))
        assert sql_services == set(_SERVICES)

    def test_has_composite_primary_key_with_song_axis(self):
        ddl = _SQL_REFERENCE.read_text()
        assert "PRIMARY KEY (service, artist_normalized, album_normalized, song_normalized)" in ddl


@pytest.mark.asyncio
class TestSetUpTrackStreamingUrlCacheSchema:
    async def test_creates_schema_table_then_alters_check(self):
        pg = AsyncMock(spec=PgSource)
        pg.execute = AsyncMock(return_value="CREATE")

        await set_up_track_streaming_url_cache_schema(pg)

        assert pg.execute.await_count == 3
        schema_sql = pg.execute.await_args_list[0].args[0]
        table_sql = pg.execute.await_args_list[1].args[0]
        alter_sql = pg.execute.await_args_list[2].args[0]
        assert "CREATE SCHEMA IF NOT EXISTS lml_cache" in schema_sql
        assert "CREATE TABLE IF NOT EXISTS lml_cache.track_streaming_url_cache" in table_sql
        assert "CONSTRAINT track_streaming_url_cache_service_valid CHECK" in table_sql
        assert (
            "PRIMARY KEY (service, artist_normalized, album_normalized, song_normalized)"
            in table_sql
        )
        assert "ALTER TABLE lml_cache.track_streaming_url_cache" in alter_sql
        assert "DROP CONSTRAINT IF EXISTS track_streaming_url_cache_service_valid" in alter_sql
        assert "ADD CONSTRAINT track_streaming_url_cache_service_valid CHECK" in alter_sql

    async def test_bootstrap_is_idempotent(self):
        pg = AsyncMock(spec=PgSource)
        pg.execute = AsyncMock(return_value="ALTER")

        await set_up_track_streaming_url_cache_schema(pg)
        first = [call.args[0] for call in pg.execute.await_args_list]
        pg.execute.reset_mock()
        await set_up_track_streaming_url_cache_schema(pg)
        second = [call.args[0] for call in pg.execute.await_args_list]

        assert first == second

    async def test_does_not_touch_the_album_cache_table(self):
        # Regression guard (PR #898): L1's own schema bootstrap must not touch
        # the album-keyed cache table at all.
        pg = AsyncMock(spec=PgSource)
        pg.execute = AsyncMock(return_value="CREATE")

        await set_up_track_streaming_url_cache_schema(pg)

        executed = [call.args[0] for call in pg.execute.await_args_list]
        assert not any("album_streaming_url_cache" in sql for sql in executed)
