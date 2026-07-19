"""Unit tests for core/dependencies.py."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

import core.dependencies as deps_module
from core.dependencies import (
    close_discogs_service,
    close_library_db,
    close_musicbrainz_pg,
    get_discogs_service,
    get_library_db,
    get_posthog_client,
    get_streaming_posthog_client,
)


@pytest.fixture(autouse=True)
def reset_globals():
    """Reset module-level singleton state between tests.

    The Discogs service + pool + musicbrainz_pg singletons live inside
    `async_singleton` closures (#283, #435), so the only safe way to
    rebuild them between tests is via their public closers. We drive the
    coroutine with `asyncio.run` so this fixture stays sync (autouse async
    fixtures don't work for sync tests under pytest-asyncio strict mode).
    """
    deps_module._library_db = None
    deps_module._object_store = None
    asyncio.run(close_discogs_service())
    asyncio.run(close_musicbrainz_pg())
    yield
    deps_module._library_db = None
    deps_module._object_store = None
    asyncio.run(close_discogs_service())
    asyncio.run(close_musicbrainz_pg())


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


def test_no_global_musicbrainz_pg_in_dependencies():
    """LML#435 (LML#357 audit follow-up): `get_musicbrainz_pg` must not use
    the `global _musicbrainz_pg` + None-check + assign lazy-init pattern.

    The pre-fix shape has a zero-width TOCTOU race today (sync `PgSource()`
    constructor — no await between the None-check and the assignment), but
    that race reactivates the moment anyone adds an await between them —
    exactly the failure mode `get_entity_store` had pre-#395, where a probe
    await opened a real race window and orphaned a PgSource per cold-start
    burst. Pin the migration to `async_singleton` so re-introducing the
    racy shape fails this test.
    """
    src = Path(deps_module.__file__).read_text()
    assert "global _musicbrainz_pg" not in src, (
        "core/dependencies.py uses `global _musicbrainz_pg` — the lock-free "
        "lazy-init pattern that LML#357's audit flagged as racy. Migrate to "
        "`async_singleton(_build_musicbrainz_pg)` (mirroring the Discogs "
        "pool + Apple Music client pattern in the same file). See LML#435."
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
# get_object_store (WXYC/library-metadata-lookup#835)
# ---------------------------------------------------------------------------


class TestGetObjectStore:
    """Storage-backend selection: bucket mode iff BOTH bucket vars are set.

    PR 1 of the volume-eviction epic wires the singleton; nothing calls it yet.
    """

    @pytest.mark.asyncio
    async def test_local_mode_when_bucket_vars_unset(self, mock_settings):
        from storage.object_store import LocalDirStore

        mock_settings.lml_bucket_name = None
        mock_settings.lml_bucket_endpoint = None
        mock_settings.library_db_path = Path("/data/library.db")

        store = await deps_module.get_object_store(mock_settings)

        assert isinstance(store, LocalDirStore)
        # Local keys resolve next to library.db, so today's on-disk siblings are
        # reachable by bare filename (library.db, streaming_availability.db).
        assert store.base_dir == Path("/data")

    @pytest.mark.asyncio
    async def test_bucket_mode_when_both_vars_set(self, mock_settings):
        from storage.object_store import S3ObjectStore

        mock_settings.lml_bucket_name = "lml-prod-data"
        mock_settings.lml_bucket_endpoint = "https://s3.example.com"

        store = await deps_module.get_object_store(mock_settings)

        assert isinstance(store, S3ObjectStore)
        assert store.bucket == "lml-prod-data"

    @pytest.mark.asyncio
    async def test_bucket_mode_threads_addressing_style(self, mock_settings):
        """The configured LML_BUCKET_ADDRESSING_STYLE reaches the S3 client (LML#834),
        so an operator can select path-style for a legacy bucket from Railway."""
        mock_settings.lml_bucket_name = "lml-prod-data"
        mock_settings.lml_bucket_endpoint = "https://s3.example.com"
        mock_settings.lml_bucket_addressing_style = "path"

        store = await deps_module.get_object_store(mock_settings)

        assert store._client.meta.config.s3["addressing_style"] == "path"

    @pytest.mark.asyncio
    async def test_bucket_mode_defaults_to_virtual_addressing(self, mock_settings):
        """With the default setting, the built client uses virtual-hosted style."""
        mock_settings.lml_bucket_name = "lml-prod-data"
        mock_settings.lml_bucket_endpoint = "https://s3.example.com"

        store = await deps_module.get_object_store(mock_settings)

        assert store._client.meta.config.s3["addressing_style"] == "virtual"

    @pytest.mark.asyncio
    async def test_only_name_set_is_local_mode(self, mock_settings):
        from storage.object_store import LocalDirStore

        mock_settings.lml_bucket_name = "lml-prod-data"
        mock_settings.lml_bucket_endpoint = None

        assert isinstance(await deps_module.get_object_store(mock_settings), LocalDirStore)

    @pytest.mark.asyncio
    async def test_only_endpoint_set_is_local_mode(self, mock_settings):
        from storage.object_store import LocalDirStore

        mock_settings.lml_bucket_name = None
        mock_settings.lml_bucket_endpoint = "https://s3.example.com"

        assert isinstance(await deps_module.get_object_store(mock_settings), LocalDirStore)

    @pytest.mark.asyncio
    async def test_singleton_is_cached(self, mock_settings):
        mock_settings.lml_bucket_name = None
        mock_settings.lml_bucket_endpoint = None

        first = await deps_module.get_object_store(mock_settings)
        second = await deps_module.get_object_store(mock_settings)

        assert first is second


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
    async def test_pool_max_size_defaults_to_5(self, mock_settings, monkeypatch):
        """With LML_DISCOGS_POOL_MAX_SIZE unset the hot-path pool stays at its
        historical 5-connection size — the env knob is inert by default so
        merging the LML#706 sizing lever changes nothing until an operator
        tunes it on staging.

        Exercises the ``_build_discogs_pool`` factory directly rather than
        ``get_discogs_service`` so it never enters the ``async_singleton``
        getter, whose import-time ``asyncio.Lock`` binds to the first loop that
        acquires it and then breaks cross-loop suite ordering.
        """
        monkeypatch.delenv("LML_DISCOGS_POOL_MAX_SIZE", raising=False)
        mock_settings.database_url_discogs = "postgresql://localhost/test"

        with (
            patch("core.dependencies.get_settings", return_value=mock_settings),
            patch("core.dependencies.asyncpg.create_pool", new_callable=AsyncMock) as mock_create,
        ):
            mock_create.return_value = AsyncMock()
            await deps_module._build_discogs_pool()

            mock_create.assert_called_once()
            assert mock_create.call_args.kwargs["max_size"] == 5

    @pytest.mark.asyncio
    async def test_pool_max_size_from_env(self, mock_settings, monkeypatch):
        """LML_DISCOGS_POOL_MAX_SIZE raises the hot-path pool ceiling so the
        in-flight cap (default 8) stops fighting a smaller pool — the LML#706
        loop/pool-starvation lever. Read via the shared resolve_positive_int_env
        helper, so it flips from Railway without a redeploy."""
        monkeypatch.setenv("LML_DISCOGS_POOL_MAX_SIZE", "15")
        mock_settings.database_url_discogs = "postgresql://localhost/test"

        with (
            patch("core.dependencies.get_settings", return_value=mock_settings),
            patch("core.dependencies.asyncpg.create_pool", new_callable=AsyncMock) as mock_create,
        ):
            mock_create.return_value = AsyncMock()
            await deps_module._build_discogs_pool()

            mock_create.assert_called_once()
            assert mock_create.call_args.kwargs["max_size"] == 15

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


class TestGetStreamingPosthogClient:
    """The streaming-check endpoint gets its own per-caller prefix (LML#659).

    Reusing the hardcoded ``"lookup"`` prefix mis-attributed the missing-key
    warning to ``caller=lookup`` and, because the upstream singleton warns once
    per prefix, suppressed the streaming-check warning entirely.
    """

    def test_short_circuits_when_telemetry_disabled(self, mock_settings):
        mock_settings.enable_telemetry = False
        with patch("core.dependencies._shared_posthog_client") as mock_shared:
            assert get_streaming_posthog_client(mock_settings) is None
        mock_shared.assert_not_called()

    def test_delegates_with_streaming_check_event_prefix(self, mock_settings):
        mock_settings.enable_telemetry = True
        with patch("core.dependencies._shared_posthog_client") as mock_shared:
            mock_shared.return_value = Mock()
            client = get_streaming_posthog_client(mock_settings)
        assert client is mock_shared.return_value
        mock_shared.assert_called_once_with(event_prefix="streaming_check")

    def test_has_distinct_identity_from_lookup_dep(self):
        """FastAPI ``dependency_overrides`` are keyed by callable identity, so the
        two per-caller deps must be distinct objects to be overridable apart."""
        assert get_streaming_posthog_client is not get_posthog_client


class TestPerCallerMissingKeyWarning:
    """End-to-end attribution against the real wxyc-fastapi singleton (LML#659).

    With telemetry enabled but ``POSTHOG_API_KEY`` unset, each distinct caller
    must log its own one-time missing-key warning under its own ``caller=``
    label — the lookup warning must not suppress the streaming-check one.
    """

    def test_each_caller_warns_under_its_own_label(self, mock_settings, caplog):
        import wxyc_fastapi.observability.posthog as upstream

        mock_settings.enable_telemetry = True  # POSTHOG_API_KEY already "" via fixture
        upstream._warned_prefixes.clear()
        try:
            with caplog.at_level("WARNING", logger=upstream.logger.name):
                assert get_posthog_client(mock_settings) is None
                assert get_streaming_posthog_client(mock_settings) is None
        finally:
            upstream._warned_prefixes.clear()

        warnings = [r.getMessage() for r in caplog.records if "POSTHOG_API_KEY" in r.getMessage()]
        assert any("caller=lookup" in m for m in warnings)
        assert any("caller=streaming_check" in m for m in warnings)
