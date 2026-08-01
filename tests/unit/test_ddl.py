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
from unittest.mock import AsyncMock

import pytest

from entity.ddl import LML_CACHE_SCHEMA_DDL, build_widen_service_check_sql, widen_service_check


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
