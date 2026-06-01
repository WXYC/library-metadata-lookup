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
from pathlib import Path
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


class TestGetAppleMusicClientRace:
    """Hypothesis #2c: ``streaming.dependencies.get_apple_music_client`` is racy.

    Same shape as the Discogs-pool race: the pre-fix getter is a sync FastAPI
    dep with a ``global _apple_music_client`` + ``is None`` check + assign. The
    dep runs in FastAPI's threadpool, so concurrent cold-start callers can
    each observe ``None``, each construct a fresh ``AppleMusicClient`` (PEM
    parse + ECDSA key load + AsyncLimiter + asyncio.Semaphore), and orphan
    all but one. The orphan's HTTP transport itself doesn't leak (the base
    class wraps that in ``async_singleton``), but the orphan's rate limiter
    and semaphore are wasted work and a transient drift hazard (two limiters
    mean ~2x the effective rate budget until the orphan is GC'd).

    Post-#451 the singleton is provided by `wxyc_fastapi.http.async_singleton`,
    which owns the double-check + `asyncio.Lock` together. The test drives
    the public getter end-to-end so a future refactor that bypasses
    `async_singleton` would re-introduce the race and re-fail this test.
    """

    @pytest.mark.asyncio
    async def test_concurrent_cold_start_creates_one_client(self):
        """Concurrent cold-start callers in ``get_apple_music_client`` must
        share a single ``AppleMusicClient`` instance.

        Pre-fix the sync FastAPI dep returned the result of a bare
        ``AppleMusicClient(...)`` call after a ``global _apple_music_client``
        check. Sync deps run in FastAPI's threadpool, so concurrent cold-
        starters could each pass the ``is None`` check before any of them
        assigned the global. Post-#451 the getter is async, the underlying
        factory runs inside ``wxyc_fastapi.http.async_singleton``'s lock,
        and every caller observes the same instance.
        """
        from config.settings import Settings
        from streaming import dependencies

        # Reset module-level state via the public closer so the next
        # `get_apple_music_client` call drives a cold-start through the
        # `async_singleton` factory (the underlying state lives in a closure
        # we can't reach directly).
        await dependencies.close_streaming_clients()

        settings = Settings(
            apple_music_team_id="TEAMID1234",
            apple_music_key_id="KEYID12345",
            apple_music_private_key="-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----\n",
        )

        init_calls: list[tuple] = []
        fake_client = AsyncMock(name="apple-music-client")

        def fake_constructor(*args, **kwargs):
            init_calls.append((args, kwargs))
            return fake_client

        try:
            with (
                patch("streaming.dependencies.get_settings", return_value=settings),
                patch(
                    "streaming.dependencies.AppleMusicClient",
                    side_effect=fake_constructor,
                ),
            ):
                tasks = [
                    asyncio.create_task(dependencies.get_apple_music_client()) for _ in range(8)
                ]
                results = await asyncio.gather(*tasks)
        finally:
            await dependencies.close_streaming_clients()

        assert len(init_calls) == 1, (
            f"AppleMusicClient was constructed {len(init_calls)} times for "
            "the same Apple Music singleton. Concurrent cold-start callers "
            "race in get_apple_music_client, orphaning all but one client — "
            "each orphan holds an AsyncLimiter and asyncio.Semaphore (two "
            "limiters mean transiently 2x the effective per-team rate "
            "budget). The lazy init must go through "
            "`wxyc_fastapi.http.async_singleton` (issue #241 / #451)."
        )
        # All callers must observe the same singleton instance.
        assert all(r is fake_client for r in results), (
            "Concurrent cold-start callers received different "
            "AppleMusicClient instances. The async_singleton wrapper must "
            "return the same instance to every caller after the factory "
            "completes (issue #451)."
        )


class TestGetSpotifyClientRace:
    """Hypothesis #2d: ``streaming.dependencies.get_spotify_client`` is racy.

    Same shape as the Apple Music race that #451 closed: the pre-fix getter is
    a sync FastAPI dep with a ``global _spotify_client`` + ``is None`` check +
    assign. The dep runs in FastAPI's threadpool, so concurrent cold-start
    callers can each observe ``None``, each construct a fresh
    ``SpotifyClient`` (AsyncLimiter + asyncio.Semaphore from
    ``BaseStreamingClient``), and orphan all but one.

    Spotify is materially the worst of the three siblings because its first
    request also lazy-fetches an OAuth bearer token; two orphaned clients
    means two OAuth token fetches before GC.

    Post-#457 the singleton is provided by `wxyc_fastapi.http.async_singleton`,
    which owns the double-check + `asyncio.Lock` together. The test drives the
    public getter end-to-end so a future refactor that bypasses
    `async_singleton` would re-introduce the race and re-fail this test.
    """

    @pytest.mark.asyncio
    async def test_concurrent_cold_start_creates_one_client(self):
        """Concurrent cold-start callers in ``get_spotify_client`` must share
        a single ``SpotifyClient`` instance.
        """
        from config.settings import Settings
        from streaming import dependencies

        await dependencies.close_streaming_clients()

        settings = Settings(
            spotify_client_id="fake-id",
            spotify_client_secret="fake-secret",
        )

        init_calls: list[tuple] = []
        fake_client = AsyncMock(name="spotify-client")

        def fake_constructor(*args, **kwargs):
            init_calls.append((args, kwargs))
            return fake_client

        try:
            with (
                patch("streaming.dependencies.get_settings", return_value=settings),
                patch(
                    "streaming.dependencies.SpotifyClient",
                    side_effect=fake_constructor,
                ),
            ):
                tasks = [asyncio.create_task(dependencies.get_spotify_client()) for _ in range(8)]
                results = await asyncio.gather(*tasks)
        finally:
            await dependencies.close_streaming_clients()

        assert len(init_calls) == 1, (
            f"SpotifyClient was constructed {len(init_calls)} times for the "
            "same Spotify singleton. Concurrent cold-start callers race in "
            "get_spotify_client, orphaning all but one client — each orphan "
            "holds an AsyncLimiter + asyncio.Semaphore AND triggers its own "
            "OAuth bearer-token fetch on first request. The lazy init must "
            "go through `wxyc_fastapi.http.async_singleton` (issue #241 / "
            "#457)."
        )
        assert all(r is fake_client for r in results), (
            "Concurrent cold-start callers received different SpotifyClient "
            "instances. async_singleton must return the same instance to "
            "every caller after the factory completes (issue #457)."
        )


class TestGetDeezerClientRace:
    """Hypothesis #2e: ``streaming.dependencies.get_deezer_client`` is racy.

    Identical shape to the Apple Music / Spotify races: pre-fix sync FastAPI
    dep with a ``global _deezer_client`` + ``is None`` check + assign.
    Concurrent cold-start callers race, orphaning all but one
    ``DeezerClient``. Each orphan holds an AsyncLimiter + asyncio.Semaphore.
    """

    @pytest.mark.asyncio
    async def test_concurrent_cold_start_creates_one_client(self):
        """Concurrent cold-start callers in ``get_deezer_client`` must share
        a single ``DeezerClient`` instance.
        """
        from streaming import dependencies

        await dependencies.close_streaming_clients()

        init_calls: list[tuple] = []
        fake_client = AsyncMock(name="deezer-client")

        def fake_constructor(*args, **kwargs):
            init_calls.append((args, kwargs))
            return fake_client

        try:
            with patch(
                "streaming.dependencies.DeezerClient",
                side_effect=fake_constructor,
            ):
                tasks = [asyncio.create_task(dependencies.get_deezer_client()) for _ in range(8)]
                results = await asyncio.gather(*tasks)
        finally:
            await dependencies.close_streaming_clients()

        assert len(init_calls) == 1, (
            f"DeezerClient was constructed {len(init_calls)} times for the "
            "same Deezer singleton. Concurrent cold-start callers race in "
            "get_deezer_client, orphaning all but one client. The lazy init "
            "must go through `wxyc_fastapi.http.async_singleton` (issue "
            "#241 / #457)."
        )
        assert all(r is fake_client for r in results), (
            "Concurrent cold-start callers received different DeezerClient "
            "instances. async_singleton must return the same instance to "
            "every caller after the factory completes (issue #457)."
        )


class TestGetBandcampClientRace:
    """Hypothesis #2f: ``streaming.dependencies.get_bandcamp_client`` is racy.

    Identical shape to the Apple Music / Spotify / Deezer races: pre-fix sync
    FastAPI dep with a ``global _bandcamp_client`` + ``is None`` check +
    assign. Concurrent cold-start callers race, orphaning all but one
    ``BandcampClient``. Each orphan holds an AsyncLimiter + asyncio.Semaphore.
    """

    @pytest.mark.asyncio
    async def test_concurrent_cold_start_creates_one_client(self):
        """Concurrent cold-start callers in ``get_bandcamp_client`` must share
        a single ``BandcampClient`` instance.
        """
        from streaming import dependencies

        await dependencies.close_streaming_clients()

        init_calls: list[tuple] = []
        fake_client = AsyncMock(name="bandcamp-client")

        def fake_constructor(*args, **kwargs):
            init_calls.append((args, kwargs))
            return fake_client

        try:
            with patch(
                "streaming.dependencies.BandcampClient",
                side_effect=fake_constructor,
            ):
                tasks = [asyncio.create_task(dependencies.get_bandcamp_client()) for _ in range(8)]
                results = await asyncio.gather(*tasks)
        finally:
            await dependencies.close_streaming_clients()

        assert len(init_calls) == 1, (
            f"BandcampClient was constructed {len(init_calls)} times for "
            "the same Bandcamp singleton. Concurrent cold-start callers "
            "race in get_bandcamp_client, orphaning all but one client. "
            "The lazy init must go through "
            "`wxyc_fastapi.http.async_singleton` (issue #241 / #457)."
        )
        assert all(r is fake_client for r in results), (
            "Concurrent cold-start callers received different BandcampClient "
            "instances. async_singleton must return the same instance to "
            "every caller after the factory completes (issue #457)."
        )


def test_no_global_apple_music_client_in_streaming_dependencies():
    """LML#451: race-prone ``global _apple_music_client`` lazy-init must be
    gone from ``streaming/dependencies.py``. The lock + double-check belongs
    inside ``wxyc_fastapi.http.async_singleton``; reintroducing a hand-rolled
    sync getter with a module-level singleton + ``is None`` check would
    re-open the same race that LML#241/#283/#435 closed elsewhere.
    """
    from streaming import dependencies as streaming_deps_module

    src = Path(streaming_deps_module.__file__).read_text()
    assert "global _apple_music_client" not in src, (
        "streaming/dependencies.py uses `global _apple_music_client` — the "
        "lock-free lazy-init pattern that LML#451 closed. Migrate to "
        "`async_singleton(_build_apple_music_client)` (mirroring the Discogs "
        "pool + MusicBrainz PgSource pattern in core/dependencies.py). See "
        "LML#451."
    )


def test_no_global_spotify_client_in_streaming_dependencies():
    """LML#457: race-prone ``global _spotify_client`` lazy-init must be gone
    from ``streaming/dependencies.py``. Mirrors the Apple Music pin above —
    the Spotify, Deezer, and Bandcamp getters all share the same hand-rolled
    lazy-init shape and must all migrate to ``async_singleton``.
    """
    from streaming import dependencies as streaming_deps_module

    src = Path(streaming_deps_module.__file__).read_text()
    assert "global _spotify_client" not in src, (
        "streaming/dependencies.py uses `global _spotify_client` — the "
        "lock-free lazy-init pattern that LML#457 closed for the three "
        "siblings of the Apple Music getter. Migrate to "
        "`async_singleton(_build_spotify_client)`."
    )


def test_no_global_deezer_client_in_streaming_dependencies():
    """LML#457: race-prone ``global _deezer_client`` lazy-init must be gone
    from ``streaming/dependencies.py``.
    """
    from streaming import dependencies as streaming_deps_module

    src = Path(streaming_deps_module.__file__).read_text()
    assert "global _deezer_client" not in src, (
        "streaming/dependencies.py uses `global _deezer_client` — the "
        "lock-free lazy-init pattern that LML#457 closed. Migrate to "
        "`async_singleton(_build_deezer_client)`."
    )


def test_no_global_bandcamp_client_in_streaming_dependencies():
    """LML#457: race-prone ``global _bandcamp_client`` lazy-init must be gone
    from ``streaming/dependencies.py``.
    """
    from streaming import dependencies as streaming_deps_module

    src = Path(streaming_deps_module.__file__).read_text()
    assert "global _bandcamp_client" not in src, (
        "streaming/dependencies.py uses `global _bandcamp_client` — the "
        "lock-free lazy-init pattern that LML#457 closed. Migrate to "
        "`async_singleton(_build_bandcamp_client)`."
    )


def test_no_module_level_asyncio_lock_in_streaming_dependencies():
    """LML#459 (LML#451 follow-up): race-prone hand-rolled ``asyncio.Lock()``
    plumbing must stay out of ``streaming/dependencies.py``. The lock +
    double-check belongs inside ``wxyc_fastapi.http.async_singleton``;
    reintroducing one at the module level (a) duplicates the contract and
    (b) means a future author could write a new lazy-init that re-races the
    FD-leak fix from #242.

    Mirrors ``tests/unit/test_dependencies.py::
    test_no_module_level_asyncio_lock_in_dependencies`` for symmetry — the
    Apple Music singleton pin (above) catches the ``global X`` re-introduction
    shape, this pin catches the ``asyncio.Lock()`` re-introduction shape.
    """
    from streaming import dependencies as streaming_deps_module

    src = Path(streaming_deps_module.__file__).read_text()
    assert "asyncio.Lock()" not in src, (
        "streaming/dependencies.py contains a module-level `asyncio.Lock()` "
        "instance — async singletons in this module must use "
        "`wxyc_fastapi.http.async_singleton(factory)` instead, which owns "
        "the lock and the double-check together. See "
        "WXYC/library-metadata-lookup#451 and "
        "WXYC/library-metadata-lookup#241."
    )
