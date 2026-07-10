"""Rate limiting utilities for Discogs API requests.

Implements:
- Semaphore for concurrent request limiting
- Token bucket rate limiter for requests per minute
- Reset function for testing
"""

import asyncio
import logging

from aiolimiter import AsyncLimiter

from discogs.breaker import DiscogsCircuitBreaker

logger = logging.getLogger(__name__)

# Lazily-initialized rate limiting primitives, stored per event loop
_rate_limiters: dict[asyncio.AbstractEventLoop, AsyncLimiter] = {}
_semaphores: dict[asyncio.AbstractEventLoop, asyncio.Semaphore] = {}
# LML#755 saturation circuit-breaker, stored per event loop alongside the
# limiter/semaphore (same process-global-on-single-worker scope).
_breakers: dict[asyncio.AbstractEventLoop, DiscogsCircuitBreaker] = {}


def _build_breaker() -> DiscogsCircuitBreaker:
    from config.settings import get_settings

    settings = get_settings()
    return DiscogsCircuitBreaker(
        failure_threshold=settings.discogs_breaker_failure_threshold,
        remaining_floor=settings.discogs_breaker_remaining_floor,
        cooldown_seconds=settings.discogs_breaker_cooldown_seconds,
    )


def get_rate_limiter() -> AsyncLimiter:
    """Get or create the rate limiter for the current event loop.

    Returns:
        AsyncLimiter configured for requests per minute
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        from config.settings import get_settings

        settings = get_settings()
        return AsyncLimiter(settings.discogs_rate_limit, 60)

    if loop not in _rate_limiters:
        from config.settings import get_settings

        settings = get_settings()
        _rate_limiters[loop] = AsyncLimiter(settings.discogs_rate_limit, 60)
        logger.debug(f"Created rate limiter: {settings.discogs_rate_limit} req/min")
    return _rate_limiters[loop]


def get_semaphore() -> asyncio.Semaphore:
    """Get or create the concurrency semaphore for the current event loop.

    Returns:
        asyncio.Semaphore for limiting concurrent requests
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        from config.settings import get_settings

        settings = get_settings()
        return asyncio.Semaphore(settings.discogs_max_concurrent)

    if loop not in _semaphores:
        from config.settings import get_settings

        settings = get_settings()
        _semaphores[loop] = asyncio.Semaphore(settings.discogs_max_concurrent)
        logger.debug(f"Created semaphore: {settings.discogs_max_concurrent} concurrent")
    return _semaphores[loop]


def get_discogs_breaker() -> DiscogsCircuitBreaker:
    """Get or create the saturation circuit-breaker for the current event loop.

    Stored per event loop (same pattern and scope as the limiter/semaphore).
    Without a running loop — the handful of legacy direct-call tests — returns
    a fresh, *unshared* breaker each time, so those off-loop paths get **no
    cross-request shedding** (each call starts CLOSED). This is latent and
    affects only direct-call tests, not the running service (all production
    callers share the one per-loop breaker).

    Returns:
        The per-loop :class:`~discogs.breaker.DiscogsCircuitBreaker`.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return _build_breaker()

    if loop not in _breakers:
        _breakers[loop] = _build_breaker()
        logger.debug("Created Discogs saturation circuit-breaker")
    return _breakers[loop]


def reset_rate_limiting() -> None:
    """Reset rate limiting state for testing."""
    global _rate_limiters, _semaphores, _breakers
    _rate_limiters.clear()
    _semaphores.clear()
    _breakers.clear()
    logger.debug("Reset rate limiting state")
