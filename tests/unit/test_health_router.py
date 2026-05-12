"""Unit tests for routers/health.py.

The health route is a thin local handler that uses ``wxyc_fastapi.healthcheck``
primitives (``Check`` dataclass + ``DEFAULT_TIMEOUT_SECONDS``) but preserves
LML's contracted response shape:

* URL stays ``GET /health`` (Railway healthcheck depends on it).
* Response body carries ``version`` (operator-facing).
* ``services.discogs_api`` may carry granular values like ``"auth-error"``,
  ``"rate-limited"``, ``"upstream-error"`` — see ``DiscogsApiCheckResult``.
  The shared ``readiness_router`` flattens those to ``"unavailable"``, which is
  why we don't mount it directly.
"""

from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from discogs.service import DiscogsApiCheckResult, DiscogsService
from library.db import LibraryDB
from routers.health import (
    _check_database,
    _check_discogs_api,
    _check_discogs_cache,
)
from tests.unit.conftest import override_deps

# ---------------------------------------------------------------------------
# Probe helpers (still local — they encode LML-specific contract values)
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
    """The probe surfaces the enum's string value verbatim into /health."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "result,expected",
        [
            (DiscogsApiCheckResult.OK, "ok"),
            (DiscogsApiCheckResult.AUTH_ERROR, "auth-error"),
            (DiscogsApiCheckResult.RATE_LIMITED, "rate-limited"),
            (DiscogsApiCheckResult.UPSTREAM_ERROR, "upstream-error"),
            (DiscogsApiCheckResult.NETWORK_ERROR, "network-error"),
            (DiscogsApiCheckResult.ERROR, "error"),
        ],
    )
    async def test_renders_each_enum_value(self, result, expected):
        svc = AsyncMock(spec=DiscogsService)
        svc.check_api = AsyncMock(return_value=result)
        assert await _check_discogs_api(svc) == expected

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
        svc.check_api = AsyncMock(return_value=DiscogsApiCheckResult.OK)
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
        assert body["services"]["discogs_api"] == "ok"
        assert body["services"]["discogs_cache"] == "ok"

    @pytest.mark.asyncio
    async def test_degraded_preserves_granular_discogs_value(self, mock_db, mock_settings):
        """Core (database) ok but optional service erroring -> degraded.

        The granular DiscogsApiCheckResult value (``auth-error``) must round-trip
        to the response body verbatim — operators rely on it to distinguish
        token rotation from rate limiting from upstream outages. The shared
        readiness_router would flatten this to ``unavailable``; the local
        handler must not.
        """
        from config.settings import get_settings
        from core.dependencies import get_discogs_service, get_library_db, get_posthog_client
        from main import app

        svc = AsyncMock(spec=DiscogsService)
        svc.check_api = AsyncMock(return_value=DiscogsApiCheckResult.AUTH_ERROR)
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
        assert body["services"]["discogs_api"] == "auth-error"
        assert body["services"]["discogs_cache"] == "error"

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
        assert body["services"]["database"] == "error"

    @pytest.mark.asyncio
    async def test_uses_shared_check_dataclass(self):
        """The route declares its probes as ``wxyc_fastapi.healthcheck.Check``s.

        Locks in the shared-primitive contract: future drift back to bespoke
        local types should be flagged here.
        """
        from wxyc_fastapi.healthcheck import Check

        from routers.health import _build_checks

        db = AsyncMock(spec=LibraryDB)
        svc = AsyncMock(spec=DiscogsService)
        checks = _build_checks(db=db, discogs_service=svc)
        assert len(checks) == 3
        assert all(isinstance(c, Check) for c in checks)
        assert [c.name for c in checks] == ["database", "discogs_api", "discogs_cache"]
        assert checks[0].required is True  # database is core
        assert checks[1].required is False  # discogs_api is optional
        assert checks[2].required is False  # discogs_cache is optional

    @pytest.mark.asyncio
    async def test_local_helpers_removed(self):
        """``_run_check`` and ``CHECK_TIMEOUT`` were deleted in favour of the
        shared ``DEFAULT_TIMEOUT_SECONDS`` constant."""
        import routers.health as health_module

        assert not hasattr(health_module, "_run_check"), (
            "_run_check should have been removed; use shared primitives instead"
        )
        assert not hasattr(health_module, "CHECK_TIMEOUT"), (
            "CHECK_TIMEOUT should have been removed; "
            "use wxyc_fastapi.healthcheck.DEFAULT_TIMEOUT_SECONDS"
        )
