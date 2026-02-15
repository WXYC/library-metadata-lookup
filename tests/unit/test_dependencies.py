"""Unit tests for core/dependencies.py."""

from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

import core.dependencies as deps_module
from core.dependencies import (
    close_discogs_service,
    close_library_db,
    flush_posthog,
    get_discogs_service,
    get_library_db,
    get_posthog_client,
    shutdown_posthog,
)


@pytest.fixture(autouse=True)
def reset_globals():
    """Reset module-level singleton state between tests."""
    deps_module._library_db = None
    deps_module._discogs_service = None
    deps_module._discogs_pool = None
    deps_module._posthog_client = None
    yield
    deps_module._library_db = None
    deps_module._discogs_service = None
    deps_module._discogs_pool = None
    deps_module._posthog_client = None


# ---------------------------------------------------------------------------
# get_library_db
# ---------------------------------------------------------------------------


class TestGetLibraryDB:
    @pytest.mark.asyncio
    async def test_creates_and_connects(self, tmp_path, mock_settings):
        db_file = tmp_path / "test.db"
        db_file.touch()
        mock_settings.library_db_path = str(db_file)

        with patch("core.dependencies.LibraryDB") as mock_db_cls:
            mock_db = AsyncMock()
            mock_db_cls.return_value = mock_db

            result = await get_library_db(mock_settings)

            mock_db_cls.assert_called_once()
            mock_db.connect.assert_called_once()
            assert result is mock_db

    @pytest.mark.asyncio
    async def test_cached_instance(self, mock_settings):
        """Second call returns the cached instance."""
        mock_db = AsyncMock()
        deps_module._library_db = mock_db

        result = await get_library_db(mock_settings)
        assert result is mock_db

    @pytest.mark.asyncio
    async def test_init_error_raises(self, mock_settings):
        from core.exceptions import ServiceInitializationError

        with patch("core.dependencies.LibraryDB") as mock_db_cls:
            mock_db_cls.return_value.connect = AsyncMock(side_effect=Exception("no db"))

            with pytest.raises(ServiceInitializationError):
                await get_library_db(mock_settings)


# ---------------------------------------------------------------------------
# close_library_db
# ---------------------------------------------------------------------------


class TestCloseLibraryDB:
    @pytest.mark.asyncio
    async def test_closes_connection(self):
        mock_db = AsyncMock()
        deps_module._library_db = mock_db

        await close_library_db()

        mock_db.close.assert_called_once()
        assert deps_module._library_db is None

    @pytest.mark.asyncio
    async def test_noop_when_none(self):
        deps_module._library_db = None
        await close_library_db()  # should not raise


# ---------------------------------------------------------------------------
# get_discogs_service
# ---------------------------------------------------------------------------


class TestGetDiscogsService:
    @pytest.mark.asyncio
    async def test_no_token_returns_none(self, mock_settings):
        mock_settings.discogs_token = None
        result = await get_discogs_service(mock_settings)
        assert result is None

    @pytest.mark.asyncio
    async def test_creates_service_with_token(self, mock_settings):
        mock_settings.discogs_token = "test-token"
        mock_settings.database_url_discogs = None

        with patch("core.dependencies.DiscogsService") as mock_svc_cls:
            mock_svc = AsyncMock()
            mock_svc_cls.return_value = mock_svc

            result = await get_discogs_service(mock_settings)

            mock_svc_cls.assert_called_once_with("test-token", cache_service=None)
            assert result is mock_svc

    @pytest.mark.asyncio
    async def test_creates_pool_with_database_url(self, mock_settings):
        mock_settings.discogs_token = "test-token"
        mock_settings.database_url_discogs = "postgresql://localhost/test"

        mock_pool = AsyncMock()

        with (
            patch("core.dependencies.asyncpg.create_pool", new_callable=AsyncMock) as mock_create,
            patch("core.dependencies.DiscogsCacheService") as mock_cache_cls,
            patch("core.dependencies.DiscogsService") as mock_svc_cls,
        ):
            mock_create.return_value = mock_pool
            mock_cache = MagicMock()
            mock_cache_cls.return_value = mock_cache
            mock_svc = AsyncMock()
            mock_svc_cls.return_value = mock_svc

            await get_discogs_service(mock_settings)

            mock_create.assert_called_once()
            mock_cache_cls.assert_called_once_with(mock_pool)
            mock_svc_cls.assert_called_once_with("test-token", cache_service=mock_cache)

    @pytest.mark.asyncio
    async def test_pool_error_degrades_gracefully(self, mock_settings):
        mock_settings.discogs_token = "test-token"
        mock_settings.database_url_discogs = "postgresql://localhost/test"

        with (
            patch(
                "core.dependencies.asyncpg.create_pool",
                new_callable=AsyncMock,
                side_effect=Exception("connection refused"),
            ),
            patch("core.dependencies.DiscogsService") as mock_svc_cls,
        ):
            mock_svc = AsyncMock()
            mock_svc_cls.return_value = mock_svc

            await get_discogs_service(mock_settings)

            # Service created without cache
            mock_svc_cls.assert_called_once_with("test-token", cache_service=None)

    @pytest.mark.asyncio
    async def test_cached_instance(self, mock_settings):
        mock_svc = AsyncMock()
        deps_module._discogs_service = mock_svc
        mock_settings.discogs_token = "test-token"

        result = await get_discogs_service(mock_settings)
        assert result is mock_svc


# ---------------------------------------------------------------------------
# close_discogs_service
# ---------------------------------------------------------------------------


class TestCloseDiscogsService:
    @pytest.mark.asyncio
    async def test_closes_service_and_pool(self):
        mock_svc = AsyncMock()
        mock_pool = AsyncMock()
        deps_module._discogs_service = mock_svc
        deps_module._discogs_pool = mock_pool

        await close_discogs_service()

        mock_svc.close.assert_called_once()
        mock_pool.close.assert_called_once()
        assert deps_module._discogs_service is None
        assert deps_module._discogs_pool is None

    @pytest.mark.asyncio
    async def test_noop_when_none(self):
        await close_discogs_service()  # should not raise


# ---------------------------------------------------------------------------
# get_posthog_client
# ---------------------------------------------------------------------------


class TestGetPosthogClient:
    def test_disabled_returns_none(self, mock_settings):
        mock_settings.enable_telemetry = False
        assert get_posthog_client(mock_settings) is None

    def test_no_key_returns_none(self, mock_settings):
        mock_settings.enable_telemetry = True
        mock_settings.posthog_api_key = None
        assert get_posthog_client(mock_settings) is None

    def test_creates_client(self, mock_settings):
        mock_settings.enable_telemetry = True
        mock_settings.posthog_api_key = "phc_test"
        mock_settings.posthog_host = "https://app.posthog.com"

        with patch("core.dependencies.Posthog") as mock_ph_cls:
            mock_client = Mock()
            mock_ph_cls.return_value = mock_client

            result = get_posthog_client(mock_settings)

            mock_ph_cls.assert_called_once_with(
                project_api_key="phc_test",
                host="https://app.posthog.com",
            )
            assert result is mock_client

    def test_cached_client(self, mock_settings):
        mock_client = Mock()
        deps_module._posthog_client = mock_client
        mock_settings.enable_telemetry = True
        mock_settings.posthog_api_key = "phc_test"

        result = get_posthog_client(mock_settings)
        assert result is mock_client


# ---------------------------------------------------------------------------
# flush_posthog / shutdown_posthog
# ---------------------------------------------------------------------------


class TestFlushPosthog:
    def test_flushes(self):
        mock_client = Mock()
        deps_module._posthog_client = mock_client
        flush_posthog()
        mock_client.flush.assert_called_once()

    def test_noop_when_none(self):
        deps_module._posthog_client = None
        flush_posthog()  # should not raise


class TestShutdownPosthog:
    def test_shuts_down(self):
        mock_client = Mock()
        deps_module._posthog_client = mock_client
        shutdown_posthog()
        mock_client.shutdown.assert_called_once()
        assert deps_module._posthog_client is None

    def test_noop_when_none(self):
        deps_module._posthog_client = None
        shutdown_posthog()  # should not raise
