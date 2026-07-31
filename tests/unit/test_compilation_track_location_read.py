"""Unit tests for the V/A recall-index read helper (LML#1022).

``get_compilation_track_locations`` is the single-query probe the
orchestrator's concurrent multi-location union uses to answer "which library
shelf locations contain track *T* credited to artist *A*?" PG is mocked here;
the real query (and its index use) runs in the integration suite and in
LML#1019's own schema tests.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from entity.compilation_track_location import (
    CompilationTrackLocationRow,
    get_compilation_track_locations,
)
from entity.sources import PgSource


@pytest.mark.asyncio
class TestGetCompilationTrackLocations:
    async def test_returns_rows_for_normalized_exact_match(self):
        pg = AsyncMock(spec=PgSource)
        pg.fetchall = AsyncMock(
            return_value=[
                {
                    "library_id": 60654,
                    "track_position": "A3",
                    "track_artist": "brian reitzell",
                    "track_title": "ikebana",
                    "credit_role": "primary",
                    "discogs_release_id": 12345,
                    "artwork_url": "https://example.com/art.jpg",
                }
            ]
        )

        result = await get_compilation_track_locations(
            pg, track_artist="Brian Reitzell", track_title="Ikebana"
        )

        assert result == [
            CompilationTrackLocationRow(
                library_id=60654,
                track_position="A3",
                track_artist="brian reitzell",
                track_title="ikebana",
                credit_role="primary",
                discogs_release_id=12345,
                artwork_url="https://example.com/art.jpg",
            )
        ]
        # Single indexed probe -- no per-candidate fan-out.
        assert pg.fetchall.await_count == 1
        query, *params = pg.fetchall.await_args.args
        assert "compilation_track_location" in query
        assert "track_artist" in query
        assert "track_title" in query
        # The query args are normalized the same way the build script wrote them.
        assert params == ["brian reitzell", "ikebana"]

    async def test_empty_artist_or_title_short_circuits_without_query(self):
        pg = AsyncMock(spec=PgSource)
        pg.fetchall = AsyncMock(return_value=[])

        assert (
            await get_compilation_track_locations(pg, track_artist="", track_title="Ikebana") == []
        )
        assert (
            await get_compilation_track_locations(pg, track_artist="Squarepusher", track_title="")
            == []
        )
        pg.fetchall.assert_not_awaited()

    async def test_no_match_returns_empty_list(self):
        pg = AsyncMock(spec=PgSource)
        pg.fetchall = AsyncMock(return_value=[])

        result = await get_compilation_track_locations(
            pg, track_artist="Squarepusher", track_title="Tommib"
        )

        assert result == []

    async def test_pg_error_degrades_to_empty_list(self):
        # Best-effort posture (mirrors get_library_release_overrides): a PG
        # failure must not break /lookup -- it degrades to "no other locations"
        # rather than raising through the concurrent probe.
        pg = AsyncMock(spec=PgSource)
        pg.fetchall = AsyncMock(side_effect=RuntimeError("pool exhausted"))

        result = await get_compilation_track_locations(
            pg, track_artist="Squarepusher", track_title="Tommib"
        )

        assert result == []
