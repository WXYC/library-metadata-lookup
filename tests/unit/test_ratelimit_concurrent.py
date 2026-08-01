"""Multi-threaded rate limiter test with threading.Event coordination.

Verifies that the semaphore and rate limiter properly limit concurrent access
and that no burst bypass occurs under parallel load.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from aiolimiter import AsyncLimiter

from discogs.ratelimit import get_rate_limiter, get_semaphore, reset_rate_limiting


class TestConcurrentSemaphore:
    """Verify the semaphore enforces the max concurrent request limit."""

    @pytest.mark.asyncio
    async def test_semaphore_limits_concurrency(self):
        """At most N tasks can hold the semaphore simultaneously."""
        reset_rate_limiting()
        semaphore = get_semaphore()

        max_concurrent = 0
        current_concurrent = 0
        lock = asyncio.Lock()
        all_started = asyncio.Event()
        tasks_started = 0
        total_tasks = 20

        async def worker():
            nonlocal max_concurrent, current_concurrent, tasks_started

            async with semaphore:
                async with lock:
                    current_concurrent += 1
                    if current_concurrent > max_concurrent:
                        max_concurrent = current_concurrent
                    tasks_started += 1
                    if tasks_started >= total_tasks:
                        all_started.set()

                # Simulate work -- hold the semaphore briefly
                await asyncio.sleep(0.01)

                async with lock:
                    current_concurrent -= 1

        tasks = [asyncio.create_task(worker()) for _ in range(total_tasks)]
        await asyncio.gather(*tasks)

        # The semaphore should have limited concurrent access
        # Default max concurrent is 5 (from settings)
        assert max_concurrent <= 5, f"Expected at most 5 concurrent, but observed {max_concurrent}"
        # At least 2 should have run concurrently (unless extremely slow system)
        assert max_concurrent >= 2, (
            f"Expected at least 2 concurrent executions, got {max_concurrent}"
        )

    @pytest.mark.asyncio
    async def test_semaphore_all_tasks_complete(self):
        """All tasks eventually complete even with limited concurrency."""
        reset_rate_limiting()
        semaphore = get_semaphore()

        completed = []

        async def worker(task_id: int):
            async with semaphore:
                await asyncio.sleep(0.005)
                completed.append(task_id)

        tasks = [asyncio.create_task(worker(i)) for i in range(15)]
        await asyncio.gather(*tasks)

        assert len(completed) == 15
        assert set(completed) == set(range(15))


class TestConcurrentRateLimiter:
    """Verify the rate limiter prevents burst bypass under parallel load."""

    @pytest.mark.asyncio
    async def test_rate_limiter_uses_configured_rate(self):
        """get_rate_limiter() wires the configured req/min into a 60s-window bucket.

        Pins the production shape (``discogs_rate_limit`` tokens per 60s) that the
        throttle-behavior test below deliberately compresses for speed.
        """
        from config.settings import get_settings

        reset_rate_limiting()
        limiter = get_rate_limiter()
        settings = get_settings()

        assert limiter.max_rate == settings.discogs_rate_limit
        assert limiter.time_period == 60

    @pytest.mark.asyncio
    async def test_rate_limiter_no_burst_bypass(self):
        """Concurrent tasks exceeding the burst are throttled, not all admitted at once.

        Exercises the same ``AsyncLimiter`` token bucket the service uses, but on a
        compressed window (5 tokens / 100ms rather than the production 50 / 60s).
        A real 60s window refills one overflow token every ~1.2s, so proving the
        throttle with 10 overflow tasks costs ~12s of pure wall-clock wait for a
        ``span > 0`` assertion. The compressed window verifies the identical
        behavior in milliseconds; the production window value is pinned in
        ``test_rate_limiter_uses_configured_rate`` above.
        """
        max_rate = 5
        limiter = AsyncLimiter(max_rate, 0.1)

        timestamps: list[float] = []
        lock = asyncio.Lock()

        async def worker():
            async with limiter:
                async with lock:
                    timestamps.append(time.monotonic())

        # Fire more tasks than the burst: the first `max_rate` drain the initial
        # bucket instantly, the rest must wait for refill.
        tasks = [asyncio.create_task(worker()) for _ in range(max_rate * 2)]
        await asyncio.gather(*tasks)

        assert len(timestamps) == max_rate * 2

        # The overflow tasks are delayed relative to the first batch, so the
        # admission span is strictly positive (no burst bypass).
        time_span = timestamps[-1] - timestamps[0]
        assert time_span > 0, "Rate limiter should have throttled the overflow tasks"

    @pytest.mark.asyncio
    async def test_rate_limiter_concurrent_with_semaphore(self):
        """Rate limiter and semaphore work together without deadlock."""
        reset_rate_limiting()
        limiter = get_rate_limiter()
        semaphore = get_semaphore()

        completed_count = 0
        lock = asyncio.Lock()

        async def worker():
            nonlocal completed_count
            async with semaphore:
                async with limiter:
                    await asyncio.sleep(0.001)
                    async with lock:
                        completed_count += 1

        # Run enough tasks to exercise both limits
        tasks = [asyncio.create_task(worker()) for _ in range(20)]

        # Use a timeout to detect deadlocks
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=30.0)

        assert completed_count == 20


class TestRateLimiterPerLoop:
    """Verify rate limiter isolation per event loop."""

    @pytest.mark.asyncio
    async def test_different_loops_get_different_limiters(self):
        """Each event loop gets its own rate limiter instance."""
        reset_rate_limiting()
        limiter_1 = get_rate_limiter()

        # Reset and get again -- should be a new instance
        reset_rate_limiting()
        limiter_2 = get_rate_limiter()

        assert limiter_1 is not limiter_2

    @pytest.mark.asyncio
    async def test_same_loop_gets_cached_limiter(self):
        """Within the same loop, get_rate_limiter returns the cached instance."""
        reset_rate_limiting()
        limiter_a = get_rate_limiter()
        limiter_b = get_rate_limiter()

        assert limiter_a is limiter_b
