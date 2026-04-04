"""Spotify Web API client with client credentials auth and rate limiting."""

from __future__ import annotations

import asyncio
import base64
import logging
import time

import httpx
from aiolimiter import AsyncLimiter

logger = logging.getLogger(__name__)


class SpotifyClient:
    """Spotify Web API client using client credentials flow for album searches."""

    ACCOUNTS_URL = "https://accounts.spotify.com/api/token"
    API_BASE = "https://api.spotify.com/v1"

    def __init__(self, client_id: str, client_secret: str):
        self._client_id = client_id
        self._client_secret = client_secret
        self._access_token: str | None = None
        self._token_expires_at: float = 0
        self._http: httpx.AsyncClient | None = None
        self._rate_limiter = AsyncLimiter(80, 30)
        self._semaphore = asyncio.Semaphore(10)
        self._max_retries = 3

    async def _get_client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=10.0)
        return self._http

    async def _ensure_token(self) -> str:
        """Get or refresh the access token via client credentials flow."""
        if self._access_token and time.time() < self._token_expires_at:
            return self._access_token

        http = await self._get_client()
        credentials = base64.b64encode(f"{self._client_id}:{self._client_secret}".encode()).decode()

        resp = await http.post(
            self.ACCOUNTS_URL,
            headers={
                "Authorization": f"Basic {credentials}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={"grant_type": "client_credentials"},
        )
        resp.raise_for_status()
        data = resp.json()

        self._access_token = data["access_token"]
        self._token_expires_at = time.time() + data["expires_in"] - 60  # refresh 60s early
        logger.debug("Acquired Spotify access token (expires in %ds)", data["expires_in"])
        return self._access_token

    async def search_album(self, artist: str, title: str, market: str = "US") -> list[dict]:
        """Search Spotify for albums matching artist + title.

        Returns a list of album dicts from the Spotify API, or an empty list on error.
        """
        try:
            async with self._semaphore:
                return await self._search_with_retry(artist, title, market)
        except Exception:
            logger.exception("Spotify search failed for %s - %s", artist, title)
            return []

    async def _search_with_retry(self, artist: str, title: str, market: str) -> list[dict]:
        http = await self._get_client()

        for attempt in range(self._max_retries):
            token = await self._ensure_token()
            await self._rate_limiter.acquire()

            query = f'artist:"{artist}" album:"{title}"'
            resp = await http.get(
                f"{self.API_BASE}/search",
                params={"q": query, "type": "album", "market": market, "limit": 5},
                headers={"Authorization": f"Bearer {token}"},
            )

            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", "5"))
                logger.warning(
                    "Spotify 429, retrying in %ds (attempt %d/%d)",
                    retry_after,
                    attempt + 1,
                    self._max_retries,
                )
                await asyncio.sleep(retry_after)
                continue

            if resp.status_code != 200:
                logger.warning(
                    "Spotify search returned %d for %s - %s", resp.status_code, artist, title
                )
                return []

            data = resp.json()
            return data.get("albums", {}).get("items", [])

        logger.error("Spotify max retries exhausted for %s - %s", artist, title)
        return []

    async def close(self) -> None:
        if self._http:
            await self._http.aclose()
            self._http = None
