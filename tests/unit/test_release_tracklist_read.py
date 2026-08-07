"""Unit tests for the discogs-cache tracklist batched read (LML#1138).

``get_release_tracklists_for_releases`` is the single-query fetch behind the
`kind: single_artist` derived-`tracks[]` arm of bulk-resolve: ONE round-trip
for a whole batch's pinned Discogs release ids, grouped in memory by the
caller. PG is mocked here; the real query runs in the ``pg``-marked
integration suite (`tests/integration/test_bulk_resolve_libraries.py`).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from asyncpg.exceptions import PostgresError

from entity.release_tracklist import ReleaseTracklistRow, get_release_tracklists_for_releases
from entity.sources import PgSource


@pytest.mark.asyncio
class TestGetReleaseTracklistsForReleases:
    async def test_returns_rows_in_one_batched_query(self):
        pg = AsyncMock(spec=PgSource)
        pg.fetchall = AsyncMock(
            return_value=[
                {
                    "release_id": 555,
                    "sequence": 1,
                    "position": "A1",
                    "title": "Metronomic Underground",
                    "credit_artist_name": None,
                },
                {
                    "release_id": 555,
                    "sequence": 2,
                    "position": "A2",
                    "title": "Cybele's Reverie",
                    "credit_artist_name": "Lætitia Sadier",
                },
            ]
        )

        rows = await get_release_tracklists_for_releases(pg, [555, 777])

        assert rows == [
            ReleaseTracklistRow(
                release_id=555,
                sequence=1,
                position="A1",
                title="Metronomic Underground",
                credit_artist_name=None,
            ),
            ReleaseTracklistRow(
                release_id=555,
                sequence=2,
                position="A2",
                title="Cybele's Reverie",
                credit_artist_name="Lætitia Sadier",
            ),
        ]
        # Single query, keyed by the id array (no per-release fan-out) —
        # bulk-resolve batches up to 1,000 inputs and a per-release query
        # would be exactly the N+1 the ticket forbids.
        assert pg.fetchall.await_count == 1
        query = pg.fetchall.await_args.args[0]
        assert "release_track" in query
        assert "ANY(" in query
        # Main-artist credits only: the extra=0 constraint mirrors the
        # SONG_AS_TRACK read path (see discogs/cache_service.py) — extra=1
        # writer/remixer credits are not per-track performer credits.
        assert "extra = 0" in query

    async def test_empty_release_ids_short_circuits_without_query(self):
        pg = AsyncMock(spec=PgSource)
        pg.fetchall = AsyncMock(return_value=[])

        rows = await get_release_tracklists_for_releases(pg, [])

        assert rows == []
        pg.fetchall.assert_not_awaited()

    async def test_pg_error_propagates(self):
        """Unlike the swallowing lml_cache readers, PG failures PROPAGATE:
        the bulk-resolve router owns the degrade decision (unvisited state +
        the `single_artist_track_read_fail_open` counter) and cannot make it
        if the failure is silently mapped to an empty tracklist — which
        would read as a genuine `(True, [])` attempt on a pinned release."""
        pg = AsyncMock(spec=PgSource)
        pg.fetchall = AsyncMock(side_effect=PostgresError("connection reset"))

        with pytest.raises(PostgresError):
            await get_release_tracklists_for_releases(pg, [555])
