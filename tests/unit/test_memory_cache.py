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
    make_normalized_cache_key,
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
# make_normalized_cache_key (LML#342 / A5)
# ---------------------------------------------------------------------------


class TestMakeNormalizedCacheKey:
    """The normalized variant runs string args through `to_match_form` (the same
    helper the resolver pre-pass uses) before hashing, so cache hits collapse
    diacritic and case variations that Discogs returns the same data for. Pins
    the contract that the in-process @async_cached decorators rely on (#342)."""

    def test_diacritic_variants_collapse_to_same_key(self):
        # Same artist with and without the eñe — both should serve from the
        # same cache entry. This is the bug #342 was filed to fix.
        k1 = make_normalized_cache_key("search", "Sonido Dueñez")
        k2 = make_normalized_cache_key("search", "Sonido Duenez")
        assert k1 == k2

    def test_case_variants_collapse_to_same_key(self):
        # Lowercase / uppercase / mixed — `to_match_form` lowercases, so all
        # three normalize to the same string and hash to the same key.
        k1 = make_normalized_cache_key("search", "Stereolab")
        k2 = make_normalized_cache_key("search", "stereolab")
        k3 = make_normalized_cache_key("search", "STEREOLAB")
        assert k1 == k2 == k3

    def test_parens_preserved_no_false_collapse(self):
        # `(Remix)` carries meaning; `to_match_form` does not strip it. The
        # base track and the remix MUST hash to distinct keys so the cache
        # never serves a remix's data when asked for the base track.
        k1 = make_normalized_cache_key("search", "Felt")
        k2 = make_normalized_cache_key("search", "Felt (Remix)")
        assert k1 != k2

    def test_non_string_args_pass_through(self):
        # Integers, bools, Nones flow through unchanged — only strings get the
        # normalization pass. Two int-keyed calls with the same int hash
        # identically; an int vs the same number as a string still differ
        # because the type information is part of the hash.
        k_int = make_normalized_cache_key("get_release", 12345)
        k_int_again = make_normalized_cache_key("get_release", 12345)
        k_str = make_normalized_cache_key("get_release", "12345")
        assert k_int == k_int_again
        assert k_int != k_str

    def test_normalizes_kwargs_too(self):
        # The pre-pass runs over kwargs as well as positional args so a
        # function called with keyword arguments gets the same key collapse.
        k1 = make_normalized_cache_key("search", artist="Sonido Dueñez")
        k2 = make_normalized_cache_key("search", artist="Sonido Duenez")
        assert k1 == k2

    def test_funcname_not_normalized(self):
        # The function-name argument is identity-preserved — case-sensitive,
        # not diacritic-folded — because it's an internal identifier not a
        # user-supplied string.
        k1 = make_normalized_cache_key("get_Release", "x")
        k2 = make_normalized_cache_key("get_release", "x")
        assert k1 != k2

    def test_punctuation_distinguishes_distinct_artists(self):
        # `to_match_form` does NOT strip punctuation, so two real-world artists
        # whose names differ only by punctuation must still hash to distinct
        # keys. Pins the safety property that the resolver pre-pass's
        # normalization layer guarantees so a future change to `to_match_form`
        # in wxyc-etl can't silently collapse them.
        k1 = make_normalized_cache_key("search", "Felt")
        k2 = make_normalized_cache_key("search", "Felt.")
        assert k1 != k2

    def test_pydantic_model_string_fields_are_normalized(self):
        # @async_cached decorates DiscogsService methods that take pydantic
        # models like DiscogsSearchRequest as their argument. Without
        # recursing into the model, two requests with the same logical
        # artist but different spellings (e.g. "Sonido Dueñez" vs
        # "Sonido Duenez") hash differently because the model's repr
        # carries the un-normalized strings. Pin recursion into BaseModel
        # so the search() cache path actually delivers #342's win.
        from discogs.models import DiscogsSearchRequest

        req_diacritic = DiscogsSearchRequest(artist="Sonido Dueñez", album="X")
        req_ascii = DiscogsSearchRequest(artist="Sonido Duenez", album="X")
        k1 = make_normalized_cache_key("search", req_diacritic)
        k2 = make_normalized_cache_key("search", req_ascii)
        assert k1 == k2

    def test_pydantic_model_passes_through_meaningful_distinctions(self):
        # Same recursion, opposite direction: if two requests differ in a
        # meaningful field, they MUST still hash differently after recursion.
        from discogs.models import DiscogsSearchRequest

        req_base = DiscogsSearchRequest(artist="Sonido Duenez", track="Cumbia")
        req_remix = DiscogsSearchRequest(artist="Sonido Duenez", track="Cumbia (Remix)")
        k1 = make_normalized_cache_key("search", req_base)
        k2 = make_normalized_cache_key("search", req_remix)
        assert k1 != k2

    def test_dict_argument_recurses_into_string_values(self):
        # General dict recursion (not just pydantic models). Future callers
        # that pass plain dicts get the same collapse behavior.
        k1 = make_normalized_cache_key("search", {"artist": "Björk"})
        k2 = make_normalized_cache_key("search", {"artist": "bjork"})
        assert k1 == k2


class TestAsyncCachedNormalization:
    """End-to-end: @async_cached should serve diacritic/case variants from the
    same cache entry, the way #342's user-typed search inputs flow through
    DiscogsService methods at runtime."""

    @pytest.mark.asyncio
    async def test_decorator_collapses_diacritic_variants(self):
        cache = create_ttl_cache(maxsize=10, ttl=300)
        call_count = 0

        @async_cached(cache)
        async def search(artist):
            nonlocal call_count
            call_count += 1
            return {"data": artist, "cached": False}

        await search("Sonido Dueñez")
        result2 = await search("Sonido Duenez")
        # Same call site, same normalized key — second call must hit the cache.
        assert call_count == 1
        assert result2["cached"] is True
        assert len(cache) == 1

    @pytest.mark.asyncio
    async def test_decorator_keeps_meaningful_variants_distinct(self):
        cache = create_ttl_cache(maxsize=10, ttl=300)
        call_count = 0

        @async_cached(cache)
        async def search(track):
            nonlocal call_count
            call_count += 1
            return {"data": track, "cached": False}

        await search("Felt")
        await search("Felt (Remix)")
        # Different normalized keys — no false collapse.
        assert call_count == 2
        assert len(cache) == 2

    @pytest.mark.asyncio
    async def test_skip_cache_still_bypasses_with_diacritic_arg(self):
        # The skip_cache flag must short-circuit BEFORE normalization runs —
        # otherwise the bypass branch would still pay the to_match_form cost
        # for diacritic-bearing args. Regression coverage for the order of
        # operations after A5.
        cache = create_ttl_cache(maxsize=10, ttl=300)
        call_count = 0

        @async_cached(cache)
        async def search(artist):
            nonlocal call_count
            call_count += 1
            return artist

        set_skip_cache(True)
        try:
            await search("Sonido Dueñez")
            await search("Sonido Dueñez")
            assert call_count == 2  # every call goes through to the function
            assert len(cache) == 0
        finally:
            set_skip_cache(False)

    @pytest.mark.asyncio
    async def test_self_stripping_still_works_with_diacritic_args(self):
        # `self` is stripped from cache_args BEFORE the per-arg normalization
        # pass runs. Pins the order so two instances calling the same method
        # with the same diacritic-bearing arg share a single cache entry.
        cache = create_ttl_cache(maxsize=10, ttl=300)

        class MyService:
            @async_cached(cache)
            async def method(self, arg):
                return arg

        svc1 = MyService()
        svc2 = MyService()

        result1 = await svc1.method("Sonido Dueñez")
        result2 = await svc2.method("Sonido Duenez")
        # Same normalized key across instances — both `self` strip and arg
        # normalization apply, leaving identical hashes.
        assert result1 == "Sonido Dueñez"
        assert result2 == "Sonido Dueñez"  # served from cache
        assert len(cache) == 1


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
