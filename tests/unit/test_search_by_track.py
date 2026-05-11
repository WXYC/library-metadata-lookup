"""Unit tests for the album-by-track endpoint and cache method.

Covers:
  - ``DiscogsCacheService.search_albums_by_track_title``: SQL parameters,
    return-shape projection, threshold pass-through, error wrapping.
  - ``GET /api/v1/discogs/search-by-track``: success, query-param plumbing,
    503 when cache is unconfigured, graceful-empty on cache errors,
    422 on missing query.
"""

from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from discogs.cache_service import CacheUnavailableError, DiscogsCacheService
from tests.unit.conftest import override_deps

# ---------------------------------------------------------------------------
# Cache method: search_albums_by_track_title
# ---------------------------------------------------------------------------


class TestSearchAlbumsByTrackTitle:
    """Direct tests of the cache method.

    Uses an AsyncMock for the underlying ``self.pool`` so we can assert
    on the parametrised SQL call without spinning a real Postgres.
    """

    def _make_svc(self, fetch_rows: list[dict]) -> DiscogsCacheService:
        svc = DiscogsCacheService.__new__(DiscogsCacheService)
        svc.pool = AsyncMock()
        svc.pool.fetch = AsyncMock(return_value=fetch_rows)
        return svc

    @pytest.mark.asyncio
    async def test_returns_projected_dicts(self):
        svc = self._make_svc(
            [
                {
                    "release_id": 1573110,
                    "master_id": 1374,
                    "release_title": "Confield",
                    "release_artist": "Autechre",
                    "track_title": "VI Scose Poise",
                    "track_position": "1",
                    "score": 1.0,
                },
            ]
        )

        result = await svc.search_albums_by_track_title("vi scose poise")

        assert result == [
            {
                "release_id": 1573110,
                "master_id": 1374,
                "release_title": "Confield",
                "release_artist": "Autechre",
                "track_title": "VI Scose Poise",
                "track_position": "1",
                "score": 1.0,
            }
        ]

    @pytest.mark.asyncio
    async def test_passes_query_limit_and_threshold_to_pg(self):
        svc = self._make_svc([])
        await svc.search_albums_by_track_title("scose poise", limit=10, score_threshold=0.5)
        # Positional args: (sql, query, threshold, limit).
        call_args = svc.pool.fetch.await_args.args
        assert call_args[1] == "scose poise"
        assert call_args[2] == 0.5
        assert call_args[3] == 10

    @pytest.mark.asyncio
    async def test_defaults_threshold_to_pg_trgm_default(self):
        svc = self._make_svc([])
        await svc.search_albums_by_track_title("anything")
        call_args = svc.pool.fetch.await_args.args
        assert call_args[2] == 0.3  # pg_trgm default
        assert call_args[3] == 50  # function default

    @pytest.mark.asyncio
    async def test_coerces_score_to_float(self):
        # asyncpg's Real → Python decimal-like type would surface as Decimal in
        # some installs; the projection coerces to plain float so callers can
        # rely on the type.
        svc = self._make_svc(
            [
                {
                    "release_id": 1,
                    "master_id": None,
                    "release_title": "X",
                    "release_artist": "Y",
                    "track_title": "T",
                    "track_position": "1",
                    "score": 0.5,
                }
            ]
        )
        result = await svc.search_albums_by_track_title("anything")
        assert isinstance(result[0]["score"], float)

    @pytest.mark.asyncio
    async def test_master_id_passes_through_null(self):
        # Releases with no master_id (singles, demos, V/A one-offs) come back
        # with master_id NULL. The projection preserves None.
        svc = self._make_svc(
            [
                {
                    "release_id": 99,
                    "master_id": None,
                    "release_title": "Untitled",
                    "release_artist": "Various",
                    "track_title": "Track",
                    "track_position": "A",
                    "score": 0.7,
                }
            ]
        )
        result = await svc.search_albums_by_track_title("track")
        assert result[0]["master_id"] is None

    @pytest.mark.asyncio
    async def test_wraps_db_errors_as_cache_unavailable(self):
        svc = DiscogsCacheService.__new__(DiscogsCacheService)
        svc.pool = AsyncMock()
        svc.pool.fetch = AsyncMock(side_effect=RuntimeError("connection refused"))

        with pytest.raises(CacheUnavailableError) as exc_info:
            await svc.search_albums_by_track_title("anything")
        assert "connection refused" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Endpoint: GET /api/v1/discogs/search-by-track
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_discogs():
    from discogs.service import DiscogsService

    return AsyncMock(spec=DiscogsService)


@pytest.fixture
def mock_cache():
    return AsyncMock(spec=DiscogsCacheService)


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


class TestSearchByTrackEndpoint:
    @pytest.mark.asyncio
    async def test_success(self, app_with_cache, mock_cache):
        mock_cache.search_albums_by_track_title = AsyncMock(
            return_value=[
                {
                    "release_id": 1573110,
                    "master_id": 1374,
                    "release_title": "Confield",
                    "release_artist": "Autechre",
                    "track_title": "VI Scose Poise",
                    "track_position": "1",
                    "score": 1.0,
                },
                {
                    "release_id": 8434,
                    "master_id": 1374,
                    "release_title": "Confield",
                    "release_artist": "Autechre",
                    "track_title": "VI Scose Poise",
                    "track_position": "A1",
                    "score": 1.0,
                },
            ]
        )

        async with AsyncClient(
            transport=ASGITransport(app=app_with_cache), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/api/v1/discogs/search-by-track",
                params={"q": "vi scose poise"},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 2
        assert body["query"] == "vi scose poise"
        assert {r["release_id"] for r in body["results"]} == {1573110, 8434}
        # Backend will join these release_ids against
        # ``library.canonical_entity_id = 'discogs:' || release_id``.
        assert all(r["release_artist"] == "Autechre" for r in body["results"])

    @pytest.mark.asyncio
    async def test_query_params_pass_through_to_cache(self, app_with_cache, mock_cache):
        mock_cache.search_albums_by_track_title = AsyncMock(return_value=[])

        async with AsyncClient(
            transport=ASGITransport(app=app_with_cache), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/api/v1/discogs/search-by-track",
                params={"q": "track", "limit": 25, "score_threshold": 0.6},
            )

        assert resp.status_code == 200
        mock_cache.search_albums_by_track_title.assert_called_once_with(
            "track", limit=25, score_threshold=0.6
        )

    @pytest.mark.asyncio
    async def test_missing_query_returns_422(self, app_with_cache):
        async with AsyncClient(
            transport=ASGITransport(app=app_with_cache), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/discogs/search-by-track")
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_threshold_out_of_range_returns_422(self, app_with_cache):
        # FastAPI's Query(ge=0, le=1) validation.
        async with AsyncClient(
            transport=ASGITransport(app=app_with_cache), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/api/v1/discogs/search-by-track",
                params={"q": "t", "score_threshold": 1.5},
            )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_no_cache_returns_503(self, app_without_cache):
        async with AsyncClient(
            transport=ASGITransport(app=app_without_cache), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/api/v1/discogs/search-by-track",
                params={"q": "anything"},
            )
        assert resp.status_code == 503
        assert "DATABASE_URL_DISCOGS" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_cache_error_returns_empty_results_not_500(self, app_with_cache, mock_cache):
        """Mirror the autocomplete endpoint's graceful-empty behavior:
        cache errors don't bubble to the caller; the empty body is the
        signal."""
        mock_cache.search_albums_by_track_title = AsyncMock(
            side_effect=CacheUnavailableError("transient connection failure")
        )

        async with AsyncClient(
            transport=ASGITransport(app=app_with_cache), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/api/v1/discogs/search-by-track",
                params={"q": "anything"},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["results"] == []
        assert body["total"] == 0
        assert body["query"] == "anything"
