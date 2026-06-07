# LML#498 — L1 cache refresh path

## Problem (recap)

LML's L1 `@async_cached` decorator pins cached Discogs entity objects for the full TTL (24h artist, 4h release, etc.). After a manual PG fix to `discogs.cache_service` PG rows, L1 keeps serving the stale object until natural TTL expiry or service restart. `skip_cache=true` bypasses but doesn't evict, so it can't actually warm new data into L1.

Concrete trigger: 2026-06-05, after fixing `artist.profile` directly in PG for artist 6998498 (Yetsuby), the iOS detail view stayed broken for the L1 TTL window.

## Scope (this PR)

**Option A only.** `?refresh=true` query param on the three entity GET endpoints. Option B (admin invalidate) is intentionally deferred — the issue recommends shipping A first, and per-entity coverage handles the common manual-fix case.

Endpoints:
- `GET /api/v1/discogs/artist/{artist_id}?refresh=true`
- `GET /api/v1/discogs/release/{release_id}?refresh=true`
- `GET /api/v1/discogs/entity/{entity_type}/{entity_id}?refresh=true` (artist / release / master)

The issue mentions a hypothetical `/api/v1/discogs/label/{id}` — it does not exist (`get_label_image` is only ever called from the lookup orchestrator). Skip.

## Design

### 1. `discogs/memory_cache.py` — stash cache + func_name on the wrapper, expose `evict_cached`

The wrapping decorator already owns the key derivation (`make_normalized_cache_key(func.__name__, *cache_args, **kwargs)`). To stay in lock-step with that derivation, the eviction surface MUST live in the same file. Two changes:

1. `async_cached` decorator attaches `wrapper._lml_cache = cache` and `wrapper._lml_func_name = func.__name__` for introspection. These are private and only used by `evict_cached`.

2. New module-level helper:

   ```python
   def evict_cached(cached_func, *args, **kwargs) -> bool:
       """Evict the L1 entry for `cached_func(*args, **kwargs)`. Returns True if removed.

       Pass args AFTER any `self`-strip (i.e., the same args the underlying function
       receives). Raises TypeError if the function is not @async_cached.
       """
       cache = getattr(cached_func, "_lml_cache", None)
       func_name = getattr(cached_func, "_lml_func_name", None)
       if cache is None or func_name is None:
           raise TypeError(f"{cached_func!r} is not @async_cached")
       key = make_normalized_cache_key(func_name, *args, **kwargs)
       return cache.pop(key, None) is not None
   ```

This means router code reads `evict_cached(DiscogsService.get_artist_details, artist_id)` rather than re-deriving the cache + name pair locally. If the decorator's key derivation ever shifts, this stays correct.

### 2. `discogs/router.py` — `?refresh=true` query param

Each handler gets a new `refresh: bool = Query(False, …)` parameter. When `True`:

1. Evict the L1 entry for `(method, id)`.
2. Record `memory_evictions_refresh` counter on the per-request stats dict (via `get_cache_stats_recorder().record("memory_evictions_refresh")`) — only when eviction actually removed something, so the counter measures real evictions, not no-op probes.
3. Fall through to the existing service call, which now misses L1 and traverses L2 (PG) → L3 (Discogs API).

The eviction is **fire-and-forget at the router seam**: we don't proactively re-warm or fetch twice. The next service call repopulates L1 naturally via the existing `@async_cached` write-on-miss path. This matches the issue's "don't fire a side-effecting refetch from within the eviction handler" constraint.

For `/entity/{entity_type}/{entity_id}`, dispatch on `entity_type` to evict the right cache (artist/release/master).

### 3. Cache-stats key declaration

`memory_evictions_refresh` must be declared in the per-request stats init so it shows up as `0` even when never recorded — keeps the PostHog event shape stable. `init_cache_stats(extra_keys=[…])` lives in `wxyc_fastapi.observability.cache_stats`; LML calls it in `lookup/router.py`. Locate that call and add the new key to the `extra_keys` argument.

### 4. Tests

**`tests/unit/test_memory_cache.py`**:
- `evict_cached` returns False on miss.
- `evict_cached` returns True and removes the entry on hit.
- After `evict_cached`, the next call invokes the underlying function (not cache hit).
- Normalization stays consistent with the decorator: `evict_cached(f, "Sonido Dueñez")` removes the entry primed by `f("Sonido Duenez")`.
- `evict_cached` on a non-decorated function raises TypeError.

**`tests/unit/test_discogs_router.py`**:
- `?refresh=true` on `/discogs/artist/{id}` calls `get_artist_details` after evicting L1.
- `?refresh=false` or absent unchanged behavior.
- `?refresh=true` on `/discogs/release/{id}` covers release leg.
- `?refresh=true` on `/discogs/entity/{type}/{id}` covers all three entity types.
- Counter is incremented on successful eviction (we can mock `get_cache_stats_recorder` or assert on `get_cache_stats()` after `init_cache_stats()`).

## Out of scope (follow-up)

- Option B (`POST /admin/cache/invalidate`) — file a follow-up if/when we need bulk or search-cache eviction.
- `search_cache` / `validation_cache` / `track_cache` eviction — keyed by string args, no entity ID, so `?refresh=true` per-entity doesn't address them. Belongs in the admin-invalidate ticket.

## Acceptance checklist

- [x] `GET /api/v1/discogs/artist/{id}?refresh=true` returns fresh data after an underlying PG row update, without a service restart.
- [x] L1 entry for that artist is gone after the request.
- [x] Existing `?refresh` absent or `=false` behavior unchanged.
- [x] Cache-stats counter `memory_evictions_refresh` distinguishes refresh-driven evictions from natural TTL expiry.
