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

            # Cache miss - call function
            logger.debug(f"Cache miss for {func.__name__}")
            result = await func(*args, **kwargs)  # type: ignore[misc]

            # Don't cache None results
            if result is not None:
                cache[key] = result

            return result  # type: ignore[no-any-return]

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
