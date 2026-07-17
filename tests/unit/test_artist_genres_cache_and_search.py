"""Unit tests for the two I/O primitives behind the bulk artist-genres
endpoint (LML#781):

- ``DiscogsCacheService.aggregate_artist_genre_style`` — the discogs-cache
  frequency aggregation (mocked asyncpg pool: asserts the SQL keys on
  ``artist_id`` vs. ``artist_name`` and returns ``{name: count}`` dicts).
- ``DiscogsService.search_release_ids_by_artist`` — the cache-miss fallback
  primitive (mocked ``_request_with_retry``: asserts the ``type=release``
  search params, ID extraction/dedup/cap, and the list-vs-None contract).

The real-SQL correctness of the aggregation lives in the ``pg``-marked
integration test ``tests/integration/test_artist_genre_aggregation_pg.py``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from discogs.breaker import DiscogsBreakerOpenError
from discogs.cache_service import CacheUnavailableError, DiscogsCacheService
from discogs.service import DiscogsService

# --------------------------------------------------------------------------
# DiscogsCacheService.aggregate_artist_genre_style
# --------------------------------------------------------------------------


@pytest.fixture
def cache_service(mock_asyncpg_pool, mock_settings):
    return DiscogsCacheService(mock_asyncpg_pool)


class TestAggregateArtistGenreStyle:
    @pytest.mark.asyncio
    async def test_keys_on_artist_id_when_provided(self, cache_service, mock_asyncpg_pool):
        mock_asyncpg_pool.fetch = AsyncMock(
            side_effect=[
                [{"name": "Rock", "n": 3}, {"name": "Electronic", "n": 1}],
                [{"name": "Indie Rock", "n": 2}],
            ]
        )
        genres, styles = await cache_service.aggregate_artist_genre_style(
            artist_id=12345, artist_name="Juana Molina"
        )
        assert genres == {"Rock": 3, "Electronic": 1}
        assert styles == {"Indie Rock": 2}
        # Two queries, both bound to the artist_id (not the name).
        genre_call, style_call = mock_asyncpg_pool.fetch.await_args_list
        assert "ra.artist_id = $1" in genre_call.args[0]
        assert "COUNT(DISTINCT ra.release_id)" in genre_call.args[0]
        assert "ra.extra = 0" in genre_call.args[0]
        assert genre_call.args[1] == 12345
        assert "release_style" in style_call.args[0]
        assert style_call.args[1] == 12345

    @pytest.mark.asyncio
    async def test_keys_on_name_when_no_artist_id(self, cache_service, mock_asyncpg_pool):
        mock_asyncpg_pool.fetch = AsyncMock(side_effect=[[{"name": "Jazz", "n": 5}], []])
        genres, styles = await cache_service.aggregate_artist_genre_style(
            artist_id=None, artist_name="Duke Ellington & John Coltrane"
        )
        assert genres == {"Jazz": 5}
        assert styles == {}
        genre_call = mock_asyncpg_pool.fetch.await_args_list[0]
        assert "lower(ra.artist_name) = lower($1)" in genre_call.args[0]
        assert genre_call.args[1] == "Duke Ellington & John Coltrane"

    @pytest.mark.asyncio
    async def test_empty_result_is_a_miss(self, cache_service, mock_asyncpg_pool):
        mock_asyncpg_pool.fetch = AsyncMock(side_effect=[[], []])
        genres, styles = await cache_service.aggregate_artist_genre_style(
            artist_id=999, artist_name="Unknown"
        )
        assert genres == {}
        assert styles == {}

    @pytest.mark.asyncio
    async def test_pg_error_raises_cache_unavailable(self, cache_service, mock_asyncpg_pool):
        mock_asyncpg_pool.fetch = AsyncMock(side_effect=RuntimeError("connection reset"))
        with pytest.raises(CacheUnavailableError):
            await cache_service.aggregate_artist_genre_style(artist_id=1, artist_name="Stereolab")


# --------------------------------------------------------------------------
# DiscogsService.search_release_ids_by_artist
# --------------------------------------------------------------------------


def _release_search_response(results: list[dict]) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {"pagination": {"page": 1, "pages": 1}, "results": results}
    return resp


@pytest.fixture
def service():
    return DiscogsService(token="test-token")


class TestSearchReleaseIdsByArtist:
    @pytest.mark.asyncio
    async def test_builds_type_release_search_and_extracts_ids(self, service):
        resp = _release_search_response(
            [{"id": 101, "type": "release"}, {"id": 102, "type": "release"}]
        )
        with patch.object(
            service, "_request_with_retry", new_callable=AsyncMock, return_value=resp
        ) as mock_req:
            ids = await service.search_release_ids_by_artist("Jessica Pratt", limit=8)

        assert ids == [101, 102]
        mock_req.assert_awaited_once_with(
            "GET",
            "/database/search",
            params={"type": "release", "artist": "Jessica Pratt", "per_page": 8},
        )

    @pytest.mark.asyncio
    async def test_dedupes_and_caps_at_limit(self, service):
        resp = _release_search_response([{"id": 1}, {"id": 1}, {"id": 2}, {"id": 3}, {"id": 4}])
        with patch.object(
            service, "_request_with_retry", new_callable=AsyncMock, return_value=resp
        ):
            ids = await service.search_release_ids_by_artist("Cat Power", limit=2)
        assert ids == [1, 2]

    @pytest.mark.asyncio
    async def test_skips_non_positive_and_non_int_ids(self, service):
        resp = _release_search_response(
            [{"id": 0}, {"id": -5}, {"id": "x"}, {"nope": 1}, {"id": 42}, "junk"]
        )
        with patch.object(
            service, "_request_with_retry", new_callable=AsyncMock, return_value=resp
        ):
            ids = await service.search_release_ids_by_artist("Chuquimamani-Condori")
        assert ids == [42]

    @pytest.mark.asyncio
    async def test_empty_results_returns_empty_list_not_none(self, service):
        resp = _release_search_response([])
        with patch.object(
            service, "_request_with_retry", new_callable=AsyncMock, return_value=resp
        ):
            ids = await service.search_release_ids_by_artist("Nobody")
        assert ids == []
        assert ids is not None

    @pytest.mark.asyncio
    async def test_couldnt_ask_returns_none(self, service):
        with patch.object(
            service, "_request_with_retry", new_callable=AsyncMock, return_value=None
        ):
            ids = await service.search_release_ids_by_artist("REZN")
        assert ids is None

    @pytest.mark.asyncio
    async def test_server_error_returns_none(self, service):
        resp = MagicMock()
        resp.status_code = 502
        resp.raise_for_status = MagicMock(
            side_effect=httpx.HTTPStatusError(
                "502", request=MagicMock(), response=MagicMock(status_code=502)
            )
        )
        with patch.object(
            service, "_request_with_retry", new_callable=AsyncMock, return_value=resp
        ):
            ids = await service.search_release_ids_by_artist("SiM")
        assert ids is None

    @pytest.mark.asyncio
    async def test_breaker_open_propagates(self, service):
        with patch.object(
            service,
            "_request_with_retry",
            new_callable=AsyncMock,
            side_effect=DiscogsBreakerOpenError("open"),
        ):
            with pytest.raises(DiscogsBreakerOpenError):
                await service.search_release_ids_by_artist("Wishy")

    @pytest.mark.asyncio
    async def test_blank_name_raises_value_error(self, service):
        with pytest.raises(ValueError):
            await service.search_release_ids_by_artist("   ")
