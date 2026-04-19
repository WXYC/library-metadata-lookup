"""Bandcamp HTTP client for artist search and catalog scraping.

Extends BaseStreamingClient with rate limiting (2 req/s) and concurrency
control (semaphore of 3). Used by bandcamp_pipeline.py for both slug
discovery (autocomplete API) and album-level matching (page scraping).
"""

from __future__ import annotations

import re

from scripts.streaming_availability.http_client import BaseStreamingClient

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


class BandcampClient(BaseStreamingClient):
    """Bandcamp HTTP client for autocomplete search and page scraping.

    Rate limit: 2 requests/second, max 3 concurrent.
    """

    def __init__(self) -> None:
        super().__init__(rate_limit=(2, 1), semaphore_limit=3)

    async def search_artist(self, artist_name: str) -> list[dict]:
        """Search Bandcamp autocomplete for artist pages.

        Returns list of dicts with keys: name, url, slug.
        Only returns band results (type "b").
        """
        client = await self._get_client()
        async with self._semaphore:
            await self._rate_limiter.acquire()
            try:
                resp = await client.get(
                    AUTOCOMPLETE_URL,
                    params={"q": artist_name, "param": "b"},
                    timeout=10.0,
                )
                if resp.status_code != 200:
                    return []
            except Exception:
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
        client = await self._get_client()
        async with self._semaphore:
            await self._rate_limiter.acquire()
            try:
                resp = await client.get(url, timeout=15.0, follow_redirects=True)
                if resp.status_code != 200:
                    return []
            except Exception:
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
