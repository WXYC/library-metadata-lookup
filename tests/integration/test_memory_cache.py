"""Integration tests for the in-memory TTL cache."""

import pytest

from discogs.memory_cache import (
    async_cached,
    clear_all_caches,
    create_ttl_cache,
    make_cache_key,
    set_skip_cache,
)

pytestmark = pytest.mark.integration


class TestCacheIntegration:
    @pytest.fixture(autouse=True)
    def cleanup(self):
        yield
        clear_all_caches()
        set_skip_cache(False)

    @pytest.mark.asyncio
    async def test_cache_hit_and_miss(self):
        """Second call with same args returns cached result."""
        cache = create_ttl_cache(maxsize=10, ttl=60)
        call_count = 0

        @async_cached(cache)
        async def fetch_data(key: str):
            nonlocal call_count
            call_count += 1
            return {"data": key}

        result1 = await fetch_data("abc")
        result2 = await fetch_data("abc")

        assert result1 == result2
        assert call_count == 1  # second call was cached

    @pytest.mark.asyncio
    async def test_different_args_separate_entries(self):
        cache = create_ttl_cache(maxsize=10, ttl=60)
        call_count = 0

        @async_cached(cache)
        async def fetch_data(key: str):
            nonlocal call_count
            call_count += 1
            return {"data": key}

        result1 = await fetch_data("abc")
        result2 = await fetch_data("xyz")

        assert result1 != result2
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_skip_cache_bypasses(self):
        cache = create_ttl_cache(maxsize=10, ttl=60)
        call_count = 0

        @async_cached(cache)
        async def fetch_data(key: str):
            nonlocal call_count
            call_count += 1
            return {"data": key}

        await fetch_data("abc")
        set_skip_cache(True)
        await fetch_data("abc")

        assert call_count == 2  # both calls executed

    @pytest.mark.asyncio
    async def test_clear_invalidation(self):
        cache = create_ttl_cache(maxsize=10, ttl=60)
        call_count = 0

        @async_cached(cache)
        async def fetch_data(key: str):
            nonlocal call_count
            call_count += 1
            return {"data": key}

        await fetch_data("abc")
        clear_all_caches()
        await fetch_data("abc")

        assert call_count == 2  # cache was cleared

    @pytest.mark.asyncio
    async def test_none_not_cached(self):
        """None results should not be cached."""
        cache = create_ttl_cache(maxsize=10, ttl=60)
        call_count = 0

        @async_cached(cache)
        async def fetch_data(key: str):
            nonlocal call_count
            call_count += 1
            return None

        await fetch_data("abc")
        await fetch_data("abc")

        assert call_count == 2  # None wasn't cached

    def test_make_cache_key_deterministic(self):
        key1 = make_cache_key("func", "arg1", "arg2")
        key2 = make_cache_key("func", "arg1", "arg2")
        assert key1 == key2

    def test_make_cache_key_different_args(self):
        key1 = make_cache_key("func", "arg1")
        key2 = make_cache_key("func", "arg2")
        assert key1 != key2
