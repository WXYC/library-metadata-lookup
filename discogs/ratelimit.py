"""Rate limiting utilities for Discogs API requests.

Implements:
- Semaphore for concurrent request limiting
- Token bucket rate limiter for requests per minute
- Reset function for testing
"""

import asyncio
import logging

from aiolimiter import AsyncLimiter

logger = logging.getLogger(__name__)

# Lazily-initialized rate limiting primitives, stored per event loop
_rate_limiters: dict[asyncio.AbstractEventLoop, AsyncLimiter] = {}
_semaphores: dict[asyncio.AbstractEventLoop, asyncio.Semaphore] = {}


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


def reset_rate_limiting() -> None:
    """Reset rate limiting state for testing."""
    global _rate_limiters, _semaphores
    _rate_limiters.clear()
    _semaphores.clear()
    logger.debug("Reset rate limiting state")
