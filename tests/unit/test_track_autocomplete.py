"""Unit tests for track autocomplete endpoint and cache service method."""

from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from discogs.cache_service import DiscogsCacheService
from tests.unit.conftest import override_deps


# ---------------------------------------------------------------------------
# Cache service: autocomplete_tracks
# ---------------------------------------------------------------------------


class TestAutocompleteTracksCache:
    @pytest.mark.asyncio
    async def test_returns_distinct_track_titles(self, mock_asyncpg_pool):
        mock_asyncpg_pool.fetch = AsyncMock(
            return_value=[
                {"title": "Percolator"},
                {"title": "Per Clansen"},
            ]
        )

        svc = DiscogsCacheService(mock_asyncpg_pool)
        result = await svc.autocomplete_tracks("Stereolab", "per", limit=20)

        assert result == ["Per Clansen", "Percolator"]
        mock_asyncpg_pool.fetch.assert_called_once()

    @pytest.mark.asyncio
    async def test_deduplicates_case_insensitive(self, mock_asyncpg_pool):
        mock_asyncpg_pool.fetch = AsyncMock(
            return_value=[
                {"title": "Percolator"},
                {"title": "percolator"},
                {"title": "PERCOLATOR"},
            ]
        )

        svc = DiscogsCacheService(mock_asyncpg_pool)
        result = await svc.autocomplete_tracks("Stereolab", "per", limit=20)

        assert len(result) == 1
        assert result[0] == "Percolator"  # keeps first occurrence

    @pytest.mark.asyncio
    async def test_respects_limit(self, mock_asyncpg_pool):
        mock_asyncpg_pool.fetch = AsyncMock(
            return_value=[{"title": f"Track {i}"} for i in range(10)]
        )

        svc = DiscogsCacheService(mock_asyncpg_pool)
        result = await svc.autocomplete_tracks("Artist", "tr", limit=5)

        assert len(result) == 5

    @pytest.mark.asyncio
    async def test_with_release_filter(self, mock_asyncpg_pool):
        mock_asyncpg_pool.fetch = AsyncMock(return_value=[{"title": "Song"}])

        svc = DiscogsCacheService(mock_asyncpg_pool)
        result = await svc.autocomplete_tracks("Artist", "so", release="Album", limit=20)

        assert result == ["Song"]
        # Verify the release parameter was passed: fetch(query, artist, q, release, limit)
        call_args = mock_asyncpg_pool.fetch.call_args
        assert call_args[0][3] == "Album"  # release param at index 3

    @pytest.mark.asyncio
    async def test_returns_empty_on_cache_error(self, mock_asyncpg_pool):
        mock_asyncpg_pool.fetch = AsyncMock(side_effect=Exception("connection lost"))

        svc = DiscogsCacheService(mock_asyncpg_pool)
        from discogs.cache_service import CacheUnavailableError

        with pytest.raises(CacheUnavailableError):
            await svc.autocomplete_tracks("Artist", "per", limit=20)

    @pytest.mark.asyncio
    async def test_returns_sorted_titles(self, mock_asyncpg_pool):
        mock_asyncpg_pool.fetch = AsyncMock(
            return_value=[
                {"title": "Zebra"},
                {"title": "Alpha"},
                {"title": "Mango"},
            ]
        )

        svc = DiscogsCacheService(mock_asyncpg_pool)
        result = await svc.autocomplete_tracks("Artist", "a", limit=20)

        assert result == ["Alpha", "Mango", "Zebra"]

    @pytest.mark.asyncio
    async def test_empty_results(self, mock_asyncpg_pool):
        mock_asyncpg_pool.fetch = AsyncMock(return_value=[])

        svc = DiscogsCacheService(mock_asyncpg_pool)
        result = await svc.autocomplete_tracks("Unknown", "xyz", limit=20)

        assert result == []


# ---------------------------------------------------------------------------
# Router: GET /api/v1/discogs/tracks/autocomplete
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_discogs():
    from discogs.service import DiscogsService

    svc = AsyncMock(spec=DiscogsService)
    return svc


@pytest.fixture
def mock_cache():
    svc = AsyncMock(spec=DiscogsCacheService)
    return svc


@pytest.fixture
def app_with_cache(mock_discogs, mock_cache, mock_settings):
    from config.settings import get_settings
    from core.dependencies import (
        get_discogs_cache_service,
        get_discogs_service,
        get_library_db,
        get_posthog_client,
    )
    from main import app

    with override_deps(
        app,
        {
            get_library_db: AsyncMock(),
            get_discogs_service: mock_discogs,
            get_discogs_cache_service: mock_cache,
            get_posthog_client: None,
            get_settings: mock_settings,
        },
    ):
        yield app


@pytest.fixture
def app_without_cache(mock_discogs, mock_settings):
    from config.settings import get_settings
    from core.dependencies import (
        get_discogs_cache_service,
        get_discogs_service,
        get_library_db,
        get_posthog_client,
    )
    from main import app

    with override_deps(
        app,
        {
            get_library_db: AsyncMock(),
            get_discogs_service: mock_discogs,
            get_discogs_cache_service: None,
            get_posthog_client: None,
            get_settings: mock_settings,
        },
    ):
        yield app


class TestTracksAutocompleteEndpoint:
    @pytest.mark.asyncio
    async def test_success(self, app_with_cache, mock_cache):
        mock_cache.autocomplete_tracks = AsyncMock(return_value=["Percolator", "Per Clansen"])

        async with AsyncClient(
            transport=ASGITransport(app=app_with_cache), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/api/v1/discogs/tracks/autocomplete",
                params={"artist": "Stereolab", "q": "per"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["results"] == ["Percolator", "Per Clansen"]
        assert data["total"] == 2
        assert data["artist"] == "Stereolab"
        assert data["cached"] is True

    @pytest.mark.asyncio
    async def test_with_release_param(self, app_with_cache, mock_cache):
        mock_cache.autocomplete_tracks = AsyncMock(return_value=["Song"])

        async with AsyncClient(
            transport=ASGITransport(app=app_with_cache), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/api/v1/discogs/tracks/autocomplete",
                params={"artist": "Stereolab", "q": "so", "release": "Dots and Loops"},
            )

        assert resp.status_code == 200
        mock_cache.autocomplete_tracks.assert_called_once_with(
            "Stereolab", "so", release="Dots and Loops", limit=20
        )

    @pytest.mark.asyncio
    async def test_missing_artist_returns_422(self, app_with_cache):
        async with AsyncClient(
            transport=ASGITransport(app=app_with_cache), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/api/v1/discogs/tracks/autocomplete",
                params={"q": "per"},
            )

        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_missing_q_returns_422(self, app_with_cache):
        async with AsyncClient(
            transport=ASGITransport(app=app_with_cache), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/api/v1/discogs/tracks/autocomplete",
                params={"artist": "Stereolab"},
            )

        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_no_cache_returns_503(self, app_without_cache):
        async with AsyncClient(
            transport=ASGITransport(app=app_without_cache), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/api/v1/discogs/tracks/autocomplete",
                params={"artist": "Stereolab", "q": "per"},
            )

        assert resp.status_code == 503

    @pytest.mark.asyncio
    async def test_cache_error_returns_empty(self, app_with_cache, mock_cache):
        from discogs.cache_service import CacheUnavailableError

        mock_cache.autocomplete_tracks = AsyncMock(
            side_effect=CacheUnavailableError("connection lost")
        )

        async with AsyncClient(
            transport=ASGITransport(app=app_with_cache), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/api/v1/discogs/tracks/autocomplete",
                params={"artist": "Stereolab", "q": "per"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["results"] == []
        assert data["total"] == 0

    @pytest.mark.asyncio
    async def test_custom_limit(self, app_with_cache, mock_cache):
        mock_cache.autocomplete_tracks = AsyncMock(return_value=["Track"])

        async with AsyncClient(
            transport=ASGITransport(app=app_with_cache), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/api/v1/discogs/tracks/autocomplete",
                params={"artist": "Artist", "q": "tr", "limit": 5},
            )

        assert resp.status_code == 200
        mock_cache.autocomplete_tracks.assert_called_once_with(
            "Artist", "tr", release=None, limit=5
        )
