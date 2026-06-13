"""Caching utilities for Discogs API responses using TTL-based LRU cache."""

import asyncio
import hashlib
import json
import logging
from collections.abc import Callable
from contextvars import ContextVar
from functools import wraps
from typing import Any, TypeVar

from cachetools import TTLCache  # type: ignore[import-untyped]
from pydantic import BaseModel
from wxyc_etl.text import to_match_form as normalize_for_comparison
from wxyc_fastapi.observability import get_cache_stats_recorder

logger = logging.getLogger(__name__)

# Registry of all caches for bulk operations
_cache_registry: list[TTLCache] = []

# Lazily-initialized caches (using settings when accessed)
_track_cache: TTLCache | None = None
_release_cache: TTLCache | None = None
_search_cache: TTLCache | None = None
_artist_cache: TTLCache | None = None
_label_cache: TTLCache | None = None
_master_cache: TTLCache | None = None
_validation_cache: TTLCache | None = None

T = TypeVar("T")

# Per-request flag to bypass all caches (in-memory and PG).
# Used for benchmarking and A/B cache comparisons.
_skip_cache_var: ContextVar[bool] = ContextVar("skip_cache", default=False)


def set_skip_cache(skip: bool) -> None:
    """Set the per-request skip_cache flag."""
    _skip_cache_var.set(skip)


def should_skip_cache() -> bool:
    """Check whether caches should be bypassed for the current request."""
    return _skip_cache_var.get(False)


def make_cache_key(func_name: str, *args, **kwargs) -> str:
    """Generate a deterministic cache key from function name and arguments.

    Args:
        func_name: Name of the function being cached
        *args: Positional arguments to the function
        **kwargs: Keyword arguments to the function

    Returns:
        MD5 hash of the serialized arguments
    """
    key_data = {
        "fn": func_name,
        "args": list(args),
        "kwargs": dict(sorted(kwargs.items())),
    }
    key_string = json.dumps(key_data, sort_keys=True, default=str)
    return hashlib.md5(key_string.encode()).hexdigest()


def _normalize_for_cache_key(value):
    """Recursively normalize a value for cache-key hashing.

    Strings flow through `to_match_form` (the resolver pre-pass normalizer).
    Pydantic models are dumped to dicts and recursed into. Dicts, lists, and
    tuples are walked element-wise. Everything else passes through unchanged,
    preserving type distinctions (an int 12345 hashes differently from the
    string "12345").

    Exported as a private module-level helper so unit tests can target the
    recursion directly if the input shape grows.
    """
    if isinstance(value, str):
        return normalize_for_comparison(value)
    if isinstance(value, BaseModel):
        # `model_dump` produces plain Python types; recurse to fold the
        # model's string fields. Order is preserved because pydantic v2
        # emits dict in field-declaration order, which keeps `make_cache_key`'s
        # `sort_keys=True` JSON dump deterministic.
        return _normalize_for_cache_key(value.model_dump())
    if isinstance(value, dict):
        return {k: _normalize_for_cache_key(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(_normalize_for_cache_key(v) for v in value)
    return value


def make_normalized_cache_key(func_name: str, *args, **kwargs) -> str:
    """Like `make_cache_key`, but recursively folds string-valued fields
    inside the args (including those nested in Pydantic models, dicts, and
    lists) through `to_match_form` before hashing.

    Diacritic and case variations of the same user-typed query collapse to
    the same key, so the in-process @async_cached caches don't end up with
    a separate entry for each spelling (LML#342 / A5). Non-string scalars
    pass through unchanged so the cache key still differentiates by type.

    The `func_name` is identity-preserved — it's an internal identifier, not
    a user-supplied string, so case-sensitive comparison is correct.

    Args:
        func_name: Name of the function being cached.
        *args: Positional arguments. Strings, BaseModels, dicts, and lists
            are recursively normalized; other scalars pass through.
        **kwargs: Keyword arguments. Same per-value normalization as positional.

    Returns:
        MD5 hash of the serialized normalized arguments.
    """
    norm_args = tuple(_normalize_for_cache_key(a) for a in args)
    norm_kwargs = {k: _normalize_for_cache_key(v) for k, v in kwargs.items()}
    return make_cache_key(func_name, *norm_args, **norm_kwargs)


def create_ttl_cache(maxsize: int, ttl: int) -> TTLCache:
    """Create a TTL cache and register it for bulk operations.

    Args:
        maxsize: Maximum number of entries in the cache
        ttl: Time-to-live in seconds for cache entries

    Returns:
        TTLCache instance
    """
    cache: TTLCache = TTLCache(maxsize=maxsize, ttl=ttl)
    _cache_registry.append(cache)
    return cache


def clear_all_caches() -> None:
    """Clear all registered caches and reset lazy caches."""
    global \
        _track_cache, \
        _release_cache, \
        _search_cache, \
        _artist_cache, \
        _label_cache, \
        _master_cache, \
        _validation_cache
    for cache in _cache_registry:
        cache.clear()
        # Drop the per-cache in-flight Future map too (LML#544). cache.clear()
        # only empties the TTLCache contents; without this, orphan Futures
        # attached via _inflight_for survive a clear and a new caller can join
        # a dead leader's future.
        inflight = getattr(cache, "_lml_inflight", None)
        if inflight is not None:
            inflight.clear()
    # Reset lazy caches so they get recreated with fresh settings
    _track_cache = None
    _release_cache = None
    _search_cache = None
    _artist_cache = None
    _label_cache = None
    _master_cache = None
    _validation_cache = None


def _set_cached_flag(result: Any, cached: bool) -> Any:
    """Set the cached flag on a result if it has one."""
    if result is None:
        return result

    if isinstance(result, dict) and "cached" in result:
        result = result.copy()
        result["cached"] = cached
        return result

    if isinstance(result, BaseModel) and hasattr(result, "cached"):
        return result.model_copy(update={"cached": cached})

    return result


def _inflight_for(cache: TTLCache) -> dict[str, "asyncio.Future[Any]"]:
    """Per-cache map of in-flight Futures for concurrent-request coalescing.

    Lazily attached to the cache instance the first time a wrapper enters the
    fallthrough path. Each entry's lifetime spans exactly one fetch — the
    leader pops it in its `finally` once the future is resolved (success,
    None, or exception). Followers share the leader's value via
    `await future` without re-entering the underlying function.

    Exported as a module-level helper (parallel to `_normalize_for_cache_key`)
    so unit tests can assert the map is empty post-resolve without reaching
    into the cache instance's private attribute. LML#537.
    """
    inflight = getattr(cache, "_lml_inflight", None)
    if inflight is None:
        inflight = {}
        cache._lml_inflight = inflight  # type: ignore[attr-defined]
    return inflight


def async_cached(cache: TTLCache) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Decorator for caching async function results.

    The decorated function's results are cached based on its arguments.
    If the result has a 'cached' field, it will be set to True on cache hits.
    None results are not cached.

    Concurrent callers for the same key coalesce onto a single in-flight
    Future, so the wrapped function runs once per key per in-flight window
    (LML#537). Followers receive the leader's value (with `cached=True`
    applied when the result shape supports it), or re-raise the leader's
    exception. The in-flight entry is dropped in a `finally`, so a transient
    failure or None result does not pin state.

    Cancellation semantics: if the leader's task is cancelled, the future
    is cancelled (not broadcast as an exception) and existing followers
    detect that, re-enter the wrapper, and either join a fresh leader or
    become one themselves. Cancellation of the leader does NOT propagate
    across unrelated request contexts that happen to share the key.

    Args:
        cache: TTLCache instance to use for caching

    Returns:
        Decorator function
    """

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        async def wrapper(*args, **kwargs) -> T:
            # Bypass cache entirely when skip_cache flag is set
            if should_skip_cache():
                return await func(*args, **kwargs)  # type: ignore[misc, no-any-return]

            # Generate cache key from function name and arguments
            # Skip 'self' if present (first arg of instance methods)
            cache_args = args
            if args and hasattr(args[0], func.__name__):
                cache_args = args[1:]

            # `make_normalized_cache_key` collapses diacritic/case variants of
            # user-typed strings to a single entry (LML#342 / A5). Non-string
            # arguments are unchanged. The function name is identity-preserved.
            key = make_normalized_cache_key(func.__name__, *cache_args, **kwargs)

            # Check cache
            if key in cache:
                logger.debug(f"Cache hit for {func.__name__}")
                get_cache_stats_recorder().record_memory_cache_hit()
                result = cache[key]
                return _set_cached_flag(result, cached=True)  # type: ignore[no-any-return]

            # Coalesce concurrent callers (LML#537). If another caller is
            # already resolving this key, await its future and return the
            # same value — marked cached=True to match serial-hit semantics.
            inflight = _inflight_for(cache)
            existing = inflight.get(key)
            if existing is not None:
                logger.debug(f"Cache in-flight join for {func.__name__}")
                get_cache_stats_recorder().record("memory_cache_inflight_join")
                try:
                    follower_result = await existing
                except asyncio.CancelledError:
                    # Leader's task was cancelled and the future was cancelled
                    # downstream. If WE were not cancelled (cancelling() == 0),
                    # don't inherit the leader's cancellation across an unrelated
                    # request boundary — retry by re-entering the wrapper. The
                    # leader's `finally` has already popped the in-flight entry,
                    # so this re-entry sees a fresh slot.
                    current = asyncio.current_task()
                    if current is None or current.cancelling() > 0:
                        raise
                    return await wrapper(*args, **kwargs)  # type: ignore[no-any-return]
                return _set_cached_flag(follower_result, cached=True)  # type: ignore[no-any-return]

            # Leader path: create the future, run the function, broadcast.
            # `get_running_loop()` is safe because `wrapper` is `async def`
            # and only runs inside an active event loop.
            future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
            inflight[key] = future
            try:
                logger.debug(f"Cache miss for {func.__name__}")
                result = await func(*args, **kwargs)  # type: ignore[misc]
                # Broadcast to followers BEFORE writing to L1, so a cache-write
                # failure does not poison the followers' view of a successful
                # fetch. The future.done() guard handles the (edge) case where
                # the future was externally cancelled while the fetch was in
                # flight — we still want to return the result to the leader's
                # caller and let the cache write surface its own error.
                if not future.done():
                    future.set_result(result)
                # Don't cache None results (existing contract).
                if result is not None:
                    try:
                        cache[key] = result
                    except Exception:
                        # The fetch succeeded and was broadcast; treat an L1
                        # write failure as a cache-layer issue, not a fetch
                        # failure. Log and continue.
                        logger.exception(
                            "L1 cache write failed for %s; result still served",
                            func.__name__,
                        )
                return result  # type: ignore[no-any-return]
            except asyncio.CancelledError:
                # Cancel the future so existing followers know the leader bailed.
                # Their `except asyncio.CancelledError` arm decides whether to
                # retry (when they themselves were not cancelled) or propagate.
                # Do NOT broadcast cancellation via set_exception — that would
                # inject CancelledError into every follower's task context.
                if not future.done():
                    future.cancel()
                raise
            except Exception as exc:
                if not future.done():
                    future.set_exception(exc)
                    # Mark the exception as retrieved on the future so asyncio
                    # doesn't log "Future exception was never retrieved" when no
                    # follower joined this in-flight window. Followers awaiting
                    # the future consume the exception independently.
                    future.exception()
                raise
            finally:
                inflight.pop(key, None)

        # Stash the cache + name so `evict_cached` can drop a single entry
        # using the SAME key derivation. Keeping these on the wrapper means a
        # future change to the decorator's keying logic forces a matching
        # change here, not a silent drift. Underscored to mark them private.
        wrapper._lml_cache = cache  # type: ignore[attr-defined]
        wrapper._lml_func_name = func.__name__  # type: ignore[attr-defined]

        return wrapper  # type: ignore[return-value]

    return decorator


def evict_cached(cached_func: Callable, *args, **kwargs) -> bool:
    """Evict the L1 entry for ``cached_func(*args, **kwargs)``.

    Args:
        cached_func: A function decorated with :func:`async_cached`. For instance
            methods, pass the class attribute (e.g. ``DiscogsService.get_artist_details``)
            rather than a bound method — bound-method access does not proxy the
            stashed ``_lml_cache`` / ``_lml_func_name`` attributes.
        *args: Positional arguments AFTER any ``self`` strip — i.e., the same
            arguments the underlying function receives. The decorator already
            strips ``self`` when caching, so callers should mirror that.
        **kwargs: Keyword arguments. Same per-value normalization as the
            decorator's key derivation (`make_normalized_cache_key`).

    Returns:
        ``True`` if a key was found and removed, ``False`` if no entry existed.

    Raises:
        TypeError: If ``cached_func`` was not decorated with ``@async_cached``.

    Example:
        >>> evict_cached(DiscogsService.get_artist_details, 6998498)
        True  # L1 had a cached ArtistDetails for that id; now gone.
    """
    cache = getattr(cached_func, "_lml_cache", None)
    func_name = getattr(cached_func, "_lml_func_name", None)
    if cache is None or func_name is None:
        raise TypeError(f"{cached_func!r} is not @async_cached — no eviction surface available")
    key = make_normalized_cache_key(func_name, *args, **kwargs)
    return cache.pop(key, None) is not None


def get_track_cache() -> TTLCache:
    """Get or create the track search cache using settings."""
    global _track_cache
    if _track_cache is None:
        from config.settings import get_settings

        settings = get_settings()
        _track_cache = create_ttl_cache(
            maxsize=settings.discogs_cache_maxsize,
            ttl=settings.discogs_track_cache_ttl,
        )
    return _track_cache


def get_release_cache() -> TTLCache:
    """Get or create the release metadata cache using settings."""
    global _release_cache
    if _release_cache is None:
        from config.settings import get_settings

        settings = get_settings()
        _release_cache = create_ttl_cache(
            maxsize=settings.discogs_cache_maxsize // 2,
            ttl=settings.discogs_release_cache_ttl,
        )
    return _release_cache


def get_search_cache() -> TTLCache:
    """Get or create the general search cache using settings."""
    global _search_cache
    if _search_cache is None:
        from config.settings import get_settings

        settings = get_settings()
        _search_cache = create_ttl_cache(
            maxsize=settings.discogs_cache_maxsize,
            ttl=settings.discogs_search_cache_ttl,
        )
    return _search_cache


def get_artist_cache() -> TTLCache:
    """Get or create the artist image cache using settings."""
    global _artist_cache
    if _artist_cache is None:
        from config.settings import get_settings

        settings = get_settings()
        _artist_cache = create_ttl_cache(
            maxsize=settings.discogs_cache_maxsize // 2,
            ttl=settings.discogs_artist_cache_ttl,
        )
    return _artist_cache


def get_label_cache() -> TTLCache:
    """Get or create the label image cache using settings."""
    global _label_cache
    if _label_cache is None:
        from config.settings import get_settings

        settings = get_settings()
        _label_cache = create_ttl_cache(
            maxsize=settings.discogs_cache_maxsize // 2,
            ttl=settings.discogs_label_cache_ttl,
        )
    return _label_cache


def get_master_cache() -> TTLCache:
    """Get or create the master release cache using settings."""
    global _master_cache
    if _master_cache is None:
        from config.settings import get_settings

        settings = get_settings()
        _master_cache = create_ttl_cache(
            maxsize=settings.discogs_cache_maxsize // 2,
            ttl=settings.discogs_release_cache_ttl,
        )
    return _master_cache


def get_validation_cache() -> TTLCache:
    """Get or create the track validation cache using settings."""
    global _validation_cache
    if _validation_cache is None:
        from config.settings import get_settings

        settings = get_settings()
        _validation_cache = create_ttl_cache(
            maxsize=settings.discogs_cache_maxsize,
            ttl=settings.discogs_track_cache_ttl,
        )
    return _validation_cache


# Convenience constants for backwards compatibility
def __getattr__(name: str):
    """Lazy initialization of cache constants for backwards compatibility."""
    if name == "TRACK_CACHE":
        return get_track_cache()
    elif name == "RELEASE_CACHE":
        return get_release_cache()
    elif name == "SEARCH_CACHE":
        return get_search_cache()
    elif name == "ARTIST_CACHE":
        return get_artist_cache()
    elif name == "LABEL_CACHE":
        return get_label_cache()
    elif name == "MASTER_CACHE":
        return get_master_cache()
    elif name == "VALIDATION_CACHE":
        return get_validation_cache()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
