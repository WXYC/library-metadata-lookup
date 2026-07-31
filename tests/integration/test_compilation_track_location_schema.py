"""Integration (``@pytest.mark.pg``) tests for the V/A recall index schema (LML#1019).

All unit coverage mocks ``PgSource``; this file drives the real DDL -- the
real composite primary key, the real ``credit_role`` / ``discogs_release_id``
CHECKs, and the real reverse index -- against an actual PostgreSQL
connection. Rows are inserted with raw SQL here (the build script -- and its
own round-trip integration coverage -- lives in a separate test module).

The EXPLAIN assertion is this ticket's headline acceptance criterion: the
reverse ``(track_artist, track_title)`` btree must actually be usable by the
planner, not just present in ``pg_indexes``.

Run with: pytest -m pg -v tests/integration/test_compilation_track_location_schema.py
"""

from __future__ import annotations

import asyncpg
import pytest
import pytest_asyncio

from entity.compilation_track_location import set_up_compilation_track_location_schema
from tests.integration.conftest import skip_if_named_tables_populated

_INSERT_SQL = """\
INSERT INTO lml_cache.compilation_track_location
    (library_id, track_position, track_artist, track_title, credit_role, discogs_release_id)
VALUES ($1, $2, $3, $4, $5, $6)\
"""


@pytest_asyncio.fixture(autouse=True)
async def set_up_recall_index_schema(pg_pool, pg_source):
    """Reset just this table (not the whole ``lml_cache`` schema), then apply its DDL.

    Mirrors ``test_library_release_override.py``'s fixture: the populated-table
    veto runs FIRST so a mispointed ``DATABASE_URL_TEST`` at a real
    discogs-cache can't drop collected data.
    """
    async with pg_pool.acquire() as conn:
        await skip_if_named_tables_populated(conn, (("lml_cache", "compilation_track_location"),))
        await conn.execute("DROP TABLE IF EXISTS lml_cache.compilation_track_location")
    await set_up_compilation_track_location_schema(pg_source)
    yield
    async with pg_pool.acquire() as conn:
        await conn.execute("DROP TABLE IF EXISTS lml_cache.compilation_track_location")


@pytest.mark.pg
class TestSchemaBootstrap:
    @pytest.mark.asyncio
    async def test_second_boot_is_a_no_op(self, pg_source, pg_pool):
        await set_up_compilation_track_location_schema(pg_source)
        async with pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT count(*)::int AS n FROM information_schema.tables "
                "WHERE table_schema = 'lml_cache' "
                "AND table_name = 'compilation_track_location'"
            )
        assert row["n"] == 1

    @pytest.mark.asyncio
    async def test_reverse_index_exists(self, pg_pool):
        async with pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT indexdef FROM pg_indexes "
                "WHERE schemaname = 'lml_cache' "
                "AND indexname = 'idx_compilation_track_location_reverse'"
            )
        assert row is not None
        assert "track_artist" in row["indexdef"]
        assert "track_title" in row["indexdef"]

    @pytest.mark.asyncio
    async def test_credit_role_check_rejects_unknown_tier(self, pg_pool):
        with pytest.raises(asyncpg.PostgresError):
            async with pg_pool.acquire() as conn:
                await conn.execute(_INSERT_SQL, 1, "A1", "the bug", "pressure", "remix", 12345)

    @pytest.mark.asyncio
    async def test_release_id_check_rejects_non_positive(self, pg_pool):
        with pytest.raises(asyncpg.PostgresError):
            async with pg_pool.acquire() as conn:
                await conn.execute(_INSERT_SQL, 1, "A1", "the bug", "pressure", "primary", 0)

    @pytest.mark.asyncio
    async def test_primary_key_rejects_duplicate(self, pg_pool):
        async with pg_pool.acquire() as conn:
            await conn.execute(_INSERT_SQL, 1, "A1", "the bug", "pressure", "primary", 12345)
            with pytest.raises(asyncpg.PostgresError):
                await conn.execute(_INSERT_SQL, 1, "A1", "the bug", "pressure", "featured", 99999)

    @pytest.mark.asyncio
    async def test_distinct_track_artist_on_same_position_is_allowed(self, pg_pool):
        # The composite PK is (library_id, track_position, track_artist) --
        # two credited artists on the same track_position (e.g. a primary
        # plus a featured artist) are two distinct rows, not a conflict.
        async with pg_pool.acquire() as conn:
            await conn.execute(_INSERT_SQL, 1, "A1", "the bug", "pressure", "primary", 12345)
            await conn.execute(_INSERT_SQL, 1, "A1", "flowdan", "pressure", "featured", 12345)
            rows = await conn.fetch(
                "SELECT track_artist FROM lml_cache.compilation_track_location "
                "WHERE library_id = 1 AND track_position = 'A1' ORDER BY track_artist"
            )
        assert [r["track_artist"] for r in rows] == ["flowdan", "the bug"]


@pytest.mark.pg
class TestReverseIndexUse:
    @pytest.mark.asyncio
    async def test_explain_uses_reverse_index(self, pg_pool):
        """The planner must be ABLE to use the reverse index for an exact-match probe.

        ``enable_seqscan = off`` (scoped to this connection's session) removes
        the small-table cost-estimate noise that would otherwise make Postgres
        prefer a sequential scan over a handful of rows regardless of index
        presence -- the deterministic way to prove the index is usable, which
        is the acceptance criterion (LML#1022 is the runtime consumer of this
        query shape; this ticket only guarantees the index supports it).
        """
        async with pg_pool.acquire() as conn:
            for i in range(20):
                await conn.execute(
                    _INSERT_SQL,
                    i,
                    "A1",
                    f"artist {i}",
                    f"title {i}",
                    "primary",
                    10000 + i,
                )
            await conn.execute("SET enable_seqscan = off")
            try:
                plan_rows = await conn.fetch(
                    "EXPLAIN SELECT * FROM lml_cache.compilation_track_location "
                    "WHERE track_artist = $1 AND track_title = $2",
                    "artist 5",
                    "title 5",
                )
            finally:
                await conn.execute("RESET enable_seqscan")
        plan = "\n".join(row["QUERY PLAN"] for row in plan_rows)
        assert "idx_compilation_track_location_reverse" in plan
        assert "Seq Scan" not in plan
