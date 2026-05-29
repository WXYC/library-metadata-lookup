"""Deezer API client for album and track searches.

No authentication required. Rate limit: ~50 requests per 5 seconds.
Used as a pre-filter to identify which albums are digitally distributed
before checking Spotify (which has stricter rate limits).
"""

from __future__ import annotations

import asyncio
import logging

from clients.streaming.base import BaseStreamingClient

logger = logging.getLogger(__name__)


class DeezerClient(BaseStreamingClient):
    """Deezer API client with rate limiting for album and track searches."""

    BASE_URL = "https://api.deezer.com"

    def __init__(self):
        super().__init__(rate_limit=(25, 5), semaphore_limit=5)

    async def search_album(self, artist: str, title: str) -> list[dict]:
        """Search Deezer for albums matching artist + title.

        Tries quoted field search first, falls back to unquoted if no results.
        Returns a list of album result dicts, or an empty list on error.
        """
        try:
            async with self._semaphore:
                url = f"{self.BASE_URL}/search/album"
                # Quoted field search (strict)
                results = await self._search(url, f'artist:"{artist}" album:"{title}"')
                if results:
                    return results
                # Unquoted fallback (fuzzier)
                return await self._search(url, f"{artist} {title}")
        except Exception:
            logger.exception("Deezer album search failed for %s - %s", artist, title)
            return []

    async def search_track(self, artist: str, track: str) -> list[dict]:
        """Search Deezer for tracks matching artist + track name.

        Tries quoted field search first, falls back to unquoted if no results.
        Returns a list of track result dicts, or an empty list on error.
        """
        try:
            async with self._semaphore:
                url = f"{self.BASE_URL}/search"
                results = await self._search(url, f'artist:"{artist}" track:"{track}"')
                if results:
                    return results
                return await self._search(url, f"{artist} {track}")
        except Exception:
            logger.exception("Deezer track search failed for %s - %s", artist, track)
            return []

    async def _search(self, url: str, query: str) -> list[dict]:
        http = await self._get_client()

        for attempt in range(3):
            await self._rate_limiter.acquire()
            resp = await http.get(url, params={"q": query, "limit": 5})

            if resp.status_code != 200:
                logger.warning("Deezer search returned %d", resp.status_code)
                return []

            data = resp.json()
            if "error" in data:
                code = data["error"].get("code")
                if code == 4:  # Quota limit exceeded
                    wait = 5 * (attempt + 1)
                    logger.debug("Deezer quota exceeded, waiting %ds", wait)
                    await asyncio.sleep(wait)
                    continue
                logger.warning("Deezer error: %s", data["error"])
                return []

            return data.get("data", [])

        logger.warning("Deezer quota retries exhausted for query: %s", query[:50])
        return []
