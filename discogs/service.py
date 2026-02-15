"""Discogs API service with caching and rate limiting."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

import httpx

from config.settings import get_settings
from core.matching import calculate_confidence, is_compilation_artist
from core.telemetry import (
    record_api_time,
    record_discogs_api_call,
    record_pg_cache_hit,
    record_pg_cache_miss,
    record_pg_time,
)
from discogs.memory_cache import (
    ARTIST_CACHE,
    LABEL_CACHE,
    RELEASE_CACHE,
    SEARCH_CACHE,
    TRACK_CACHE,
    async_cached,
    should_skip_cache,
)
from discogs.models import (
    DiscogsSearchRequest,
    DiscogsSearchResponse,
    DiscogsSearchResult,
    ReleaseInfo,
    ReleaseMetadataResponse,
    TrackItem,
    TrackReleasesResponse,
)
from discogs.ratelimit import get_rate_limiter, get_semaphore

if TYPE_CHECKING:
    from discogs.cache_service import DiscogsCacheService

# Import Sentry breadcrumb helper (fail gracefully if not initialized)
try:
    from core.sentry import add_discogs_breadcrumb
except ImportError:

    def add_discogs_breadcrumb(
        operation: str, data: dict[str, Any] | None = None, level: str = "info"
    ) -> None:
        pass  # No-op if Sentry not available


logger = logging.getLogger(__name__)

DISCOGS_API_BASE = "https://api.discogs.com"


class DiscogsService:
    """Service for all Discogs API interactions with caching.

    Supports an optional PostgreSQL cache service for faster lookups.
    When cache_service is provided, queries check local cache first,
    then fall back to Discogs API, and cache API results for future queries.
    """

    def __init__(self, token: str, cache_service: DiscogsCacheService | None = None):
        """Initialize the service with a Discogs API token.

        Args:
            token: Discogs API token
            cache_service: Optional PostgreSQL cache service for local caching
        """
        self.token = token
        self.cache_service = cache_service
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create the HTTP client."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=DISCOGS_API_BASE,
                headers={
                    "Authorization": f"Discogs token={self.token}",
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

    async def check_api(self) -> bool:
        """Check Discogs API connectivity."""
        try:
            client = await self._get_client()
            resp = await client.get("/oauth/identity")
            return bool(resp.status_code == 200)
        except Exception:
            return False

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
                            # Exponential backoff: 1s, 2s, 4s...
                            delay = 2**attempt
                            logger.warning(
                                f"Discogs rate limit hit, retrying in {delay}s "
                                f"(attempt {attempt + 1}/{max_retries + 1})"
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
        self, track: str, artist: str | None = None, limit: int = 20
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
        # Try local cache first
        if self.cache_service and not should_skip_cache():
            try:
                add_discogs_breadcrumb(
                    "cache_search_releases_by_track",
                    {"track": track, "artist": artist},
                )
                start = time.perf_counter()
                cached_releases = await self.cache_service.search_releases_by_track(
                    track=track, artist=artist, limit=limit
                )
                record_pg_time((time.perf_counter() - start) * 1000)
                if cached_releases:
                    logger.info(f"Cache hit: found {len(cached_releases)} releases for '{track}'")
                    record_pg_cache_hit()
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
                record_pg_cache_miss()
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
            params["artist"] = artist

        logger.info(f"Searching Discogs for releases with track: '{track}', artist: {artist}")

        try:
            start = time.perf_counter()
            response = await self._request_with_retry("GET", "/database/search", params=params)

            if response is not None:
                record_api_time((time.perf_counter() - start) * 1000)
                record_discogs_api_call()
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
                start = time.perf_counter()
                response = await self._request_with_retry(
                    "GET", "/database/search", params=query_params
                )

                if response is not None:
                    record_api_time((time.perf_counter() - start) * 1000)
                    record_discogs_api_call()
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
                start = time.perf_counter()
                cached_release = await self.cache_service.get_release(release_id)
                record_pg_time((time.perf_counter() - start) * 1000)
                if cached_release:
                    logger.info(f"Cache hit: release {release_id}")
                    record_pg_cache_hit()
                    add_discogs_breadcrumb("cache_hit", {"release_id": release_id})
                    return cached_release
                logger.debug(f"Cache miss for release {release_id}")
                record_pg_cache_miss()
                add_discogs_breadcrumb("cache_miss", {"release_id": release_id})
            except Exception as e:
                logger.warning(f"Cache lookup failed, falling back to API: {e}")
                add_discogs_breadcrumb("cache_error", {"error": str(e)}, level="warning")

        # Fall back to Discogs API
        try:
            start = time.perf_counter()
            response = await self._request_with_retry("GET", f"/releases/{release_id}")

            if response is None:
                logger.warning(f"Failed to fetch release {release_id} (rate limited or error)")
                return None

            record_api_time((time.perf_counter() - start) * 1000)
            record_discogs_api_call()
            response.raise_for_status()
            data = response.json()

            # Extract artists
            artists = data.get("artists", [])
            artist_name = artists[0].get("name", "") if artists else ""
            artist_id = artists[0].get("id") if artists else None

            # Extract labels
            labels = data.get("labels", [])
            label_name = labels[0].get("name") if labels else None
            label_id = labels[0].get("id") if labels else None

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
    async def get_artist_image(self, artist_id: int) -> str | None:
        """Fetch primary image for a Discogs artist.

        Args:
            artist_id: Discogs artist ID

        Returns:
            Image URI string, or None if unavailable
        """
        try:
            start = time.perf_counter()
            response = await self._request_with_retry("GET", f"/artists/{artist_id}")
            if response is None:
                return None
            record_api_time((time.perf_counter() - start) * 1000)
            record_discogs_api_call()
            add_discogs_breadcrumb("get_artist_image", {"artist_id": artist_id})
            response.raise_for_status()
            data = response.json()
            images = data.get("images", [])
            return images[0].get("uri") if images else None
        except Exception as e:
            logger.warning(f"Failed to fetch artist image for {artist_id}: {e}")
            return None

    @async_cached(LABEL_CACHE)
    async def get_label_image(self, label_id: int) -> str | None:
        """Fetch primary image for a Discogs label.

        Args:
            label_id: Discogs label ID

        Returns:
            Image URI string, or None if unavailable
        """
        try:
            start = time.perf_counter()
            response = await self._request_with_retry("GET", f"/labels/{label_id}")
            if response is None:
                return None
            record_api_time((time.perf_counter() - start) * 1000)
            record_discogs_api_call()
            add_discogs_breadcrumb("get_label_image", {"label_id": label_id})
            response.raise_for_status()
            data = response.json()
            images = data.get("images", [])
            return images[0].get("uri") if images else None
        except Exception as e:
            logger.warning(f"Failed to fetch label image for {label_id}: {e}")
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
                start = time.perf_counter()
                cached = await self.cache_service.search_releases(
                    artist=request.artist,
                    album=request.album or request.track,
                    limit=limit,
                )
                record_pg_time((time.perf_counter() - start) * 1000)
                if cached:
                    logger.info(f"Cache hit: found {len(cached)} releases for search")
                    record_pg_cache_hit()
                    add_discogs_breadcrumb("cache_hit", {"count": len(cached)})
                    results = []
                    for row in cached:
                        confidence = calculate_confidence(
                            request.artist,
                            request.album,
                            row["artist_name"],
                            row["title"],
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
                record_pg_cache_miss()
                add_discogs_breadcrumb("cache_miss", {"artist": request.artist})
            except Exception as e:
                logger.warning(f"Cache search failed, falling back to API: {e}")
                add_discogs_breadcrumb("cache_error", {"error": str(e)}, level="warning")

        logger.info(f"Searching Discogs with params: {params}")

        try:
            start = time.perf_counter()
            response = await self._request_with_retry("GET", "/database/search", params=params)

            if response is None:
                logger.warning("Discogs search failed (rate limited or error)")
                return DiscogsSearchResponse(cached=False)

            record_api_time((time.perf_counter() - start) * 1000)
            record_discogs_api_call()
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
                start = time.perf_counter()
                response = await self._request_with_retry(
                    "GET", "/database/search", params=fallback_params
                )
                if response is not None:
                    record_api_time((time.perf_counter() - start) * 1000)
                    record_discogs_api_call()
                    response.raise_for_status()
                    data = response.json()

            results = []
            for item in data.get("results", []):
                cover_url = item.get("thumb")
                if not cover_url or "spacer.gif" in cover_url:
                    cover_url = None

                title = item.get("title", "")
                result_artist, album = self._parse_title(title)

                confidence = calculate_confidence(
                    request.artist, request.album, result_artist, album
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

        if "artist" not in params and "release_title" not in params:
            return {}

        return params

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
                start = time.perf_counter()
                cached_result = await self.cache_service.validate_track_on_release(
                    release_id, track, artist
                )
                record_pg_time((time.perf_counter() - start) * 1000)
                if cached_result is not None:
                    logger.info(
                        f"Cache {'validated' if cached_result else 'rejected'}: "
                        f"'{track}' by '{artist}' on release {release_id}"
                    )
                    record_pg_cache_hit()
                    add_discogs_breadcrumb(
                        "cache_hit", {"release_id": release_id, "validated": cached_result}
                    )
                    return cached_result
                logger.debug(f"Cache miss for validation on release {release_id}")
                record_pg_cache_miss()
                add_discogs_breadcrumb("cache_miss", {"release_id": release_id})
            except Exception as e:
                logger.warning(f"Cache validation failed, falling back to API: {e}")
                add_discogs_breadcrumb("cache_error", {"error": str(e)}, level="warning")

        # Fall back to API via get_release
        release = await self.get_release(release_id)
        if release is None:
            return False

        track_lower = track.lower()
        artist_lower = artist.lower()

        for item in release.tracklist:
            item_title = item.title.lower()
            # Check if track title matches
            if track_lower not in item_title and item_title not in track_lower:
                continue

            # Check per-track artists first (for compilations)
            if item.artists:
                for track_artist in item.artists:
                    track_artist_lower = track_artist.lower().split("(")[0].strip()
                    if artist_lower in track_artist_lower or track_artist_lower in artist_lower:
                        logger.info(
                            f"Validated: '{track}' by '{artist}' found on release {release_id}"
                        )
                        return True
            else:
                # For single-artist releases, check release artist
                release_artist = release.artist.lower()
                # Remove Discogs numbering like "(2)"
                release_artist = release_artist.split("(")[0].strip()

                if artist_lower in release_artist or release_artist in artist_lower:
                    logger.info(f"Validated: '{track}' by '{artist}' found on release {release_id}")
                    return True

        logger.info(f"Track '{track}' by '{artist}' NOT found on release {release_id}")
        return False
