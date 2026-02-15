"""Unit tests for routers/health.py."""

from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from discogs.service import DiscogsService
from library.db import LibraryDB
from routers.health import (
    _check_database,
    _check_discogs_api,
    _check_discogs_cache,
    _run_check,
)
from tests.unit.conftest import override_deps

# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


class TestCheckDatabase:
    @pytest.mark.asyncio
    async def test_ok(self):
        db = AsyncMock(spec=LibraryDB)
        db.is_available = AsyncMock(return_value=True)
        assert await _check_database(db) == "ok"

    @pytest.mark.asyncio
    async def test_error(self):
        db = AsyncMock(spec=LibraryDB)
        db.is_available = AsyncMock(return_value=False)
        assert await _check_database(db) == "error"


class TestCheckDiscogsApi:
    @pytest.mark.asyncio
    async def test_ok(self):
        svc = AsyncMock(spec=DiscogsService)
        svc.check_api = AsyncMock(return_value=True)
        assert await _check_discogs_api(svc) == "ok"

    @pytest.mark.asyncio
    async def test_error(self):
        svc = AsyncMock(spec=DiscogsService)
        svc.check_api = AsyncMock(return_value=False)
        assert await _check_discogs_api(svc) == "error"

    @pytest.mark.asyncio
    async def test_none_service(self):
        assert await _check_discogs_api(None) == "unavailable"


class TestCheckDiscogsCache:
    @pytest.mark.asyncio
    async def test_ok(self):
        svc = AsyncMock(spec=DiscogsService)
        svc.cache_service = AsyncMock()
        svc.cache_service.is_available = AsyncMock(return_value=True)
        assert await _check_discogs_cache(svc) == "ok"

    @pytest.mark.asyncio
    async def test_error(self):
        svc = AsyncMock(spec=DiscogsService)
        svc.cache_service = AsyncMock()
        svc.cache_service.is_available = AsyncMock(return_value=False)
        assert await _check_discogs_cache(svc) == "error"

    @pytest.mark.asyncio
    async def test_none_service(self):
        assert await _check_discogs_cache(None) == "unavailable"

    @pytest.mark.asyncio
    async def test_no_cache_service(self):
        svc = AsyncMock(spec=DiscogsService)
        svc.cache_service = None
        assert await _check_discogs_cache(svc) == "unavailable"


class TestRunCheck:
    @pytest.mark.asyncio
    async def test_success(self):
        async def ok_check():
            return "ok"

        assert await _run_check(ok_check()) == "ok"

    @pytest.mark.asyncio
    async def test_timeout(self):
        import asyncio

        async def slow_check():
            await asyncio.sleep(100)
            return "ok"

        with patch("routers.health.CHECK_TIMEOUT", 0.01):
            result = await _run_check(slow_check())
        assert result == "timeout"


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


class TestHealthEndpoint:
    @pytest.fixture
    def mock_db(self):
        db = AsyncMock(spec=LibraryDB)
        db.is_available = AsyncMock(return_value=True)
        return db

    @pytest.fixture
    def mock_discogs(self):
        svc = AsyncMock(spec=DiscogsService)
        svc.check_api = AsyncMock(return_value=True)
        svc.cache_service = AsyncMock()
        svc.cache_service.is_available = AsyncMock(return_value=True)
        return svc

    @pytest.mark.asyncio
    async def test_healthy(self, mock_db, mock_discogs, mock_settings):
        from config.settings import get_settings
        from core.dependencies import get_discogs_service, get_library_db, get_posthog_client
        from main import app

        with override_deps(
            app,
            {
                get_library_db: mock_db,
                get_discogs_service: mock_discogs,
                get_posthog_client: None,
                get_settings: mock_settings,
            },
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get("/health")

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "healthy"
        assert "version" in body
        assert body["services"]["database"] == "ok"

    @pytest.mark.asyncio
    async def test_degraded(self, mock_db, mock_settings):
        """Core (database) ok but optional service erroring -> degraded."""
        from config.settings import get_settings
        from core.dependencies import get_discogs_service, get_library_db, get_posthog_client
        from main import app

        svc = AsyncMock(spec=DiscogsService)
        svc.check_api = AsyncMock(return_value=False)
        svc.cache_service = AsyncMock()
        svc.cache_service.is_available = AsyncMock(return_value=False)

        with override_deps(
            app,
            {
                get_library_db: mock_db,
                get_discogs_service: svc,
                get_posthog_client: None,
                get_settings: mock_settings,
            },
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get("/health")

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "degraded"

    @pytest.mark.asyncio
    async def test_unhealthy_returns_503(self, mock_settings):
        """Core service (database) down -> unhealthy + 503."""
        from config.settings import get_settings
        from core.dependencies import get_discogs_service, get_library_db, get_posthog_client
        from main import app

        db = AsyncMock(spec=LibraryDB)
        db.is_available = AsyncMock(return_value=False)

        with override_deps(
            app,
            {
                get_library_db: db,
                get_discogs_service: None,
                get_posthog_client: None,
                get_settings: mock_settings,
            },
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get("/health")

        assert resp.status_code == 503
        body = resp.json()
        assert body["status"] == "unhealthy"
