"""Unit tests for lookup/router.py."""

from unittest.mock import AsyncMock, Mock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from discogs.service import DiscogsService
from library.db import LibraryDB
from lookup.models import LookupResponse


LOOKUP_BODY = {"artist": "Queen", "album": "The Game", "raw_message": "Queen - The Game"}


@pytest.fixture
def mock_db():
    return AsyncMock(spec=LibraryDB)


@pytest.fixture
def mock_discogs():
    return AsyncMock(spec=DiscogsService)


@pytest.fixture
def app_client(mock_db, mock_discogs, mock_settings):
    from main import app
    from core.dependencies import get_library_db, get_discogs_service, get_posthog_client
    from config.settings import get_settings

    app.dependency_overrides[get_library_db] = lambda: mock_db
    app.dependency_overrides[get_discogs_service] = lambda: mock_discogs
    app.dependency_overrides[get_posthog_client] = lambda: None
    app.dependency_overrides[get_settings] = lambda: mock_settings
    yield app
    app.dependency_overrides.clear()


class TestHandleLookup:
    @pytest.mark.asyncio
    async def test_successful_lookup(self, app_client):
        response = LookupResponse(results=[], search_type="direct")

        with patch("lookup.router.perform_lookup", new_callable=AsyncMock) as mock_lookup:
            mock_lookup.return_value = response
            async with AsyncClient(
                transport=ASGITransport(app=app_client), base_url="http://test"
            ) as client:
                resp = await client.post("/api/v1/lookup", json=LOOKUP_BODY)

        assert resp.status_code == 200
        body = resp.json()
        assert body["search_type"] == "direct"

    @pytest.mark.asyncio
    async def test_telemetry_sent_when_posthog_configured(
        self, mock_db, mock_discogs, mock_settings
    ):
        from main import app
        from core.dependencies import get_library_db, get_discogs_service, get_posthog_client
        from config.settings import get_settings

        mock_posthog = Mock()
        mock_posthog.capture = Mock()
        mock_posthog.flush = Mock()

        app.dependency_overrides[get_library_db] = lambda: mock_db
        app.dependency_overrides[get_discogs_service] = lambda: mock_discogs
        app.dependency_overrides[get_posthog_client] = lambda: mock_posthog
        app.dependency_overrides[get_settings] = lambda: mock_settings

        response = LookupResponse(results=[], search_type="direct")

        try:
            with patch("lookup.router.perform_lookup", new_callable=AsyncMock) as mock_lookup:
                mock_lookup.return_value = response
                async with AsyncClient(
                    transport=ASGITransport(app=app), base_url="http://test"
                ) as client:
                    resp = await client.post("/api/v1/lookup", json=LOOKUP_BODY)

            assert resp.status_code == 200
            # Telemetry sends capture calls via send_to_posthog
            assert mock_posthog.capture.call_count >= 1
        finally:
            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_error_returns_500(self, app_client):
        with patch(
            "lookup.router.perform_lookup",
            new_callable=AsyncMock,
            side_effect=Exception("boom"),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app_client), base_url="http://test"
            ) as client:
                resp = await client.post("/api/v1/lookup", json=LOOKUP_BODY)

        assert resp.status_code == 500

    @pytest.mark.asyncio
    async def test_http_exception_passthrough(self, app_client):
        from fastapi import HTTPException

        with patch(
            "lookup.router.perform_lookup",
            new_callable=AsyncMock,
            side_effect=HTTPException(status_code=400, detail="Bad request"),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app_client), base_url="http://test"
            ) as client:
                resp = await client.post("/api/v1/lookup", json=LOOKUP_BODY)

        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_skip_cache_flag(self, app_client):
        response = LookupResponse(results=[], search_type="direct")

        with patch("lookup.router.perform_lookup", new_callable=AsyncMock) as mock_lookup, \
             patch("lookup.router.set_skip_cache") as mock_set_skip:
            mock_lookup.return_value = response
            async with AsyncClient(
                transport=ASGITransport(app=app_client), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/api/v1/lookup?skip_cache=true", json=LOOKUP_BODY
                )

        assert resp.status_code == 200
        mock_set_skip.assert_called_once_with(True)

    @pytest.mark.asyncio
    async def test_cache_stats_initialized(self, app_client):
        response = LookupResponse(results=[], search_type="direct")

        with patch("lookup.router.perform_lookup", new_callable=AsyncMock) as mock_lookup, \
             patch("lookup.router.init_cache_stats") as mock_init:
            mock_lookup.return_value = response
            async with AsyncClient(
                transport=ASGITransport(app=app_client), base_url="http://test"
            ) as client:
                resp = await client.post("/api/v1/lookup", json=LOOKUP_BODY)

        assert resp.status_code == 200
        mock_init.assert_called_once()
