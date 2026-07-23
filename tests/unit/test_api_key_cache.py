"""Unit tests for core/api_key_cache.py (LML per-consumer API keys plan).

PG is mocked/patched throughout; ``tests/integration/test_api_keys_pg.py``
drives the real schema + queries end-to-end.
"""

from __future__ import annotations

import asyncio
import hashlib
from unittest.mock import AsyncMock, patch

import pytest

from core.api_key_cache import (
    ApiKeyCache,
    generate_token,
    get_api_key_cache,
    hash_token,
    reset_api_key_cache,
    start_api_key_cache,
    stop_api_key_cache,
)


async def _drain_background_tasks(timeout_s: float = 5.0) -> None:
    """Await every scheduled ``touch_last_used`` write, bounded by ``timeout_s``.

    Mirrors ``tests/conftest.py``'s ``drain_streaming_warm_tasks`` -- looping
    because a task's done-callback (discarding it from the set) runs after
    ``gather`` returns, so one pass can still observe a non-empty set.
    """
    from core import api_key_cache as mod

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while mod._background_tasks:
        remaining = deadline - loop.time()
        if remaining <= 0:
            pytest.fail(
                f"api-key-cache background-task drain exceeded {timeout_s}s; "
                f"{len(mod._background_tasks)} task(s) still tracked"
            )
        await asyncio.wait_for(
            asyncio.gather(*list(mod._background_tasks), return_exceptions=True),
            timeout=remaining,
        )


@pytest.fixture(autouse=True)
def _isolate():
    """Every test starts with an unstarted singleton and an empty task set."""
    from core import api_key_cache as mod

    reset_api_key_cache()
    mod._background_tasks.clear()
    yield
    reset_api_key_cache()
    mod._background_tasks.clear()


class TestGenerateToken:
    def test_has_the_lml_prefix(self):
        assert generate_token().startswith("lml_")

    def test_two_calls_are_distinct(self):
        # High-entropy random values -- a collision here would indicate a
        # broken RNG, not bad luck.
        assert generate_token() != generate_token()

    def test_is_reasonably_long(self):
        # lml_ (4 chars) + base64url(32 bytes) (~43 chars, no padding).
        assert len(generate_token()) >= 40


class TestHashToken:
    def test_is_deterministic(self):
        assert hash_token("lml_abc123") == hash_token("lml_abc123")

    def test_different_inputs_hash_differently(self):
        assert hash_token("lml_abc123") != hash_token("lml_xyz789")

    def test_is_a_sha256_hex_digest(self):
        token = "lml_abc123"
        assert hash_token(token) == hashlib.sha256(token.encode()).hexdigest()
        assert len(hash_token(token)) == 64


class TestApiKeyCacheResolve:
    def test_resolve_returns_caller_on_hit(self):
        cache = ApiKeyCache(pg=AsyncMock())
        cache._hash_to_caller = {hash_token("secret-token"): "wxyc-canary"}

        assert cache.resolve("secret-token") == "wxyc-canary"

    def test_resolve_returns_none_on_miss(self):
        cache = ApiKeyCache(pg=AsyncMock())
        cache._hash_to_caller = {hash_token("secret-token"): "wxyc-canary"}

        assert cache.resolve("unknown-token") is None

    def test_resolve_treats_revoked_key_same_as_unknown_key(self):
        # A revoked row is simply never returned by the active-key SELECT
        # (entity/api_keys.py), so from ApiKeyCache's point of view a
        # "revoked" token was never in the snapshot at all -- indistinguishable
        # from a token that never existed. Both resolve() calls return the
        # identical None.
        cache = ApiKeyCache(pg=AsyncMock())
        cache._hash_to_caller = {hash_token("still-active"): "backend-service"}

        assert cache.resolve("revoked-token") is None
        assert cache.resolve("never-issued-token") is None

    def test_is_empty_reflects_snapshot_size(self):
        cache = ApiKeyCache(pg=AsyncMock())
        assert cache.is_empty is True
        cache._hash_to_caller = {hash_token("secret-token"): "wxyc-canary"}
        assert cache.is_empty is False


class TestApiKeyCacheRefresh:
    @pytest.mark.asyncio
    async def test_refresh_populates_snapshot_from_active_rows(self):
        cache = ApiKeyCache(pg=AsyncMock())
        rows = [
            {"key_hash": hash_token("token-a"), "caller_name": "wxyc-canary"},
            {"key_hash": hash_token("token-b"), "caller_name": "backend-service"},
        ]
        with patch("core.api_key_cache.fetch_active_keys", AsyncMock(return_value=rows)):
            await cache.refresh()

        assert cache.resolve("token-a") == "wxyc-canary"
        assert cache.resolve("token-b") == "backend-service"

    @pytest.mark.asyncio
    async def test_refresh_never_raises_on_fetch_failure(self):
        cache = ApiKeyCache(pg=AsyncMock())
        with patch(
            "core.api_key_cache.fetch_active_keys",
            AsyncMock(side_effect=RuntimeError("pg blip")),
        ):
            await cache.refresh()  # must not raise

    @pytest.mark.asyncio
    async def test_refresh_keeps_prior_snapshot_when_fetch_raises(self):
        # The load-bearing correlated-failure guard: a transient PG blip
        # during refresh() must NOT clear or replace the existing snapshot --
        # only a successful fetch swaps it.
        cache = ApiKeyCache(pg=AsyncMock())
        good_rows = [{"key_hash": hash_token("token-a"), "caller_name": "wxyc-canary"}]
        with patch("core.api_key_cache.fetch_active_keys", AsyncMock(return_value=good_rows)):
            await cache.refresh()
        assert cache.resolve("token-a") == "wxyc-canary"

        with patch(
            "core.api_key_cache.fetch_active_keys",
            AsyncMock(side_effect=RuntimeError("pg blip")),
        ):
            await cache.refresh()

        # The previously-loaded key still resolves -- the blip did not empty
        # the cache.
        assert cache.resolve("token-a") == "wxyc-canary"
        assert cache.is_empty is False

    @pytest.mark.asyncio
    async def test_refresh_logs_on_fetch_failure(self, caplog):
        import logging

        cache = ApiKeyCache(pg=AsyncMock())
        with caplog.at_level(logging.ERROR, logger="core.api_key_cache"):
            with patch(
                "core.api_key_cache.fetch_active_keys",
                AsyncMock(side_effect=RuntimeError("pg blip")),
            ):
                await cache.refresh()

        assert any(r.name == "core.api_key_cache" for r in caplog.records)


class TestApiKeyCacheTouchLastUsed:
    @pytest.mark.asyncio
    async def test_schedules_a_background_task(self):
        from core import api_key_cache as mod

        cache = ApiKeyCache(pg=AsyncMock())
        cache._hash_to_caller = {hash_token("secret-token"): "wxyc-canary"}

        with patch("core.api_key_cache.mark_key_used", AsyncMock()) as mark:
            cache.touch_last_used("secret-token")
            assert len(mod._background_tasks) == 1
            await _drain_background_tasks()

        mark.assert_awaited_once_with(cache._pg, hash_token("secret-token"))
        assert not mod._background_tasks

    @pytest.mark.asyncio
    async def test_background_task_actually_runs_to_completion(self):
        # Not just "create_task was called" -- the scheduled write must
        # actually execute under a real event loop, guarding the strong-
        # reference anchoring in `_background_tasks` (the same regression
        # this repo already tests for around
        # lookup.enrichment.background._background_tasks). Draining (rather
        # than separately awaiting a signal the task itself sets) is what
        # forces the event loop to actually run the task: awaiting an
        # ALREADY-done task/future is a no-yield fast path in asyncio, so a
        # drain called only after the task has already finished would never
        # see its done-callback fire before the deadline.
        completed = asyncio.Event()

        async def fake_mark_key_used(pg, key_hash):
            completed.set()

        cache = ApiKeyCache(pg=AsyncMock())
        with patch("core.api_key_cache.mark_key_used", fake_mark_key_used):
            cache.touch_last_used("secret-token")
            await _drain_background_tasks()

        assert completed.is_set()

    @pytest.mark.asyncio
    async def test_touch_within_the_throttle_window_does_not_reschedule(self):
        # Finding 1: the SQL WHERE clause throttles the *write* to hourly, but
        # a per-request UPDATE still costs a pool acquire + round-trip on the
        # contended discogs-cache pool (LML#706). An in-process guard skips
        # scheduling the task at all when this hash was touched within the
        # window, so a hot caller pays at most one background write per window.
        from core import api_key_cache as mod

        cache = ApiKeyCache(pg=AsyncMock())
        with patch("core.api_key_cache.mark_key_used", AsyncMock()) as mark:
            # Two touches 1s apart -- the second is well inside the 1h window.
            with patch("core.api_key_cache.time.monotonic", side_effect=[100.0, 101.0]):
                cache.touch_last_used("secret-token")
                cache.touch_last_used("secret-token")
            assert len(mod._background_tasks) == 1
            await _drain_background_tasks()
        assert mark.await_count == 1

    @pytest.mark.asyncio
    async def test_touch_fires_again_after_the_throttle_window(self):
        from core.api_key_cache import _TOUCH_THROTTLE_SECONDS

        cache = ApiKeyCache(pg=AsyncMock())
        with patch("core.api_key_cache.mark_key_used", AsyncMock()) as mark:
            with patch(
                "core.api_key_cache.time.monotonic",
                side_effect=[100.0, 100.0 + _TOUCH_THROTTLE_SECONDS + 1.0],
            ):
                cache.touch_last_used("secret-token")
                cache.touch_last_used("secret-token")
            await _drain_background_tasks()
        assert mark.await_count == 2

    @pytest.mark.asyncio
    async def test_touch_throttle_is_per_key(self):
        # The throttle is keyed by hash, so one hot caller cannot suppress
        # another caller's last_used_at touch: two distinct keys each fire on
        # their first touch even back-to-back.
        cache = ApiKeyCache(pg=AsyncMock())
        with patch("core.api_key_cache.mark_key_used", AsyncMock()) as mark:
            with patch("core.api_key_cache.time.monotonic", side_effect=[100.0, 100.5]):
                cache.touch_last_used("token-a")
                cache.touch_last_used("token-b")
            await _drain_background_tasks()
        assert mark.await_count == 2

    @pytest.mark.asyncio
    async def test_survives_gc_pressure_before_completion(self):
        # Regression guard for the exact bug the strong-reference set exists
        # to prevent: force a garbage-collection pass immediately after
        # scheduling, before the task has had any chance to run (no await
        # point has occurred yet), and confirm it still completes rather than
        # being silently reaped.
        import gc

        completed = asyncio.Event()

        async def fake_mark_key_used(pg, key_hash):
            completed.set()

        cache = ApiKeyCache(pg=AsyncMock())
        with patch("core.api_key_cache.mark_key_used", fake_mark_key_used):
            cache.touch_last_used("secret-token")
            gc.collect()
            await _drain_background_tasks()

        assert completed.is_set()


class TestApiKeyCacheAccessor:
    def test_get_returns_none_before_start(self):
        assert get_api_key_cache() is None

    def test_reset_seeds_a_known_cache(self):
        cache = ApiKeyCache(pg=AsyncMock())
        reset_api_key_cache(cache)
        assert get_api_key_cache() is cache

    def test_reset_with_no_args_clears_to_none(self):
        reset_api_key_cache(ApiKeyCache(pg=AsyncMock()))
        reset_api_key_cache()
        assert get_api_key_cache() is None


class TestStartStopApiKeyCache:
    @pytest.mark.asyncio
    async def test_start_loads_initial_snapshot_and_publishes_singleton(self):
        rows = [{"key_hash": hash_token("token-a"), "caller_name": "wxyc-canary"}]
        with patch("core.api_key_cache.fetch_active_keys", AsyncMock(return_value=rows)):
            task = await start_api_key_cache(AsyncMock(), refresh_seconds=60)
        try:
            cache = get_api_key_cache()
            assert cache is not None
            assert cache.resolve("token-a") == "wxyc-canary"
        finally:
            await stop_api_key_cache(task)

    @pytest.mark.asyncio
    async def test_stop_cancels_the_refresh_task(self):
        with patch("core.api_key_cache.fetch_active_keys", AsyncMock(return_value=[])):
            task = await start_api_key_cache(AsyncMock(), refresh_seconds=60)

        await stop_api_key_cache(task)
        assert task.cancelled()

    @pytest.mark.asyncio
    async def test_stop_does_not_clear_the_singleton(self):
        # Mirrors core/event_loop_lag.py's stop_sampler(): shutdown cancels
        # the task but does not touch the process-global singleton.
        with patch("core.api_key_cache.fetch_active_keys", AsyncMock(return_value=[])):
            task = await start_api_key_cache(AsyncMock(), refresh_seconds=60)

        await stop_api_key_cache(task)
        assert get_api_key_cache() is not None

    @pytest.mark.asyncio
    async def test_refresh_loop_reloads_on_interval(self):
        from core.api_key_cache import _refresh_loop

        cache = ApiKeyCache(pg=AsyncMock())
        rows = [{"key_hash": hash_token("token-a"), "caller_name": "wxyc-canary"}]
        with patch("core.api_key_cache.fetch_active_keys", AsyncMock(return_value=rows)):
            task = asyncio.create_task(_refresh_loop(cache, 0.01))
            await asyncio.sleep(0.03)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert cache.resolve("token-a") == "wxyc-canary"
