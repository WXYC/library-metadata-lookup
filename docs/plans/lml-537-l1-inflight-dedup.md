# LML #537 (follow-up) — L1 in-flight dedup in `@async_cached`

**Parent issue:** [WXYC/library-metadata-lookup#537](https://github.com/WXYC/library-metadata-lookup/issues/537) — *Discogs cache hit ratio plateaued at ~50%*

**Sibling investigations:** issue #537's compounding-cause set: (1) `get_release` predicate strictness on artwork columns, (2) **L1 `@async_cached` race (this plan)**, (3) Discogs semaphore wait. The first ticket lands the highest-leverage, smallest-blast-radius fix.

## Background

The `@async_cached` decorator at `discogs/memory_cache.py:188-221` does a non-atomic check-then-set across an `await` of the wrapped function:

```python
if key in cache:                                    # check
    return _set_cached_flag(cache[key], cached=True)
result = await func(*args, **kwargs)                # await yields the event loop
if result is not None:
    cache[key] = result                             # set
```

For network-bound wrapped functions (`get_release`, `get_artist_details`, `search`, `validate_track_on_release`, `_search_track`, `get_master`, `get_label_image`, and `cache_service.py`'s artist-search cache), the `await` window covers a PG round-trip plus the Discogs API call — easily hundreds of milliseconds to seconds. Every caller that arrives during that window sees the same L1 miss and enters the fallthrough seam independently.

Production tracing (issue #537 latest comment) shows duplicate fetches inside a 10-second window for the same release_id: release 28184356 fetched 3× at 13:05:41/44/45, release 3657075 fetched 3× at 13:05:34/39/40. Two real concurrent paths put the same `release_id` into a single `asyncio.gather` within one request:

1. **`search_compilations_for_track` Wave-A/Wave-B merge** (`lookup/orchestrator.py:1331-1356`) dedups by **album-name lowercase**, not release_id. Two release entries with different album-string formatting (typographic vs ASCII apostrophe, trailing version tag) can share a `release_id`. They flow into `_chunked_gather(raw_releases, process_release, MAX_SEARCH_RESULTS=5)`, where `process_release` → `validate_track_on_release(release_id, …)` → `get_release(release_id)` runs in `asyncio.gather`.
2. **Validation → get_release nesting**: `validate_track_on_release` itself wraps `get_release`. Even a single fan-out element drives two cached calls — one for the validation row, one for the release row. Five concurrent chunk items × two nested cached calls each is enough to reproduce the observed 2-3× duplicate fetches.

The duplicates multiply pressure on the 5-permit Discogs semaphore (`discogs/service.py:373-374`), inflating the `lml.discogs.semaphore` p95 wait (9.7s). Closing the race shrinks the API call count, which shrinks the semaphore queue.

## Scope of this change

A single file change to `discogs/memory_cache.py` adding per-key in-flight `asyncio.Future` coalescing inside `async_cached`. No call-site changes. No new public API. No behavioral change for serial callers, single-instance tests, or any code path that doesn't have concurrent L1 misses on the same key.

Out of scope (separate tickets):

- Predicate widening for `get_release`'s `is_pg_hit` (Cause #1).
- Bumping Discogs semaphore from 5 (Cause #3).
- Probe methodology fixes (`cache_warm_histogram.py` writer-source split).
- Anything in `lookup/orchestrator.py` or `discogs/fallthrough.py`.

## Design

### Mechanism

A per-cache `dict[str, asyncio.Future]` maps cache key → in-flight future for any key currently being resolved. The wrapper becomes:

```python
async def wrapper(*args, **kwargs) -> T:
    if should_skip_cache():
        return await func(*args, **kwargs)

    key = make_normalized_cache_key(...)

    if key in cache:                                      # L1 hit (unchanged)
        return _set_cached_flag(cache[key], cached=True)

    in_flight = _inflight_for(cache)
    if key in in_flight:                                  # follower path
        result = await in_flight[key]
        return _set_cached_flag(result, cached=True)

    future: asyncio.Future = asyncio.get_running_loop().create_future()
    in_flight[key] = future
    try:                                                  # leader path
        result = await func(*args, **kwargs)
        if result is not None:
            cache[key] = result
        future.set_result(result)
        return result
    except BaseException as exc:
        future.set_exception(exc)
        raise
    finally:
        in_flight.pop(key, None)
```

The per-cache map is stashed on the `TTLCache` instance (a single attribute set in `create_ttl_cache` — no global registry — so each cache owns its own in-flight set). This keeps the wrapper threadless and respects the fact that distinct caches with the same key string should not coalesce.

### Why a Future, not a Lock or Event

A `Lock` would serialize all callers through the seam, but the second arrival would still re-enter the cache lookup and might miss again if the TTL evicted between unlock and the second check. A `Future` lets the leader compute once and broadcast the result to all followers atomically.

### Cleanup invariant

The `finally` clause removes the in-flight entry **after** the future is resolved. Followers that took a reference before the `pop` are not affected — they hold the resolved future. New callers arriving after the `pop` either find the value in the L1 cache (success path) or start a fresh fetch (None/raise path).

### None-result semantics

The leader stores `result` in `cache[key]` only if non-None (preserving the existing "don't cache None" contract). The leader still does `future.set_result(None)` so followers receive the same None. The next arrival sees no `cache` entry and no `in_flight` entry → starts a fresh fetch. This matches the existing serial-caller behavior: None results don't pin a value, so the next call retries.

### Raise semantics

If `await func(...)` raises a non-cancellation `Exception`, the leader broadcasts it to followers via `future.set_exception(exc)`, then the `finally` clause pops the in-flight entry. Followers re-raise the same exception instance. The next caller (post-pop) starts fresh.

This matches the orchestrator's existing error-handling expectation: a transient Discogs failure should not silently pin a bad cached entry; the next call should retry. Followers see the exact same exception the leader saw — the future broadcasts the leader's instance, not a reconstruction, so Sentry deduplication, traceback chaining, and identity-based downstream checks behave normally.

### Cancellation semantics (the asymmetric case)

Cancellation is **not** a value — it's an out-of-band signal scoped to a task. In production, the leader and the followers can be in **unrelated request contexts** that happen to share a module-level singleton cache. If Request A's task is cancelled (per-request timeout, client disconnect), broadcasting the cancellation across the future would inject `CancelledError` into Request B's task tree, even though B was never cancelled. That's wrong.

The implementation handles cancellation specifically:

- **Leader cancelled**: the `except asyncio.CancelledError` arm cancels the future (so followers know the leader bailed) and re-raises, terminating the leader's task normally.
- **Follower receives cancellation from `await future`**: the `except asyncio.CancelledError` arm checks `asyncio.current_task().cancelling()`. If 0, the follower's own task was not cancelled — re-enter the wrapper to either join a fresh leader or become one. If >0, the follower was actually cancelled — re-raise.

This restores the asymmetry: cancellation is per-task, the dedup layer does not flatten it across unrelated requests. Cost: a cancelled leader trades one follower retry per concurrent caller (bounded by the orchestrator's concurrency cap). Benefit: one client's disconnect can't cascade into another client's failures.

### Cache-write failure isolation

The leader's broadcast (`future.set_result(result)`) fires **before** `cache[key] = result`. If the L1 write raises (e.g., a future cachetools eviction-callback throws), followers still see the successful fetch and only the leader's caller sees the cache-layer error. A `try/except` around the cache write logs and continues — a cache failure is not the same as a fetch failure.

### Interaction with `skip_cache` flag

`should_skip_cache()` short-circuits at the top of the wrapper, before any cache or in-flight bookkeeping. Skip-cache callers bypass both the L1 cache and the dedup, preserving the existing A/B-comparison contract (every skip-cache call hits the underlying function).

### Interaction with `evict_cached`

`evict_cached` only touches the `TTLCache` (`cache.pop(key, None)`). It does **not** touch the in-flight map. Justification: if an eviction races with an in-flight fetch, the leader has already started; the right behavior is to let it finish (followers get the value), then the next post-eviction caller will miss and fetch. Evicting an in-flight entry would just orphan the followers.

### Interaction with `clear_all_caches`

`clear_all_caches()` resets each registered cache's `_lml_inflight` map alongside the TTL contents. Without this, in-flight Futures attached lazily by `_inflight_for` would survive a clear and a post-clear caller could join a dead leader's future — defeating the point of the clear.

### Interaction with `should_skip_cache` across leader / follower

Edge case: leader started under `skip_cache=False`, follower arrives with `skip_cache=True`. The current `should_skip_cache()` is a `ContextVar`-scoped flag (per-request). A follower in a different request with `skip_cache=True` short-circuits at the top of the wrapper and never reaches the in-flight check — it gets its own underlying call, which is correct. The leader and follower are in different "skip-cache stances" and that's preserved.

## Tests (TDD)

Append to `tests/unit/test_memory_cache.py`. Each test is appended as a new method of a new `TestAsyncCachedInFlightDedup` class. Existing tests are not modified.

### Red phase — failing tests

All tests live in a new `TestAsyncCachedInFlightDedup` class with a docstring explaining the class tests "concurrent request coalescing in `@async_cached`'s in-flight Future map." Each test creates its own isolated `create_ttl_cache(...)` instance (matching the existing `TestAsyncCached` pattern in `test_memory_cache.py:319-405`) to avoid test-order dependencies on a shared cache.

1. **`test_concurrent_same_key_calls_underlying_once`** — N=5 concurrent `asyncio.gather` calls to the same wrapped key against a slow coroutine (await `asyncio.sleep(0.05)` inside). Pre-fix: call_count == 5. Post-fix: call_count == 1, all callers receive the same result.
2. **`test_concurrent_distinct_keys_each_call_underlying`** — N=5 concurrent calls with distinct keys. call_count == 5 always (sanity check that dedup is key-scoped, not global).
3. **`test_leader_raise_propagates_to_followers`** — leader's underlying function raises `RuntimeError`. All N followers also raise `RuntimeError`. call_count == 1 (only the leader entered the function). A subsequent serial call starts fresh (no pinned exception in the in-flight map).
4. **`test_leader_none_result_followers_get_none`** — leader returns None. All N followers receive None (explicitly asserted: `result is None` per follower, no timeout, no raise). No entry written to L1 cache. Subsequent serial call invokes the function again (no pinned None).
5. **`test_followers_get_cached_flag_true_when_result_supports_it`** — leader returns `{"data": "x", "cached": False}`. Followers get the same dict with `cached=True` set, matching the existing serial cache-hit semantics.
6. **`test_skip_cache_in_follower_short_circuits_independently`** — leader starts with `skip_cache=False` (no skip). Mid-await, a separate task with `skip_cache=True` calls the same key. The skip-cache caller bypasses both cache and dedup, invokes the function independently. (Tests the ContextVar boundary.)
7. **`test_inflight_entry_cleaned_up_after_resolve`** — after `await wrapper(...)` returns, `_inflight_for(cache)` is empty. Subsequent concurrent calls re-enter the leader/follower split correctly.
8. **`test_evict_cached_does_not_touch_inflight`** — leader is mid-await; concurrent `evict_cached` on the same key. Followers still get the leader's result; the L1 cache is then evicted; the next post-eviction call misses and re-fetches.

### Green phase — implementation

Single file edit at `discogs/memory_cache.py`. New module-level private helper `_inflight_for(cache)` that lazily attaches a `dict[str, asyncio.Future[Any]]` to the cache instance via `getattr(cache, "_lml_inflight", None) or setattr`. Wrapper rewritten as the design block above. `create_ttl_cache` is not modified (lazy attachment in `_inflight_for` avoids needing to touch the factory).

**Pre-decided** (per reviewer #2): `_inflight_for` is exported at module-level (parallel to `_normalize_for_cache_key` at `memory_cache.py:67`) so cleanup tests can assert `_inflight_for(cache) == {}` without reaching into `cache._lml_inflight` directly. The underscore-prefix keeps it private but accessible.

**Pre-decided** (per reviewer #3): the implementation includes a one-line comment at the `asyncio.get_running_loop().create_future()` site explaining that `get_running_loop()` is safe here because the wrapper is `async def` and only runs inside an active event loop.

### Refactor pass

After green, evaluate whether the in-flight dict needs a more precise type annotation (`dict[str, asyncio.Future[T]]` vs `dict[str, asyncio.Future[Any]]`); the existing wrapper uses `T = TypeVar("T")` but doesn't push it through to runtime, so `Any` is consistent with the surrounding code.

No behavioral changes during refactor.

## Risk surface

- **Memory leak via abandoned futures.** The `finally` clause always pops the in-flight entry, including on `CancelledError`. Followers see cancellation via the future being cancelled and either retry (if their own task was not cancelled) or propagate (if it was) — see the cancellation semantics section above.
- **Performance regression on serial callers.** The added `if key in in_flight` check is `O(1)` dict membership against a dict that's empty in steady-state for serial callers. Negligible.
- **Thread safety.** asyncio coroutines are single-threaded per event loop. The in-flight dict is touched only within the wrapper's coroutine body — no `await` between read and write of the dict — so no race within the dedup logic itself.
- **Cross-event-loop test pollution.** The in-flight map is stashed on the cache instance, which is shared across tests via the module-level registry. `clear_all_caches()` resets the per-cache `_lml_inflight` map alongside the TTL contents, so prior-test orphans don't survive into the next test. Each test in `TestAsyncCachedInFlightDedup` also creates its own `cache` via `create_ttl_cache`, providing belt-and-suspenders isolation.
- **Telemetry observability.** The follower path records a `memory_cache_inflight_join` event via the cache stats recorder so the dedup is visible in the cache-stats stream; without this, in-flight joins would be invisible to dashboards (the L1 hit counter wouldn't tick because the entry isn't in the TTLCache yet). The Sentry semaphore/limiter spans (`lml.discogs.semaphore`, `lml.discogs.rate_limiter`) still only attach to the leader's trace — a follower's transaction looks fast because it skipped the seam entirely. This is a deeper observability tradeoff and out of scope; the `memory_cache_inflight_join` counter is the operationally-useful signal for "is the dedup firing?"

## Acceptance

- All 8 new unit tests pass.
- Existing 36 `test_memory_cache.py` tests pass unchanged.
- `tests/integration/test_memory_cache.py` unchanged and passes.
- `ruff check` + `ruff format --check` clean.
- One follow-up PR (separate ticket) considers whether to add an integration-style test that exercises the dedup through `discogs/service.py:get_release` with a stubbed Discogs HTTP client, to pin the seam-level behavior end-to-end. Out of scope for this PR's TDD pass.

## Estimated diff

- `discogs/memory_cache.py`: ~30 lines added, ~5 modified, 0 removed.
- `tests/unit/test_memory_cache.py`: ~150 lines added.
- No other files.
