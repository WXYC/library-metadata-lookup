"""Unit tests for `DiscogsCacheService.get_artist_details_bulk`.

Batched form of the per-id `get_artist_details` PG read. Phase 2 of the
artist-search-alias plan PR 2 composer.

Contract:

- 4 PG round-trips per call (one per table consulted: ``artist``,
  ``artist_alias``, ``artist_name_variation``, ``artist_member``),
  each ``WHERE artist_id = ANY($1::int[])``. ``artist_url`` is NOT
  queried — the composer doesn't read ``details.urls``.
- Returns ``dict[int, ArtistDetails]`` keyed by discogs artist id.
- Cache-miss artist ids are absent from the returned dict (never
  None-valued).
- Empty input → empty dict, zero PG round-trips.
- PG-only — no Discogs API escalation, no rate-limit semaphore acquire.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from discogs.cache_service import DiscogsCacheService


@pytest.fixture
def cache_service(mock_asyncpg_pool):
    return DiscogsCacheService(mock_asyncpg_pool)


def _make_table_router(**table_results):
    """Route ``pool.fetch`` calls by table name in the query.

    Same idea as ``tests/unit/test_cache_service.py:make_fetch_router`` but
    matches table names in WHERE clauses with bulk-binding shapes
    (``artist_id = ANY(...)``). Longest table name wins so
    ``artist_name_variation`` beats ``artist``.
    """
    sorted_tables = sorted(table_results.keys(), key=len, reverse=True)

    async def route(query, *args):
        for table_name in sorted_tables:
            if table_name in query:
                return table_results[table_name]
        return []

    return route


class TestGetArtistDetailsBulk:
    @pytest.mark.asyncio
    async def test_empty_input_returns_empty_dict_without_querying(
        self, cache_service, mock_asyncpg_pool
    ):
        """No ids → zero PG round-trips."""
        result = await cache_service.get_artist_details_bulk([])
        assert result == {}
        assert mock_asyncpg_pool.fetch.await_count == 0

    @pytest.mark.asyncio
    async def test_returns_dict_keyed_by_artist_id(self, cache_service, mock_asyncpg_pool):
        """Two ids in → dict with two ArtistDetails values keyed by id."""
        mock_asyncpg_pool.fetch = AsyncMock(
            side_effect=_make_table_router(
                **{
                    "FROM artist ": [
                        {"id": 2154, "name": "Stereolab", "profile": None, "image_url": None},
                        {"id": 305253, "name": "Juana Molina", "profile": None, "image_url": None},
                    ],
                    "artist_alias": [
                        {"artist_id": 2154, "alias_id": 500, "alias_name": "Stereolab Variant"},
                    ],
                    "artist_name_variation": [
                        {"artist_id": 305253, "name": "J. Molina"},
                    ],
                    "artist_member": [],
                    "artist_url": [],
                }
            )
        )

        result = await cache_service.get_artist_details_bulk([2154, 305253])

        assert set(result.keys()) == {2154, 305253}
        assert result[2154].name == "Stereolab"
        assert result[305253].name == "Juana Molina"
        # Child rows are routed to the right parent.
        assert len(result[2154].aliases) == 1
        assert result[2154].aliases[0].name == "Stereolab Variant"
        assert result[2154].name_variations == []
        assert result[305253].aliases == []
        assert result[305253].name_variations == ["J. Molina"]

    @pytest.mark.asyncio
    async def test_cache_miss_ids_absent_from_dict(self, cache_service, mock_asyncpg_pool):
        """Ids not present in ``artist`` table are simply missing from the result."""
        mock_asyncpg_pool.fetch = AsyncMock(
            side_effect=_make_table_router(
                **{
                    "FROM artist ": [
                        {"id": 2154, "name": "Stereolab", "profile": None, "image_url": None},
                    ],
                    "artist_alias": [],
                    "artist_name_variation": [],
                    "artist_member": [],
                    "artist_url": [],
                }
            )
        )
        result = await cache_service.get_artist_details_bulk([2154, 99999])
        assert 2154 in result
        assert 99999 not in result

    @pytest.mark.asyncio
    async def test_exactly_four_pg_round_trips(self, cache_service, mock_asyncpg_pool):
        """The bulk read is 4 PG calls regardless of input cardinality.

        Pins the cost contract: a 1000-id batch is 4 round-trips, not
        5000. `artist_url` is intentionally NOT queried — the artist-
        search-alias composer never reads `details.urls`, so the bulk
        method skips that fetch.
        """
        mock_asyncpg_pool.fetch = AsyncMock(return_value=[])
        await cache_service.get_artist_details_bulk(list(range(100)))
        assert mock_asyncpg_pool.fetch.await_count == 4

    @pytest.mark.asyncio
    async def test_artist_url_table_not_queried(self, cache_service, mock_asyncpg_pool):
        """artist_url is not in the gather; bulk variant skips it deliberately."""
        mock_asyncpg_pool.fetch = AsyncMock(return_value=[])
        await cache_service.get_artist_details_bulk([2154])
        all_sql = [call[0][0] for call in mock_asyncpg_pool.fetch.await_args_list]
        # No fetch over artist_url.
        assert not any("FROM artist_url" in sql for sql in all_sql)

    @pytest.mark.asyncio
    async def test_input_order_does_not_affect_output_contents(
        self, cache_service, mock_asyncpg_pool
    ):
        """Same ids in different orders → same dict (set equality + identical values)."""
        artist_rows = [
            {"id": 2154, "name": "Stereolab", "profile": None, "image_url": None},
            {"id": 305253, "name": "Juana Molina", "profile": None, "image_url": None},
        ]
        mock_asyncpg_pool.fetch = AsyncMock(
            side_effect=_make_table_router(
                **{
                    "FROM artist ": artist_rows,
                    "artist_alias": [],
                    "artist_name_variation": [],
                    "artist_member": [],
                    "artist_url": [],
                }
            )
        )
        result_a = await cache_service.get_artist_details_bulk([2154, 305253])

        mock_asyncpg_pool.fetch = AsyncMock(
            side_effect=_make_table_router(
                **{
                    "FROM artist ": artist_rows,
                    "artist_alias": [],
                    "artist_name_variation": [],
                    "artist_member": [],
                    "artist_url": [],
                }
            )
        )
        result_b = await cache_service.get_artist_details_bulk([305253, 2154])

        assert set(result_a.keys()) == set(result_b.keys())
        for artist_id in result_a:
            assert result_a[artist_id].name == result_b[artist_id].name

    @pytest.mark.asyncio
    async def test_queries_filter_by_artist_id_any(self, cache_service, mock_asyncpg_pool):
        """All four child-table queries bind the list via ``ANY($1)``.

        The cost-contract test pins the call count; this one pins the
        binding shape. If we ever regress to a per-id loop, the binding
        becomes scalar and this catches it.
        """
        mock_asyncpg_pool.fetch = AsyncMock(return_value=[])
        await cache_service.get_artist_details_bulk([2154, 305253])

        # Every fetch call bound exactly one positional arg: the list.
        for call in mock_asyncpg_pool.fetch.await_args_list:
            # call.args = (sql, list_arg)
            assert "ANY(" in call[0][0]
            bound = call[0][1]
            assert isinstance(bound, list)
            assert set(bound) == {2154, 305253}
