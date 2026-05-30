"""iTunes Search API client for album-level Apple Music searches."""

from __future__ import annotations

import asyncio
import logging

from clients.streaming.base import BaseStreamingClient
from clients.streaming.matching import find_best_match
from streaming.models import SourceMatch

logger = logging.getLogger(__name__)


class AppleMusicClient(BaseStreamingClient):
    """iTunes Search API client with rate limiting for album searches.

    Unlike the existing _fetch_apple_music_url() in the service which searches
    by song (entity=song), this searches by album (entity=album) for better
    album-level matching.
    """

    BASE_URL = "https://itunes.apple.com/search"

    def __init__(self):
        super().__init__(rate_limit=(15, 60), semaphore_limit=3)
        self._max_retries = 2

    async def find_album_match(self, artist: str, title: str) -> SourceMatch | None:
        """Search iTunes for ``(artist, title)`` and return the best match.

        See ``BaseStreamingClient.find_album_match`` for the contract. The
        iTunes response shape (``artistName`` / ``collectionName`` /
        ``collectionViewUrl``) is encapsulated here. The wrong-artist
        verification from LML#389 lives in the shared
        ``is_acceptable_match`` floor inside ``find_best_match``.
        """
        results = await self.search_album(artist, title)
        if not results:
            return None
        best = find_best_match(
            results,
            artist,
            title,
            artist_fn=lambda x: x["artistName"],
            title_fn=lambda x: x["collectionName"],
            url_fn=lambda x: x["collectionViewUrl"],
        )
        if best is None:
            return None
        return SourceMatch(url=best["url"], confidence=best["confidence"])

    async def search_album(self, artist: str, title: str) -> list[dict]:
        """Search iTunes for albums matching artist + title.

        Returns a list of album result dicts, or an empty list on error.
        """
        try:
            async with self._semaphore:
                return await self._search_with_retry(artist, title)
        except Exception:
            logger.exception("Apple Music search failed for %s - %s", artist, title)
            return []

    async def _search_with_retry(self, artist: str, title: str) -> list[dict]:
        http = await self._get_client()

        for attempt in range(self._max_retries):
            await self._rate_limiter.acquire()

            resp = await http.get(
                self.BASE_URL,
                params={
                    "term": f"{artist} {title}",
                    "entity": "album",
                    "media": "music",
                    "limit": 5,
                },
            )

            if resp.status_code == 403:
                logger.warning(
                    "iTunes 403 rate limit, sleeping 60s (attempt %d/%d)",
                    attempt + 1,
                    self._max_retries,
                )
                await asyncio.sleep(60)
                continue

            if resp.status_code != 200:
                logger.warning(
                    "iTunes search returned %d for %s - %s",
                    resp.status_code,
                    artist,
                    title,
                )
                return []

            data = resp.json()
            return data.get("results", [])

        logger.error("iTunes max retries exhausted for %s - %s", artist, title)
        return []
