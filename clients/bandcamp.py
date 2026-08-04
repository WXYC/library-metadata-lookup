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

from clients.bandcamp_breaker import BandcampBreakerOpenError, get_bandcamp_probe_breaker
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


class BandcampRateLimitedError(Exception):
    """Raised by ``_request_with_retry(..., fail_fast=True)`` when a single
    fail-fast attempt gets a 429.

    Distinct from a plain ``None`` return (which the non-fail-fast retry loop
    still returns after exhausting its backoff): a 429 under ``fail_fast`` is
    a *shed* the caller must not treat as "asked, got nothing" -- propagating
    it as an exception rather than returning ``None`` lets
    ``entity.streaming_url_cache.resolve_streaming_url_with_cache`` (also
    supporting ``fail_fast=True``) tell a rate-limit shed apart from a
    genuine no-match, so it never records a negative-cache row for a shed.
    See ``clients/bandcamp_breaker.py``.
    """


class BandcampTransportError(Exception):
    """Raised by ``search_artist`` / ``fetch_artist_catalog`` / ``search_albums``
    under ``fail_fast=True`` when the underlying ``_request_with_retry`` call
    fails at the transport layer (network error, connect timeout) or returns
    a non-200/non-429 response (5xx, Cloudflare 403/1015, etc.).

    Distinct from both :class:`BandcampRateLimitedError` (a 429, a genuine
    rate-limit shed) and a clean 200 response with no match: this is
    "couldn't tell", not "asked, got nothing." Left unraised -- degrading to
    ``None``/``[]`` the way the default (retrying) mode does -- it would be
    indistinguishable from a real no-match, which under ``fail_fast`` would
    let :meth:`clients.bandcamp_breaker.BandcampProbeBreaker.record_success`
    declare a HALF_OPEN trial recovered while Bandcamp is still down, and
    would let ``entity.streaming_url_cache.resolve_streaming_url_with_cache``
    UPSERT a 7-day false-negative row (LML#1106 review, FIX 2).

    Propagates through ``find_album_match``'s fail-fast wrapper, which
    records it via :meth:`clients.bandcamp_breaker.BandcampProbeBreaker.
    record_transport_failure` rather than ``record_success`` -- see that
    method's docstring (LML#1106 review, FIX 1) for why a transport failure
    gets its own breaker method that counts toward opening, unlike
    ``discogs/breaker.py``'s neutral ``record_server_error``.

    ``search_artist`` and ``search_albums`` raise it directly, and it always
    propagates unmodified from those two -- neither has anywhere else in
    ``_find_album_match_impl`` to be caught. ``fetch_artist_catalog`` is the
    one leg where a raise here does NOT always propagate: LML#1106 review
    FIX 5 catches it inside ``_find_album_match_impl`` so the album-first
    fallback still runs, and a fresh :class:`BandcampTransportError` is only
    re-raised from there if that fallback ALSO fails to produce a match --
    a hit means "we got an answer" and resolves normally (LML#1106 review
    round 2, FIX A; see ``_find_album_match_impl`` for the full conditional
    table).

    Only meaningful under ``fail_fast``: the default mode keeps its
    pre-#1106 degrade-to-``None``/``[]`` behavior unchanged.
    """


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


def _reject_malshaped_fail_fast_body(data: object, url: str) -> dict:
    """Guard a fail-fast-parsed autocomplete body's SHAPE, not just its
    parseability (LML#1106 review round 2, FIX B).

    ``resp.json()`` succeeding only guarantees valid JSON -- a valid-JSON
    non-object body (a bare list, a bare string), a ``results`` field that
    isn't a list, or a ``results`` list whose ITEMS aren't objects still
    crashes the ``for item in data.get("results",
    [])`` loop downstream with an ``AttributeError``, which escapes the
    fail-fast taxonomy entirely and lands in
    ``lookup/enrichment/bandcamp_probe.py``'s ``except Exception`` catch-all
    -- a loud ``logger.exception`` per item (the #755 flood shape) routed
    through the no-op-in-CLOSED ``record_aborted`` instead of the counted
    ``record_transport_failure`` shed path, so a sustained mal-shaped-body
    run would never trip the breaker. Treat it exactly like an unparseable
    body: raise :class:`BandcampTransportError`.

    Only called from ``fail_fast`` call sites -- default mode never guards
    shape and still propagates a raw ``AttributeError`` on these bodies,
    unchanged.
    """
    if not isinstance(data, dict):
        raise BandcampTransportError(url)
    results = data.get("results", [])
    if not isinstance(results, list) or not all(isinstance(item, dict) for item in results):
        raise BandcampTransportError(url)
    return data


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

    async def _request_with_retry(
        self, method: str, url: str, *, fail_fast: bool = False, **kwargs
    ) -> httpx.Response | None:
        """Make an HTTP request with retry on 429.

        Returns the response, or None on failure after retries. LML#1040:
        the retry loop itself is ``discogs.admission.retry_429``, shared with
        ``DiscogsService._request_with_retry``; this method supplies
        Bandcamp's own permit acquisition, delay policy, and log wording
        (byte-identical to the pre-#1040 inline loop -- see
        ``tests/unit/test_bandcamp_retry_characterization.py``).

        ``fail_fast=True`` (LML#1106) is a single-attempt mode with no
        ``retry_429`` backoff loop -- for the response-path caller
        (LML#1098, ``lookup/enrichment/bandcamp_probe.py``, merged into this
        branch alongside it) where inline request latency must not stack a
        3s+ retry sleep on top of that caller's own wall-clock ceiling. A
        429 on that
        single attempt raises :class:`BandcampRateLimitedError` instead of
        degrading to ``None``, so the caller can tell a shed apart from a
        genuine no-match (and never record a negative-cache row for it). A
        request-layer failure (network error) still degrades to ``None``
        either way -- that is not a rate-limit signal.
        """
        client = await self._get_client()

        async def _attempt(attempt: int) -> httpx.Response:
            async with self._semaphore:
                await self._rate_limiter.acquire()
                try:
                    return await client.request(method, url, **kwargs)
                except Exception as e:
                    raise _BandcampRequestFailedError() from e

        if fail_fast:
            try:
                response = await _attempt(0)
            except _BandcampRequestFailedError:
                return None
            if response.status_code == 429:
                raise BandcampRateLimitedError(url)
            return response

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

    async def find_album_match(
        self, artist: str, title: str, *, fail_fast: bool = False
    ) -> SourceMatch | None:
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

        ``fail_fast=True`` (LML#1106) is a single-attempt mode for the
        response-path caller (LML#1098, merged into this branch): every underlying HTTP call makes a
        single attempt (no retry-on-429 backoff), and the whole call is gated
        by ``BandcampProbeBreaker`` so a sustained 429 run sheds
        (``BandcampBreakerOpenError``) before touching the network at all. A
        429 that does reach the network raises ``BandcampRateLimitedError``
        (propagated, not swallowed to ``None``) so the caller can't confuse a
        shed with a genuine no-match. A non-429 transport failure (5xx,
        Cloudflare 403/1015, connect timeout, non-200 -- raised by
        ``search_artist`` / ``fetch_artist_catalog`` / ``search_albums`` as
        :class:`BandcampTransportError`) is likewise propagated rather than
        degrading to a clean ``None`` -- EXCEPT that a ``fetch_artist_catalog``
        failure whose album-first fallback then HITS resolves normally (we
        got an answer; LML#1106 review round 2, FIX A) rather than raising.
        Whenever it does propagate, it is recorded via
        ``record_transport_failure``, not ``record_success``, so it can
        neither declare a HALF_OPEN trial recovered nor let
        ``resolve_streaming_url_with_cache`` UPSERT a false-negative row for
        it -- and, unlike an unexpected raise, it counts toward opening the
        breaker on its own (LML#1106 review, FIX 1 -- see
        ``clients/bandcamp_breaker.py``'s module docstring for why this
        differs from ``record_aborted``).

        Every exit from the fail-fast call -- success, a shed, a transport
        error, an unexpected raise, or a cancellation (``asyncio.
        CancelledError``, a ``BaseException`` and the designed timeout
        mechanism for this mode, #1098) -- records exactly one terminal
        breaker outcome via a ``try/finally`` guard, so none of them can
        strand a HALF_OPEN trial the way a bare ``except Exception`` (which
        ``CancelledError`` does not match) used to (LML#1106 review, FIX 1;
        mirrors ``discogs.admission.admit_or_shed``'s LML#787 ``finally``).
        """
        if not fail_fast:
            return await self._find_album_match_impl(artist, title, fail_fast=False)

        breaker = get_bandcamp_probe_breaker()
        epoch = breaker.allow_request()
        if epoch is None:
            raise BandcampBreakerOpenError(f"Bandcamp fail-fast breaker open: {artist} - {title}")

        recorded = False
        try:
            result = await self._find_album_match_impl(artist, title, fail_fast=True)
        except BandcampRateLimitedError:
            breaker.record_shed(epoch=epoch)
            recorded = True
            raise
        except BandcampTransportError:
            breaker.record_transport_failure(epoch=epoch)
            recorded = True
            raise
        else:
            breaker.record_success(epoch=epoch)
            recorded = True
            return result
        finally:
            # LML#1106 review FIX 1: any exit neither branch above named --
            # an unexpected raise, or a cancellation escaping both `except`
            # clauses (``CancelledError`` is a ``BaseException``) -- still
            # records a terminal outcome exactly once via ``record_aborted``,
            # so it can't strand a HALF_OPEN trial forever (LML#787 shape).
            # ``record_aborted`` is a no-op in CLOSED for any state --
            # neither branch above uses it, and it never touches
            # ``_consecutive_failures`` in any state, so this fallback is
            # NOT "resetting the CLOSED-state failure run"; it exists purely
            # to resolve a stranded HALF_OPEN trial.
            if not recorded:
                breaker.record_aborted(epoch=epoch)

    async def _find_album_match_impl(
        self, artist: str, title: str, *, fail_fast: bool
    ) -> SourceMatch | None:
        """The artist-first + album-first fallback body, unwrapped from the
        breaker (see ``find_album_match``, which is the public entry point)."""
        artist_results = await self.search_artist(artist, fail_fast=fail_fast)
        match: SourceMatch | None = None
        best_artist: dict | None = None
        best_artist_score = 0.0
        for result in artist_results:
            s = score_match(artist, result["name"])
            if s >= 80.0 and s > best_artist_score:
                best_artist = result
                best_artist_score = s
        # Only set True under fail_fast (default mode never raises
        # BandcampTransportError from fetch_artist_catalog) -- see the
        # post-fallback check below for why this can't be decided here.
        catalog_leg_failed = False
        if best_artist is not None:
            # Default mode: a fetch failure (None) is treated as "no catalog"
            # for the live match path -- there is no retry loop here, so it
            # degrades to no match and falls through to the album-first
            # fallback below. Under fail_fast, ``fetch_artist_catalog``
            # instead raises ``BandcampTransportError`` on a transport
            # failure; LML#1106 review FIX 5 catches it here so it degrades
            # the SAME way -- an empty catalog that falls through to the
            # fallback -- rather than pre-empting it. The fallback's own
            # transport failure (below) still raises unconditionally. A
            # CLEAN MISS from the fallback is a separate case (LML#1106
            # review round 2, FIX A): the album-autocomplete index has
            # strictly lower recall than the catalog scrape (that is why
            # #1069 added it as a fallback, not a replacement), so "the
            # fallback found nothing" after this leg genuinely couldn't ask
            # is NOT evidence of absence -- ``catalog_leg_failed`` records
            # that so the post-fallback check below can still raise instead
            # of resolving to a cachable ``None``.
            try:
                catalog = (
                    await self.fetch_artist_catalog(best_artist["slug"], fail_fast=fail_fast) or []
                )
            except BandcampTransportError:
                catalog = []
                catalog_leg_failed = True
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
            fallback_match = await self.find_album_match_via_search(
                artist, title, fail_fast=fail_fast
            )
        except BandcampSearchUnavailableError:
            # No retry channel on the live path (same posture as the
            # catalog-fetch-failure case above) -- degrade to no match.
            return None

        if fallback_match is not None:
            # We got an answer -- safe to treat as a genuine resolution even
            # though the catalog leg itself couldn't ask.
            return fallback_match

        if catalog_leg_failed:
            # The catalog leg genuinely couldn't ask, and the strictly-
            # lower-recall fallback came back clean: that combination is a
            # couldn't-ask, not a confirmed no-match, so it must never
            # resolve to a cachable ``None`` (LML#1106 review round 2, FIX
            # A). Raising here lets ``find_album_match``'s wrapper record it
            # via ``record_transport_failure`` (counts toward opening the
            # breaker) instead of ``record_success``.
            raise BandcampTransportError(
                f"catalog leg failed for {artist!r}; album-first fallback clean-missed"
            )

        return None

    async def search_artist(self, artist_name: str, *, fail_fast: bool = False) -> list[dict]:
        """Search Bandcamp autocomplete for artist pages.

        Returns list of dicts with keys: name, url, slug.
        Only returns band results (type "b").

        ``fail_fast=True`` (LML#1106, FIX 2): a non-200 response or a
        network-layer failure raises :class:`BandcampTransportError` instead
        of degrading to ``[]`` -- an empty list must mean "asked, no artists
        matched", not "couldn't ask". The default mode's degrade-to-``[]``
        behavior is unchanged. LML#1106 review FIX 6: a 200 whose body isn't
        valid JSON (a Cloudflare HTML interstitial, a truncated response) is
        the SAME "couldn't ask" shape -- under ``fail_fast`` it also raises
        :class:`BandcampTransportError` rather than letting the raw
        ``json.JSONDecodeError`` escape the fail-fast taxonomy entirely.
        LML#1106 review round 2, FIX B: a 200 whose body IS valid JSON but
        not object-shaped (a bare list, a bare string) or whose ``results``
        field isn't a list is the same "couldn't ask" shape and is guarded
        the same way (see ``_reject_malshaped_fail_fast_body``). Default
        mode is unchanged: an unparseable or mal-shaped body still
        propagates exactly as it does today.
        """
        resp = await self._request_with_retry(
            "GET",
            AUTOCOMPLETE_URL,
            params={"q": artist_name, "param": "b"},
            timeout=10.0,
            fail_fast=fail_fast,
        )
        if resp is None or resp.status_code != 200:
            if fail_fast:
                raise BandcampTransportError(AUTOCOMPLETE_URL)
            return []

        if fail_fast:
            try:
                data = resp.json()
            except ValueError as e:
                raise BandcampTransportError(AUTOCOMPLETE_URL) from e
            data = _reject_malshaped_fail_fast_body(data, AUTOCOMPLETE_URL)
        else:
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

    async def fetch_artist_catalog(
        self, slug: str, *, fail_fast: bool = False
    ) -> list[dict] | None:
        """Fetch album list from {slug}.bandcamp.com/music.

        Returns a list of dicts with keys ``url``, ``title`` (deduplicated by
        URL) on a successful fetch -- possibly empty if the page has no album
        links. In the default mode, returns ``None`` on a fetch failure
        (network error, timeout, non-200, or 429 after retries), which
        callers must NOT treat as a definitively-empty catalog: an empty list
        means "the artist has no releases" while ``None`` means "we couldn't
        tell" and should be retried rather than recorded as a final result
        (#661). Under ``fail_fast=True`` (LML#1106, FIX 2), that same
        transport failure instead raises :class:`BandcampTransportError` --
        see ``search_artist`` for why a raise, not ``None``, is required
        there.
        """
        url = f"https://{slug}.bandcamp.com/music"
        resp = await self._request_with_retry(
            "GET", url, timeout=15.0, follow_redirects=True, fail_fast=fail_fast
        )
        if resp is None or resp.status_code != 200:
            if fail_fast:
                raise BandcampTransportError(url)
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

    async def search_albums(self, query: str, *, fail_fast: bool = False) -> list[dict] | None:
        """Search Bandcamp autocomplete for album-type results (LML#1069).

        Unlike ``search_artist`` (which collapses any fetch failure to
        ``[]``, fine for its no-persistence live-only caller), this
        distinguishes a transient fetch failure (``None`` -- network error,
        non-200, or exhausted 429 backoff) from a genuine empty result set
        (``[]``), mirroring ``fetch_artist_catalog``'s None/[] split (#661).
        The offline album-search drain needs that distinction to avoid
        durably recording a blip as ``not_found``.

        Returns a list of dicts with keys ``artist``, ``title``, ``url``
        (de-doubled via ``fix_autocomplete_url``) on success, or (default
        mode) ``None`` on fetch failure. Under ``fail_fast=True`` (LML#1106,
        FIX 2), that same failure instead raises
        :class:`BandcampTransportError` -- see ``search_artist`` for why
        (including the FIX 6 unparseable-body case and the FIX B round-2
        mal-shaped-body case).
        """
        resp = await self._request_with_retry(
            "GET",
            AUTOCOMPLETE_URL,
            params={"q": query, "param": "a"},
            timeout=10.0,
            fail_fast=fail_fast,
        )
        if resp is None or resp.status_code != 200:
            if fail_fast:
                raise BandcampTransportError(AUTOCOMPLETE_URL)
            return None

        if fail_fast:
            try:
                data = resp.json()
            except ValueError as e:
                raise BandcampTransportError(AUTOCOMPLETE_URL) from e
            data = _reject_malshaped_fail_fast_body(data, AUTOCOMPLETE_URL)
        else:
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

    async def find_album_match_via_search(
        self, artist: str, title: str, *, fail_fast: bool = False
    ) -> SourceMatch | None:
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
            BandcampSearchUnavailableError: default mode only -- the
                autocomplete request failed after retries (see
                ``search_albums``). Offline callers should catch this to
                avoid a durable not_found write on a transient blip; the live
                runtime path (PR2) is expected to catch it and degrade to
                "no match" like any other adapter failure.
            BandcampTransportError: ``fail_fast=True`` only (LML#1106, FIX
                2) -- ``search_albums`` raises this directly on the same
                underlying failure instead of returning ``None``, so it
                propagates from here uncaught (``results`` is never ``None``
                under ``fail_fast``, so the ``BandcampSearchUnavailableError``
                branch below is unreachable in that mode).
        """
        query = f"{artist} {clean_title_for_query(title)}".strip()
        results = await self.search_albums(query, fail_fast=fail_fast)
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
