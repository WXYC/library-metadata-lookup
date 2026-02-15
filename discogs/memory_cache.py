"""Caching utilities for Discogs API responses using TTL-based LRU cache."""

import hashlib
import json
import logging
from collections.abc import Callable
from contextvars import ContextVar
from functools import wraps
from typing import Any, TypeVar

from cachetools import TTLCache  # type: ignore[import-untyped]
from pydantic import BaseModel

from core.telemetry import record_memory_cache_hit

logger = logging.getLogger(__name__)

# Registry of all caches for bulk operations
_cache_registry: list[TTLCache] = []

# Lazily-initialized caches (using settings when accessed)
_track_cache: TTLCache | None = None
_release_cache: TTLCache | None = None
_search_cache: TTLCache | None = None
_artist_cache: TTLCache | None = None
_label_cache: TTLCache | None = None

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


def create_ttl_cache(maxsize: int, ttl: int) -> TTLCache:
    """Create a TTL cache and register it for bulk operations.

    Args:
        maxsize: Maximum number of entries in the cache
        ttl: Time-to-live in seconds for cache entries

    Returns:
        TTLCache instance
    """
    cache = TTLCache(maxsize=maxsize, ttl=ttl)
    _cache_registry.append(cache)
    return cache


def clear_all_caches() -> None:
    """Clear all registered caches and reset lazy caches."""
    global _track_cache, _release_cache, _search_cache, _artist_cache, _label_cache
    for cache in _cache_registry:
        cache.clear()
    # Reset lazy caches so they get recreated with fresh settings
    _track_cache = None
    _release_cache = None
    _search_cache = None
    _artist_cache = None
    _label_cache = None


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


def async_cached(cache: TTLCache) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Decorator for caching async function results.

    The decorated function's results are cached based on its arguments.
    If the result has a 'cached' field, it will be set to True on cache hits.
    None results are not cached.

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

            key = make_cache_key(func.__name__, *cache_args, **kwargs)

            # Check cache
            if key in cache:
                logger.debug(f"Cache hit for {func.__name__}")
                record_memory_cache_hit()
                result = cache[key]
                return _set_cached_flag(result, cached=True)  # type: ignore[no-any-return]

            # Cache miss - call function
            logger.debug(f"Cache miss for {func.__name__}")
            result = await func(*args, **kwargs)  # type: ignore[misc]

            # Don't cache None results
            if result is not None:
                cache[key] = result

            return result  # type: ignore[no-any-return]

        return wrapper  # type: ignore[return-value]

    return decorator


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
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
