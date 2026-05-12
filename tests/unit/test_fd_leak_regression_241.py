"""Regression tests for issue #241 — FD exhaustion on LML prod 2026-05-01.

Around 21:44 UTC the production container hit ``OSError: [Errno 24] Too many
open files`` and stopped accepting connections, taking down /api/v1/lookup
and the iOS request feature. The recovery was a no-op redeploy that swapped
the container; the leak source survives until something changes.

These tests encode the two prime hypotheses identified during incident
investigation as concrete contracts the code should satisfy:

1. ``_fetch_apple_music_url`` is on the request hot path and must NOT
   construct a fresh ``httpx.AsyncClient`` per call. Every other outbound
   client in the service (Discogs, Spotify, Deezer, Bandcamp, AppleMusicClient)
   is a process-lifetime singleton with explicit ``close()`` on shutdown;
   this one slipped through. Each instantiation opens a fresh transport
   (DNS resolver socket + connection pool) — under sustained traffic from
   request-o-matic ``/health/ready`` polls + real iOS DJ requests, those
   accumulate FDs faster than they can be cleaned up.

2. Two lazy-init asyncpg pools have a TOCTOU race:
   ``scripts/entity_resolution/sources.py:PgSource._get_pool`` and
   ``core/dependencies.py:get_discogs_service``. Both check ``self._pool is
   None`` then ``await asyncpg.create_pool(...)`` without a lock. Concurrent
   first-callers each pass the check, each create a pool, and all but one
   pool is orphaned with no reference to ``close()`` it. Each orphaned pool
   holds up to 5 connections (FDs).

These tests fail today; they should pass after fixes ship.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from discogs.models import ReleaseMetadataResponse


class TestFetchAppleMusicUrlNoLeak:
    """Hypothesis #1: the artwork-enrichment hot path must not instantiate
    ``httpx.AsyncClient`` per call.

    The desired contract: ``enrich_artwork_results`` accepts (or sources from
    DI) a shared ``httpx.AsyncClient`` / ``AppleMusicClient`` and threads it
    down to ``_fetch_apple_music_url``. The orchestrator owns no transient
    clients of its own.
    """

    @pytest.mark.asyncio
    async def test_enrich_artwork_results_does_not_construct_httpx_client(self):
        """A full enrich pass over multiple items must not instantiate any
        ``httpx.AsyncClient`` inside the orchestrator. Every instantiation
        on the hot path is a leaked-FD risk (issue #241).
        """
        from lookup.orchestrator import enrich_artwork_results
        from tests.factories import make_discogs_result, make_library_item

        items_with_artwork = [
            (
                make_library_item(id=i, artist=f"Artist {i}", title=f"Album {i}"),
                make_discogs_result(release_id=i, artist=f"Artist {i}", album=f"Album {i}"),
            )
            for i in range(1, 4)
        ]

        discogs_service = AsyncMock()

        def make_release(release_id: int) -> ReleaseMetadataResponse:
            return ReleaseMetadataResponse(
                release_id=release_id,
                title=f"Album {release_id}",
                artist=f"Artist {release_id}",
                year=2000 + release_id,
                artist_id=None,
                release_url=f"https://discogs.com/release/{release_id}",
            )

        discogs_service.get_release.side_effect = lambda rid: make_release(rid)

        instantiations: list[tuple] = []

        class TrackingAsyncClient:
            """Stand-in for ``httpx.AsyncClient`` that records construction
            and yields a get-stub returning an empty iTunes response.
            """

            def __init__(self, *args, **kwargs):
                instantiations.append((args, kwargs))

            async def __aenter__(self):
                stub = AsyncMock()
                stub.get = AsyncMock(
                    return_value=httpx.Response(
                        200,
                        json={"results": []},
                        request=httpx.Request("GET", "https://itunes.apple.com/search"),
                    )
                )
                return stub

            async def __aexit__(self, exc_type, exc, tb):
                return None

        with patch("lookup.orchestrator.httpx.AsyncClient", TrackingAsyncClient):
            await enrich_artwork_results(items_with_artwork, discogs_service, song="Song")

        assert instantiations == [], (
            f"lookup.orchestrator constructed {len(instantiations)} "
            f"httpx.AsyncClient(s) during a single enrich pass over "
            f"{len(items_with_artwork)} items. Each instantiation opens a "
            "fresh transport (DNS + connection pool); under sustained "
            "/api/v1/lookup traffic this leaks FDs (issue #241). "
            "enrich_artwork_results should accept a shared client and "
            "thread it through to _fetch_apple_music_url."
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
        from scripts.entity_resolution.sources import PgSource

        create_calls: list[tuple] = []
        release_event = asyncio.Event()

        async def fake_create_pool(*args, **kwargs):
            create_calls.append((args, kwargs))
            # Yield long enough for every concurrent caller to pass
            # the ``is None`` check before any of us assigns ``self._pool``.
            await release_event.wait()
            return AsyncMock(name="pool")

        with patch(
            "scripts.entity_resolution.sources.asyncpg.create_pool",
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
