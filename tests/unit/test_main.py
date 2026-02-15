"""Unit tests for main.py."""

from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient


class TestLifespan:
    @pytest.mark.asyncio
    async def test_shutdown_calls_cleanup(self, mock_settings):
        """Lifespan context manager calls shutdown functions on exit."""
        from main import app, lifespan

        with (
            patch("main.shutdown_posthog") as mock_ph_shutdown,
            patch("main.close_library_db", new_callable=AsyncMock) as mock_db_close,
            patch("main.close_discogs_service", new_callable=AsyncMock) as mock_discogs_close,
        ):
            async with lifespan(app):
                pass  # startup

            # shutdown should have run
            mock_ph_shutdown.assert_called_once()
            mock_db_close.assert_called_once()
            mock_discogs_close.assert_called_once()


class TestMiddleware:
    @pytest.mark.asyncio
    async def test_posthog_flush_middleware(self, mock_settings):
        """PostHog flush middleware flushes after each request."""
        from config.settings import get_settings
        from core.dependencies import get_discogs_service, get_library_db, get_posthog_client
        from main import app

        mock_db = AsyncMock()
        mock_db.is_available = AsyncMock(return_value=True)

        app.dependency_overrides[get_library_db] = lambda: mock_db
        app.dependency_overrides[get_discogs_service] = lambda: None
        app.dependency_overrides[get_posthog_client] = lambda: None
        app.dependency_overrides[get_settings] = lambda: mock_settings

        try:
            with patch("main.flush_posthog") as mock_flush:
                async with AsyncClient(
                    transport=ASGITransport(app=app), base_url="http://test"
                ) as client:
                    await client.get("/health")

                mock_flush.assert_called()
        finally:
            app.dependency_overrides.clear()


class TestAppRouterRegistration:
    def test_routes_registered(self):
        from main import app

        routes = [r.path for r in app.routes]
        assert "/health" in routes
        assert "/api/v1/lookup" in routes
        assert "/api/v1/library/search" in routes

    def test_app_metadata(self):
        from main import app

        assert app.title is not None
        assert app.version is not None
