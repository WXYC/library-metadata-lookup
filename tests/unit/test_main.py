"""Unit tests for main.py."""

from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient


class _FakeAcquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc_info):
        return None


class _FakeConnection:
    def __init__(self, fail_on: str | None = None):
        self.executed: list[str] = []
        self.fail_on = fail_on

    async def execute(self, sql: str, *args: object) -> str:
        self.executed.append(sql)
        if self.fail_on is not None and self.fail_on in sql:
            raise RuntimeError(f"injected failure on {self.fail_on}")
        return "OK"


class _FakePool:
    def __init__(self, fail_on: str | None = None):
        self.connection = _FakeConnection(fail_on=fail_on)
        self.acquire_count = 0

    def acquire(self):
        self.acquire_count += 1
        return _FakeAcquire(self.connection)


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

    @pytest.mark.asyncio
    async def test_lml_cache_bootstraps_degrade_independently(self, mock_settings):
        """A throw in an earlier ``lml_cache.*`` bootstrap must not skip the
        later ones — each degrades on its own, so the streaming catalog (last
        in line) still bootstraps when the streaming-URL cache (first) fails.

        The five ``set_up_*`` functions are patched at their source modules:
        the lifespan imports them lazily inside the guarded block, so the
        late ``from entity.X import set_up_Y`` binds the patched attribute.
        """
        import main
        from main import app, lifespan

        pool = _FakePool()
        with (
            patch.object(main.settings, "database_url_discogs", "postgresql://unit/test"),
            # bucket_mode is a property over these two fields; force local
            # mode so the lifespan's boot-fetch branch stays out of the way.
            patch.object(main.settings, "lml_bucket_name", None),
            patch.object(main.settings, "lml_bucket_endpoint", None),
            patch(
                "core.dependencies.get_discogs_pool",
                new_callable=AsyncMock,
                return_value=pool,
            ),
            patch(
                "entity.streaming_url_cache.set_up_streaming_url_cache_schema",
                new_callable=AsyncMock,
                side_effect=RuntimeError("streaming-URL bootstrap boom"),
            ) as mock_url_cache,
            patch(
                "entity.release_resolution_cache.set_up_release_resolution_cache_schema",
                new_callable=AsyncMock,
            ) as mock_resolution,
            patch(
                "entity.library_release_override.set_up_library_release_override_schema",
                new_callable=AsyncMock,
            ) as mock_override,
            patch(
                "entity.discogs_rate_bucket.set_up_discogs_rate_bucket_schema",
                new_callable=AsyncMock,
            ) as mock_rate_bucket,
            patch(
                "entity.streaming_catalog.set_up_streaming_catalog_schema",
                new_callable=AsyncMock,
            ) as mock_catalog,
            patch("main.shutdown_posthog"),
            patch("main.close_library_db", new_callable=AsyncMock),
            patch("main.close_discogs_service", new_callable=AsyncMock),
        ):
            async with lifespan(app):
                pass  # startup must not raise despite the first bootstrap failing

            assert pool.acquire_count == 1
            assert pool.connection.executed[:2] == [
                main._LML_CACHE_BOOTSTRAP_LOCK_TIMEOUT,
                main._LML_CACHE_BOOTSTRAP_ADVISORY_LOCK,
            ]
            assert pool.connection.executed[-2:] == [
                main._LML_CACHE_BOOTSTRAP_ADVISORY_UNLOCK,
                main._LML_CACHE_BOOTSTRAP_RESET_LOCK_TIMEOUT,
            ]
            mock_url_cache.assert_awaited_once()
            mock_resolution.assert_awaited_once()
            mock_override.assert_awaited_once()
            mock_rate_bucket.assert_awaited_once()
            mock_catalog.assert_awaited_once()
            locked_source = mock_url_cache.await_args.args[0]
            assert mock_resolution.await_args.args[0] is locked_source
            assert mock_override.await_args.args[0] is locked_source
            assert mock_rate_bucket.await_args.args[0] is locked_source
            assert mock_catalog.await_args.args[0] is locked_source

    @pytest.mark.asyncio
    async def test_lml_cache_bootstrap_lock_failure_is_nonfatal(self):
        """A bootstrap advisory-lock timeout skips caches, not the whole boot."""
        import main
        from entity.sources import PgSource

        pool = _FakePool(fail_on=main._LML_CACHE_BOOTSTRAP_ADVISORY_LOCK)
        bootstrap = AsyncMock()

        await main._run_lml_cache_bootstraps(
            PgSource(pool=pool),
            (("Streaming-URL cache", bootstrap),),
        )

        bootstrap.assert_not_awaited()
        assert pool.connection.executed == [
            main._LML_CACHE_BOOTSTRAP_LOCK_TIMEOUT,
            main._LML_CACHE_BOOTSTRAP_ADVISORY_LOCK,
            main._LML_CACHE_BOOTSTRAP_RESET_LOCK_TIMEOUT,
        ]


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

        # FastAPI >= 0.137 wraps include_router(...) calls in _IncludedRouter,
        # so the underlying APIRoute objects must be reached through
        # effective_route_contexts() rather than read off app.routes directly.
        routes = []
        for r in app.routes:
            if hasattr(r, "effective_route_contexts"):
                routes.extend(ctx.path for ctx in r.effective_route_contexts())
            elif hasattr(r, "path"):
                routes.append(r.path)
        assert "/health" in routes
        assert "/api/v1/lookup" in routes
        assert "/api/v1/library/search" in routes

    def test_app_metadata(self):
        from main import app

        assert app.title is not None
        assert app.version is not None
