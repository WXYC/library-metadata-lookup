"""Discogs API service with caching and rate limiting."""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Iterator
from enum import StrEnum
from typing import TYPE_CHECKING, Any

import httpx
import sentry_sdk
from rapidfuzz import fuzz
from wxyc_etl.text import is_compilation_artist
from wxyc_fastapi.http import async_singleton
from wxyc_fastapi.observability import (
    add_breadcrumb,
    get_cache_stats_recorder,
    timed_api,
)

from config.settings import get_settings
from discogs.breaker import DiscogsBreakerOpenError
from discogs.fallthrough import apply_request_ctx_tags, fallthrough, request_context
from discogs.matching import normalize_artist_for_validation, normalize_for_track_comparison
from discogs.memory_cache import (
    ARTIST_CACHE,
    LABEL_CACHE,
    MASTER_CACHE,
    RELEASE_CACHE,
    SEARCH_CACHE,
    TRACK_CACHE,
    VALIDATION_CACHE,
    async_cached,
)
from discogs.models import (
    ArtistCredit,
    ArtistDetails,
    ArtistRef,
    DiscogsArtistSearchResult,
    DiscogsSearchRequest,
    DiscogsSearchResponse,
    DiscogsSearchResult,
    LabelCredit,
    MasterRelease,
    MemberRef,
    ReleaseInfo,
    ReleaseMetadataResponse,
    ReleaseVideo,
    TrackItem,
    TrackReleasesResponse,
)
from discogs.ratelimit import get_discogs_breaker, get_discogs_rate_gate, get_semaphore

if TYPE_CHECKING:
    from discogs.cache_service import DiscogsCacheService


def add_discogs_breadcrumb(
    operation: str,
    data: dict[str, Any] | None = None,
    level: str = "info",
) -> None:
    """Module-local alias that pins the ``"discogs"`` category for breadcrumbs."""
    add_breadcrumb(category="discogs", message=operation, data=data, level=level)


logger = logging.getLogger(__name__)

DISCOGS_API_BASE = "https://api.discogs.com"


# Cap individual retry sleeps. Discogs's per-token rate-limit window is 60
# seconds; once we cross that, the bucket has reset and there's no benefit to
# waiting longer for the same 429.
_MAX_RETRY_DELAY_SECONDS = 60.0

# LML#755 saturation-breaker counter. Emitted on the #683 ``cache.*`` counter
# surface (``get_cache_stats_recorder().record(...)``) every time a live Discogs
# request is shed because the breaker is OPEN, so breaker-open time is alertable
# on the same PostHog/Sentry seam as the row-less flag degradation alerts.
BREAKER_OPEN_STAT_KEY = "discogs_breaker_open_shed"

# Log fingerprint for `search_artists`' malformed-item page-distrust path.
# The live smoke (tests/integration/test_search_artists_live.py) matches on
# this to tell payload-shape drift (fail red) apart from transient
# unavailability (skip) — both paths return None to callers.
SEARCH_ARTISTS_DISTRUST_LOG_PREFIX = "search_artists distrusting page"

# Fuzzy fallback for `validate_track_on_release` artist matching. Strict substring
# matching loses on collaboration trios where neither name is a substring of the
# other (e.g., request "Orcutt Shelley Miller" vs release artist
# "Bill Orcutt, Tashi Shelley & Robbie Miller"). rapidfuzz token_set_ratio is
# order- and stopword-tolerant; the threshold was chosen so that one shared
# token across two short names ("Bill Orcutt" vs "Orcutt Shelley Miller") just
# clears the bar (~70.6) while unrelated artists score well below 50. See LML#210.
_ARTIST_FUZZY_MATCH_THRESHOLD = 70

# Fuzzy fallback for `validate_track_on_release` track-title matching (LML#334).
# The bidirectional substring gate loses on typographic noise that leaves token
# content intact: a singular/plural typo ("tower of dub" vs "Towers Of Dub"), a
# dropped interior word ("smells teen spirit" vs "Smells Like Teen Spirit"), or a
# dash-vs-paren suffix ("de la soul - radio edit" vs "de la soul (radio edit)").
# token_set_ratio is order- and stopword-tolerant. The threshold is stricter than
# the artist-side 70 because track titles are short, so a single shared token can
# score 50+; 85 lets the substitutions above through (each scores >= 96) while
# rejecting one-shared-token adversarial near-misses ("towers of dub" vs "towers
# of london" scores ~82). Mirrored in `discogs/cache_service.py` so cache hits and
# misses agree on the verdict.
_TRACK_TITLE_FUZZY_MATCH_THRESHOLD = 85


def _approx_semaphore_queue_depth(semaphore: asyncio.Semaphore) -> int:
    """Return an approximate count of waiters queued behind ``semaphore``.

    Used only for Sentry-span observability (see WXYC/library-metadata-lookup#358).
    The value is read *before* the awaiting caller takes a permit, so it reflects
    pre-acquire backlog rather than steady-state utilisation.

    Inspects ``asyncio.Semaphore._waiters`` — a private CPython attribute, but the
    only way to see queued tasks without acquiring a permit ourselves. If the
    attribute is missing on a future CPython release, the helper returns ``-1``
    so the metric is identifiable as "unknown" rather than silently misleading.
    """
    try:
        waiters = semaphore._waiters  # type: ignore[attr-defined]
        return len(waiters) if waiters else 0
    except AttributeError:
        return -1


def _parse_ratelimit_remaining(raw: str | None) -> int | None:
    """Parse the ``X-Discogs-Ratelimit-Remaining`` header into an int, or None.

    Feeds the LML#755 breaker's proactive floor. A missing/non-numeric header is
    ``None`` (unknown), which the breaker treats as "no floor signal" rather than
    zero — a malformed header must not spuriously trip the shed.
    """
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _compute_retry_delay(attempt: int, retry_after_header: str | None) -> float:
    """Compute how long to sleep before the next retry of a 429-rate-limited request.

    If Discogs sent a numeric ``Retry-After`` header, honor it (capped).
    Otherwise, use exponential backoff with jitter so multiple parallel
    backfill containers don't synchronize their retry waves into the next 429.

    Args:
        attempt: 0-indexed retry attempt number.
        retry_after_header: Raw value of the ``Retry-After`` response header,
            or None. RFC 9110 allows seconds (numeric) or HTTP-date; Discogs
            sends seconds, so non-numeric values fall through to backoff.

    Returns:
        Delay in seconds, never exceeding ``_MAX_RETRY_DELAY_SECONDS``.
    """
    if retry_after_header is not None:
        try:
            return min(float(retry_after_header), _MAX_RETRY_DELAY_SECONDS)
        except ValueError:
            pass
    base = 2**attempt
    jitter = random.uniform(0.5, 1.5)
    return min(base * jitter, _MAX_RETRY_DELAY_SECONDS)


def calculate_confidence(
    request_artist: str | None,
    request_album: str | None,
    result_artist: str,
    result_album: str,
    request_label: str | None = None,
    result_label: str | None = None,
    request_format: str | None = None,
    result_format: str | None = None,
) -> float:
    """Calculate confidence score for how well a search result matches a request.

    Scoring rules:
    - Exact artist match: +0.4
    - Partial artist match (substring): +0.3
    - Exact album match: +0.4
    - Partial album match (substring): +0.3
    - Both fields match well (score >= 0.6): +0.2 bonus
    - Exact label match: +0.1
    - Partial label match (substring): +0.05
    - Format match: +0.05
    - Minimum score for any result: 0.2

    Args:
        request_artist: Artist from the search request
        request_album: Album from the search request
        result_artist: Artist from the search result
        result_album: Album from the search result
        request_label: Label from the library item (optional)
        result_label: Label from the Discogs result (optional)
        request_format: Discogs format term from library item (optional)
        result_format: Discogs format term from the result (optional)

    Returns:
        Confidence score between 0.2 and 1.0
    """
    score = 0.0

    def normalize(s: str | None) -> str:
        return s.lower().strip() if s else ""

    req_artist = normalize(request_artist)
    req_album = normalize(request_album)
    res_artist = normalize(result_artist)
    res_album = normalize(result_album)

    # Artist match
    if req_artist and res_artist:
        if req_artist == res_artist:
            score += 0.4
        elif req_artist in res_artist or res_artist in req_artist:
            score += 0.3

    # Album match
    if req_album and res_album:
        if req_album == res_album:
            score += 0.4
        elif req_album in res_album or res_album in req_album:
            score += 0.3

    # Bonus for both matches
    if score >= 0.6:
        score += 0.2

    # Base score if we got any result
    if score == 0:
        score = 0.2

    # Label match (bonus signal, no penalty for mismatch)
    req_label = normalize(request_label)
    res_label = normalize(result_label)
    if req_label and res_label:
        if req_label == res_label:
            score += 0.1
        elif req_label in res_label or res_label in req_label:
            score += 0.05

    # Format match (bonus signal, no penalty for mismatch)
    req_fmt = normalize(request_format)
    res_fmt = normalize(result_format)
    if req_fmt and res_fmt and req_fmt == res_fmt:
        score += 0.05

    return min(score, 1.0)


class DiscogsApiCheckResult(StrEnum):
    """Outcome of a Discogs API connectivity probe.

    The string values are surfaced verbatim by ``GET /health`` so operators can
    distinguish auth drift, rate limiting, and upstream outages without a log
    pull. See ``routers/health.py:_check_discogs_api``.
    """

    OK = "ok"
    AUTH_ERROR = "auth-error"  # 401, 403
    RATE_LIMITED = "rate-limited"  # 429
    UPSTREAM_ERROR = "upstream-error"  # 5xx
    NETWORK_ERROR = "network-error"  # connection / timeout
    ERROR = "error"  # unknown / other


class DiscogsService:
    """Service for all Discogs API interactions with caching.

    Supports an optional PostgreSQL cache service for faster lookups.
    When cache_service is provided, queries check local cache first,
    then fall back to Discogs API, and cache API results for future queries.
    """

    def __init__(
        self,
        token: str | None = None,
        cache_service: DiscogsCacheService | None = None,
        *,
        api_key: str | None = None,
        api_secret: str | None = None,
    ):
        """Initialize the service with Discogs API credentials.

        Supports two auth methods (token takes precedence when both are supplied):
          1. Personal access token: ``DiscogsService(token="abc123")``
          2. OAuth consumer key/secret: ``DiscogsService(api_key="k", api_secret="s")``
        """
        if token:
            self._auth_header = f"Discogs token={token}"
        elif api_key and api_secret:
            self._auth_header = f"Discogs key={api_key}, secret={api_secret}"
        else:
            raise ValueError("Provide either token or api_key+api_secret")
        self.token = token or api_key  # backward-compat for callers reading .token
        self.cache_service = cache_service
        # Test seam: tests assign ``service._client = mock`` before any call;
        # the singleton getter respects that pre-set value (see _get_client).
        self._client: httpx.AsyncClient | None = None
        # Per-instance singleton — see clients/streaming/base.py for the
        # same pattern (LML#241 FD-leak class).
        self._build_client, self._close_client = async_singleton(self._make_client)

    async def _make_client(self) -> httpx.AsyncClient:
        """Construct the underlying HTTP client.

        Method (not free function) so ``self._auth_header`` closes over the
        per-instance token-vs-key/secret distinction without threading the
        header through ``async_singleton``'s factory signature.
        """
        return httpx.AsyncClient(
            base_url=DISCOGS_API_BASE,
            headers={
                "Authorization": self._auth_header,
                "User-Agent": "LibraryMetadataLookupService/1.0",
            },
            timeout=10.0,
        )

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create the HTTP client.

        Honors a test-set ``self._client`` (tests sometimes pre-assign a
        mock client); otherwise delegates to the singleton getter so
        concurrent first-callers race-safely converge on one instance.
        """
        if self._client is not None:
            return self._client
        client = await self._build_client()
        # Cache on the instance so future calls (and ``service._client``
        # introspection in tests) see the same value the singleton holds.
        self._client = client
        return client

    async def close(self):
        """Close the HTTP client.

        For a test-injected ``self._client`` (a mock), the singleton's
        internal instance is still ``None``, so ``_close_client()`` is a
        no-op for it. Acceptable because tests use ``AsyncMock``, which
        needs no teardown; a real pre-assigned client (only seen in tests
        today) would be a leak.
        """
        await self._close_client()
        self._client = None

    async def check_api(self) -> DiscogsApiCheckResult:
        """Probe Discogs API connectivity, classifying the failure mode.

        Returns a ``DiscogsApiCheckResult`` so ``/health`` can distinguish
        token-rotation drift (401/403), rate limits (429), upstream outages
        (5xx), and network failures from each other. The result is also
        projected onto the active Sentry trace as the ``discogs_api.check``
        tag so historic incidents are queryable in trace explorer.
        """
        # NetworkError covers DNS/refused/reset; TimeoutException covers
        # every connect/read/write/pool timeout. Both signal "couldn't reach
        # Discogs" — anything else (LocalProtocolError, UnsupportedProtocol,
        # RemoteProtocolError) is a programmer/protocol bug and falls through
        # to ERROR with a log line.
        try:
            client = await self._get_client()
            resp = await client.get("/oauth/identity")
        except (httpx.NetworkError, httpx.TimeoutException):
            result = DiscogsApiCheckResult.NETWORK_ERROR
        except Exception as exc:
            logger.warning("Unexpected error in Discogs check_api: %r", exc)
            result = DiscogsApiCheckResult.ERROR
        else:
            status = resp.status_code
            if status == 200:
                result = DiscogsApiCheckResult.OK
            elif status in (401, 403):
                result = DiscogsApiCheckResult.AUTH_ERROR
            elif status == 429:
                result = DiscogsApiCheckResult.RATE_LIMITED
            elif 500 <= status < 600:
                result = DiscogsApiCheckResult.UPSTREAM_ERROR
            else:
                result = DiscogsApiCheckResult.ERROR

        sentry_sdk.set_tag("discogs_api.check", result.value)
        return result

    def _record_breaker_shed(self, method: str, path: str) -> None:
        """Record one LML#755 breaker shed on the #683 ``cache.*`` counter.

        Per-shed logging is ``debug`` on purpose (FIX 7): the human-readable
        OPEN/CLOSED transitions are logged once by the breaker itself, so a
        per-shed ``warning`` would flood the log through a multi-hour flood.
        """
        get_cache_stats_recorder().record(BREAKER_OPEN_STAT_KEY)
        logger.debug("Discogs saturation breaker shed live request: %s %s", method, path)

    async def _request_with_retry(
        self,
        method: str,
        path: str,
        params: dict | None = None,
        max_retries: int | None = None,
    ) -> httpx.Response | None:
        """Make an HTTP request with rate limiting and retry on 429.

        Args:
            method: HTTP method (GET, POST, etc.)
            path: API path (e.g., "/database/search")
            params: Optional query parameters
            max_retries: Max retry attempts on 429 (defaults to settings)

        Returns:
            httpx.Response on success, None on exhausted retries or error
        """
        if max_retries is None:
            max_retries = get_settings().discogs_max_retries

        client = await self._get_client()
        semaphore = get_semaphore()
        rate_gate = get_discogs_rate_gate()
        breaker = get_discogs_breaker()

        # LML#755 saturation shed. When the breaker is OPEN, fast-fail *before*
        # ``rate_limiter.acquire()`` — no queuing on the 50/min limiter, no
        # network call, no 429 backoff sleep. The shed raises
        # ``DiscogsBreakerOpenError`` (NOT a ``None`` return): a shed is
        # "couldn't ask, try later", which callers must treat as *unknown* —
        # never a confirmed-empty verdict that would poison a durable negative
        # cache (LML#755 review, FIX 1). Cache hits never reach here — they
        # short-circuit in ``fallthrough`` before the live probe — so only the
        # live-probe tail is shed. ``epoch`` guards the half-open trial: the
        # terminal ``record_*`` below only drives a half-open transition when its
        # epoch still matches (FIX 6).
        epoch = breaker.allow_request()
        if epoch is None:
            self._record_breaker_shed(method, path)
            raise DiscogsBreakerOpenError(f"Discogs saturation breaker open: {method} {path}")

        # LML#537: tag both wait spans with the seam's method + cache_state
        # via ``apply_request_ctx_tags`` (a no-op when no context is active —
        # the case for the small handful of legacy direct-call tests).
        # Production callers enter via ``fallthrough()`` (dominant) or via
        # the ``request_context()`` helper (the four API-only methods that
        # bypass the seam and the four ``cache is None`` fallback branches).

        # The semaphore wraps a single attempt, not the whole retry loop
        # (LML#569). A 429's inter-attempt ``asyncio.sleep(Retry-After)`` runs
        # *outside* the held permit, so a caller riding out a 30-60s rate-limit
        # window no longer parks one of the 5 permits for that whole window and
        # amplifies ``lml.discogs.semaphore`` acquire-wait for unrelated callers
        # (the last unhandled tail of #537, cause #3). The egress cap is still
        # the per-attempt ``rate_limiter.acquire()`` below — releasing the
        # semaphore during the sleep does not bypass the AsyncLimiter.
        #
        # The per-attempt try/finally guarantees exactly one release per
        # acquire: cancellation mid-sleep (sleep is outside the block) or a
        # raise inside the request leaves no leaked permit and never
        # double-releases. As a result the ``lml.discogs.semaphore`` /
        # ``lml.discogs.rate_limiter`` spans now fire once per attempt rather
        # than once per request — a span-count schema change documented in the
        # #569 PR (no current dashboard/alert aggregates on that count).
        #
        # LML#787: an ADMITTED request must always resolve its breaker
        # bookkeeping. The terminal ``record_*`` sites below set ``recorded``;
        # any exit that reaches the outer ``finally`` without one — the
        # ``httpx.RequestError`` → ``return None`` path, cancellation during
        # the request / limiter acquire / retry sleep, or an unexpected raise —
        # reports ``record_aborted``. If the victim was the HALF_OPEN trial,
        # that re-OPENs the breaker (fresh cool-down → fresh trial) instead of
        # latching it shedding forever (the 2026-07-13 incident); in every
        # other state/epoch it is a no-op. The mid-flight shed raise inside the
        # loop also lands here: its entry epoch is stale by construction
        # (``_open`` bumped it), so the abort is a guaranteed no-op.
        recorded = False
        try:
            for attempt in range(max_retries + 1):
                # LML#755 in-flight shed (FIX 3 / R2-1): re-check the breaker at
                # the top of each attempt via the READ-ONLY
                # ``should_shed_inflight`` — NOT the state-mutating
                # ``allow_request``. A request already past the entry gate must
                # not ride its full ~62s backoff after the breaker opens
                # mid-flight, but it must also not promote itself to the
                # half-open trial (it holds its stale entry ``epoch``, so its
                # terminal ``record_*`` would epoch-mismatch and latch the
                # breaker HALF_OPEN forever — R2-1). The predicate lets the
                # genuine trial (matching epoch) finish and sheds everyone else.
                # The first attempt (attempt 0) was just admitted above, so only
                # re-check on retries.
                if attempt > 0 and breaker.should_shed_inflight(epoch):
                    self._record_breaker_shed(method, path)
                    raise DiscogsBreakerOpenError(
                        f"Discogs saturation breaker opened mid-flight: {method} {path}"
                    )

                # Explicit acquire/release (not `async with semaphore:`) so the
                # wait is wrapped in a Sentry span. The 5-permit semaphore is the
                # dominant source of pre-request dark time on backfill cascades —
                # sampling the queue depth right before the await gives the trace
                # explorer a relative-load signal per call. See
                # WXYC/library-metadata-lookup#358.
                with sentry_sdk.start_span(op="lock.acquire", name="lml.discogs.semaphore") as span:
                    span.set_data(
                        "lml.semaphore.queue_depth", _approx_semaphore_queue_depth(semaphore)
                    )
                    apply_request_ctx_tags(span)
                    await semaphore.acquire()

                try:
                    with sentry_sdk.start_span(
                        op="lock.acquire", name="lml.discogs.rate_limiter"
                    ) as span:
                        apply_request_ctx_tags(span)
                        # LML#841: shared PG token bucket when enabled, else/on-error
                        # the per-process AsyncLimiter (fail-open inside the gate).
                        await rate_gate.acquire()

                    response = await client.request(method, path, params=params)

                    # Log rate limit remaining for observability.
                    remaining = response.headers.get("X-Discogs-Ratelimit-Remaining")
                    if remaining:
                        logger.debug(f"Discogs rate limit remaining: {remaining}")
                except httpx.RequestError as e:
                    logger.error(
                        "Discogs request failed: %s %s -> %s: %r",
                        method,
                        path,
                        type(e).__name__,
                        e,
                        exc_info=True,
                    )
                    # No terminal ``record_*`` here on purpose: a network-layer
                    # failure is not a rate-limit signal, so it neither feeds
                    # the failure run nor closes a trial. The outer ``finally``
                    # reports ``record_aborted`` (LML#787) so a dying trial
                    # re-OPENs instead of latching.
                    return None
                finally:
                    # Released before the retry sleep below, on success, and on
                    # any error/cancellation in this attempt — one release per
                    # acquire.
                    semaphore.release()

                # LML#755: feed the breaker **once per request, on the terminal
                # outcome** (FIX 2/5), not per attempt — the counting unit is
                # failed *requests*, not failed attempts. A non-429 terminal
                # response ends the loop, so we record here and return; a 429
                # that still has retries left loops WITHOUT recording (the
                # failure is only terminal once retries are exhausted).
                remaining_value = _parse_ratelimit_remaining(remaining)
                if response.status_code != 429:
                    recorded = True
                    if response.status_code >= 500:
                        # 5xx is neutral: don't reset the failure run, don't
                        # close a half-open trial (FIX 5). Only opens on an
                        # exhausted floor.
                        breaker.record_server_error(remaining=remaining_value, epoch=epoch)
                    else:
                        breaker.record_success(remaining=remaining_value, epoch=epoch)
                    return response

                if attempt < max_retries:
                    retry_after = response.headers.get("Retry-After")
                    delay = _compute_retry_delay(attempt, retry_after)
                    logger.warning(
                        f"Discogs rate limit hit, retrying in {delay:.2f}s "
                        f"(attempt {attempt + 1}/{max_retries + 1}, "
                        f"Retry-After={retry_after})"
                    )
                    # Sleep WITHOUT holding the permit; re-acquire on the next
                    # pass.
                    await asyncio.sleep(delay)
                    continue

                # Retries exhausted: this request definitively 429'd. Record
                # exactly one failure (per-request unit) carrying the last-seen
                # remaining so an at/below-floor bucket opens proactively even
                # before the reactive threshold (FIX 2).
                logger.error("Discogs rate limit hit, max retries exhausted")
                recorded = True
                breaker.record_failure(remaining=remaining_value, epoch=epoch)
                return None

            return None
        finally:
            if not recorded:
                breaker.record_aborted(epoch=epoch)

    def _parse_title(self, title: str) -> tuple[str, str]:
        """Parse Discogs title format 'Artist - Album' into components."""
        if " - " in title:
            parts = title.split(" - ", 1)
            return parts[0].strip(), parts[1].strip()
        return "", title

    @async_cached(TRACK_CACHE)
    async def search_releases_by_track(
        self,
        track: str,
        artist: str | None = None,
        limit: int = 20,
        artist_as_keyword: bool = False,
    ) -> TrackReleasesResponse:
        """Search for ALL releases containing a track.

        Read-through via :func:`fallthrough` with ``pg_write=None`` and the
        negative-cache hooks wired. PG read is skipped when
        ``artist_as_keyword=True`` because the PG cache filters by
        release-level artist which excludes VA compilations where the artist
        is credited on individual tracks — the API keyword search handles
        that correctly with ``format=Compilation``. Negative-cache check runs
        for every query (its key includes ``artist_as_keyword`` so a negative
        answer on one shape doesn't poison the other). Read-only by design
        (no API write-back); see #393.

        Args:
            track: Track title to search for
            artist: Optional artist name for filtering
            limit: Maximum number of results

        Returns:
            TrackReleasesResponse with list of releases
        """

        async def _pg_read() -> TrackReleasesResponse | None:
            assert cache is not None
            cached_releases = await cache.search_releases_by_track(
                track=track, artist=artist, limit=limit
            )
            if not cached_releases:
                return None
            return TrackReleasesResponse(
                track=track,
                artist=artist,
                releases=cached_releases,
                total=len(cached_releases),
                cached=True,
            )

        def _on_negative_hit() -> TrackReleasesResponse:
            return TrackReleasesResponse(
                track=track, artist=artist, releases=[], total=0, cached=True
            )

        async def _api_fetch() -> TrackReleasesResponse | None:
            releases: list[ReleaseInfo] = []
            seen_albums: set = set()
            # LML#755 FIX 1: track whether any live Discogs call failed to
            # return a response (breaker shed → raise, or retry-exhaustion /
            # httpx-error → ``None``). A shed/absent call is "couldn't ask", NOT
            # "we asked, nothing" — so if any leg couldn't ask we return ``None``
            # instead of the empty response, and the seam skips the durable
            # negative-cache write. (The shed *raises*; the ``except`` below
            # already returns ``None``. This flag additionally catches the
            # ``None``-return laundering that predates the breaker.)
            any_call_absent = False

            params: dict = {
                "type": "release",
                "track": track,
                "per_page": limit,
            }
            if artist:
                if artist_as_keyword:
                    # Use q (keyword) instead of artist (field filter) to find
                    # VA compilations where the artist is credited on tracks.
                    # Also filter by format=Compilation to exclude single/album
                    # releases that dominate results for common track names.
                    params["q"] = artist
                    params["format"] = "Compilation"
                else:
                    params["artist"] = artist

            logger.info(f"Searching Discogs for releases with track: '{track}', artist: {artist}")

            try:
                async with timed_api():
                    response = await self._request_with_retry(
                        "GET", "/database/search", params=params
                    )

                if response is not None:
                    get_cache_stats_recorder().record_api_call()
                    response.raise_for_status()
                    data = response.json()

                    for result in data.get("results", []):
                        release_info = self._process_search_result(result, seen_albums)
                        if release_info:
                            releases.append(release_info)
                else:
                    any_call_absent = True

                logger.info(f"Track search found {len(releases)} releases")

                # Supplement with keyword search if few results
                if len(releases) < 3:
                    query_parts = [track]
                    if artist:
                        query_parts.append(artist)

                    query_params: dict = {
                        "type": "release",
                        "q": " ".join(query_parts),
                        "per_page": limit,
                    }

                    logger.info(f"Supplementing with keyword search: '{query_params['q']}'")
                    async with timed_api():
                        response = await self._request_with_retry(
                            "GET", "/database/search", params=query_params
                        )

                    if response is not None:
                        get_cache_stats_recorder().record_api_call()
                        response.raise_for_status()
                        data = response.json()

                        for result in data.get("results", []):
                            release_info = self._process_search_result(result, seen_albums)
                            if release_info:
                                releases.append(release_info)

                        logger.info(f"After keyword search: {len(releases)} total releases")
                    else:
                        any_call_absent = True

                # If any live leg couldn't ask AND we have nothing to show, this
                # is an "unknown", not a confirmed-empty — return ``None`` so the
                # seam skips the negative-cache write. A partial result (some
                # releases found despite one absent leg) is still returned.
                if any_call_absent and not releases:
                    return None

                return TrackReleasesResponse(
                    track=track,
                    artist=artist,
                    releases=releases[:limit],
                    total=len(releases[:limit]),
                    cached=False,
                )

            except DiscogsBreakerOpenError:
                # LML#805: a saturation-breaker shed is expected degrade
                # ("couldn't ask, try later"), NOT a search failure — log it at
                # DEBUG so a sustained OPEN episode doesn't flood Sentry with a
                # per-request error event (the #755 flood: 32.5K events in 4
                # days). The OPEN/CLOSED transitions are logged once each by the
                # breaker, and ``lml.cache.discogs_breaker_open_shed`` carries the
                # volume. Return ``None`` (same as the generic path) so the seam
                # still skips both the write-back and negative-record paths.
                logger.debug(
                    "Discogs saturation breaker shed search_releases_by_track; "
                    "returning None (cache-only)"
                )
                return None
            except Exception as e:
                logger.error(f"Discogs search failed: {e}")
                # An exception-path empty is NOT a "we asked, nothing" verdict
                # — it's "we couldn't ask, try later" (5xx via
                # ``raise_for_status`` lands here). Return ``None`` so the seam
                # skips both the write-back and negative-record paths. The caller
                # wraps ``None`` into an empty ``TrackReleasesResponse``.
                return None

        cache = self.cache_service
        bc = {"track": track, "artist": artist, "artist_as_keyword": artist_as_keyword}

        def _empty_error_response() -> TrackReleasesResponse:
            """The shape returned to callers when the API leg failed."""
            return TrackReleasesResponse(track=track, artist=artist, cached=False)

        if cache is None:
            with request_context("search_releases_by_track"):
                return await _api_fetch() or _empty_error_response()

        # The negative-cache check fires regardless of ``artist_as_keyword``
        # because its key includes that dimension. The PG read is only safe
        # when ``artist_as_keyword`` is False (see method docstring).
        pg_read_hook = None if artist_as_keyword else _pg_read

        result = await fallthrough(
            label="search_releases_by_track",
            pg_read=pg_read_hook,
            api_fetch=_api_fetch,
            # pg_write=None: read-only by design — see method docstring.
            pg_negative_check=lambda: cache.lookup_negative_hit(artist, track, artist_as_keyword),
            pg_negative_record=lambda: cache.record_lookup_negative(
                artist, track, artist_as_keyword
            ),
            on_negative_hit=_on_negative_hit,
            is_empty=lambda r: not r.releases,
            breadcrumb_data=bc,
        )
        # Seam returns None only when ``_api_fetch`` returned None (the
        # exception path). Wrap into the error-shape response so the API
        # consumer always gets a populated model.
        return result or _empty_error_response()

    async def search_releases_by_album_title(
        self,
        album: str,
        limit: int = 10,
    ) -> TrackReleasesResponse:
        """Search Discogs for releases matching ``album`` title alone.

        Used as a fallback when ``search_compilations_for_track``'s parallel
        artist-scoped probes return no usable releases — typically because
        the inbound artist string can't be canonicalized to a single Discogs
        entity (trio collaborations, mid-typed names, etc.). Builds Discogs
        API params::

            {"type": "release", "release_title": album, "per_page": limit}

        Title-only — no ``format`` filter. Earlier revisions of this method
        constrained ``format=Compilation`` on the assumption that the
        motivating cases were all Various-Artists releases, but the trio
        repro from #237 (release 34993109) is *not* classified as a
        compilation in Discogs, so the format filter was excluding the
        target. The orchestrator's downstream pipeline already gates
        candidates by (a) library album presence via ``search_album_fuzzy``
        and (b) per-release ``validate_track_on_release`` (PR #236's fuzzy
        token-set-ratio validator), so over-fetching from this method is
        bounded — only candidates that match the library catalog cost an
        API call to validate.

        API-only — no cache leg. ``cache_service.search_releases_by_title``
        exists but has no analogous gating on the downstream library/library
        match, and would require additional SQL to compose. The fallback
        fires rarely (guarded by a three-condition check in the orchestrator)
        so the single-API-call cost per fire is acceptable.

        See WXYC/library-metadata-lookup#319.
        """
        if not album or not album.strip():
            return TrackReleasesResponse(track="", artist=None, cached=False)

        params: dict[str, Any] = {
            "type": "release",
            "release_title": album,
            "per_page": limit,
        }

        logger.info(f"Searching Discogs releases by album title: '{album}'")

        # Different pressings of the same album are distinct candidates here —
        # the trio repro from #237 has five releases titled "Orcutt Shelley
        # Miller", and Discogs ordering means the canonical target (34993109)
        # isn't the first one returned. Dedupe by release_id (defensive) but
        # NOT by album title — the orchestrator's downstream library + track
        # validation will pick the right one, and de-duping by title here
        # silently drops the target.
        releases: list[ReleaseInfo] = []
        seen_release_ids: set[int] = set()
        # Fresh per-result set means ``_process_search_result``'s album-dedup
        # branch never triggers across iterations.
        try:
            with request_context("search_releases_by_album_title"):
                async with timed_api():
                    response = await self._request_with_retry(
                        "GET", "/database/search", params=params
                    )
            if response is not None:
                get_cache_stats_recorder().record_api_call()
                response.raise_for_status()
                data = response.json()
                for result in data.get("results", []):
                    release_info = self._process_search_result(result, set())
                    if release_info and release_info.release_id not in seen_release_ids:
                        releases.append(release_info)
                        seen_release_ids.add(release_info.release_id)
        except Exception as e:
            logger.warning(f"search_releases_by_album_title failed for '{album}': {e}")

        return TrackReleasesResponse(
            track="",
            artist=None,
            releases=releases[:limit],
            total=len(releases[:limit]),
            cached=False,
        )

    async def search_artists(self, name: str) -> list[DiscogsArtistSearchResult] | None:
        """Search Discogs artists by name — one ``type=artist`` page (LML#759).

        The tier-3 exact-form uniqueness probe for the bare-name artist
        resolver: ``/database/search?type=artist&per_page=100``, single page
        by design. The resolver treats page 1 as the observation universe —
        a name whose exact-form family doesn't fit on one page is ambiguous
        long before page 2 — so no pagination crawl, ever.

        API-only — no cache leg. The discogs-cache is a pair-wise-filtered
        ~50K biased sample, so for bare touring names it can corroborate but
        never decide (the whole point of #759's verify-before-mint model);
        the cache evidence legs live in
        ``cache_service.artist_equality_candidates`` /
        ``artist_trigram_candidates``.

        The return distinction is load-bearing for ``candidate_count``
        (null never means zero):

        - ``list`` (possibly empty): Discogs answered; the list is the
          measured single-page observation. Titles are raw Discogs strings,
          "(N)" disambiguator intact (see ``DiscogsArtistSearchResult``).
        - ``None``: couldn't ask — 429-exhausted, network error, non-2xx,
          or a body containing anything unparseable, including a single
          malformed result item (a partially-understood page could yield
          a false-unique ``candidate_count``, so the whole observation is
          untrusted). Callers must treat ``None`` as *unknown*, never as
          a confirmed-empty verdict — in particular, never coalesce it
          away with ``results or []`` / ``if not results``, which
          silently turns "couldn't ask" into "measured zero".

        Raises:
            ValueError: on blank/whitespace-only ``name`` — a caller error,
                not a measurement. Returning ``[]`` here would fabricate a
                "Discogs answered, zero candidates" observation for a probe
                that was never sent; the resolver short-circuits empty
                identity-match forms before this tier.
            DiscogsBreakerOpenError: propagated from ``_request_with_retry``
                when the LML#755 saturation breaker sheds the call, so the
                resolver can short-circuit the rest of its batch to
                ``escalation_unavailable`` instead of paying one shed per
                remaining name.
        """
        if not name or not name.strip():
            raise ValueError("search_artists requires a non-blank name")

        params: dict[str, Any] = {"type": "artist", "q": name, "per_page": 100}

        add_discogs_breadcrumb("search_artists", {"name": name})
        with request_context("search_artists"):
            async with timed_api():
                response = await self._request_with_retry("GET", "/database/search", params=params)

        if response is None:
            return None
        get_cache_stats_recorder().record_api_call()

        try:
            response.raise_for_status()
            data = response.json()
            raw_items = data.get("results", [])
            if not isinstance(raw_items, list):
                raise TypeError(f"'results' is {type(raw_items).__name__}, not a list")
        except Exception as e:
            # Couldn't obtain a contract-shaped envelope: non-2xx, JSON decode
            # failure, or a body that isn't the results-object shape at all.
            # An intermediary (CDN error page, proxy) can 200 with anything,
            # so this class is "couldn't ask", not evidence of Discogs drift.
            logger.warning(f"search_artists failed for '{name}': {e}")
            return None

        results: list[DiscogsArtistSearchResult] = []
        try:
            for item in raw_items:
                artist_id = item.get("id")
                title = item.get("title")
                if artist_id is None or not title:
                    # Any malformed item distrusts the WHOLE page: silently
                    # skipping it would return a truncated page as a trusted
                    # measurement — dropping a null-id "Popsicle (2)" leaves
                    # candidate_count=1, a false-unique that mints wrong.
                    logger.warning(
                        f"{SEARCH_ARTISTS_DISTRUST_LOG_PREFIX} for '{name}': "
                        f"malformed item {item!r}"
                    )
                    return None
                results.append(DiscogsArtistSearchResult(artist_id=artist_id, title=title))
        except Exception as e:
            # A proper `results` list whose ITEMS defeat parsing (non-dict
            # item, model validation failure on id/title types) is the same
            # distrust semantic as the None-id guard: Discogs answered, the
            # item contract moved. Carries the distrust fingerprint so the
            # live smoke fails red on this drift class instead of skipping.
            logger.warning(
                f"{SEARCH_ARTISTS_DISTRUST_LOG_PREFIX} for '{name}': item parse failed: {e}"
            )
            return None

        return results

    async def search_release_ids_by_artist(self, name: str, *, limit: int = 10) -> list[int] | None:
        """Discover an artist's release IDs — one ``type=release`` page (LML#781).

        The cache-miss fallback primitive for the bulk artist-genres endpoint:
        ``/database/search?type=release&artist=<name>``, a single page. The
        caller fans out ``get_release`` over the returned IDs (bounded by
        ``limit``) to sample the artist's genre/style distribution while
        persisting each release to the discogs-cache.

        API-only — a cache-miss artist has no ``release_artist`` rows to read,
        so this is the only way to locate their releases. Homonym caveat: the
        Discogs search matches by artist *name*, not the (unknown-to-search)
        Discogs artist ID, so callers with a ``discogs_artist_id`` should rely
        on the cache path for homonym safety; this fallback is best-effort.

        The ``list``-vs-``None`` distinction mirrors ``search_artists``:

        - ``list`` (possibly empty): Discogs answered. ``[]`` means "asked, this
          artist has no releases" — a confirmed-empty measurement.
        - ``None``: couldn't ask — 429-exhausted, network error, non-2xx, or an
          unparseable body. Callers must treat it as *unknown*, never as a
          confirmed-empty verdict.

        Returns release IDs in Discogs' search-relevance order, deduplicated,
        capped at ``limit``.

        Raises:
            ValueError: on a blank/whitespace-only ``name`` — a caller error,
                not a measurement (the endpoint short-circuits blank names
                before reaching this tier).
            DiscogsBreakerOpenError: propagated from ``_request_with_retry`` when
                the LML#755 saturation breaker sheds the call, so the caller can
                mark the artist ``unavailable`` rather than pay one shed per
                remaining release.
        """
        if not name or not name.strip():
            raise ValueError("search_release_ids_by_artist requires a non-blank name")

        params: dict[str, Any] = {"type": "release", "artist": name, "per_page": limit}

        add_discogs_breadcrumb("search_release_ids_by_artist", {"name": name})
        with request_context("search_release_ids_by_artist"):
            async with timed_api():
                response = await self._request_with_retry("GET", "/database/search", params=params)

        if response is None:
            return None
        get_cache_stats_recorder().record_api_call()

        try:
            response.raise_for_status()
            data = response.json()
            raw_items = data.get("results", [])
            if not isinstance(raw_items, list):
                raise TypeError(f"'results' is {type(raw_items).__name__}, not a list")
        except Exception as e:
            # Couldn't obtain a contract-shaped envelope: non-2xx, JSON decode
            # failure, or a body that isn't the results-object shape. "Couldn't
            # ask", not a confirmed-empty measurement.
            logger.warning(f"search_release_ids_by_artist failed for '{name}': {e}")
            return None

        release_ids: list[int] = []
        seen: set[int] = set()
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            release_id = item.get("id")
            if isinstance(release_id, int) and release_id > 0 and release_id not in seen:
                release_ids.append(release_id)
                seen.add(release_id)
        return release_ids[:limit]

    def _process_search_result(self, result: dict, seen_albums: set) -> ReleaseInfo | None:
        """Process a single search result into a ReleaseInfo.

        Args:
            result: Raw Discogs API result
            seen_albums: Set of already-seen album titles (for deduplication)

        Returns:
            ReleaseInfo if valid, None if should be skipped
        """
        title = result.get("title", "")
        result_artist, album = self._parse_title(title)

        if not album:
            return None

        album_key = album.lower()
        if album_key in seen_albums:
            return None

        seen_albums.add(album_key)

        release_id = result.get("id")
        if release_id is None:
            return None

        is_compilation = is_compilation_artist(result_artist)

        return ReleaseInfo(
            album=album,
            artist=result_artist,
            release_id=release_id,
            release_url=f"https://www.discogs.com/release/{release_id}",
            is_compilation=is_compilation,
        )

    @async_cached(RELEASE_CACHE)
    async def get_release(
        self, release_id: int, *, lean: bool = False
    ) -> ReleaseMetadataResponse | None:
        """Get full release metadata by ID.

        Read-through via :func:`fallthrough`: in-memory (decorator) → PG → API
        + write-back. Full read-through — ``ReleaseMetadataResponse`` maps
        cleanly to the cache's normalized schema. See #393 for the per-method
        write-back policy table.

        Args:
            release_id: Discogs release ID
            lean: LML#894 (lever L4a) / LML#895 (lever L4c). When ``True``, route
                the PG read through the SEPARATE lean cache method
                (:meth:`DiscogsCacheService.get_release_lean`), which collapses
                the per-child hydration into ``json_agg`` reads (≤ 2 PG
                round-trips) and never surfaces the ``release_video`` child
                ``/lookup`` never uses. The shared cached read (``lean=False``,
                the default) is untouched and keeps serving ``/discogs/*``. The
                ``@async_cached`` key includes ``lean``, so lean and full
                results occupy disjoint L1 entries — a lean read can never be
                served to (or poison) the ``/discogs/*`` full-object surface.

        Returns:
            ReleaseMetadataResponse with full metadata, or None on error
        """
        # LML#546 observability backstop. The LML#518 decision record puts
        # ``id <= 0`` validation at the caller, not at this boundary, to avoid
        # masking LML-internal bugs in other call paths. We honor that — this
        # log does NOT raise and does NOT change behavior. It exists so that
        # if a future caller forgets the guard (e.g. LML#525's multiplexer),
        # the regression is visible in Sentry breadcrumbs instead of
        # silently tombstoning the row (LML#510). ``stack_info=True`` captures
        # the caller chain so triage can identify the unguarded site without
        # re-running the request.
        if release_id <= 0:
            logger.warning(
                "DiscogsService.get_release received non-positive id=%d — "
                "caller must validate (see LML#518). Proceeding per the "
                "'callers validate' policy; the 404 from Discogs will "
                "tombstone this id permanently. See LML#546.",
                release_id,
                stack_info=True,
            )

        async def _api_fetch() -> ReleaseMetadataResponse | None:
            try:
                async with timed_api():
                    response = await self._request_with_retry("GET", f"/releases/{release_id}")

                if response is None:
                    logger.warning(f"Failed to fetch release {release_id} (rate limited or error)")
                    return None

                get_cache_stats_recorder().record_api_call()

                # LML#510: discriminate 404 from other failures before
                # raise_for_status. A 404 means "Discogs confirms no such
                # release id" — turn it into a tombstone-shaped row so the
                # fallthrough seam's pg_write lands it and subsequent calls
                # short-circuit on the cache instead of re-hitting the API.
                if response.status_code == 404:
                    get_cache_stats_recorder().record("release_404_tombstone_written")
                    return ReleaseMetadataResponse(
                        release_id=release_id,
                        title="",
                        artist="",
                        release_url=f"https://www.discogs.com/release/{release_id}",
                        not_found=True,
                    )

                # LML#510: split out 5xx so Discogs-outage signal stays
                # legible separately from tombstone activity. Counted at
                # the discrimination point so we measure raw incidence
                # (not the post-retry view).
                if 500 <= response.status_code < 600:
                    get_cache_stats_recorder().record("release_5xx_passthrough")

                response.raise_for_status()
                data = response.json()

                # Extract all artists
                raw_artists = data.get("artists", [])
                artist_credits = [
                    ArtistCredit(
                        artist_id=a.get("id"),
                        name=a.get("name", ""),
                        join=a.get("join", ""),
                    )
                    for a in raw_artists
                ]
                artist_name = artist_credits[0].name if artist_credits else ""
                artist_id = artist_credits[0].artist_id if artist_credits else None

                # Extract extra artists (credits)
                raw_extras = data.get("extraartists", [])
                extra_artist_credits = [
                    ArtistCredit(
                        artist_id=a.get("id"),
                        name=a.get("name", ""),
                        role=a.get("role"),
                    )
                    for a in raw_extras
                ]

                # Extract all labels
                raw_labels = data.get("labels", [])
                label_credits = [
                    LabelCredit(
                        label_id=lbl.get("id"),
                        name=lbl.get("name", ""),
                        catno=lbl.get("catno"),
                    )
                    for lbl in raw_labels
                ]
                label_name = label_credits[0].name if label_credits else None
                label_id = label_credits[0].label_id if label_credits else None

                # Extract full release date
                released = data.get("released")

                # Extract tracklist with per-track artists (for compilations)
                tracklist = [
                    TrackItem(
                        position=t.get("position", ""),
                        title=t.get("title", ""),
                        duration=t.get("duration"),
                        artists=[a.get("name", "") for a in t.get("artists", [])],
                    )
                    for t in data.get("tracklist", [])
                ]

                # Extract artwork
                images = data.get("images", [])
                artwork_url = images[0].get("uri") if images else None

                # Extract videos (skip entries without a URI)
                videos = [
                    ReleaseVideo(
                        src=v["uri"],
                        title=v.get("title"),
                        duration=v.get("duration"),
                        embed=v.get("embed", True),
                    )
                    for v in data.get("videos", [])
                    if v.get("uri")
                ]

                # LML#688: surface the release's Discogs master_id so a caller
                # (Backend catalog-popularity) can collapse multiple pressings/
                # formats of one logical album by the master. Discogs returns the
                # integer ``master_id: 0`` (a sentinel, not an absent key) for a
                # release with no master — the same no-master value the canonical
                # discogs_client treats as falsy and that this repo already guards
                # as ``master_id <= 0`` in ``markup_parser``. Normalize any
                # non-positive / non-int to ``None`` so the wire contract
                # (``null`` for no master) holds AND a cold-API write-back never
                # persists ``master_id = 0`` into the PG cache (cache_service
                # write_release warms straight from this value).
                raw_master_id = data.get("master_id")
                master_id = (
                    raw_master_id if isinstance(raw_master_id, int) and raw_master_id > 0 else None
                )

                return ReleaseMetadataResponse(
                    release_id=release_id,
                    master_id=master_id,
                    title=data.get("title", ""),
                    artist=artist_name,
                    year=data.get("year"),
                    label=label_name,
                    artist_id=artist_id,
                    label_id=label_id,
                    genres=data.get("genres", []),
                    styles=data.get("styles", []),
                    tracklist=tracklist,
                    artwork_url=artwork_url,
                    release_url=f"https://www.discogs.com/release/{release_id}",
                    cached=False,
                    artists=artist_credits,
                    extra_artists=extra_artist_credits,
                    labels=label_credits,
                    released=released,
                    videos=videos,
                )

            except DiscogsBreakerOpenError:
                # LML#755 FIX 1: a breaker shed is "couldn't ask" — propagate it
                # so ``validate_track_on_release`` doesn't turn a missing release
                # into a definitive ``False`` (dropping a valid candidate). A
                # genuine 404/None or network/5xx error still degrades to ``None``
                # below.
                raise
            except Exception as e:
                logger.error(f"Failed to fetch release {release_id}: {e}")
                return None

        cache = self.cache_service
        value: ReleaseMetadataResponse | None
        if cache is None:
            with request_context("get_release"):
                value = await _api_fetch()
        else:
            value = await fallthrough(
                label="get_release",
                pg_read=lambda: (cache.get_release_lean if lean else cache.get_release)(release_id),
                api_fetch=_api_fetch,
                # LML#510: mypy widens fallthrough's generic `T` to
                # `T | None` when the result is assigned to a variable
                # instead of directly returned, so `cache.write_release`
                # (which takes `ReleaseMetadataResponse`, not the wider
                # `ReleaseMetadataResponse | None`) trips arg-type
                # validation. Pre-510, the call was `return await
                # fallthrough(...)` and the return-type annotation
                # bidirectionally constrained `T`. The runtime contract is
                # unchanged.
                pg_write=cache.write_release,  # type: ignore[arg-type]
                # LML#542 widened predicate. The cache row is a HIT when any
                # one of three signals says "we already have something useful
                # for this id":
                #
                #   1. `not_found` — LML#510 tombstone; the boundary
                #      translation below converts it to None for the caller.
                #      Checked explicitly so a tombstone with NULL
                #      `artwork_checked_at` (not produced by today's write
                #      path, but cheap defense) still short-circuits.
                #   2. `tracklist` non-empty — the bulk loader and the live
                #      write_release path both stamp `release_track` rows
                #      whenever the release tree is populated. If we have
                #      tracks, the release exists and is hydrated; artwork
                #      can be back-filled out of band without paying a
                #      Discogs round-trip on the hot path.
                #   3. `artwork_checked_at IS NOT NULL` — LML has hit the
                #      live API for this release at least once, regardless
                #      of whether Discogs returned a cover. Keeps the
                #      "asked, no cover" tail from re-burning the
                #      rate-limit budget (the #423 invariant).
                #
                # The artwork-columns-only predicate from #423 was diagnosed
                # in #537's `cache_miss_provenance` probe as causing ~20%
                # of `get_release` calls to fall through to the API even
                # though the full release tree (release + release_artist +
                # release_track + release_track_artist) was already in PG —
                # only the artwork columns were still NULL. Widening here
                # decouples artwork availability from cache-hit eligibility;
                # `extended=true` consumers still surface artwork when the
                # row has it, but we no longer FETCH purely to populate it.
                is_pg_hit=lambda v: (
                    v is not None
                    and (bool(v.not_found) or bool(v.tracklist) or v.artwork_checked_at is not None)
                ),
                breadcrumb_data={"release_id": release_id},
            )
        # LML#510 boundary: tombstone → None. Counter keeps the
        # `pg_cache_hit - tombstone_returned` dashboard math interpretable.
        if value is not None and value.not_found:
            get_cache_stats_recorder().record("tombstone_returned")
            return None
        return value

    @async_cached(ARTIST_CACHE)
    async def get_artist_details(
        self, artist_id: int, *, lean: bool = False
    ) -> ArtistDetails | None:
        """Fetch full artist details from Discogs.

        Read-through via :func:`fallthrough`: in-memory (decorator) → PG → API
        + write-back. Full read-through — ``ArtistDetails`` maps cleanly to
        the cache's normalized schema. See #393 for the per-method write-back
        policy table.

        Args:
            artist_id: Discogs artist ID
            lean: LML#894 (lever L4a) / LML#895 (lever L4c). When ``True``, route
                the PG read through the SEPARATE lean cache method
                (:meth:`DiscogsCacheService.get_artist_details_lean`), which folds
                ``artist_url`` into the parent row via ``json_agg`` (a single PG
                round-trip; ``profile`` + ``urls`` are still read) and never reads
                the ``artist_alias`` / ``artist_name_variation`` /
                ``artist_member`` children ``/lookup`` never uses. The shared
                cached read (``lean=False``, the default) is untouched and keeps
                serving ``/discogs/*``; the ``@async_cached`` key includes
                ``lean`` so lean and full results occupy disjoint L1 entries.

        Returns:
            ArtistDetails with full metadata, or None on error
        """
        # LML#546 observability backstop. See get_release above for the full
        # rationale. Log-only, does NOT raise, does NOT change behavior.
        if artist_id <= 0:
            logger.warning(
                "DiscogsService.get_artist_details received non-positive id=%d — "
                "caller must validate (see LML#518). Proceeding per the "
                "'callers validate' policy; the 404 from Discogs will "
                "tombstone this id permanently. See LML#546.",
                artist_id,
                stack_info=True,
            )

        async def _api_fetch() -> ArtistDetails | None:
            try:
                async with timed_api():
                    response = await self._request_with_retry("GET", f"/artists/{artist_id}")
                if response is None:
                    return None
                get_cache_stats_recorder().record_api_call()
                add_discogs_breadcrumb("get_artist_details", {"artist_id": artist_id})

                # LML#510: discriminate 404 from other failures. The
                # tombstone-shaped row carries `name = ""` as the identifier
                # sentinel; the fallthrough seam's pg_write stamps
                # `fetched_at = now()` via cache_service so subsequent reads
                # short-circuit on `is_pg_hit` (fetched_at != None).
                if response.status_code == 404:
                    get_cache_stats_recorder().record("artist_404_tombstone_written")
                    return ArtistDetails(
                        artist_id=artist_id,
                        name="",
                        not_found=True,
                    )

                response.raise_for_status()
                data = response.json()

                images = data.get("images", [])
                image_url = images[0].get("uri") if images else None

                return ArtistDetails(
                    artist_id=artist_id,
                    name=data.get("name", ""),
                    profile=data.get("profile") or None,
                    image_url=image_url,
                    name_variations=data.get("namevariations", []),
                    aliases=[
                        ArtistRef(id=a["id"], name=a["name"])
                        for a in data.get("aliases", [])
                        if "id" in a and "name" in a
                    ],
                    members=[
                        MemberRef(
                            id=m["id"],
                            name=m["name"],
                            active=m.get("active", True),
                        )
                        for m in data.get("members", [])
                        if "id" in m and "name" in m
                    ],
                    urls=data.get("urls", []),
                    cached=False,
                )

            except DiscogsBreakerOpenError:
                # LML#805: a saturation-breaker shed is expected degrade, not an
                # artist-fetch failure — DEBUG, not ERROR, so a sustained OPEN
                # episode doesn't flood Sentry (#755). Return ``None`` (same as
                # the generic path). The LML#510 ERROR path below is reserved for
                # genuine fetch failures / artist 404s, which stay alertable.
                logger.debug(
                    "Discogs saturation breaker shed get_artist_details for %s; returning None",
                    artist_id,
                )
                return None
            except Exception as e:
                # LML#510: promoted from warning → error so Sentry's default
                # `error+` filter captures artist 404s post-deploy. The
                # tombstone counter above counts the write side; this log
                # is the Sentry corroboration surface.
                logger.error(f"Failed to fetch artist details for {artist_id}: {e}")
                return None

        cache = self.cache_service
        value: ArtistDetails | None
        if cache is None:
            with request_context("get_artist_details"):
                value = await _api_fetch()
        else:
            value = await fallthrough(
                label="get_artist_details",
                pg_read=lambda: (
                    cache.get_artist_details_lean if lean else cache.get_artist_details
                )(artist_id),
                api_fetch=_api_fetch,
                # LML#510: see the note on `get_release` above.
                pg_write=cache.write_artist_details,  # type: ignore[arg-type]
                # `fetched_at IS NULL` marks a stub row created by the monthly
                # rebuild's stub-from-`release_artist` path — `(id, name)` only,
                # no profile / aliases / members / urls / variations. Falling
                # through to the API + write-back is the back-fill path. Once
                # `fetched_at` is set, both "asked, got a profile" and "asked,
                # no profile" are full hits — we don't re-ask Discogs about the
                # no-profile tail. Same shape of fix as the `get_release`
                # artwork discriminator above. See WXYC#502 (and #497 for the
                # rebuild-path fix that creates these stubs in the first place).
                #
                # LML#510 tombstones land here with `fetched_at = now()` so
                # they hit-fall-through correctly; the boundary translation
                # below converts `not_found=True` back to None for callers.
                is_pg_hit=lambda v: v is not None and v.fetched_at is not None,
                breadcrumb_data={"artist_id": artist_id},
            )
        if value is not None and value.not_found:
            get_cache_stats_recorder().record("tombstone_returned")
            return None
        return value

    async def get_artist_image(self, artist_id: int) -> str | None:
        """Fetch primary image for a Discogs artist.

        Delegates to get_artist_details which handles caching.

        Args:
            artist_id: Discogs artist ID

        Returns:
            Image URI string, or None if unavailable
        """
        details = await self.get_artist_details(artist_id)
        return details.image_url if details else None

    @async_cached(LABEL_CACHE)
    async def get_label_image(self, label_id: int) -> str | None:
        """Fetch primary image for a Discogs label.

        Args:
            label_id: Discogs label ID

        Returns:
            Image URI string, or None if unavailable
        """
        try:
            with request_context("get_label_image"):
                async with timed_api():
                    response = await self._request_with_retry("GET", f"/labels/{label_id}")
            if response is None:
                return None
            get_cache_stats_recorder().record_api_call()
            add_discogs_breadcrumb("get_label_image", {"label_id": label_id})
            response.raise_for_status()
            data = response.json()
            images = data.get("images", [])
            return images[0].get("uri") if images else None
        except Exception as e:
            logger.warning(f"Failed to fetch label image for {label_id}: {e}")
            return None

    @async_cached(MASTER_CACHE)
    async def get_master(self, master_id: int) -> MasterRelease | None:
        """Fetch master release metadata from Discogs.

        Args:
            master_id: Discogs master release ID

        Returns:
            MasterRelease with title and year, or None on error
        """
        try:
            with request_context("get_master"):
                async with timed_api():
                    response = await self._request_with_retry("GET", f"/masters/{master_id}")
            if response is None:
                return None
            get_cache_stats_recorder().record_api_call()
            add_discogs_breadcrumb("get_master", {"master_id": master_id})
            response.raise_for_status()
            data = response.json()

            # ``main_release`` is Discogs' canonical release for the master.
            # Coerce to a positive int; absent or the ``0`` sentinel -> None so
            # the LML#858 API-tail drain skips masters with no release to pin
            # instead of leaking release 0 into ``get_release`` (LML#510/#546).
            raw_main = data.get("main_release")
            main_release_id = raw_main if isinstance(raw_main, int) and raw_main > 0 else None

            return MasterRelease(
                master_id=master_id,
                title=data.get("title", ""),
                year=data.get("year"),
                main_release_id=main_release_id,
                cached=False,
            )
        except Exception as e:
            logger.warning(f"Failed to fetch master release {master_id}: {e}")
            return None

    @async_cached(SEARCH_CACHE)
    async def search(
        self, request: DiscogsSearchRequest, limit: int = 5, skip_pg: bool = False
    ) -> DiscogsSearchResponse | None:
        """General release search for artwork discovery.

        Read-through via :func:`fallthrough` with ``pg_write=None`` — the PG
        cache's ``search_releases`` query is indexed against ETL-populated
        ``release_artist`` / ``release_track`` data. Writing arbitrary Discogs
        ``/database/search`` API hits back wouldn't fit that schema (they'd
        be denormalized release stubs without the indexed columns the read
        query depends on). Read-only by design; see #393.

        Args:
            request: Search parameters (artist, album, track)
            limit: Maximum number of results to return
            skip_pg: Bypass the PG cache leg and go straight to the API.
                The library-miss probe sets this on its floor-reject retry
                (LML#784): a non-empty ``pg_read`` is terminal at the seam,
                so a cache candidate set that all floor-fails must be
                re-searched API-only or the working API arm stays masked.

        Returns:
            DiscogsSearchResponse with ranked results (possibly empty on a
            genuine 200-with-no-results negative), or None when the Discogs
            call *degraded* — rate-limited / retries exhausted, saturation
            breaker shed, or a 5xx. None is deliberately propagated so
            ``@async_cached`` does not memoize a transient failure as a
            1-hour no-match; the next call retries (LML#918).
        """
        params = self._build_search_params(request, limit=limit)
        if not params:
            logger.warning("No searchable fields in request")
            return DiscogsSearchResponse(cached=False)

        async def _pg_read() -> DiscogsSearchResponse | None:
            assert cache is not None  # narrowed by the caller before invocation
            cached = await cache.search_releases(
                artist=request.artist,
                album=request.album or request.track,
                limit=limit,
            )
            if not cached:
                return None
            results = []
            for row in cached:
                confidence = calculate_confidence(
                    request.artist,
                    request.album,
                    row["artist_name"],
                    row["title"],
                    request_label=request.label,
                    result_label=row.get("label_name"),
                    request_format=request.format,
                    result_format=None,  # cache doesn't include format yet
                )
                results.append(
                    DiscogsSearchResult(
                        album=row["title"],
                        artist=row["artist_name"],
                        artist_credits=row.get("artist_credits") or None,
                        release_id=row["release_id"],
                        release_url=f"https://www.discogs.com/release/{row['release_id']}",
                        artwork_url=row.get("artwork_url"),
                        confidence=confidence,
                    )
                )
            results.sort(key=lambda r: r.confidence, reverse=True)
            return DiscogsSearchResponse(
                results=results, total=len(results), cached=True, pg_served=True
            )

        async def _api_fetch() -> DiscogsSearchResponse | None:
            logger.info(f"Searching Discogs with params: {params}")
            try:
                async with timed_api():
                    response = await self._request_with_retry(
                        "GET", "/database/search", params=params
                    )

                if response is None:
                    # Degraded (rate-limited / retries exhausted). Return None so
                    # @async_cached does NOT memoize this transient failure as a
                    # 1-hour no-match (LML#918). A genuine 200-with-no-results
                    # below stays a real, cacheable (empty) response.
                    logger.warning("Discogs search failed (rate limited or error)")
                    return None

                get_cache_stats_recorder().record_api_call()
                response.raise_for_status()
                data = response.json()

                # If strict search returned nothing, try fuzzy query
                if not data.get("results") and (request.artist or request.album):
                    query_parts = []
                    if request.artist:
                        query_parts.append(request.artist)
                    if request.album:
                        query_parts.append(request.album)
                    fallback_params: dict[str, Any] = {
                        "type": "release",
                        "per_page": limit,
                        "q": " ".join(query_parts),
                    }
                    logger.info(f"Strict search empty, trying fuzzy query: {fallback_params}")
                    async with timed_api():
                        response = await self._request_with_retry(
                            "GET", "/database/search", params=fallback_params
                        )
                    if response is None:
                        # Degraded fuzzy leg (rate-limited / retries exhausted).
                        # The strict leg returned 200-empty, but the fuzzy ``q=``
                        # query is the one that resolves non-library releases, so
                        # a transient failure here must NOT be cached as a
                        # no-match — return None like the strict-leg degrade
                        # above so @async_cached skips it (LML#918). Otherwise
                        # the strict empty would be memoized for an hour on
                        # exactly the non-library path this fix targets.
                        logger.warning("Discogs fuzzy search failed (rate limited or error)")
                        return None
                    get_cache_stats_recorder().record_api_call()
                    response.raise_for_status()
                    data = response.json()

                results = []
                for item in data.get("results", []):
                    cover_url = item.get("thumb")
                    if not cover_url or "spacer.gif" in cover_url:
                        cover_url = None

                    title = item.get("title", "")
                    result_artist, album = self._parse_title(title)

                    # Extract label and format from Discogs search result
                    result_labels = item.get("label", [])
                    result_label = result_labels[0] if result_labels else None
                    result_formats = item.get("format", [])
                    result_format = result_formats[0] if result_formats else None

                    confidence = calculate_confidence(
                        request.artist,
                        request.album,
                        result_artist,
                        album,
                        request_label=request.label,
                        result_label=result_label,
                        request_format=request.format,
                        result_format=result_format,
                    )

                    release_id = item.get("id")
                    release_url = f"https://www.discogs.com/release/{release_id}"

                    results.append(
                        DiscogsSearchResult(
                            album=album,
                            artist=result_artist,
                            release_id=release_id,
                            release_url=release_url,
                            artwork_url=cover_url,
                            confidence=confidence,
                        )
                    )

                results.sort(key=lambda r: r.confidence, reverse=True)

                return DiscogsSearchResponse(
                    results=results,
                    total=len(results),
                    cached=False,
                )

            except DiscogsBreakerOpenError:
                # LML#805: a saturation-breaker shed is expected degrade, not a
                # search failure — DEBUG, not ERROR, so a sustained OPEN episode
                # doesn't flood Sentry with per-request error events (#755). The
                # breaker logs the OPEN/CLOSED transitions; the shed counter
                # carries the volume. Return None like the generic degraded
                # path so the shed isn't memoized as a no-match (LML#918).
                logger.debug("Discogs saturation breaker shed search; returning None (no-poison)")
                return None
            except Exception as e:
                logger.error(f"Discogs search failed: {e}")
                return None

        cache = self.cache_service
        if cache is None or skip_pg:
            # ``skip`` is the seam's existing cache-bypass state (LML#537
            # taxonomy), so the #784 floor-reject retry is distinguishable
            # from ``no_pg`` in the wait-time histograms.
            with request_context("search", "skip" if skip_pg else "no_pg"):
                result = await _api_fetch()
        else:
            result = await fallthrough(
                label="search",
                pg_read=_pg_read,
                api_fetch=_api_fetch,
                # pg_write=None: read-only by design — see method docstring.
                breadcrumb_data={"artist": request.artist, "album": request.album},
            )
        # ``result`` is None when the API leg degraded (rate-limited / breaker
        # shed / 5xx): LML#918 propagates that None so @async_cached skips
        # memoizing the transient failure. A PG hit or a genuine (possibly
        # empty) API parse is a real response and is cached as before.
        return result

    def _build_search_params(self, request: DiscogsSearchRequest, limit: int = 5) -> dict:
        """Build search params using Discogs-specific fields.

        Args:
            request: Search request with artist/album/track
            limit: Maximum number of results to return

        Returns:
            Dict of search parameters, or empty dict if no searchable fields
        """
        params: dict = {
            "type": "release",
            "per_page": limit,
        }

        if request.artist:
            params["artist"] = request.artist
        if request.album:
            params["release_title"] = request.album
        elif request.track:
            params["release_title"] = request.track

        if request.label:
            label = request.label.strip()
            if label and label.lower() != "null":
                params["label"] = request.label
        if request.format:
            params["format"] = request.format

        if "artist" not in params and "release_title" not in params:
            return {}

        return params

    @async_cached(VALIDATION_CACHE)
    async def validate_track_on_release(self, release_id: int, track: str, artist: str) -> bool:
        """Validate that a track by an artist exists on a release.

        Read-through via :func:`fallthrough` with ``pg_write=None`` — the
        validation verdict itself isn't separately cached on the API path.
        The API fallback goes through ``get_release``, which writes back to
        the ``release`` cache via its own seam call, so the release rows do
        get persisted; only the validation answer is re-derived per call.
        Read-only by design; see #393.

        Args:
            release_id: Discogs release ID
            track: Track title to find
            artist: Artist name to find

        Returns:
            True if the track by the artist is found on the release
        """

        async def _api_fetch() -> bool | None:
            # Fall back to API via get_release. Returns False (not None) when
            # the release is missing or the track isn't on it, because the
            # method's contract is ``-> bool``. The seam treats False as a
            # valid result (it's not None), so it'll be returned to the
            # caller.
            release = await self.get_release(release_id)
            if release is None:
                return False

            track_lower = normalize_for_track_comparison(track)
            artist_lower = normalize_artist_for_validation(artist)
            return _scan_tracklist_for_match(
                release, release_id, track, track_lower, artist, artist_lower
            )

        cache = self.cache_service
        if cache is not None:
            validated = await fallthrough(
                label="validate_track_on_release",
                pg_read=lambda: cache.validate_track_on_release(release_id, track, artist),
                api_fetch=_api_fetch,
                # pg_write=None: read-only by design — see method docstring.
                breadcrumb_data={
                    "release_id": release_id,
                    "track": track,
                    "artist": artist,
                },
            )
        else:
            validated = await _api_fetch()
        # The API path always returns bool (never None), so the seam's
        # ``T | None`` here is always bool in practice.
        return bool(validated)

    async def get_track_credit_on_release(self, release_id: int, track: str) -> str | None:
        """Recover the per-track credit for ``track`` on a release (LML#660).

        Where :meth:`validate_track_on_release` answers "is *this* artist on the
        track?", this answers "*which* artist is credited for this track?" — the
        SONG_AS_TRACK carry-through has no typed artist to validate against, so it
        recovers the track's actual credited performer from the release tracklist
        to anchor the row-less resolve (and the #632 cache key) on a real artist
        instead of the release-level "Various" marker. This supersedes the LML#649
        stopgap that suppressed the carry-through under that "Various" anchor.

        Read-only: rides ``get_release`` (its own read-through cache) and never
        writes. Returns the joined per-track credit, or ``None`` when the release
        can't be fetched, no track title matches, or the matched track carries
        only a release-level credit (empty ``artists``).

        Args:
            release_id: Discogs release ID to fetch.
            track: Track title to locate in the tracklist.

        Returns:
            The matched track's per-track credit (``artists`` joined), or ``None``.
        """
        release = await self.get_release(release_id)
        if release is None:
            return None
        track_lower = normalize_for_track_comparison(track)
        return _scan_tracklist_for_credit(release, track_lower)


def _iter_title_matched_items(
    release: ReleaseMetadataResponse, track_lower: str
) -> Iterator[TrackItem]:
    """Yield tracklist items whose normalized title matches ``track_lower``.

    The shared title-match step behind both tracklist scans —
    :func:`_scan_tracklist_for_match` (validation) and
    :func:`_scan_tracklist_for_credit` (credit recovery, LML#660): a bidirectional
    substring on the normalized title, then a ``token_set_ratio`` fuzzy fallback
    (LML#334) so typographic noise that leaves token content intact — singular/
    plural, a dropped interior word, dash-vs-paren suffixes — still matches.
    Centralized so the two scans' title rule can't drift apart. ``track_lower`` is
    pre-normalized by the caller.
    """
    for item in release.tracklist or []:
        item_title = normalize_for_track_comparison(item.title)
        if track_lower in item_title or item_title in track_lower:
            yield item
        elif (
            item_title
            and fuzz.token_set_ratio(track_lower, item_title) >= _TRACK_TITLE_FUZZY_MATCH_THRESHOLD
        ):
            yield item


def _scan_tracklist_for_match(
    release: ReleaseMetadataResponse,
    release_id: int,
    track: str,
    track_lower: str,
    artist: str,
    artist_lower: str,
) -> bool:
    """Tracklist match logic extracted from ``validate_track_on_release``.

    Kept as a module-level helper so ``validate_track_on_release``'s body
    after the seam refactor reads as orchestration, not algorithm. Inputs
    are pre-normalized by the caller (single call to
    ``normalize_for_track_comparison`` / ``normalize_artist_for_validation``)
    so the inner loop is hot-path arithmetic.
    """
    for item in _iter_title_matched_items(release, track_lower):
        # Per-track credits first (compilations, or tracks crediting the
        # writers/performers). A positive hit short-circuits.
        if item.artists:
            for track_artist in item.artists:
                track_artist_lower = normalize_artist_for_validation(track_artist)
                if artist_lower in track_artist_lower or track_artist_lower in artist_lower:
                    logger.info(f"Validated: '{track}' by '{artist}' found on release {release_id}")
                    return True
            # Fuzzy fallback: when no individual per-track artist substring-matches,
            # compare against the joined credit string. Catches collaboration trios
            # where each member is credited separately but the request uses the
            # compact group name (LML#210).
            joined = normalize_artist_for_validation(" ".join(item.artists))
            if (
                joined
                and fuzz.token_set_ratio(artist_lower, joined) >= _ARTIST_FUZZY_MATCH_THRESHOLD
            ):
                logger.info(f"Validated (fuzzy): '{track}' by '{artist}' on release {release_id}")
                return True

        # Release-level artist. Always consulted when per-track credits
        # haven't already matched — per-track credits often list band
        # members / producers / writers rather than the band itself (e.g.,
        # The Orb's "Towers Of Dub" on Live 93 is credited to Paterson /
        # Weston / Fehlmann), so the release-level credit is the last
        # word on "is this track by this artist."
        release_artist = normalize_artist_for_validation(release.artist)
        if release_artist and (artist_lower in release_artist or release_artist in artist_lower):
            logger.info(f"Validated: '{track}' by '{artist}' found on release {release_id}")
            return True
        if (
            release_artist
            and fuzz.token_set_ratio(artist_lower, release_artist) >= _ARTIST_FUZZY_MATCH_THRESHOLD
        ):
            logger.info(f"Validated (fuzzy): '{track}' by '{artist}' on release {release_id}")
            return True

    logger.info(f"Track '{track}' by '{artist}' NOT found on release {release_id}")
    return False


def _scan_tracklist_for_credit(release: ReleaseMetadataResponse, track_lower: str) -> str | None:
    """Return the per-track credit for the title-matched track, or ``None`` (LML#660).

    The credit-recovery counterpart to :func:`_scan_tracklist_for_match`: same
    bidirectional-substring title rule, but artist-blind — it surfaces *which*
    artist the track credits rather than checking a supplied one. The first
    title-matched track that carries a per-track ``artists`` list wins; its
    credit is joined with a space, matching the joined form
    ``_scan_tracklist_for_match``'s LML#210 fuzzy fallback validates against, so
    the recovered anchor re-validates consistently on the resolve path. A
    title-matched track with no per-track ``artists`` (release-level-only credit)
    is skipped; when none carries one, ``None`` is returned and the caller falls
    back to LML#649 suppression rather than anchor on the release-level "Various".

    ``track_lower`` is pre-normalized by the caller (single
    ``normalize_for_track_comparison`` call), mirroring ``_scan_tracklist_for_match``.
    """
    for item in _iter_title_matched_items(release, track_lower):
        if item.artists:
            joined = " ".join(a.strip() for a in item.artists if a and a.strip())
            if joined:
                return joined
    return None


def find_track_position(release: ReleaseMetadataResponse, track: str) -> str | None:
    """Return the display ``position`` of the first title-matched track (LML#699).

    The position-recovery sibling of :func:`_scan_tracklist_for_credit`: artist-
    blind, it surfaces *where* the played track sits in the tracklist (its
    ``position`` — e.g. ``"A1"`` / ``"5"``) so the BMI writer-credit enrichment
    can scope per-track composer credits to the resolved playcut. Reuses the
    shared :func:`_iter_title_matched_items` title rule, so the position resolves
    identically to the validation / credit scans. The first title-matched track
    *with a non-empty position* wins (the playcut); a title-matched track whose
    position is empty is skipped, so a later same-titled track that carries a
    position is still found rather than forfeited (mirroring how
    :func:`_scan_tracklist_for_credit` skips items lacking the wanted field).
    Returns ``None`` when ``track`` normalizes empty (an empty needle would
    substring-match every track), no title matches, or no matched track carries
    a position — the caller then falls back to release-level credits. ``track``
    is raw; it is normalized here.
    """
    track_lower = normalize_for_track_comparison(track)
    if not track_lower:
        return None
    for item in _iter_title_matched_items(release, track_lower):
        if item.position:
            return item.position
    return None
