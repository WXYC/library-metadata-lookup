"""Unit tests for discogs/router.py."""

from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

from discogs.models import (
    DiscogsSearchResponse,
    ReleaseMetadataResponse,
    TrackReleasesResponse,
)
from discogs.router import _require_service
from discogs.service import DiscogsService
from tests.factories import make_discogs_result
from tests.unit.conftest import override_deps

# ---------------------------------------------------------------------------
# _require_service
# ---------------------------------------------------------------------------


class TestRequireService:
    def test_returns_service(self):
        svc = AsyncMock(spec=DiscogsService)
        assert _require_service(svc) is svc

    def test_none_raises_503(self):
        with pytest.raises(HTTPException) as exc_info:
            _require_service(None)
        assert exc_info.value.status_code == 503


# ---------------------------------------------------------------------------
# Endpoint tests
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_discogs():
    svc = AsyncMock(spec=DiscogsService)
    return svc


@pytest.fixture
def app_with_discogs(mock_discogs, mock_settings):
    from config.settings import get_settings
    from core.dependencies import get_discogs_service, get_library_db, get_posthog_client
    from main import app

    with override_deps(
        app,
        {
            get_library_db: AsyncMock(),
            get_discogs_service: mock_discogs,
            get_posthog_client: None,
            get_settings: mock_settings,
        },
    ):
        yield app


@pytest.fixture
def app_without_discogs(mock_settings):
    from config.settings import get_settings
    from core.dependencies import get_discogs_service, get_library_db, get_posthog_client
    from main import app

    with override_deps(
        app,
        {
            get_library_db: AsyncMock(),
            get_discogs_service: None,
            get_posthog_client: None,
            get_settings: mock_settings,
        },
    ):
        yield app


class TestTrackReleases:
    @pytest.mark.asyncio
    async def test_success(self, app_with_discogs, mock_discogs):
        mock_discogs.search_releases_by_track = AsyncMock(
            return_value=TrackReleasesResponse(track="Song", releases=[], total=0)
        )

        async with AsyncClient(
            transport=ASGITransport(app=app_with_discogs), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/discogs/track-releases", params={"track": "Song"})

        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_no_service_returns_503(self, app_without_discogs):
        async with AsyncClient(
            transport=ASGITransport(app=app_without_discogs), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/discogs/track-releases", params={"track": "Song"})

        assert resp.status_code == 503


class TestGetRelease:
    @pytest.mark.asyncio
    async def test_found(self, app_with_discogs, mock_discogs):
        mock_discogs.get_release = AsyncMock(
            return_value=ReleaseMetadataResponse(
                release_id=123,
                title="Album",
                artist="Artist",
                release_url="https://discogs.com/release/123",
            )
        )

        async with AsyncClient(
            transport=ASGITransport(app=app_with_discogs), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/discogs/release/123")

        assert resp.status_code == 200
        assert resp.json()["title"] == "Album"

    @pytest.mark.asyncio
    async def test_not_found(self, app_with_discogs, mock_discogs):
        mock_discogs.get_release = AsyncMock(return_value=None)

        async with AsyncClient(
            transport=ASGITransport(app=app_with_discogs), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/discogs/release/999")

        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_no_service_returns_503(self, app_without_discogs):
        async with AsyncClient(
            transport=ASGITransport(app=app_without_discogs), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/discogs/release/123")

        assert resp.status_code == 503


class TestSearchReleases:
    @pytest.mark.asyncio
    async def test_success(self, app_with_discogs, mock_discogs):
        mock_discogs.search = AsyncMock(
            return_value=DiscogsSearchResponse(
                results=[
                    make_discogs_result(
                        release_id=1,
                        album="Album",
                        artist="Artist",
                    )
                ],
                total=1,
            )
        )

        async with AsyncClient(
            transport=ASGITransport(app=app_with_discogs), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/discogs/search",
                json={"artist": "Artist", "album": "Album"},
            )

        assert resp.status_code == 200
        assert resp.json()["total"] == 1

    @pytest.mark.asyncio
    async def test_no_params_returns_400(self, app_with_discogs):
        async with AsyncClient(
            transport=ASGITransport(app=app_with_discogs), base_url="http://test"
        ) as client:
            resp = await client.post("/api/v1/discogs/search", json={})

        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_no_service_returns_503(self, app_without_discogs):
        async with AsyncClient(
            transport=ASGITransport(app=app_without_discogs), base_url="http://test"
        ) as client:
            resp = await client.post("/api/v1/discogs/search", json={"artist": "Artist"})

        assert resp.status_code == 503
