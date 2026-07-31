"""Integration (``@pytest.mark.pg``) tests for the V/A recall-index read path (LML#1022).

All unit coverage in ``tests/unit/test_compilation_track_location_read.py``
mocks ``PgSource``; this file drives ``get_compilation_track_locations``
against a real PostgreSQL connection, including the normalization boundary
(a differently-cased/punctuated query still hits the normalized rows) and the
multi-location fan-in the union depends on (the same track credited on
several shelf copies).

Run with: pytest -m pg -v tests/integration/test_compilation_track_location_read.py
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from entity.compilation_track_location import (
    CompilationTrackLocationRow,
    get_compilation_track_locations,
    set_up_compilation_track_location_schema,
)
from tests.integration.conftest import skip_if_named_tables_populated

_INSERT_SQL = """\
INSERT INTO lml_cache.compilation_track_location
    (library_id, track_position, track_artist, track_title, credit_role,
     discogs_release_id, artwork_url)
VALUES ($1, $2, $3, $4, $5, $6, $7)\
"""


@pytest_asyncio.fixture(autouse=True)
async def set_up_recall_index_schema(pg_pool, pg_source):
    """Reset just this table, then apply its DDL -- mirrors the #1019 schema-test fixture."""
    async with pg_pool.acquire() as conn:
        await skip_if_named_tables_populated(conn, (("lml_cache", "compilation_track_location"),))
        await conn.execute("DROP TABLE IF EXISTS lml_cache.compilation_track_location")
    await set_up_compilation_track_location_schema(pg_source)
    yield
    async with pg_pool.acquire() as conn:
        await conn.execute("DROP TABLE IF EXISTS lml_cache.compilation_track_location")


@pytest.mark.pg
class TestGetCompilationTrackLocationsPg:
    @pytest.mark.asyncio
    async def test_normalized_query_hits_normalized_rows(self, pg_source, pg_pool):
        async with pg_pool.acquire() as conn:
            await conn.execute(
                _INSERT_SQL,
                60654,
                "A3",
                "brian reitzell",
                "ikebana",
                "primary",
                12345,
                "https://example.com/lit.jpg",
            )

        # Differently-cased/punctuated than the stored normalized form.
        result = await get_compilation_track_locations(
            pg_source, track_artist="Brian Reitzell", track_title="Ikebana"
        )

        assert result == [
            CompilationTrackLocationRow(
                library_id=60654,
                track_position="A3",
                track_artist="brian reitzell",
                track_title="ikebana",
                credit_role="primary",
                discogs_release_id=12345,
                artwork_url="https://example.com/lit.jpg",
            )
        ]

    @pytest.mark.asyncio
    async def test_multiple_shelf_locations_all_returned(self, pg_source, pg_pool):
        async with pg_pool.acquire() as conn:
            await conn.execute(
                _INSERT_SQL, 60654, "A3", "squarepusher", "tommib", "primary", 12345, None
            )
            await conn.execute(
                _INSERT_SQL, 70001, "B2", "squarepusher", "tommib", "extra", 67890, None
            )

        result = await get_compilation_track_locations(
            pg_source, track_artist="Squarepusher", track_title="Tommib"
        )

        assert {row.library_id for row in result} == {60654, 70001}

    @pytest.mark.asyncio
    async def test_no_match_returns_empty_list(self, pg_source):
        result = await get_compilation_track_locations(
            pg_source, track_artist="Nobody", track_title="Nothing"
        )
        assert result == []
