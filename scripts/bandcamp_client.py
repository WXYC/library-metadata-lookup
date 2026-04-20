"""Bandcamp HTTP client for artist search and catalog scraping.

Extends BaseStreamingClient with rate limiting (1 req/s) and concurrency
control (semaphore of 2). Retries on 429 with exponential backoff.
Used by bandcamp_pipeline.py for both slug discovery (autocomplete API)
and album-level matching (page scraping).
"""

from __future__ import annotations

import asyncio
import logging
import re

import httpx

from scripts.streaming_availability.http_client import BaseStreamingClient

log = logging.getLogger(__name__)

AUTOCOMPLETE_URL = "https://bandcamp.com/api/fuzzysearch/2/app_autocomplete"

_SLUG_RE = re.compile(r"https?://([a-z0-9-]+)\.bandcamp\.com")

# Primary: <a href="/album/..."> followed by <p class="title">...</p>
_ALBUM_WITH_TITLE_RE = re.compile(
    r'<a\s+href="(/album/[^"]+)"[^>]*>.*?<p\s+class="title">([^<]+)</p>',
    re.DOTALL,
)

# Fallback: just <a href="/album/...">
_ALBUM_HREF_RE = re.compile(r'href="(/album/[^"]+)"')


def extract_slug(url: str | None) -> str | None:
    """Extract Bandcamp subdomain slug from a URL.

    >>> extract_slug("https://autechre.bandcamp.com/album/confield")
    'autechre'
    >>> extract_slug("https://open.spotify.com/artist/123")
    >>> extract_slug(None)
    """
    if not url:
        return None
    match = _SLUG_RE.match(url)
    return match.group(1) if match else None


MAX_RETRIES = 3
RETRY_BASE_DELAY = 5.0  # seconds


class BandcampClient(BaseStreamingClient):
    """Bandcamp HTTP client for autocomplete search and page scraping.

    Rate limit: 1 request/second, max 2 concurrent.
    Retries on 429 with exponential backoff.
    """

    def __init__(self) -> None:
        super().__init__(rate_limit=(1, 1), semaphore_limit=2)

    async def _request_with_retry(self, method: str, url: str, **kwargs) -> httpx.Response | None:
        """Make an HTTP request with retry on 429.

        Returns the response, or None on failure after retries.
        """
        client = await self._get_client()
        for attempt in range(MAX_RETRIES + 1):
            async with self._semaphore:
                await self._rate_limiter.acquire()
                try:
                    resp = await client.request(method, url, **kwargs)
                except Exception:
                    return None

            if resp.status_code == 429:
                if attempt < MAX_RETRIES:
                    delay = RETRY_BASE_DELAY * (2**attempt)
                    log.warning(f"429 rate limited, retrying in {delay}s (attempt {attempt + 1})")
                    await asyncio.sleep(delay)
                    continue
                log.warning(f"429 rate limited after {MAX_RETRIES} retries, giving up: {url}")
                return None

            return resp
        return None

    async def search_artist(self, artist_name: str) -> list[dict]:
        """Search Bandcamp autocomplete for artist pages.

        Returns list of dicts with keys: name, url, slug.
        Only returns band results (type "b").
        """
        resp = await self._request_with_retry(
            "GET",
            AUTOCOMPLETE_URL,
            params={"q": artist_name, "param": "b"},
            timeout=10.0,
        )
        if resp is None or resp.status_code != 200:
            return []

        data = resp.json()
        results = []
        for item in data.get("results", []):
            if item.get("type") != "b":
                continue
            url = item.get("url", "")
            slug = extract_slug(url)
            if slug:
                results.append(
                    {
                        "name": item.get("name", ""),
                        "url": url,
                        "slug": slug,
                    }
                )
        return results

    async def fetch_artist_catalog(self, slug: str) -> list[dict]:
        """Fetch album list from {slug}.bandcamp.com/music.

        Returns list of dicts with keys: url, title.
        Deduplicates by URL.
        """
        url = f"https://{slug}.bandcamp.com/music"
        resp = await self._request_with_retry("GET", url, timeout=15.0, follow_redirects=True)
        if resp is None or resp.status_code != 200:
            return []

        html = resp.text
        seen_paths: set[str] = set()
        albums: list[dict] = []

        # Primary regex: extract both path and title
        for match in _ALBUM_WITH_TITLE_RE.finditer(html):
            path, title = match.groups()
            if path in seen_paths:
                continue
            seen_paths.add(path)
            albums.append(
                {
                    "url": f"https://{slug}.bandcamp.com{path}",
                    "title": title.strip(),
                }
            )

        # Fallback: href-only regex when title pattern doesn't match
        if not albums:
            for match in _ALBUM_HREF_RE.finditer(html):
                path = match.group(1)
                if path in seen_paths:
                    continue
                seen_paths.add(path)
                albums.append(
                    {
                        "url": f"https://{slug}.bandcamp.com{path}",
                        "title": path.split("/")[-1].replace("-", " "),
                    }
                )

        return albums
