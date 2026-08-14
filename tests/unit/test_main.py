"""Unit tests for main.py."""

from unittest.mock import AsyncMock, patch

import pytest


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
    async def test_artist_wikipedia_bio_bootstrap_runs_under_the_lock(self, mock_settings):
        """LML#513/#1192 Phase B: ``set_up_artist_wikipedia_bio_schema`` must
        be a registered ``bootstraps`` entry, run under the same
        session-scoped advisory lock every other ``lml_cache.*`` bootstrap
        gets (the same ``locked_source`` every sibling call receives)."""
        import main
        from main import app, lifespan

        pool = _FakePool()
        with (
            patch.object(main.settings, "database_url_discogs", "postgresql://unit/test"),
            patch.object(main.settings, "lml_bucket_name", None),
            patch.object(main.settings, "lml_bucket_endpoint", None),
            patch(
                "core.dependencies.get_discogs_pool",
                new_callable=AsyncMock,
                return_value=pool,
            ),
            patch(
                "entity.artist_wikipedia_bio.set_up_artist_wikipedia_bio_schema",
                new_callable=AsyncMock,
            ) as mock_wikipedia_bio,
            patch(
                "entity.streaming_url_cache.set_up_streaming_url_cache_schema",
                new_callable=AsyncMock,
            ) as mock_url_cache,
            patch("main.shutdown_posthog"),
            patch("main.close_library_db", new_callable=AsyncMock),
            patch("main.close_discogs_service", new_callable=AsyncMock),
        ):
            async with lifespan(app):
                pass

            mock_wikipedia_bio.assert_awaited_once()
            mock_url_cache.assert_awaited_once()
            assert mock_wikipedia_bio.await_args.args[0] is mock_url_cache.await_args.args[0]

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

    @pytest.mark.asyncio
    async def test_lml_cache_bootstrap_connection_acquire_failure_is_nonfatal(self):
        """A pooled-connection checkout failure degrades, it does not crash boot.

        The lifespan calls this helper without its own try/except, so a
        connection acquisition that raises (checkout timeout, dead connection on
        a degraded discogs-cache PG) must be swallowed here or it aborts startup
        and crash-loops the container — the opposite of the #706 hardening.
        """
        import main
        from entity.sources import PgSource

        class _FailingAcquire:
            async def __aenter__(self):
                raise RuntimeError("pool checkout failed")

            async def __aexit__(self, *exc_info):
                return None

        class _FailingPool:
            def acquire(self):
                return _FailingAcquire()

        bootstrap = AsyncMock()

        # Must not raise.
        await main._run_lml_cache_bootstraps(
            PgSource(pool=_FailingPool()),
            (("Streaming-URL cache", bootstrap),),
        )

        bootstrap.assert_not_awaited()


class TestMiddleware:
    def test_no_per_request_posthog_flush_middleware(self, mock_settings):
        """The request path must not synchronously flush PostHog (LML#881).

        ``posthog_flush_middleware`` used to call ``flush_posthog()`` on every response.
        ``flush_posthog()`` is ``Posthog.flush()`` == ``queue.join()`` — a blocking wait on
        the asyncio event loop until the background consumer drains every queued event, so a
        PostHog slowdown stalled the loop for tens of seconds once per in-flight request,
        serializing the whole service behind PostHog delivery. Delivery is instead covered by
        the consumer thread's periodic flush + the lifespan ``shutdown_posthog()`` + posthog's
        ``atexit`` join, so the per-request flush is removed. Regression guard: no HTTP
        middleware named ``posthog_flush_middleware`` may be registered on the app.
        """
        from main import app

        dispatch_names = []
        for mw in app.user_middleware:
            # ``@app.middleware("http")`` registers ``BaseHTTPMiddleware(dispatch=<func>)``;
            # Starlette exposes the function in ``.kwargs`` (newer) or positionally in
            # ``.args`` (older), so check both.
            dispatch = getattr(mw, "kwargs", {}).get("dispatch")
            if dispatch is None:
                dispatch = next((a for a in getattr(mw, "args", ()) if callable(a)), None)
            if dispatch is not None:
                dispatch_names.append(getattr(dispatch, "__name__", ""))

        assert "posthog_flush_middleware" not in dispatch_names, (
            "posthog_flush_middleware must be removed — it blocks the event loop with a "
            "synchronous queue.join() on every request (LML#881)"
        )

    @pytest.mark.asyncio
    async def test_request_path_never_calls_flush_posthog(self, mock_settings):
        """Behavioral companion to the structural guard above (LML#949).

        The structural test only proves ``posthog_flush_middleware`` isn't registered by
        name; it would stay green even if a future change called ``flush_posthog()`` from
        somewhere else on the request path (a dependency, a route handler, a different
        middleware). This test drives an actual request through the ASGI app end to end and
        asserts ``flush_posthog`` is never invoked while handling it — ``main`` no longer
        imports ``flush_posthog`` at all post-fix, so the patch target is created on the fly
        (``create=True``); if a regression reintroduces the import and a call, this starts
        patching the real bound name and the call would be caught the same way the deleted
        ``test_posthog_flush_middleware`` used to assert the opposite.
        """
        from httpx import ASGITransport, AsyncClient

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
            with patch("main.flush_posthog", create=True) as mock_flush:
                async with AsyncClient(
                    transport=ASGITransport(app=app), base_url="http://test"
                ) as client:
                    response = await client.get("/health")

            assert response.status_code == 200
            mock_flush.assert_not_called()
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
