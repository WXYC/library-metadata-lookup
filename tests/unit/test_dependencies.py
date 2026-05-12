"""Unit tests for core/dependencies.py."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

import core.dependencies as deps_module
from core.dependencies import (
    close_discogs_service,
    close_library_db,
    get_discogs_service,
    get_library_db,
    get_posthog_client,
)


@pytest.fixture(autouse=True)
def reset_globals():
    """Reset module-level singleton state between tests.

    The Discogs service + pool singletons live inside `async_singleton`
    closures (WXYC/library-metadata-lookup#283), so the only safe way to
    rebuild them between tests is via their public closers. We drive the
    coroutine with `asyncio.run` so this fixture stays sync (autouse async
    fixtures don't work for sync tests under pytest-asyncio strict mode).
    """
    deps_module._library_db = None
    asyncio.run(close_discogs_service())
    yield
    deps_module._library_db = None
    asyncio.run(close_discogs_service())


def test_no_module_level_asyncio_lock_in_dependencies():
    """LML#283: race-prone hand-rolled `asyncio.Lock()` plumbing must be
    gone from `core/dependencies.py`. The lock + double-check belongs
    inside `wxyc_fastapi.http.async_singleton`; reintroducing one at the
    module level (a) duplicates the contract and (b) means a future author
    could write a new lazy-init that re-races the FD-leak fix from #242.
    """
    src = Path(deps_module.__file__).read_text()
    assert "asyncio.Lock()" not in src, (
        "core/dependencies.py contains a module-level `asyncio.Lock()` "
        "instance — async singletons in this module must use "
        "`wxyc_fastapi.http.async_singleton(factory)` instead, which owns "
        "the lock and the double-check together. See WXYC/library-metadata-lookup#283 "
        "and WXYC/library-metadata-lookup#241."
    )


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

    @pytest.mark.asyncio
    async def test_missing_db_file_returns_unavailable_instance(self, mock_settings):
        """When library.db doesn't exist, return a LibraryDB that reports unavailable."""
        mock_settings.library_db_path = "/nonexistent/library.db"

        with patch("core.dependencies.LibraryDB") as mock_db_cls:
            mock_db = AsyncMock()
            mock_db.is_available = AsyncMock(return_value=False)
            mock_db.connect = AsyncMock(side_effect=FileNotFoundError("not found"))
            mock_db_cls.return_value = mock_db

            result = await get_library_db(mock_settings)

            assert result is mock_db
            assert await result.is_available() is False

    @pytest.mark.asyncio
    async def test_missing_db_file_allows_reconnect_after_upload(self, mock_settings):
        """After close_library_db(), next call re-initializes from scratch."""
        mock_settings.library_db_path = "/nonexistent/library.db"

        with patch("core.dependencies.LibraryDB") as mock_db_cls:
            # First call: file missing
            mock_db_missing = AsyncMock()
            mock_db_missing.is_available = AsyncMock(return_value=False)
            mock_db_missing.connect = AsyncMock(side_effect=FileNotFoundError("not found"))

            # Second call: file exists
            mock_db_ok = AsyncMock()
            mock_db_ok.is_available = AsyncMock(return_value=True)
            mock_db_ok.connect = AsyncMock()

            mock_db_cls.side_effect = [mock_db_missing, mock_db_ok]

            result1 = await get_library_db(mock_settings)
            assert await result1.is_available() is False

            await close_library_db()

            result2 = await get_library_db(mock_settings)
            assert await result2.is_available() is True


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
    """The Discogs service factory loads its config from `get_settings()`
    inside the `async_singleton`-wrapped factory (WXYC/library-metadata-lookup#283),
    so each test patches `core.dependencies.get_settings` to inject the
    desired settings shape rather than relying on the dropped per-call
    `settings` argument.
    """

    @pytest.mark.asyncio
    async def test_no_credentials_returns_none(self, mock_settings):
        mock_settings.discogs_token = None
        mock_settings.discogs_api_key = None
        mock_settings.discogs_api_secret = None
        with patch("core.dependencies.get_settings", return_value=mock_settings):
            result = await get_discogs_service(mock_settings)
        assert result is None

    @pytest.mark.asyncio
    async def test_partial_key_secret_treated_as_no_credentials(self, mock_settings):
        # Only one of key/secret is set — not enough to authenticate.
        mock_settings.discogs_token = None
        mock_settings.discogs_api_key = "only-key"
        mock_settings.discogs_api_secret = None
        with patch("core.dependencies.get_settings", return_value=mock_settings):
            result = await get_discogs_service(mock_settings)
        assert result is None

    @pytest.mark.asyncio
    async def test_creates_service_with_token(self, mock_settings):
        mock_settings.discogs_token = "test-token"
        mock_settings.database_url_discogs = None

        with (
            patch("core.dependencies.get_settings", return_value=mock_settings),
            patch("core.dependencies.DiscogsService") as mock_svc_cls,
        ):
            mock_svc = AsyncMock()
            mock_svc_cls.return_value = mock_svc

            result = await get_discogs_service(mock_settings)

            mock_svc_cls.assert_called_once_with(token="test-token", cache_service=None)
            assert result is mock_svc

    @pytest.mark.asyncio
    async def test_creates_service_with_key_secret(self, mock_settings):
        mock_settings.discogs_token = None
        mock_settings.discogs_api_key = "my-key"
        mock_settings.discogs_api_secret = "my-secret"
        mock_settings.database_url_discogs = None

        with (
            patch("core.dependencies.get_settings", return_value=mock_settings),
            patch("core.dependencies.DiscogsService") as mock_svc_cls,
        ):
            mock_svc = AsyncMock()
            mock_svc_cls.return_value = mock_svc

            result = await get_discogs_service(mock_settings)

            mock_svc_cls.assert_called_once_with(
                api_key="my-key", api_secret="my-secret", cache_service=None
            )
            assert result is mock_svc

    @pytest.mark.asyncio
    async def test_token_takes_precedence_over_key_secret(self, mock_settings):
        mock_settings.discogs_token = "test-token"
        mock_settings.discogs_api_key = "my-key"
        mock_settings.discogs_api_secret = "my-secret"
        mock_settings.database_url_discogs = None

        with (
            patch("core.dependencies.get_settings", return_value=mock_settings),
            patch("core.dependencies.DiscogsService") as mock_svc_cls,
        ):
            mock_svc_cls.return_value = AsyncMock()
            await get_discogs_service(mock_settings)
            # Token wins; key/secret are not passed.
            mock_svc_cls.assert_called_once_with(token="test-token", cache_service=None)

    @pytest.mark.asyncio
    async def test_creates_pool_with_database_url(self, mock_settings):
        mock_settings.discogs_token = "test-token"
        mock_settings.database_url_discogs = "postgresql://localhost/test"

        mock_pool = AsyncMock()

        with (
            patch("core.dependencies.get_settings", return_value=mock_settings),
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
            mock_svc_cls.assert_called_once_with(token="test-token", cache_service=mock_cache)

    @pytest.mark.asyncio
    async def test_pool_error_degrades_gracefully(self, mock_settings):
        mock_settings.discogs_token = "test-token"
        mock_settings.database_url_discogs = "postgresql://localhost/test"

        with (
            patch("core.dependencies.get_settings", return_value=mock_settings),
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
            mock_svc_cls.assert_called_once_with(token="test-token", cache_service=None)

    @pytest.mark.asyncio
    async def test_cached_instance(self, mock_settings):
        """A second call returns the same singleton without re-running the factory."""
        mock_settings.discogs_token = "test-token"
        mock_settings.database_url_discogs = None

        with (
            patch("core.dependencies.get_settings", return_value=mock_settings),
            patch("core.dependencies.DiscogsService") as mock_svc_cls,
        ):
            mock_svc_cls.return_value = AsyncMock()

            first = await get_discogs_service(mock_settings)
            second = await get_discogs_service(mock_settings)

            assert first is second
            mock_svc_cls.assert_called_once()


# ---------------------------------------------------------------------------
# close_discogs_service
# ---------------------------------------------------------------------------


class TestCloseDiscogsService:
    @pytest.mark.asyncio
    async def test_closes_service_and_pool(self, mock_settings):
        """The closer must tear down both the service (which holds the
        outbound HTTP client) and the cache pool, in that order.

        The mocks use `spec=...` to restrict their attribute surface to the
        real classes' methods so `async_singleton`'s closer dispatches to
        the actual teardown method (`DiscogsService.close` /
        `asyncpg.Pool.close`) rather than to AsyncMock's auto-created
        `aclose`. The dispatch order matters: a real `DiscogsService` has
        only `close()`, and a real `asyncpg.Pool` has only `close()`.
        """
        from discogs.service import DiscogsService

        mock_settings.discogs_token = "test-token"
        mock_settings.database_url_discogs = "postgresql://localhost/test"

        # `spec=DiscogsService` ensures `hasattr(svc, "aclose")` is False so
        # the singleton's closer falls through to `close()`, matching prod.
        mock_svc = AsyncMock(spec=DiscogsService)

        # asyncpg.Pool's runtime class is private; a plain AsyncMock with
        # `aclose` explicitly removed is sufficient and matches the public
        # surface (`close()` returning a coroutine).
        mock_pool = AsyncMock()
        del mock_pool.aclose

        with (
            patch("core.dependencies.get_settings", return_value=mock_settings),
            patch(
                "core.dependencies.asyncpg.create_pool",
                new_callable=AsyncMock,
                return_value=mock_pool,
            ),
            patch("core.dependencies.DiscogsService", return_value=mock_svc),
            patch("core.dependencies.DiscogsCacheService"),
        ):
            await get_discogs_service(mock_settings)

            await close_discogs_service()

            mock_svc.close.assert_called_once()
            mock_pool.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_noop_when_none(self):
        await close_discogs_service()  # should not raise


# ---------------------------------------------------------------------------
# get_posthog_client (LML-side gating)
# ---------------------------------------------------------------------------


class TestGetPosthogClient:
    """LML adds an `enable_telemetry` short-circuit on top of the wxyc-fastapi singleton."""

    def test_short_circuits_when_telemetry_disabled(self, mock_settings):
        mock_settings.enable_telemetry = False
        with patch("core.dependencies._shared_posthog_client") as mock_shared:
            assert get_posthog_client(mock_settings) is None
        mock_shared.assert_not_called()

    def test_delegates_with_lookup_event_prefix(self, mock_settings):
        mock_settings.enable_telemetry = True
        with patch("core.dependencies._shared_posthog_client") as mock_shared:
            mock_shared.return_value = Mock()
            client = get_posthog_client(mock_settings)
        assert client is mock_shared.return_value
        mock_shared.assert_called_once_with(event_prefix="lookup")
