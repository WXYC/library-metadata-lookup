"""Bandcamp HTTP client for artist search and catalog scraping.

Extends BaseStreamingClient with rate limiting (1 req/s) and concurrency
control (semaphore of 2). Retries on 429 with exponential backoff.
Used by bandcamp_pipeline.py for both slug discovery (autocomplete API)
and album-level matching (page scraping).
"""

from __future__ import annotations

# LML#1040: ``_request_with_retry``'s 429 loop now delegates to the shared
# ``discogs.admission.retry_429``, whose ``asyncio.sleep(delay)`` call is no
# longer textually in this module -- but ``tests/unit/
# test_bandcamp_retry_characterization.py`` patches
# ``clients.bandcamp.asyncio.sleep`` (module-ATTRIBUTE patching against
# whatever ``asyncio`` name this module exposes). That patch mutates the
# SAME shared ``asyncio`` module object ``discogs.admission`` also imports,
# so it still takes effect there; removing this import would only break the
# patch-target resolution, not any real behavior. Keep it.
import asyncio  # noqa: F401
import logging
import re

import httpx

from clients.streaming.base import BaseStreamingClient
from clients.streaming.matching import find_best_source_match, score_match
from discogs.admission import retry_429
from streaming.models import SourceMatch

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


def _compute_bandcamp_retry_delay(attempt: int, retry_after: str | None) -> float:
    """Bandcamp's pre-LML#1040 429 delay policy, unchanged: a valid numeric
    ``Retry-After`` wins verbatim (no cap); otherwise exponential backoff with
    NO jitter -- unlike Discogs's ``discogs.service._compute_retry_delay``,
    which both jitters and caps at 60s. Kept byte-identical to the pre-#1040
    inline computation; see ``tests/unit/test_bandcamp_retry_characterization.py``.
    """
    if retry_after:
        try:
            return float(retry_after)
        except ValueError:
            pass
    return RETRY_BASE_DELAY * (2**attempt)


class _BandcampRequestFailedError(Exception):
    """Internal sentinel: ``client.request()`` raised inside one attempt.

    Distinguishes a request-layer failure -- converted to ``None``, matching
    the pre-LML#1040 bare ``except Exception: return None`` that wrapped ONLY
    the ``client.request()`` call -- from an exception raised by
    ``self._rate_limiter.acquire()`` or the ``async with self._semaphore:``
    block, which the pre-#1040 code did NOT catch (and still doesn't: this
    sentinel is only raised from inside the inner ``try``, so anything else
    propagates unconverted, same as before).
    """


class BandcampClient(BaseStreamingClient):
    """Bandcamp HTTP client for autocomplete search and page scraping.

    Rate limit: 1 request/second, max 2 concurrent.
    Retries on 429 with exponential backoff.
    """

    def __init__(self) -> None:
        super().__init__(rate_limit=(1, 1), semaphore_limit=2)

    async def _request_with_retry(self, method: str, url: str, **kwargs) -> httpx.Response | None:
        """Make an HTTP request with retry on 429.

        Returns the response, or None on failure after retries. LML#1040:
        the retry loop itself is ``discogs.admission.retry_429``, shared with
        ``DiscogsService._request_with_retry``; this method supplies
        Bandcamp's own permit acquisition, delay policy, and log wording
        (byte-identical to the pre-#1040 inline loop -- see
        ``tests/unit/test_bandcamp_retry_characterization.py``).
        """
        client = await self._get_client()

        async def _attempt(attempt: int) -> httpx.Response:
            async with self._semaphore:
                await self._rate_limiter.acquire()
                try:
                    return await client.request(method, url, **kwargs)
                except Exception as e:
                    raise _BandcampRequestFailedError() from e

        try:
            response = await retry_429(
                _attempt,
                max_retries=MAX_RETRIES,
                compute_delay=_compute_bandcamp_retry_delay,
                on_retry=lambda attempt, delay, retry_after: log.warning(
                    f"429 rate limited, retrying in {delay}s (attempt {attempt + 1})"
                ),
                on_exhausted=lambda: log.warning(
                    f"429 rate limited after {MAX_RETRIES} retries, giving up: {url}"
                ),
            )
        except _BandcampRequestFailedError:
            return None

        return None if response.status_code == 429 else response

    async def find_album_match(self, artist: str, title: str) -> SourceMatch | None:
        """Search Bandcamp for ``(artist, title)`` and return the best match.

        See ``BaseStreamingClient.find_album_match`` for the contract.
        Two-phase: autocomplete the artist subdomain, then scrape the
        artist's ``/music`` catalog and fuzzy-match the album title. Both
        phases' response shapes are encapsulated here.
        """
        artist_results = await self.search_artist(artist)
        if not artist_results:
            return None
        best_artist: dict | None = None
        best_artist_score = 0.0
        for result in artist_results:
            s = score_match(artist, result["name"])
            if s >= 80.0 and s > best_artist_score:
                best_artist = result
                best_artist_score = s
        if best_artist is None:
            return None
        # A fetch failure (None) is treated as "no catalog" for the live
        # match path -- there is no retry loop here, so it degrades to no match.
        catalog = await self.fetch_artist_catalog(best_artist["slug"]) or []
        matched_artist_name = best_artist["name"]
        return find_best_source_match(
            catalog,
            artist,
            title,
            artist_fn=lambda _: matched_artist_name,
            title_fn=lambda x: x["title"],
            url_fn=lambda x: x["url"],
            service="bandcamp",
        )

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

    async def fetch_artist_catalog(self, slug: str) -> list[dict] | None:
        """Fetch album list from {slug}.bandcamp.com/music.

        Returns a list of dicts with keys ``url``, ``title`` (deduplicated by
        URL) on a successful fetch -- possibly empty if the page has no album
        links. Returns ``None`` on a fetch failure (network error, timeout,
        non-200, or 429 after retries), which callers must NOT treat as a
        definitively-empty catalog: an empty list means "the artist has no
        releases" while ``None`` means "we couldn't tell" and should be retried
        rather than recorded as a final result (#661).
        """
        url = f"https://{slug}.bandcamp.com/music"
        resp = await self._request_with_retry("GET", url, timeout=15.0, follow_redirects=True)
        if resp is None or resp.status_code != 200:
            return None

        # Bandcamp serves UTF-8 but its Content-Type often omits `charset=`;
        # force UTF-8 so diacritic-bearing album titles don't mojibake. See
        # release/bandcamp_resolver.py for the same shape.
        resp.encoding = "utf-8"
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
