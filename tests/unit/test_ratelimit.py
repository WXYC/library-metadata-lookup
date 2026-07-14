"""Unit tests for discogs/ratelimit.py."""

import pytest

from config.settings import get_settings
from discogs.ratelimit import (
    _build_breaker,
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


class TestBuildBreaker:
    """LML#787 review: ``_build_breaker`` passes every breaker knob through
    from settings — including the watchdog multiplier, which was
    constructor-only (hard-coded in prod) before this pin."""

    def test_passes_all_breaker_knobs_from_settings(self, monkeypatch):
        monkeypatch.setenv("DISCOGS_BREAKER_FAILURE_THRESHOLD", "7")
        monkeypatch.setenv("DISCOGS_BREAKER_REMAINING_FLOOR", "5")
        monkeypatch.setenv("DISCOGS_BREAKER_COOLDOWN_SECONDS", "45.0")
        monkeypatch.setenv("DISCOGS_BREAKER_TRIAL_WATCHDOG_MULTIPLIER", "9.0")
        get_settings.cache_clear()
        try:
            breaker = _build_breaker()
        finally:
            get_settings.cache_clear()
        assert breaker._failure_threshold == 7
        assert breaker._remaining_floor == 5
        assert breaker._cooldown_seconds == 45.0
        assert breaker._trial_watchdog_multiplier == 9.0


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
