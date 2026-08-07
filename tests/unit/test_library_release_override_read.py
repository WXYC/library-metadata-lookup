"""Unit tests for the library-release override PG read helper (LML#850).

``get_library_release_overrides`` is the single-query prefetch the orchestrator
uses to map a request's library ids to their verified Discogs release ids. PG
is mocked here; the real query runs in the integration suite.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from asyncpg.exceptions import PostgresError

from entity.library_release_override import (
    get_library_release_overrides,
    get_library_release_overrides_strict,
)
from entity.sources import PgSource


@pytest.mark.asyncio
class TestGetLibraryReleaseOverrides:
    async def test_returns_id_to_release_map(self):
        pg = AsyncMock(spec=PgSource)
        pg.fetchall = AsyncMock(
            return_value=[
                {"library_id": 42, "discogs_release_id": 1000},
                {"library_id": 77, "discogs_release_id": 2000},
            ]
        )

        result = await get_library_release_overrides(pg, [42, 77, 99])

        assert result == {42: 1000, 77: 2000}
        # Single query, keyed by the id list (no per-item fan-out).
        assert pg.fetchall.await_count == 1
        query = pg.fetchall.await_args.args[0]
        assert "library_release_override" in query
        assert "ANY(" in query

    async def test_empty_id_list_short_circuits_without_query(self):
        pg = AsyncMock(spec=PgSource)
        pg.fetchall = AsyncMock(return_value=[])

        result = await get_library_release_overrides(pg, [])

        assert result == {}
        pg.fetchall.assert_not_awaited()

    async def test_pg_error_degrades_to_empty_map(self):
        # Best-effort posture (mirrors the streaming / resolution caches): a PG
        # failure must not break the lookup path — it degrades to "no overrides"
        # and the fuzzy pick runs as before.
        pg = AsyncMock(spec=PgSource)
        pg.fetchall = AsyncMock(side_effect=RuntimeError("pool exhausted"))

        result = await get_library_release_overrides(pg, [42])

        assert result == {}


@pytest.mark.asyncio
class TestGetLibraryReleaseOverridesStrict:
    """LML#1138: the bulk-resolve arm needs the SAME single-query prefetch
    with the OPPOSITE failure posture — PG errors propagate so the router
    can degrade to the unvisited `(False, [])` state AND emit the
    `single_artist_track_read_fail_open` counter. The swallowing reader
    above would silently map an outage to "no pins", indistinguishable
    from the routine un-pinned case, blinding the counter."""

    async def test_returns_id_to_release_map(self):
        pg = AsyncMock(spec=PgSource)
        pg.fetchall = AsyncMock(
            return_value=[
                {"library_id": 42, "discogs_release_id": 1000},
                {"library_id": 77, "discogs_release_id": 2000},
            ]
        )

        result = await get_library_release_overrides_strict(pg, [42, 77, 99])

        assert result == {42: 1000, 77: 2000}
        assert pg.fetchall.await_count == 1
        query = pg.fetchall.await_args.args[0]
        assert "library_release_override" in query
        assert "ANY(" in query

    async def test_empty_id_list_short_circuits_without_query(self):
        pg = AsyncMock(spec=PgSource)
        pg.fetchall = AsyncMock(return_value=[])

        result = await get_library_release_overrides_strict(pg, [])

        assert result == {}
        pg.fetchall.assert_not_awaited()

    async def test_pg_error_propagates(self):
        pg = AsyncMock(spec=PgSource)
        pg.fetchall = AsyncMock(side_effect=PostgresError("pool exhausted"))

        with pytest.raises(PostgresError):
            await get_library_release_overrides_strict(pg, [42])
