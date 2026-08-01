"""Unit tests for identity/dependencies.py — get_entity_store probe behaviour."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import asyncpg
import pytest
from fastapi import Depends, FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

import core.dependencies as core_deps
import identity.dependencies as deps
from config.settings import Settings


@pytest.fixture(autouse=True)
def _reset_dep_state():
    """Ensure each test sees a fresh dependency cache."""
    deps._entity_store = None
    deps._entity_probe_failed = False
    yield
    deps._entity_store = None
    deps._entity_probe_failed = False


@pytest.fixture
def _reset_discogs_pool():
    """Tear down the core-dependencies discogs pool around tests that exercise it."""

    async def _run():
        await core_deps.close_discogs_pool()

    asyncio.run(_run())
    yield
    asyncio.run(_run())


@pytest.fixture
def _fake_discogs_pool(monkeypatch, _reset_discogs_pool):
    """Stub ``asyncpg.create_pool`` so ``get_discogs_pool`` returns a fake.

    Post-WXYC#395 the entity store reaches into ``core.dependencies.get_discogs_pool``
    instead of constructing its own pool. Tests that exercise the probe
    therefore need the shared pool factory to succeed; otherwise the entity
    store short-circuits to ``None`` before the probe ever runs.
    """
    fake_pool = AsyncMock(name="shared-discogs-pool")
    fake_pool.fetchval = AsyncMock(return_value=1)

    async def fake_create_pool(*args, **kwargs):
        return fake_pool

    monkeypatch.setattr("core.dependencies.asyncpg.create_pool", fake_create_pool)
    return fake_pool


def _settings(dsn: str | None) -> Settings:
    return Settings(
        discogs_token=None,
        database_url_discogs=dsn,
        sentry_dsn=None,
        posthog_api_key=None,
        enable_telemetry=False,
        library_db_path="test_library.db",
    )


@pytest.mark.asyncio
async def test_returns_none_when_dsn_unset():
    assert await deps.get_entity_store(_settings(None)) is None


@pytest.mark.asyncio
async def test_returns_none_when_probe_raises_undefined_table(monkeypatch, _fake_discogs_pool):
    """Probe failure (entity schema not applied) → store disabled."""

    async def boom(self, query, *args):
        raise asyncpg.UndefinedTableError('relation "entity.identity" does not exist')

    monkeypatch.setattr("entity.sources.PgSource.fetchone", boom)
    settings = _settings("postgresql://x:y@127.0.0.1:1/z")
    monkeypatch.setattr("core.dependencies.get_settings", lambda: settings)

    result = await deps.get_entity_store(settings)
    assert result is None


@pytest.mark.asyncio
async def test_returns_none_when_probe_raises_oserror(monkeypatch, _fake_discogs_pool):
    """Probe failure (PG host unreachable) → store disabled."""

    async def boom(self, query, *args):
        raise OSError("connection refused")

    monkeypatch.setattr("entity.sources.PgSource.fetchone", boom)
    settings = _settings("postgresql://x:y@127.0.0.1:1/z")
    monkeypatch.setattr("core.dependencies.get_settings", lambda: settings)

    result = await deps.get_entity_store(settings)
    assert result is None


@pytest.mark.asyncio
async def test_probe_failure_is_cached(monkeypatch, _fake_discogs_pool):
    """A failed probe is not retried on subsequent calls (process-lifetime cache)."""
    call_count = 0

    async def boom(self, query, *args):
        nonlocal call_count
        call_count += 1
        raise asyncpg.UndefinedTableError("nope")

    monkeypatch.setattr("entity.sources.PgSource.fetchone", boom)
    settings = _settings("postgresql://x:y@127.0.0.1:1/z")
    monkeypatch.setattr("core.dependencies.get_settings", lambda: settings)

    assert await deps.get_entity_store(settings) is None
    assert await deps.get_entity_store(settings) is None
    assert await deps.get_entity_store(settings) is None
    assert call_count == 1


@pytest.mark.asyncio
async def test_returns_store_when_probe_succeeds(monkeypatch, _fake_discogs_pool):
    async def ok(self, query, *args):
        return None  # SELECT ... LIMIT 0 returns no rows

    monkeypatch.setattr("entity.sources.PgSource.fetchone", ok)
    settings = _settings("postgresql://x:y@127.0.0.1:1/z")
    monkeypatch.setattr("core.dependencies.get_settings", lambda: settings)

    result = await deps.get_entity_store(settings)
    assert result is not None


# ---------------------------------------------------------------------------
# Consolidation contract (WXYC/library-metadata-lookup#395): the entity store
# must draw its discogs-cache connection from the same pool-provider seam as
# the Discogs cache service. Two pools on one DSN contend for the same
# backend connection budget and surface different degradation signals — see
# the issue body for the audit detail.
# ---------------------------------------------------------------------------


class TestSinglePoolContract:
    """The entity store and the Discogs cache service must share one
    ``asyncpg.Pool`` for ``DATABASE_URL_DISCOGS``.
    """

    @pytest.mark.asyncio
    async def test_entity_store_and_discogs_cache_share_one_pool(
        self, monkeypatch, _reset_discogs_pool
    ):
        """``get_entity_store`` and ``get_discogs_service`` must resolve to the
        same underlying asyncpg pool object so there is one connection-budget
        and one degradation signal for the discogs-cache DB.
        """
        settings = Settings(
            discogs_token="test-token",
            database_url_discogs="postgresql://x:y@127.0.0.1:1/z",
            sentry_dsn=None,
            posthog_api_key=None,
            enable_telemetry=False,
            library_db_path="test_library.db",
        )

        # One pool fake covers both call sites. `fetchval("SELECT 1")` is the
        # cache-service health probe; the entity-store probe runs via the
        # `PgSource.fetchone` monkeypatch below — both end up grabbing the
        # same pool from `get_discogs_pool`.
        fake_pool = AsyncMock(name="shared-discogs-pool")
        fake_pool.fetchval = AsyncMock(return_value=1)

        async def fake_create_pool(*args, **kwargs):
            return fake_pool

        async def probe_ok(self, query, *args):
            return None  # SELECT ... LIMIT 0

        monkeypatch.setattr("entity.sources.PgSource.fetchone", probe_ok)

        with (
            patch("core.dependencies.get_settings", return_value=settings),
            patch("core.dependencies.asyncpg.create_pool", side_effect=fake_create_pool),
        ):
            store = await deps.get_entity_store(settings)
            service = await core_deps.get_discogs_service(settings)

        assert store is not None
        assert service is not None
        assert service.cache_service is not None
        # `store._pg` wraps a PgSource that should reference the same pool the
        # cache service got. The PgSource keeps the pool on `_pool` so it can
        # be acquired for queries; cache_service stores it on `pool`.
        assert store._pg._pool is service.cache_service.pool is fake_pool

    @pytest.mark.asyncio
    async def test_get_discogs_pool_is_public(self):
        """`core.dependencies.get_discogs_pool` must be a public, awaitable
        getter. The entity-store seam imports it by this name (WXYC#395);
        renaming or unpublishing it would silently break the consolidation.
        """
        from core import dependencies

        assert hasattr(dependencies, "get_discogs_pool"), (
            "core.dependencies.get_discogs_pool must be public — it is the "
            "single seam the entity-store reaches into for its discogs-cache "
            "pool (WXYC/library-metadata-lookup#395)."
        )
        assert callable(dependencies.get_discogs_pool)


class TestEntityStoreLazyInitRace:
    """Concurrent first-callers to ``get_entity_store`` must produce exactly
    one probe call. The pre-consolidation code constructed a fresh
    ``PgSource`` per racing caller, each of which ran the probe — orphaning
    everything but the last writer. Lock-guarded init closes that window.
    """

    @pytest.mark.asyncio
    async def test_concurrent_get_entity_store_probes_once(self, monkeypatch, _reset_discogs_pool):
        settings = Settings(
            discogs_token="test-token",
            database_url_discogs="postgresql://x:y@127.0.0.1:1/z",
            sentry_dsn=None,
            posthog_api_key=None,
            enable_telemetry=False,
            library_db_path="test_library.db",
        )

        fake_pool = AsyncMock(name="shared-discogs-pool")
        fake_pool.fetchval = AsyncMock(return_value=1)

        async def fake_create_pool(*args, **kwargs):
            return fake_pool

        probe_calls = 0
        release_event = asyncio.Event()

        async def probe_ok(self, query, *args):
            nonlocal probe_calls
            probe_calls += 1
            # Yield long enough for every concurrent caller to reach the
            # probe await before any of them assigns the cached store.
            await release_event.wait()
            return None

        monkeypatch.setattr("entity.sources.PgSource.fetchone", probe_ok)

        with (
            patch("core.dependencies.get_settings", return_value=settings),
            patch("core.dependencies.asyncpg.create_pool", side_effect=fake_create_pool),
        ):
            tasks = [asyncio.create_task(deps.get_entity_store(settings)) for _ in range(8)]
            for _ in range(4):
                await asyncio.sleep(0)
            release_event.set()
            results = await asyncio.gather(*tasks)

        assert probe_calls == 1, (
            f"PgSource.fetchone probe ran {probe_calls} times for 8 "
            "concurrent first-callers — the lazy init must be lock-guarded "
            "so only one caller drives the probe (WXYC/library-metadata-lookup#395 / #357)."
        )
        # All callers must see the same EntityStore instance.
        assert len({id(r) for r in results}) == 1


@pytest.mark.asyncio
async def test_returns_none_when_probe_raises_interface_error(
    monkeypatch, _fake_discogs_pool, caplog
):
    """LML#767: asyncpg's client-side InterfaceError (pool/connection
    lifecycle) is classified as a transient DB-unreachable probe failure —
    the same arm as PostgresError/OSError, distinguished from the generic
    catch-all by the "probe failed; disabling identity routes" log line."""
    import logging

    async def boom(self, query, *args):
        raise asyncpg.exceptions.InterfaceError("pool is closing")

    monkeypatch.setattr("entity.sources.PgSource.fetchone", boom)
    settings = _settings("postgresql://x:y@127.0.0.1:1/z")
    monkeypatch.setattr("core.dependencies.get_settings", lambda: settings)

    with caplog.at_level(logging.WARNING, logger="identity.dependencies"):
        result = await deps.get_entity_store(settings)

    assert result is None
    assert deps._entity_probe_failed is True
    assert any("probe failed; disabling identity routes" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# require_service (LML#1036) — generic sibling of require_entity_store
# ---------------------------------------------------------------------------


async def _fake_getter() -> str | None:
    """Stand-in FastAPI getter dependency for `require_service` unit tests."""
    return None


class TestRequireService:
    @pytest.mark.asyncio
    async def test_passes_a_resolved_service_through(self):
        """A non-None value reaches the caller unchanged — the getter is
        never consulted directly, since the value is supplied to the guard's
        own `service` parameter, exactly as FastAPI's Depends() resolution
        would supply it."""
        guard = deps.require_service(_fake_getter, "irrelevant detail")
        sentinel = object()
        assert await guard(service=sentinel) is sentinel

    @pytest.mark.asyncio
    async def test_raises_503_with_the_exact_detail_when_the_getter_yields_none(self):
        detail = "Widget service is not available. Set WIDGET_TOKEN."
        guard = deps.require_service(_fake_getter, detail)
        with pytest.raises(HTTPException) as exc_info:
            await guard(service=None)
        assert exc_info.value.status_code == 503
        assert exc_info.value.detail == detail

    @pytest.mark.asyncio
    async def test_wired_as_a_route_dependency_resolves_the_getter_via_depends(self):
        """The guard's default parameter is `Depends(getter)`, not a direct
        call to `getter()` — so when mounted on a route, FastAPI's own
        dependency-injection resolves it (and `app.dependency_overrides`
        reaches it), matching how `discogs/router.py` and `cache/router.py`
        wire it in (``Depends(require_service(get_discogs_service, ...))``)."""

        async def _unavailable_getter() -> str | None:
            return None

        async def _available_getter() -> str | None:
            return "widget-service"

        detail = "Widget service is not available."
        unavailable_guard = deps.require_service(_unavailable_getter, detail)
        available_guard = deps.require_service(_available_getter, detail)

        app = FastAPI()

        @app.get("/unavailable")
        async def unavailable_probe(service: str = Depends(unavailable_guard)) -> dict:
            return {"service": service}

        @app.get("/available")
        async def available_probe(service: str = Depends(available_guard)) -> dict:
            return {"service": service}

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            unavailable_resp = await client.get("/unavailable")
            available_resp = await client.get("/available")

        assert unavailable_resp.status_code == 503
        assert unavailable_resp.json()["detail"] == detail
        assert available_resp.status_code == 200
        assert available_resp.json() == {"service": "widget-service"}
