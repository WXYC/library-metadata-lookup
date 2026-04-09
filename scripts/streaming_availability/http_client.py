"""Base HTTP client for streaming service API integrations."""

from __future__ import annotations

import asyncio

import httpx
from aiolimiter import AsyncLimiter


class BaseStreamingClient:
    """Base class for streaming service HTTP clients.

    Provides lazy httpx.AsyncClient lifecycle management, rate limiting
    via aiolimiter, and concurrency control via asyncio.Semaphore.

    Args:
        rate_limit: Tuple of (max_rate, time_period) for AsyncLimiter.
        semaphore_limit: Maximum concurrent requests.
    """

    def __init__(self, rate_limit: tuple[float, float], semaphore_limit: int):
        self._http: httpx.AsyncClient | None = None
        self._rate_limiter = AsyncLimiter(*rate_limit)
        self._semaphore = asyncio.Semaphore(semaphore_limit)

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create the shared HTTP client."""
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=10.0)
        return self._http

    async def close(self) -> None:
        """Close the HTTP client and release resources."""
        if self._http:
            await self._http.aclose()
            self._http = None
