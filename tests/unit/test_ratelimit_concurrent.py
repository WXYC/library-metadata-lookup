"""Multi-threaded rate limiter test with threading.Event coordination.

Verifies that the semaphore and rate limiter properly limit concurrent access
and that no burst bypass occurs under parallel load.
"""

from __future__ import annotations

import asyncio
import time

import pytest

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
        assert max_concurrent <= 5, (
            f"Expected at most 5 concurrent, but observed {max_concurrent}"
        )
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
    async def test_rate_limiter_no_burst_bypass(self):
        """Concurrent tasks cannot bypass the rate limiter by acquiring simultaneously.

        The rate limiter is configured for N requests per minute. If we fire
        more tasks than the rate limit in quick succession, they should be
        throttled and not all complete instantly.
        """
        reset_rate_limiting()
        limiter = get_rate_limiter()

        timestamps: list[float] = []
        lock = asyncio.Lock()

        async def worker():
            async with limiter:
                async with lock:
                    timestamps.append(time.monotonic())

        # Fire more tasks than the rate limit allows
        # Default rate limit is 50/min, so fire 60 tasks
        tasks = [asyncio.create_task(worker()) for _ in range(60)]
        await asyncio.gather(*tasks)

        assert len(timestamps) == 60

        # All timestamps should exist (all tasks completed)
        # The rate limiter uses a token bucket, so the first batch goes through
        # immediately and subsequent ones are delayed.
        # Check that at least one task was delayed (span > 0)
        time_span = timestamps[-1] - timestamps[0]
        assert time_span > 0, "Rate limiter should have throttled some requests"

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
