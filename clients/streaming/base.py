"""Base HTTP client for streaming service API integrations."""

from __future__ import annotations

import asyncio

import httpx
from aiolimiter import AsyncLimiter
from wxyc_fastapi.http import async_singleton

from streaming.models import SourceMatch


class BaseStreamingClient:
    """Base class for streaming service HTTP clients.

    Provides lazy httpx.AsyncClient lifecycle management, rate limiting
    via aiolimiter, concurrency control via asyncio.Semaphore, and the
    ``find_album_match`` interface that lets the streaming-check
    orchestrator gather over any client uniformly.

    Args:
        rate_limit: Tuple of (max_rate, time_period) for AsyncLimiter.
        semaphore_limit: Maximum concurrent requests.
    """

    def __init__(self, rate_limit: tuple[float, float], semaphore_limit: int):
        self._rate_limiter = AsyncLimiter(*rate_limit)
        self._semaphore = asyncio.Semaphore(semaphore_limit)
        # Test seam: tests assign ``client._http = mock`` before any call;
        # the singleton getter respects that pre-set value (see below).
        self._http: httpx.AsyncClient | None = None
        # One (getter, closer) pair per instance — subclass clients
        # (Bandcamp, Deezer, Spotify, Apple Music) must stay isolated.
        # See LML#241/#242 for the FD-leak race this lock prevents and
        # WXYC/wxyc-fastapi#5 for the helper.
        self._singleton_get, self._singleton_close = async_singleton(self._make_client)

    async def _make_client(self) -> httpx.AsyncClient:
        """Construct the underlying httpx client. Override to customize."""
        return httpx.AsyncClient(timeout=10.0)

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create the shared HTTP client.

        Honors an externally-set ``self._http`` (test injection) so unit
        tests that pre-assign a mock client can keep doing so. Otherwise
        delegates to the lock-guarded singleton.
        """
        if self._http is not None:
            return self._http
        client = await self._singleton_get()
        # Cache on the instance so future calls (and test introspection)
        # see the same value the singleton holds.
        self._http = client
        return client

    async def close(self) -> None:
        """Close the HTTP client and release resources."""
        await self._singleton_close()
        self._http = None

    async def find_album_match(self, artist: str, title: str) -> SourceMatch | None:
        """Search this service for an album match and return a normalized verdict.

        The deep contract the streaming-check orchestrator (LML#392) gathers
        over: each provider absorbs its own response-shape adaptation so the
        orchestrator never branches on service identity. Subclasses MUST
        override; the base raises ``NotImplementedError`` so a missing
        override fails loudly the first time the orchestrator dispatches.
        """
        raise NotImplementedError(f"{type(self).__name__} must override find_album_match")
