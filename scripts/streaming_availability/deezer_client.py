"""Deezer API client for album and track searches.

No authentication required. Rate limit: ~50 requests per 5 seconds.
Used as a pre-filter to identify which albums are digitally distributed
before checking Spotify (which has stricter rate limits).
"""

from __future__ import annotations

import asyncio
import logging

import httpx
from aiolimiter import AsyncLimiter

logger = logging.getLogger(__name__)


class DeezerClient:
    """Deezer API client with rate limiting for album and track searches."""

    BASE_URL = "https://api.deezer.com"

    def __init__(self):
        self._http: httpx.AsyncClient | None = None
        self._rate_limiter = AsyncLimiter(40, 5)
        self._semaphore = asyncio.Semaphore(5)

    async def _get_client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=10.0)
        return self._http

    async def search_album(self, artist: str, title: str) -> list[dict]:
        """Search Deezer for albums matching artist + title.

        Returns a list of album result dicts, or an empty list on error.
        """
        try:
            async with self._semaphore:
                return await self._search(
                    f"{self.BASE_URL}/search/album",
                    f'artist:"{artist}" album:"{title}"',
                )
        except Exception:
            logger.exception("Deezer album search failed for %s - %s", artist, title)
            return []

    async def search_track(self, artist: str, track: str) -> list[dict]:
        """Search Deezer for tracks matching artist + track name.

        Returns a list of track result dicts, or an empty list on error.
        """
        try:
            async with self._semaphore:
                return await self._search(
                    f"{self.BASE_URL}/search",
                    f'artist:"{artist}" track:"{track}"',
                )
        except Exception:
            logger.exception("Deezer track search failed for %s - %s", artist, track)
            return []

    async def _search(self, url: str, query: str) -> list[dict]:
        http = await self._get_client()
        await self._rate_limiter.acquire()

        resp = await http.get(url, params={"q": query, "limit": 5})

        if resp.status_code != 200:
            logger.warning("Deezer search returned %d", resp.status_code)
            return []

        data = resp.json()
        if "error" in data:
            logger.warning("Deezer error: %s", data["error"])
            return []

        return data.get("data", [])

    async def close(self) -> None:
        if self._http:
            await self._http.aclose()
            self._http = None
