"""Unit tests for discogs/memory_cache.py."""

import asyncio

import pytest
from pydantic import BaseModel

from discogs.memory_cache import (
    _cache_registry,
    _inflight_for,
    _set_cached_flag,
    async_cached,
    clear_all_caches,
    create_ttl_cache,
    evict_cached,
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
# Concurrent request coalescing (LML#537)
# ---------------------------------------------------------------------------


class TestAsyncCachedInFlightDedup:
    """`@async_cached` coalesces concurrent callers for the same key onto a
    single in-flight Future so the wrapped function runs once per key per
    in-flight window — closing the non-atomic check-then-set race documented
    in LML#537. Each test creates its own isolated cache to avoid test-order
    dependencies on a shared in-flight map."""

    @pytest.mark.asyncio
    async def test_concurrent_same_key_calls_underlying_once(self):
        # The race: N concurrent callers all see L1 miss, all enter
        # fallthrough independently. With the in-flight Future, the leader
        # runs once and broadcasts to all followers.
        cache = create_ttl_cache(maxsize=10, ttl=300)
        call_count = 0

        @async_cached(cache)
        async def slow_fetch(arg):
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(0.05)
            return {"data": arg, "cached": False}

        results = await asyncio.gather(*[slow_fetch("x") for _ in range(5)])

        assert call_count == 1
        for r in results:
            assert r["data"] == "x"

    @pytest.mark.asyncio
    async def test_concurrent_distinct_keys_each_call_underlying(self):
        # Dedup is key-scoped, not global: concurrent calls with distinct
        # keys each invoke the function once.
        cache = create_ttl_cache(maxsize=10, ttl=300)
        call_count = 0

        @async_cached(cache)
        async def slow_fetch(arg):
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(0.01)
            return {"data": arg, "cached": False}

        keys = ["a", "b", "c", "d", "e"]
        await asyncio.gather(*[slow_fetch(k) for k in keys])

        assert call_count == 5

    @pytest.mark.asyncio
    async def test_leader_raise_propagates_to_followers(self):
        # Transient API failure: leader raises, all followers re-raise the
        # same exception type, function ran exactly once. A subsequent serial
        # call starts fresh — no exception pinned in the in-flight map.
        cache = create_ttl_cache(maxsize=10, ttl=300)
        call_count = 0

        @async_cached(cache)
        async def raising_fetch(arg):
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(0.01)
            raise RuntimeError("upstream boom")

        results = await asyncio.gather(
            *[raising_fetch("x") for _ in range(4)], return_exceptions=True
        )
        assert call_count == 1
        assert all(isinstance(r, RuntimeError) for r in results)
        assert all(str(r) == "upstream boom" for r in results)

        # Fresh serial call: in-flight entry was cleared, function runs again.
        with pytest.raises(RuntimeError):
            await raising_fetch("x")
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_leader_none_result_followers_get_none(self):
        # None results don't get cached in the L1 TTL (existing contract).
        # Followers must still receive the None verbatim (no timeout, no
        # raise), and the in-flight entry must be cleared so a subsequent
        # call re-invokes the function.
        cache = create_ttl_cache(maxsize=10, ttl=300)
        call_count = 0

        @async_cached(cache)
        async def maybe_none(arg):
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(0.01)
            return None

        results = await asyncio.gather(*[maybe_none("x") for _ in range(4)])

        assert call_count == 1
        for r in results:
            assert r is None
        assert len(cache) == 0

        # Subsequent serial call invokes the function again — None wasn't pinned.
        result = await maybe_none("x")
        assert result is None
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_followers_get_cached_flag_true_when_result_supports_it(self):
        # When the leader's result is a dict with a `cached` field, followers
        # should receive it with `cached=True` set, matching the serial cache-hit
        # path's behavior.
        cache = create_ttl_cache(maxsize=10, ttl=300)

        @async_cached(cache)
        async def fetch(arg):
            await asyncio.sleep(0.01)
            return {"data": arg, "cached": False}

        results = await asyncio.gather(*[fetch("x") for _ in range(3)])

        # At least one of the three is the leader (cached=False on its result
        # because the leader returns its own freshly-computed value). The
        # followers receive the result via the future and should be marked
        # cached=True. We don't care which order — just that at least one is
        # marked cached=True so the seam-level instrumentation is consistent.
        cached_count = sum(1 for r in results if r["cached"] is True)
        assert cached_count >= 2  # at least 2 followers see cached=True

    @pytest.mark.asyncio
    async def test_skip_cache_in_follower_short_circuits_independently(self):
        # `should_skip_cache()` short-circuits at the top of the wrapper,
        # before any cache or in-flight bookkeeping. A skip-cache caller
        # bypasses the dedup entirely and invokes the underlying function
        # directly — even while a leader is mid-await on the same key.
        cache = create_ttl_cache(maxsize=10, ttl=300)
        call_count = 0

        @async_cached(cache)
        async def fetch(arg):
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(0.02)
            return {"data": arg, "cached": False}

        async def normal_caller():
            return await fetch("x")

        async def skip_caller():
            set_skip_cache(True)
            try:
                return await fetch("x")
            finally:
                set_skip_cache(False)

        await asyncio.gather(normal_caller(), skip_caller())
        # Two callers entered the function: the leader (normal) and the
        # skip-cache caller. The skip-cache caller does not share the
        # leader's future.
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_inflight_entry_cleaned_up_after_resolve(self):
        # After `await wrapper(...)` returns, the in-flight map for that key
        # must be empty. Otherwise abandoned futures pile up and the next
        # concurrent batch would still observe leader/follower coalescing,
        # but cleanup is what makes the map memory-stable.
        cache = create_ttl_cache(maxsize=10, ttl=300)

        @async_cached(cache)
        async def fetch(arg):
            await asyncio.sleep(0.01)
            return {"data": arg, "cached": False}

        await fetch("x")
        assert _inflight_for(cache) == {}

        # A second concurrent batch hits the cache directly (already populated)
        # so it never enters the in-flight branch — still must leave the map
        # empty.
        await asyncio.gather(*[fetch("x") for _ in range(3)])
        assert _inflight_for(cache) == {}

    @pytest.mark.asyncio
    async def test_evict_cached_does_not_touch_inflight(self):
        # `evict_cached` operates on the TTLCache only — it does NOT pop the
        # in-flight Future map. So a mid-await eviction must leave the
        # leader's future intact: the leader still resolves, followers still
        # get the result. The downstream L1 state after the race is governed
        # by normal evict + write ordering (covered by existing tests); the
        # invariant this test pins is the dedup-layer one.
        cache = create_ttl_cache(maxsize=10, ttl=300)
        call_count = 0

        @async_cached(cache)
        async def fetch(arg):
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(0.05)
            return {"data": arg, "cached": False}

        inflight_during_evict: dict[str, asyncio.Future] = {}

        async def follower():
            return await fetch("x")

        async def evictor():
            # Wait a tick so the leader is mid-await with the future pinned.
            await asyncio.sleep(0.01)
            # Snapshot the in-flight map, call evict, confirm the map is
            # unchanged. The leader's future is still in place after evict.
            before = dict(_inflight_for(cache))
            evict_cached(fetch, "x")
            after = dict(_inflight_for(cache))
            inflight_during_evict.update(after)
            assert before == after, "evict_cached must not touch the in-flight map"
            assert len(after) == 1, "leader's future must still be pinned"

        leader_task = asyncio.create_task(fetch("x"))
        follower_task = asyncio.create_task(follower())
        evictor_task = asyncio.create_task(evictor())

        leader_result, follower_result, _ = await asyncio.gather(
            leader_task, follower_task, evictor_task
        )

        # Both leader and follower see the same data — eviction didn't sever
        # the future.
        assert leader_result["data"] == "x"
        assert follower_result["data"] == "x"
        # Only one underlying call — the follower joined the leader's future.
        assert call_count == 1
        # Leader's `finally` popped its entry; map empty post-resolve.
        assert _inflight_for(cache) == {}
        # The evictor saw the leader's pinned future mid-flight.
        assert len(inflight_during_evict) == 1


# ---------------------------------------------------------------------------
# evict_cached (LML#498)
# ---------------------------------------------------------------------------


class TestEvictCached:
    """`evict_cached` mirrors the @async_cached key derivation so a single
    helper at the router seam can drop a specific entry without re-deriving
    the cache reference or key shape. Pins parity with the decorator so the
    two never drift (LML#498)."""

    @pytest.mark.asyncio
    async def test_returns_false_on_miss(self):
        cache = create_ttl_cache(maxsize=10, ttl=300)

        @async_cached(cache)
        async def my_func(arg):
            return arg

        # Nothing primed; eviction is a no-op and reports it.
        assert evict_cached(my_func, "never-primed") is False

    @pytest.mark.asyncio
    async def test_returns_true_and_removes_entry(self):
        cache = create_ttl_cache(maxsize=10, ttl=300)

        @async_cached(cache)
        async def my_func(arg):
            return {"data": arg, "cached": False}

        await my_func("a")
        assert len(cache) == 1
        assert evict_cached(my_func, "a") is True
        assert len(cache) == 0

    @pytest.mark.asyncio
    async def test_next_call_repopulates(self):
        # After eviction the wrapping decorator's miss branch re-runs the
        # underlying function and re-fills the cache. This is the property
        # the ?refresh=true endpoint relies on.
        cache = create_ttl_cache(maxsize=10, ttl=300)
        call_count = 0

        @async_cached(cache)
        async def my_func(arg):
            nonlocal call_count
            call_count += 1
            return {"data": arg, "cached": False}

        await my_func("a")
        await my_func("a")  # cache hit
        assert call_count == 1

        assert evict_cached(my_func, "a") is True
        await my_func("a")  # miss after eviction → invokes underlying
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_normalization_parity_with_decorator(self):
        # The decorator collapses diacritic/case variants to one entry; the
        # evictor MUST use the same key derivation so passing the ASCII form
        # drops an entry primed by the diacritic form. Otherwise a refresh
        # path could silently fail to clear what the cache hit serves.
        cache = create_ttl_cache(maxsize=10, ttl=300)

        @async_cached(cache)
        async def search(artist):
            return {"artist": artist, "cached": False}

        await search("Sonido Dueñez")
        assert len(cache) == 1
        # Evict using the ASCII spelling — should still hit the same key.
        assert evict_cached(search, "Sonido Duenez") is True
        assert len(cache) == 0

    @pytest.mark.asyncio
    async def test_does_not_evict_unrelated_entries(self):
        cache = create_ttl_cache(maxsize=10, ttl=300)

        @async_cached(cache)
        async def my_func(arg):
            return arg

        await my_func("a")
        await my_func("b")
        assert len(cache) == 2

        assert evict_cached(my_func, "a") is True
        assert len(cache) == 1
        # "b" still primed.
        assert evict_cached(my_func, "b") is True
        assert len(cache) == 0

    def test_raises_on_non_decorated_callable(self):
        async def plain_func(arg):
            return arg

        with pytest.raises(TypeError, match="not @async_cached"):
            evict_cached(plain_func, "x")

    @pytest.mark.asyncio
    async def test_evicts_method_using_class_attribute(self):
        # Router-side call shape: `evict_cached(DiscogsService.method, id)`.
        # The class attribute resolves to the wrapper directly (no bound-method
        # __getattr__ shenanigans), so the stashed cache+func_name are visible.
        # Args are passed AFTER `self` is stripped, matching the cache key the
        # decorator stored.
        cache = create_ttl_cache(maxsize=10, ttl=300)

        class MyService:
            @async_cached(cache)
            async def fetch(self, item_id):
                return {"id": item_id, "cached": False}

        svc = MyService()
        await svc.fetch(42)
        assert len(cache) == 1

        assert evict_cached(MyService.fetch, 42) is True
        assert len(cache) == 0


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
