"""Regression tests for issue #241 — FD exhaustion on LML prod 2026-05-01.

Around 21:44 UTC the production container hit ``OSError: [Errno 24] Too many
open files`` and stopped accepting connections, taking down /api/v1/lookup
and the iOS request feature. The recovery was a no-op redeploy that swapped
the container; the leak source survives until something changes.

These tests encode the two prime hypotheses identified during incident
investigation as concrete contracts the code should satisfy:

1. The artwork-enrichment hot path must NOT construct ``httpx.AsyncClient``
   instances. Every outbound client in the service (Discogs, Spotify,
   Deezer, Bandcamp, AppleMusicClient) is a process-lifetime singleton with
   explicit ``close()`` on shutdown. The legacy ``_fetch_apple_music_url``
   used to construct a fresh client per call — that was removed in LML#241
   and the function itself in LML#444; the orchestrator no longer imports
   ``httpx`` at all.

2. Two lazy-init asyncpg pools have a TOCTOU race:
   ``entity/sources.py:PgSource._get_pool`` and
   ``core/dependencies.py:get_discogs_service``. Both check ``self._pool is
   None`` then ``await asyncpg.create_pool(...)`` without a lock. Concurrent
   first-callers each pass the check, each create a pool, and all but one
   pool is orphaned with no reference to ``close()`` it. Each orphaned pool
   holds up to 5 connections (FDs).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest


class TestOrchestratorOwnsNoHttpxClients:
    """Hypothesis #1: the artwork-enrichment hot path must not own transient
    ``httpx.AsyncClient`` instances.

    Strongest form of the invariant: ``lookup.orchestrator`` does not import
    ``httpx`` at all (LML#444 removed the last reference when ``_fetch_apple_music_url``
    was deleted). Any future regression that re-introduces ``import httpx``
    into the module — typically the first step toward a per-call client
    construction — fails this test before it can ship the FD-leak shape from
    issue #241.
    """

    def test_orchestrator_does_not_import_httpx(self):
        """The orchestrator's request hot path must source HTTP clients via
        DI (e.g. ``AppleMusicClient``) rather than construct them. The
        BaseStreamingClient singleton pattern guarantees one process-lifetime
        ``httpx.AsyncClient`` per service with explicit shutdown.
        """
        import lookup.orchestrator as orchestrator_module

        assert not hasattr(orchestrator_module, "httpx"), (
            "lookup.orchestrator must not import httpx. Every outbound HTTP "
            "client in this service is a process-lifetime singleton with "
            "explicit close() on shutdown — the orchestrator sources from "
            "DI rather than constructing. Re-introducing httpx here is the "
            "first step toward the per-call construction pattern that "
            "leaked FDs in issue #241."
        )


class TestPgSourcePoolRace:
    """Hypothesis #2a: ``PgSource._get_pool`` is racy.

    Two concurrent first-callers each pass the ``self._pool is None`` check,
    each ``await asyncpg.create_pool(...)``, then both assign ``self._pool``
    in turn. The loser orphans its pool — up to 5 connections (FDs) with no
    reference to close them.
    """

    @pytest.mark.asyncio
    async def test_concurrent_get_pool_calls_create_pool_once(self):
        """Concurrent ``_get_pool`` callers must share a single pool. The
        lazy init needs an asyncio.Lock around the check-and-set.
        """
        from entity.sources import PgSource

        create_calls: list[tuple] = []
        release_event = asyncio.Event()

        async def fake_create_pool(*args, **kwargs):
            create_calls.append((args, kwargs))
            # Yield long enough for every concurrent caller to pass
            # the ``is None`` check before any of us assigns ``self._pool``.
            await release_event.wait()
            return AsyncMock(name="pool")

        with patch(
            "entity.sources.asyncpg.create_pool",
            side_effect=fake_create_pool,
        ):
            src = PgSource("postgres://fake/db")
            tasks = [asyncio.create_task(src._get_pool()) for _ in range(8)]
            # Let every task progress to the create_pool await.
            for _ in range(4):
                await asyncio.sleep(0)
            release_event.set()
            await asyncio.gather(*tasks)

        assert len(create_calls) == 1, (
            f"asyncpg.create_pool was called {len(create_calls)} times for "
            "the same PgSource. Concurrent first-callers race between the "
            "`if self._pool is None` check and the assignment, orphaning "
            "all but one pool — each orphan holds up to 5 connections. "
            "_get_pool needs an asyncio.Lock around the lazy init "
            "(issue #241)."
        )


class TestGetDiscogsServicePoolRace:
    """Hypothesis #2b: ``core.dependencies.get_discogs_service`` is racy.

    Same shape as PgSource: concurrent cold-start callers each enter the
    ``_discogs_pool is None`` branch, each ``await asyncpg.create_pool(...)``,
    and orphan all but one pool.

    Post-#283 the singleton is provided by `wxyc_fastapi.http.async_singleton`,
    which owns the double-check + `asyncio.Lock` together. The test still
    drives `core.dependencies.get_discogs_service` end-to-end so a future
    refactor that bypasses `async_singleton` would re-introduce the race
    and re-fail this test.
    """

    @pytest.mark.asyncio
    async def test_concurrent_get_discogs_service_creates_one_pool(self):
        """Concurrent first-callers in ``get_discogs_service`` must
        produce a single shared discogs-cache pool.
        """
        from config.settings import Settings
        from core import dependencies

        # Reset module-level singletons via the public closer so the next
        # `get_discogs_service` call drives a cold-start through the
        # `async_singleton` factory (the underlying state lives in a closure
        # we can't reach directly).
        await dependencies.close_discogs_service()

        settings = Settings(
            discogs_token="test-token",
            database_url_discogs="postgres://fake-discogs/db",
        )

        create_calls: list[tuple] = []
        release_event = asyncio.Event()

        async def fake_create_pool(*args, **kwargs):
            create_calls.append((args, kwargs))
            await release_event.wait()
            return AsyncMock(name="discogs-pool")

        try:
            with (
                patch("core.dependencies.get_settings", return_value=settings),
                patch(
                    "core.dependencies.asyncpg.create_pool",
                    side_effect=fake_create_pool,
                ),
            ):
                tasks = [
                    asyncio.create_task(dependencies.get_discogs_service(settings))
                    for _ in range(8)
                ]
                for _ in range(4):
                    await asyncio.sleep(0)
                release_event.set()
                await asyncio.gather(*tasks)
        finally:
            await dependencies.close_discogs_service()

        assert len(create_calls) == 1, (
            f"asyncpg.create_pool was called {len(create_calls)} times for "
            "the discogs cache. Concurrent cold-start callers race in "
            "get_discogs_service, orphaning all but one pool — each orphan "
            "holds up to 5 connections. The lazy init must go through "
            "`wxyc_fastapi.http.async_singleton` (issue #241 / #283)."
        )
