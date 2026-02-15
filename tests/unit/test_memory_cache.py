"""Unit tests for discogs/memory_cache.py."""

import pytest
from pydantic import BaseModel

from discogs.memory_cache import (
    _cache_registry,
    _set_cached_flag,
    async_cached,
    clear_all_caches,
    create_ttl_cache,
    get_release_cache,
    get_search_cache,
    get_track_cache,
    make_cache_key,
    set_skip_cache,
    should_skip_cache,
)

# ---------------------------------------------------------------------------
# skip_cache flag
# ---------------------------------------------------------------------------


class TestSkipCache:
    def test_default_is_false(self):
        assert should_skip_cache() is False

    def test_set_and_check(self):
        set_skip_cache(True)
        assert should_skip_cache() is True
        set_skip_cache(False)
        assert should_skip_cache() is False


# ---------------------------------------------------------------------------
# make_cache_key
# ---------------------------------------------------------------------------


class TestMakeCacheKey:
    def test_deterministic(self):
        k1 = make_cache_key("func", "a", "b", x=1)
        k2 = make_cache_key("func", "a", "b", x=1)
        assert k1 == k2

    def test_different_args_differ(self):
        k1 = make_cache_key("func", "a")
        k2 = make_cache_key("func", "b")
        assert k1 != k2

    def test_different_funcs_differ(self):
        k1 = make_cache_key("func1", "a")
        k2 = make_cache_key("func2", "a")
        assert k1 != k2

    def test_kwargs_order_independent(self):
        k1 = make_cache_key("f", x=1, y=2)
        k2 = make_cache_key("f", y=2, x=1)
        assert k1 == k2


# ---------------------------------------------------------------------------
# create_ttl_cache / clear_all_caches
# ---------------------------------------------------------------------------


class TestCreateAndClear:
    def test_create_registers_cache(self):
        initial_count = len(_cache_registry)
        cache = create_ttl_cache(maxsize=10, ttl=60)
        assert len(_cache_registry) == initial_count + 1
        assert cache in _cache_registry

    def test_clear_all_caches_empties_entries(self):
        cache = create_ttl_cache(maxsize=10, ttl=60)
        cache["key"] = "value"
        assert len(cache) == 1
        clear_all_caches()
        assert len(cache) == 0


# ---------------------------------------------------------------------------
# _set_cached_flag
# ---------------------------------------------------------------------------


class TestSetCachedFlag:
    def test_none_returns_none(self):
        assert _set_cached_flag(None, cached=True) is None

    def test_dict_with_cached_key(self):
        d = {"cached": False, "data": "test"}
        result = _set_cached_flag(d, cached=True)
        assert result["cached"] is True
        # Original should be unchanged (copy)
        assert d["cached"] is False

    def test_dict_without_cached_key(self):
        d = {"data": "test"}
        result = _set_cached_flag(d, cached=True)
        assert result is d  # returned as-is

    def test_pydantic_model_with_cached(self):
        class MyModel(BaseModel):
            cached: bool = False
            value: str = "test"

        m = MyModel()
        result = _set_cached_flag(m, cached=True)
        assert result.cached is True
        assert m.cached is False  # original unchanged

    def test_other_type_returned_as_is(self):
        result = _set_cached_flag("string", cached=True)
        assert result == "string"


# ---------------------------------------------------------------------------
# async_cached decorator
# ---------------------------------------------------------------------------


class TestAsyncCached:
    @pytest.mark.asyncio
    async def test_cache_miss_then_hit(self):
        cache = create_ttl_cache(maxsize=10, ttl=300)
        call_count = 0

        @async_cached(cache)
        async def my_func(arg):
            nonlocal call_count
            call_count += 1
            return {"data": arg, "cached": False}

        # First call: cache miss
        result1 = await my_func("a")
        assert result1["data"] == "a"
        assert call_count == 1

        # Second call: cache hit
        result2 = await my_func("a")
        assert result2["data"] == "a"
        assert result2["cached"] is True
        assert call_count == 1  # not called again

    @pytest.mark.asyncio
    async def test_skip_cache_bypasses(self):
        cache = create_ttl_cache(maxsize=10, ttl=300)
        call_count = 0

        @async_cached(cache)
        async def my_func(arg):
            nonlocal call_count
            call_count += 1
            return arg

        set_skip_cache(True)
        try:
            await my_func("a")
            await my_func("a")
            assert call_count == 2
        finally:
            set_skip_cache(False)

    @pytest.mark.asyncio
    async def test_none_result_not_cached(self):
        cache = create_ttl_cache(maxsize=10, ttl=300)
        call_count = 0

        @async_cached(cache)
        async def my_func():
            nonlocal call_count
            call_count += 1
            return None

        await my_func()
        await my_func()
        assert call_count == 2  # called both times since None not cached

    @pytest.mark.asyncio
    async def test_strips_self_from_cache_key(self):
        """For instance methods, 'self' should not be part of the cache key."""
        cache = create_ttl_cache(maxsize=10, ttl=300)

        class MyService:
            @async_cached(cache)
            async def method(self, arg):
                return arg

        svc1 = MyService()
        svc2 = MyService()

        result1 = await svc1.method("x")
        result2 = await svc2.method("x")
        # Both should use same cache key (self stripped)
        assert result1 == result2
        assert len(cache) == 1

    @pytest.mark.asyncio
    async def test_different_args_separate_entries(self):
        cache = create_ttl_cache(maxsize=10, ttl=300)

        @async_cached(cache)
        async def my_func(arg):
            return arg

        await my_func("a")
        await my_func("b")
        assert len(cache) == 2


# ---------------------------------------------------------------------------
# Lazy cache getters
# ---------------------------------------------------------------------------


class TestLazyCacheGetters:
    def test_get_track_cache(self):
        cache = get_track_cache()
        assert cache is not None

    def test_get_release_cache(self):
        cache = get_release_cache()
        assert cache is not None

    def test_get_search_cache(self):
        cache = get_search_cache()
        assert cache is not None


# ---------------------------------------------------------------------------
# Module-level __getattr__
# ---------------------------------------------------------------------------


class TestModuleGetattr:
    def test_track_cache_constant(self):
        import discogs.memory_cache as mc

        cache = mc.TRACK_CACHE
        assert cache is not None

    def test_release_cache_constant(self):
        import discogs.memory_cache as mc

        cache = mc.RELEASE_CACHE
        assert cache is not None

    def test_search_cache_constant(self):
        import discogs.memory_cache as mc

        cache = mc.SEARCH_CACHE
        assert cache is not None

    def test_unknown_attr_raises(self):
        import discogs.memory_cache as mc

        with pytest.raises(AttributeError, match="no attribute"):
            _ = mc.NONEXISTENT
