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
import html
import logging
import re

import httpx

from clients.streaming.base import BaseStreamingClient
from clients.streaming.matching import (
    find_best_source_match,
    is_acceptable_match,
    score_match,
)
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

# LML#1069: the autocomplete API's ``url`` field comes back doubled --
# "https://x.bandcamp.comhttps://x.bandcamp.com/album/y" -- keep the LAST
# "https://..." segment. Passes an already-clean URL through unchanged (no
# earlier "https://" occurrence to jump past), so this keeps working
# transparently if Bandcamp ever fixes the quirk.
_HTTPS_PREFIX = "https://"

# Query-side format junk a DJ never types but the library carries verbatim:
# trailing vinyl-size markers (12", 7", 10"), trailing bracketed tags
# ([missing], [EP]), and a trailing "(studio)" qualifier. Conservative and
# query-only -- scoring still sees the raw title (clients/bandcamp.py's
# ``find_album_match_via_search``), so over-stripping here costs recall, not
# precision.
#
# LML#1096 audit: deliberately kept independent of (and more aggressive
# than) ``clients/streaming/matching.py``'s canonical, scoring-side
# ``strip_format_suffix``/``_CATALOG_NUMBER_BRACKET_RE`` -- that regex
# conservatively preserves multi-word brackets ("[Disc One]") and
# pure-numeric brackets ("[2019]") since they can be real title content on
# the SCORING axis; a search QUERY has no such precision cost from
# over-stripping. See ``test_strips_content_the_canonical_regex_preserves``
# in ``tests/unit/test_bandcamp_album_search.py`` for the pinned divergence.
_QUERY_BRACKET_TAG_RE = re.compile(r"\s*\[[^\]]*\]\s*$")
_QUERY_STUDIO_SUFFIX_RE = re.compile(r"\s*\(studio\)\s*$", re.IGNORECASE)
_QUERY_VINYL_SIZE_RE = re.compile(r"\s+\d{1,2}[\"”]\s*$")

# Recall-recovery normalization (LML#1069 near-miss corpus) applied to BOTH
# the query title and each candidate's title as a SECOND acceptance pass --
# never widens the floor itself, just reconciles structural noise Bandcamp's
# own listings carry that a DJ's library entry doesn't.
#   - trailing bracketed catalog tag ("Ghost Dance [KLP186]") -- Pine Hill Haints
#   - trailing year-range parenthetical ("As / If / When (1978-83)") -- Z'ev
#   - slash spacing ("Sunset / Sunrise" <-> "Sunset/Sunrise") -- The Dutchess & The Duke
# Deliberately does NOT attempt a leading-catalog-number strip (Christoph De
# Babalon's "044 (Hilf Dir Selbst!)") -- no pattern was found that's safe
# against legitimate numeric-leading titles ("2112"); accepted as a measured
# loss rather than risking a false-accept elsewhere.
#
# The bracket-tag strip reuses _QUERY_BRACKET_TAG_RE verbatim (same pattern,
# same "trailing bracket is noise" reasoning) rather than a second compiled
# copy -- see clean_title_for_query for that pattern's own definition.
_NORMALIZE_YEAR_RANGE_RE = re.compile(r"\s*\(\d{4}-\d{2,4}\)\s*$")
_NORMALIZE_SLASH_SPACING_RE = re.compile(r"\s*/\s*")

# Bandcamp album-page ``og:title`` meta content: "Title, by Artist".
_OG_TITLE_RE = re.compile(r'<meta property="og:title" content="([^"]+)"')
_OG_TITLE_SPLIT_RE = re.compile(r"^(.*), by (.+)$")


class BandcampSearchUnavailableError(Exception):
    """Raised by ``find_album_match_via_search`` when the album-autocomplete
    request failed after retries (network error, non-200, or exhausted 429
    backoff) -- as opposed to a request that succeeded but found no
    floor-clearing match.

    The distinction matters to the offline album-search drain
    (scripts/bandcamp_pipeline.py): a transient blip must not be durably
    recorded as ``not_found`` (#661), so the drain catches this and leaves
    the row ``pending``/re-runnable rather than writing. The PR2 runtime
    probe fallback has no such persistence concern and is expected to catch
    this and degrade to "no match" like any other adapter failure.
    """


def fix_autocomplete_url(url: str) -> str:
    """De-double the Bandcamp autocomplete API's ``url`` field.

    >>> fix_autocomplete_url(
    ...     "https://x.bandcamp.comhttps://x.bandcamp.com/album/y"
    ... )
    'https://x.bandcamp.com/album/y'
    >>> fix_autocomplete_url("https://x.bandcamp.com/album/y")
    'https://x.bandcamp.com/album/y'
    """
    idx = url.rfind(_HTTPS_PREFIX)
    return url[idx:] if idx > 0 else url


def clean_title_for_query(title: str) -> str:
    """Strip library format-suffix junk from a title for the autocomplete
    QUERY STRING only -- scoring still compares against the raw title.

    Handles the shapes seen in the LML#1069 measurement: trailing vinyl-size
    markers (``12"``), trailing bracketed tags (``[missing]``), and a
    trailing ``(studio)`` qualifier. Conservative: when in doubt, leaves the
    token in, since a slightly noisier query only costs recall while the
    80/80 floor still guards precision.
    """
    if not title:
        return title
    result = _QUERY_BRACKET_TAG_RE.sub("", title)
    result = _QUERY_STUDIO_SUFFIX_RE.sub("", result)
    result = _QUERY_VINYL_SIZE_RE.sub("", result)
    return result.strip()


def normalize_bc_title(title: str) -> str:
    """Recall-recovery normalization for the LML#1069 near-miss corpus.

    Applied to both sides (query title and candidate title) as a SECOND
    acceptance pass in ``find_album_match_via_search`` -- only consulted
    when the raw-field pass misses. See the near-miss corpus in
    ``plans/lml-1069-bandcamp-album-first.md`` for the TRUE/FALSE rows this
    is parameterized against.
    """
    if not title:
        return title
    result = _QUERY_BRACKET_TAG_RE.sub("", title)
    result = _NORMALIZE_YEAR_RANGE_RE.sub("", result)
    result = _NORMALIZE_SLASH_SPACING_RE.sub("/", result)
    return result.strip()


def parse_og_title(og_title: str) -> tuple[str, str] | None:
    """Split a Bandcamp album page's ``og:title`` content ("Title, by Artist")
    into ``(title, artist)``. Returns ``None`` if the content doesn't match
    that shape.

    The HTML attribute value is entity-escaped (``&#39;``, ``&amp;``, etc.)
    by Bandcamp's page rendering, so both halves are unescaped before being
    returned -- otherwise a genuinely correct match with an apostrophe or
    ampersand in its title/artist scores tens of points below the acceptance
    floor against the un-escaped library string (LML#1069 album-search
    backlog).
    """
    match = _OG_TITLE_SPLIT_RE.match(og_title)
    if not match:
        return None
    return html.unescape(match.group(1)), html.unescape(match.group(2))


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
        Artist-first: autocomplete the artist subdomain, then scrape the
        artist's ``/music`` catalog and fuzzy-match the album title. When
        that yields nothing, falls back to the shared album-first
        ``find_album_match_via_search`` (LML#1069 PR2) -- a label/imprint-
        hosted release never has an own-artist catalog match to find, so
        without the fallback this method can never see it. Worst case this
        costs one extra rate-limited autocomplete request; both phases'
        response shapes stay encapsulated here.
        """
        artist_results = await self.search_artist(artist)
        match: SourceMatch | None = None
        best_artist: dict | None = None
        best_artist_score = 0.0
        for result in artist_results:
            s = score_match(artist, result["name"])
            if s >= 80.0 and s > best_artist_score:
                best_artist = result
                best_artist_score = s
        if best_artist is not None:
            # A fetch failure (None) is treated as "no catalog" for the live
            # match path -- there is no retry loop here, so it degrades to no match.
            catalog = await self.fetch_artist_catalog(best_artist["slug"]) or []
            matched_artist_name = best_artist["name"]
            match = find_best_source_match(
                catalog,
                artist,
                title,
                artist_fn=lambda _: matched_artist_name,
                title_fn=lambda x: x["title"],
                url_fn=lambda x: x["url"],
                service="bandcamp",
            )
        if match is not None:
            return match

        try:
            return await self.find_album_match_via_search(artist, title)
        except BandcampSearchUnavailableError:
            # No retry channel on the live path (same posture as the
            # catalog-fetch-failure case above) -- degrade to no match.
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

    async def search_albums(self, query: str) -> list[dict] | None:
        """Search Bandcamp autocomplete for album-type results (LML#1069).

        Unlike ``search_artist`` (which collapses any fetch failure to
        ``[]``, fine for its no-persistence live-only caller), this
        distinguishes a transient fetch failure (``None`` -- network error,
        non-200, or exhausted 429 backoff) from a genuine empty result set
        (``[]``), mirroring ``fetch_artist_catalog``'s None/[] split (#661).
        The offline album-search drain needs that distinction to avoid
        durably recording a blip as ``not_found``.

        Returns a list of dicts with keys ``artist``, ``title``, ``url``
        (de-doubled via ``fix_autocomplete_url``) on success, or ``None`` on
        fetch failure.
        """
        resp = await self._request_with_retry(
            "GET",
            AUTOCOMPLETE_URL,
            params={"q": query, "param": "a"},
            timeout=10.0,
        )
        if resp is None or resp.status_code != 200:
            return None

        data = resp.json()
        results = []
        for item in data.get("results", []):
            if item.get("type") != "a":
                continue
            results.append(
                {
                    "artist": item.get("band_name", ""),
                    "title": item.get("name", ""),
                    "url": fix_autocomplete_url(item.get("url", "")),
                }
            )
        return results

    async def find_album_match_via_search(self, artist: str, title: str) -> SourceMatch | None:
        """Album-first Bandcamp discovery (LML#1069): search the general
        Bandcamp index for ``artist title`` and bind by the returned
        ``band_name``, rather than requiring the artist's own catalog page
        to carry the album. Recovers label/imprint-hosted releases (and
        heals artist-first false negatives) that ``find_album_match`` can
        never see, since that path only ever looks at the matched artist's
        own subdomain catalog.

        Exactly one autocomplete request. Runs the shared 80/80-floor
        matcher (``find_best_source_match``) up to twice against that SAME
        result set: once on raw fields, and -- only if that misses -- once
        more with ``normalize_bc_title`` applied to both sides' titles,
        accepting the first pass that clears the floor (see
        ``normalize_bc_title`` for the near-miss corpus this recovers).

        Raises:
            BandcampSearchUnavailableError: the autocomplete request failed
                after retries (see ``search_albums``). Offline callers
                should catch this to avoid a durable not_found write on a
                transient blip; the live runtime path (PR2) is expected to
                catch it and degrade to "no match" like any other adapter
                failure.
        """
        query = f"{artist} {clean_title_for_query(title)}".strip()
        results = await self.search_albums(query)
        if results is None:
            raise BandcampSearchUnavailableError(query)
        if not results:
            return None

        match = find_best_source_match(
            results,
            artist,
            title,
            artist_fn=lambda r: r["artist"],
            title_fn=lambda r: r["title"],
            url_fn=lambda r: r["url"],
            service="bandcamp",
        )
        if match is not None:
            return match

        normalized_title = normalize_bc_title(title)
        return find_best_source_match(
            results,
            artist,
            normalized_title,
            artist_fn=lambda r: r["artist"],
            title_fn=lambda r: normalize_bc_title(r["title"]),
            url_fn=lambda r: r["url"],
            service="bandcamp",
        )

    async def verify_album_page(self, url: str, artist: str, title: str) -> bool:
        """Fetch a matched album page and confirm its ``og:title`` clears the
        80/80 floor against the requested ``(artist, title)``.

        The album-search drain's insurance against persisting a wrong direct
        link (worse than no link at all, per the LML#1069 plan's house rule)
        before a write. Conservative: any fetch or parse failure returns
        ``False`` -- an unverifiable hit is not written.

        Runs the SAME raw-then-normalized acceptance pass as
        ``find_album_match_via_search``: raw fields first, then
        ``normalize_bc_title`` on both sides' titles if that misses. Without
        the second pass, a match ``find_album_match_via_search`` only found
        via its own normalized pass (e.g. a library title carrying a
        cataloger annotation the real release title never had -- "Dreamy
        [full-length]" vs the page's "Dreamy") gets re-scored here against
        the raw un-normalized title and wrongly rejected -- the LML#1069
        85-row verify_failed floor.
        """
        resp = await self._request_with_retry("GET", url, timeout=15.0, follow_redirects=True)
        if resp is None or resp.status_code != 200:
            return False
        resp.encoding = "utf-8"
        match = _OG_TITLE_RE.search(resp.text)
        if not match:
            return False
        parsed = parse_og_title(match.group(1))
        if parsed is None:
            return False
        page_title, page_artist = parsed
        artist_score = score_match(artist, page_artist)
        if is_acceptable_match(artist_score, score_match(title, page_title)):
            return True
        return is_acceptable_match(
            artist_score,
            score_match(normalize_bc_title(title), normalize_bc_title(page_title)),
        )
