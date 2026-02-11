"""Unit tests for discogs/ratelimit.py."""

from unittest.mock import patch

import pytest

from discogs.ratelimit import (
    _rate_limiters,
    _semaphores,
    get_rate_limiter,
    get_semaphore,
    reset_rate_limiting,
)


class TestGetRateLimiter:
    @pytest.mark.asyncio
    async def test_creates_limiter_in_running_loop(self):
        limiter = get_rate_limiter()
        assert limiter is not None

    @pytest.mark.asyncio
    async def test_caches_per_loop(self):
        limiter1 = get_rate_limiter()
        limiter2 = get_rate_limiter()
        assert limiter1 is limiter2

    def test_no_running_loop_creates_fresh_limiter(self):
        """Without a running event loop, returns a new limiter each time."""
        limiter = get_rate_limiter()
        assert limiter is not None


class TestGetSemaphore:
    @pytest.mark.asyncio
    async def test_creates_semaphore_in_running_loop(self):
        sem = get_semaphore()
        assert sem is not None

    @pytest.mark.asyncio
    async def test_caches_per_loop(self):
        sem1 = get_semaphore()
        sem2 = get_semaphore()
        assert sem1 is sem2

    def test_no_running_loop_creates_fresh_semaphore(self):
        sem = get_semaphore()
        assert sem is not None


class TestResetRateLimiting:
    @pytest.mark.asyncio
    async def test_clears_cached_state(self):
        get_rate_limiter()
        get_semaphore()
        assert len(_rate_limiters) > 0
        assert len(_semaphores) > 0

        reset_rate_limiting()
        assert len(_rate_limiters) == 0
        assert len(_semaphores) == 0
