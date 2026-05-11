"""Discogs API service with caching and rate limiting."""

from __future__ import annotations

import asyncio
import logging
import random
from enum import StrEnum
from typing import TYPE_CHECKING, Any

import httpx
import sentry_sdk
from rapidfuzz import fuzz
from wxyc_etl.text import is_compilation_artist
from wxyc_fastapi.observability import (
    add_breadcrumb,
    get_cache_stats_recorder,
    timed_api,
    timed_pg,
)

from config.settings import get_settings
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
    should_skip_cache,
)
from discogs.models import (
    ArtistCredit,
    ArtistDetails,
    ArtistRef,
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
from discogs.ratelimit import get_rate_limiter, get_semaphore

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

# Fuzzy fallback for `validate_track_on_release` artist matching. Strict substring
# matching loses on collaboration trios where neither name is a substring of the
# other (e.g., request "Orcutt Shelley Miller" vs release artist
# "Bill Orcutt, Tashi Shelley & Robbie Miller"). rapidfuzz token_set_ratio is
# order- and stopword-tolerant; the threshold was chosen so that one shared
# token across two short names ("Bill Orcutt" vs "Orcutt Shelley Miller") just
# clears the bar (~70.6) while unrelated artists score well below 50. See LML#210.
_ARTIST_FUZZY_MATCH_THRESHOLD = 70


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
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create the HTTP client."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=DISCOGS_API_BASE,
                headers={
                    "Authorization": self._auth_header,
                    "User-Agent": "LibraryMetadataLookupService/1.0",
                },
                timeout=10.0,
            )
        return self._client

    async def close(self):
        """Close the HTTP client."""
        if self._client:
            await self._client.aclose()
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
        rate_limiter = get_rate_limiter()

        async with semaphore:
            for attempt in range(max_retries + 1):
                await rate_limiter.acquire()

                try:
                    response = await client.request(method, path, params=params)

                    # Log rate limit remaining for observability
                    remaining = response.headers.get("X-Discogs-Ratelimit-Remaining")
                    if remaining:
                        logger.debug(f"Discogs rate limit remaining: {remaining}")

                    if response.status_code == 429:
                        if attempt < max_retries:
                            retry_after = response.headers.get("Retry-After")
                            delay = _compute_retry_delay(attempt, retry_after)
                            logger.warning(
                                f"Discogs rate limit hit, retrying in {delay:.2f}s "
                                f"(attempt {attempt + 1}/{max_retries + 1}, "
                                f"Retry-After={retry_after})"
                            )
                            await asyncio.sleep(delay)
                            continue
                        else:
                            logger.error("Discogs rate limit hit, max retries exhausted")
                            return None

                    return response

                except httpx.RequestError as e:
                    logger.error(f"Discogs request failed: {e}")
                    return None

        return None

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

        Uses a hybrid approach with optional PostgreSQL cache:
        1. Try local cache first (if available)
        2. On cache miss, search Discogs API
        3. Supplement with keyword search if few results

        Args:
            track: Track title to search for
            artist: Optional artist name for filtering
            limit: Maximum number of results

        Returns:
            TrackReleasesResponse with list of releases
        """
        # Try local cache first.
        # Skip cache when artist_as_keyword=True: the PG cache filters by
        # release-level artist which excludes VA compilations where the
        # artist is credited on individual tracks.  The API keyword search
        # handles this correctly with format=Compilation.
        if self.cache_service and not should_skip_cache() and not artist_as_keyword:
            try:
                add_discogs_breadcrumb(
                    "cache_search_releases_by_track",
                    {"track": track, "artist": artist},
                )
                async with timed_pg():
                    cached_releases = await self.cache_service.search_releases_by_track(
                        track=track, artist=artist, limit=limit
                    )
                if cached_releases:
                    logger.info(f"Cache hit: found {len(cached_releases)} releases for '{track}'")
                    get_cache_stats_recorder().record_pg_cache_hit()
                    add_discogs_breadcrumb(
                        "cache_hit", {"track": track, "count": len(cached_releases)}
                    )
                    return TrackReleasesResponse(
                        track=track,
                        artist=artist,
                        releases=cached_releases,
                        total=len(cached_releases),
                        cached=True,
                    )
                logger.debug(f"Cache miss for track '{track}'")
                get_cache_stats_recorder().record_pg_cache_miss()
                add_discogs_breadcrumb("cache_miss", {"track": track})
            except Exception as e:
                logger.warning(f"Cache lookup failed, falling back to API: {e}")
                add_discogs_breadcrumb("cache_error", {"error": str(e)}, level="warning")

        # Fall back to Discogs API
        releases: list[ReleaseInfo] = []
        seen_albums: set = set()

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
                response = await self._request_with_retry("GET", "/database/search", params=params)

            if response is not None:
                get_cache_stats_recorder().record_api_call()
                response.raise_for_status()
                data = response.json()

                for result in data.get("results", []):
                    release_info = self._process_search_result(result, seen_albums)
                    if release_info:
                        releases.append(release_info)

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

            return TrackReleasesResponse(
                track=track,
                artist=artist,
                releases=releases[:limit],
                total=len(releases[:limit]),
                cached=False,
            )

        except Exception as e:
            logger.error(f"Discogs search failed: {e}")
            return TrackReleasesResponse(track=track, artist=artist, cached=False)

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
    async def get_release(self, release_id: int) -> ReleaseMetadataResponse | None:
        """Get full release metadata by ID.

        Uses optional PostgreSQL cache with write-back strategy:
        1. Try local cache first (if available)
        2. On cache miss, fetch from Discogs API
        3. Write API result back to cache for future queries

        Args:
            release_id: Discogs release ID

        Returns:
            ReleaseMetadataResponse with full metadata, or None on error
        """
        # Try local cache first
        if self.cache_service and not should_skip_cache():
            try:
                add_discogs_breadcrumb("cache_get_release", {"release_id": release_id})
                async with timed_pg():
                    cached_release = await self.cache_service.get_release(release_id)
                if cached_release:
                    logger.info(f"Cache hit: release {release_id}")
                    get_cache_stats_recorder().record_pg_cache_hit()
                    add_discogs_breadcrumb("cache_hit", {"release_id": release_id})
                    return cached_release
                logger.debug(f"Cache miss for release {release_id}")
                get_cache_stats_recorder().record_pg_cache_miss()
                add_discogs_breadcrumb("cache_miss", {"release_id": release_id})
            except Exception as e:
                logger.warning(f"Cache lookup failed, falling back to API: {e}")
                add_discogs_breadcrumb("cache_error", {"error": str(e)}, level="warning")

        # Fall back to Discogs API
        try:
            async with timed_api():
                response = await self._request_with_retry("GET", f"/releases/{release_id}")

            if response is None:
                logger.warning(f"Failed to fetch release {release_id} (rate limited or error)")
                return None

            get_cache_stats_recorder().record_api_call()
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

            release = ReleaseMetadataResponse(
                release_id=release_id,
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

            # Write back to cache for future queries
            if self.cache_service and not should_skip_cache():
                try:
                    add_discogs_breadcrumb("cache_write_release", {"release_id": release_id})
                    await self.cache_service.write_release(release)
                    logger.debug(f"Cached release {release_id}")
                except Exception as e:
                    logger.warning(f"Failed to cache release {release_id}: {e}")
                    add_discogs_breadcrumb("cache_write_error", {"error": str(e)}, level="warning")

            return release

        except Exception as e:
            logger.error(f"Failed to fetch release {release_id}: {e}")
            return None

    @async_cached(ARTIST_CACHE)
    async def get_artist_details(self, artist_id: int) -> ArtistDetails | None:
        """Fetch full artist details from Discogs.

        Uses optional PostgreSQL cache with write-back strategy:
        1. Try local cache first (if available)
        2. On cache miss, fetch from Discogs API
        3. Write API result back to cache for future queries

        Args:
            artist_id: Discogs artist ID

        Returns:
            ArtistDetails with full metadata, or None on error
        """
        # Try local cache first
        if self.cache_service and not should_skip_cache():
            try:
                add_discogs_breadcrumb("cache_get_artist_details", {"artist_id": artist_id})
                async with timed_pg():
                    cached_details = await self.cache_service.get_artist_details(artist_id)
                if cached_details:
                    logger.info(f"Cache hit: artist {artist_id}")
                    get_cache_stats_recorder().record_pg_cache_hit()
                    return cached_details
                logger.debug(f"Cache miss for artist {artist_id}")
                get_cache_stats_recorder().record_pg_cache_miss()
            except Exception as e:
                logger.warning(f"Cache lookup failed, falling back to API: {e}")

        try:
            async with timed_api():
                response = await self._request_with_retry("GET", f"/artists/{artist_id}")
            if response is None:
                return None
            get_cache_stats_recorder().record_api_call()
            add_discogs_breadcrumb("get_artist_details", {"artist_id": artist_id})
            response.raise_for_status()
            data = response.json()

            images = data.get("images", [])
            image_url = images[0].get("uri") if images else None

            details = ArtistDetails(
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

            # Write back to cache
            if self.cache_service and not should_skip_cache():
                try:
                    await self.cache_service.write_artist_details(details)
                    logger.debug(f"Cached artist {artist_id}")
                except Exception as e:
                    logger.warning(f"Failed to cache artist {artist_id}: {e}")

            return details

        except Exception as e:
            logger.warning(f"Failed to fetch artist details for {artist_id}: {e}")
            return None

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
            async with timed_api():
                response = await self._request_with_retry("GET", f"/masters/{master_id}")
            if response is None:
                return None
            get_cache_stats_recorder().record_api_call()
            add_discogs_breadcrumb("get_master", {"master_id": master_id})
            response.raise_for_status()
            data = response.json()

            return MasterRelease(
                master_id=master_id,
                title=data.get("title", ""),
                year=data.get("year"),
                cached=False,
            )
        except Exception as e:
            logger.warning(f"Failed to fetch master release {master_id}: {e}")
            return None

    @async_cached(SEARCH_CACHE)
    async def search(self, request: DiscogsSearchRequest, limit: int = 5) -> DiscogsSearchResponse:
        """General release search for artwork discovery.

        Args:
            request: Search parameters (artist, album, track)
            limit: Maximum number of results to return

        Returns:
            DiscogsSearchResponse with ranked results
        """
        params = self._build_search_params(request, limit=limit)
        if not params:
            logger.warning("No searchable fields in request")
            return DiscogsSearchResponse(cached=False)

        # Try local cache first
        if self.cache_service and not should_skip_cache():
            try:
                add_discogs_breadcrumb(
                    "cache_search_releases",
                    {"artist": request.artist, "album": request.album},
                )
                async with timed_pg():
                    cached = await self.cache_service.search_releases(
                        artist=request.artist,
                        album=request.album or request.track,
                        limit=limit,
                    )
                if cached:
                    logger.info(f"Cache hit: found {len(cached)} releases for search")
                    get_cache_stats_recorder().record_pg_cache_hit()
                    add_discogs_breadcrumb("cache_hit", {"count": len(cached)})
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
                                release_id=row["release_id"],
                                release_url=f"https://www.discogs.com/release/{row['release_id']}",
                                artwork_url=row.get("artwork_url"),
                                confidence=confidence,
                            )
                        )
                    results.sort(key=lambda r: r.confidence, reverse=True)
                    return DiscogsSearchResponse(results=results, total=len(results), cached=True)
                logger.debug("Cache miss for search")
                get_cache_stats_recorder().record_pg_cache_miss()
                add_discogs_breadcrumb("cache_miss", {"artist": request.artist})
            except Exception as e:
                logger.warning(f"Cache search failed, falling back to API: {e}")
                add_discogs_breadcrumb("cache_error", {"error": str(e)}, level="warning")

        logger.info(f"Searching Discogs with params: {params}")

        try:
            async with timed_api():
                response = await self._request_with_retry("GET", "/database/search", params=params)

            if response is None:
                logger.warning("Discogs search failed (rate limited or error)")
                return DiscogsSearchResponse(cached=False)

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
                if response is not None:
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

        except Exception as e:
            logger.error(f"Discogs search failed: {e}")
            return DiscogsSearchResponse(cached=False)

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
            params["label"] = request.label
        if request.format:
            params["format"] = request.format

        if "artist" not in params and "release_title" not in params:
            return {}

        return params

    @async_cached(VALIDATION_CACHE)
    async def validate_track_on_release(self, release_id: int, track: str, artist: str) -> bool:
        """Validate that a track by an artist exists on a release.

        Uses optional PostgreSQL cache for validation:
        1. Try cache validation first (if available)
        2. On cache miss (None), fall back to API via get_release

        Args:
            release_id: Discogs release ID
            track: Track title to find
            artist: Artist name to find

        Returns:
            True if the track by the artist is found on the release
        """
        # Try cache validation first
        if self.cache_service and not should_skip_cache():
            try:
                add_discogs_breadcrumb(
                    "cache_validate_track",
                    {"release_id": release_id, "track": track, "artist": artist},
                )
                async with timed_pg():
                    cached_result = await self.cache_service.validate_track_on_release(
                        release_id, track, artist
                    )
                if cached_result is not None:
                    logger.info(
                        f"Cache {'validated' if cached_result else 'rejected'}: "
                        f"'{track}' by '{artist}' on release {release_id}"
                    )
                    get_cache_stats_recorder().record_pg_cache_hit()
                    add_discogs_breadcrumb(
                        "cache_hit", {"release_id": release_id, "validated": cached_result}
                    )
                    return cached_result
                logger.debug(f"Cache miss for validation on release {release_id}")
                get_cache_stats_recorder().record_pg_cache_miss()
                add_discogs_breadcrumb("cache_miss", {"release_id": release_id})
            except Exception as e:
                logger.warning(f"Cache validation failed, falling back to API: {e}")
                add_discogs_breadcrumb("cache_error", {"error": str(e)}, level="warning")

        # Fall back to API via get_release
        release = await self.get_release(release_id)
        if release is None:
            return False

        track_lower = normalize_for_track_comparison(track)
        artist_lower = normalize_artist_for_validation(artist)

        for item in release.tracklist or []:
            item_title = normalize_for_track_comparison(item.title)
            # Check if track title matches
            if track_lower not in item_title and item_title not in track_lower:
                continue

            # Check per-track artists first (for compilations)
            if item.artists:
                for track_artist in item.artists:
                    track_artist_lower = normalize_artist_for_validation(track_artist)
                    if artist_lower in track_artist_lower or track_artist_lower in artist_lower:
                        logger.info(
                            f"Validated: '{track}' by '{artist}' found on release {release_id}"
                        )
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
                    logger.info(
                        f"Validated (fuzzy): '{track}' by '{artist}' on release {release_id}"
                    )
                    return True
            else:
                # For single-artist releases, check release artist
                release_artist = normalize_artist_for_validation(release.artist)

                if artist_lower in release_artist or release_artist in artist_lower:
                    logger.info(f"Validated: '{track}' by '{artist}' found on release {release_id}")
                    return True

                # Fuzzy fallback for the same trio scenario (LML#210), but where
                # all members are listed in the single release-artist string.
                if (
                    release_artist
                    and fuzz.token_set_ratio(artist_lower, release_artist)
                    >= _ARTIST_FUZZY_MATCH_THRESHOLD
                ):
                    logger.info(
                        f"Validated (fuzzy): '{track}' by '{artist}' on release {release_id}"
                    )
                    return True

        logger.info(f"Track '{track}' by '{artist}' NOT found on release {release_id}")
        return False
