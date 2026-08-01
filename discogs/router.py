"""FastAPI router for Discogs API endpoints."""

import logging
from collections.abc import Callable

import sentry_sdk
from fastapi import APIRouter, Depends, HTTPException, Query

from core.dependencies import get_discogs_cache_service, get_discogs_service
from core.service_unavailable import service_unavailable_detail
from discogs.breaker import DiscogsBreakerOpenError
from discogs.cache_service import CacheUnavailableError, DiscogsCacheService
from discogs.markup_parser import DiscogsServiceResolver, parse_async
from discogs.memory_cache import evict_cached
from discogs.models import (
    ArtistDetails,
    EntityResolveResponse,
    EntityType,
    ReleaseMetadataResponse,
    TrackReleasesResponse,
    TracksAutocompleteResponse,
)
from discogs.service import DiscogsService
from identity.dependencies import require_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/discogs", tags=["discogs"])

# Shared description for the ?refresh query param so every entity endpoint
# documents the same behavior in the OpenAPI schema. LML#498.
_REFRESH_DESCRIPTION = (
    "When true, evict the in-memory L1 cache entry for this entity before "
    "fetching. Forces re-traversal of L2 (PG) and L3 (Discogs API), which is "
    "needed after a manual fix to the underlying PG cache row that would "
    "otherwise stay invisible until L1's TTL expires. NOTE: does NOT clear "
    "LML#510 tombstones (`not_found = TRUE`) in L2 — use "
    "`DELETE /admin/discogs/tombstone/{type}/{id}` for that recovery surface."
)

# LML#1036: does NOT fit `service_unavailable_detail`'s "is not available"
# shape -- this one is phrased "is not configured". Kept as a hand-written
# literal rather than changed to match every other service's detail bytes.
_DISCOGS_SERVICE_UNAVAILABLE_DETAIL = (
    "Discogs service is not configured. Set DISCOGS_TOKEN environment variable."
)
_DISCOGS_CACHE_UNAVAILABLE_DETAIL = service_unavailable_detail(
    "Discogs cache", "Set DATABASE_URL_DISCOGS."
)

# LML#1036: was a hand-written `_require_service` guard, a verbatim copy of
# cache/router.py's own (now also retired). Both are instances of the shared
# `require_service` factory (identity/dependencies.py).
_require_discogs_service = require_service(get_discogs_service, _DISCOGS_SERVICE_UNAVAILABLE_DETAIL)


def _refresh_l1(cached_func: Callable, *args, **kwargs) -> None:
    """Evict an L1 entry on a refresh request and tag the Sentry trace.

    Only tags when eviction actually removed something — a no-op probe
    (refresh on an already-cold key) doesn't count. The Sentry tag
    ``lml.cache.refresh_evicted`` and transaction data
    ``lml.cache.memory_evictions_refresh`` give operators a filterable signal
    that a refresh-driven miss caused any downstream L2/L3 traversal — distinct
    from a natural TTL expiry, which leaves no marker. LML#498.
    """
    if not evict_cached(cached_func, *args, **kwargs):
        return
    try:
        sentry_sdk.set_tag("lml.cache.refresh_evicted", "true")
        transaction = sentry_sdk.get_current_scope().transaction
        if transaction is not None:
            transaction.set_data("lml.cache.memory_evictions_refresh", 1)
    except Exception as e:
        logger.warning("Failed to tag refresh eviction on Sentry transaction: %s", e)


@router.get(
    "/track-releases",
    response_model=TrackReleasesResponse,
    summary="Find all releases containing a track",
    responses={
        200: {"description": "List of releases returned"},
        422: {"description": "Missing required track parameter"},
        503: {"description": "Discogs service not configured"},
    },
)
async def get_track_releases(
    track: str = Query(..., description="Track/song title to search for"),
    artist: str | None = Query(None, description="Optional artist name for filtering"),
    limit: int = Query(20, ge=1, le=100, description="Maximum number of results"),
    svc: DiscogsService = Depends(_require_discogs_service),
) -> TrackReleasesResponse:
    """Find all releases containing a specific track."""
    return await svc.search_releases_by_track(track, artist, limit)


@router.get(
    "/release/{release_id}",
    response_model=ReleaseMetadataResponse,
    summary="Get full release metadata",
    responses={
        200: {"description": "Release metadata returned"},
        404: {"description": "Release not found"},
        503: {"description": "Discogs service not configured"},
    },
)
async def get_release(
    release_id: int,
    refresh: bool = Query(False, description=_REFRESH_DESCRIPTION),
    svc: DiscogsService = Depends(_require_discogs_service),
) -> ReleaseMetadataResponse:
    """Get full metadata for a release by ID."""
    if refresh:
        _refresh_l1(DiscogsService.get_release, release_id)
    try:
        result = await svc.get_release(release_id)
    except DiscogsBreakerOpenError as e:
        # LML#755 R2-3: the Discogs saturation breaker shed this live probe. 503
        # (transient / retryable), not a raw 500 — the caller should back off and
        # retry rather than treat this as a hard failure.
        raise HTTPException(
            status_code=503,
            detail="Discogs temporarily shed (rate-saturated); retry shortly.",
        ) from e

    if result is None:
        raise HTTPException(
            status_code=404,
            detail=f"Release {release_id} not found",
        )

    return result


@router.get(
    "/artist/{artist_id}",
    response_model=ArtistDetails,
    summary="Get full artist details",
    responses={
        200: {"description": "Artist details returned"},
        404: {"description": "Artist not found"},
        503: {"description": "Discogs service not configured"},
    },
)
async def get_artist(
    artist_id: int,
    refresh: bool = Query(False, description=_REFRESH_DESCRIPTION),
    svc: DiscogsService = Depends(_require_discogs_service),
) -> ArtistDetails:
    """Get full details for an artist by Discogs ID."""
    if refresh:
        _refresh_l1(DiscogsService.get_artist_details, artist_id)
    try:
        result = await svc.get_artist_details(artist_id)
    except DiscogsBreakerOpenError as e:
        # LML#1031: defensive wrap, mirroring resolve_entity's framing below —
        # NOT closing an active 500. get_artist_details's own _api_fetch
        # currently CATCHES DiscogsBreakerOpenError internally and returns
        # None (LML#805, service.py ~1685-1695) rather than propagating it,
        # unlike get_release's _api_fetch, which re-raises (service.py
        # ~1520-1526, "FIX 1"). So today a real breaker shed here surfaces as
        # the 404 branch below, not this one. This except exists so the 503
        # contract holds the moment get_artist_details ever starts
        # propagating a shed instead of swallowing it. LML#1049 tracks the
        # decision on whether/how to close that gap (shed -> misleading 404).
        raise HTTPException(
            status_code=503,
            detail="Discogs temporarily shed (rate-saturated); retry shortly.",
        ) from e

    if result is None:
        raise HTTPException(
            status_code=404,
            detail=f"Artist {artist_id} not found",
        )

    if result.profile:
        try:
            resolver = DiscogsServiceResolver(svc)
            result.profile_tokens = await parse_async(result.profile, resolver)
        except Exception:
            logger.warning("Failed to parse profile markup for artist %d", artist_id)

    return result


@router.get(
    "/entity/{entity_type}/{entity_id}",
    response_model=EntityResolveResponse,
    summary="Resolve a Discogs entity to its name",
    responses={
        200: {"description": "Entity resolved"},
        404: {"description": "Entity not found"},
        422: {"description": "Invalid entity type"},
        503: {"description": "Discogs service not configured"},
    },
)
async def resolve_entity(
    entity_type: EntityType,
    entity_id: int,
    refresh: bool = Query(False, description=_REFRESH_DESCRIPTION),
    svc: DiscogsService = Depends(_require_discogs_service),
) -> EntityResolveResponse:
    """Resolve a Discogs entity (artist, release, or master) to its name."""
    # Dispatch to the cached method matching this entity type. Adding a new
    # `EntityType` value MUST come with a matching entry here; the parametrized
    # test in test_discogs_router.py walks every enum value so a missing leg
    # surfaces as a test failure rather than a silent refresh no-op.
    cached_funcs_by_type = {
        EntityType.artist: DiscogsService.get_artist_details,
        EntityType.release: DiscogsService.get_release,
        EntityType.master: DiscogsService.get_master,
    }
    if refresh:
        _refresh_l1(cached_funcs_by_type[entity_type], entity_id)

    try:
        if entity_type == EntityType.artist:
            artist = await svc.get_artist_details(entity_id)
            if artist is None:
                raise HTTPException(status_code=404, detail=f"Artist {entity_id} not found")
            return EntityResolveResponse(name=artist.name, type=entity_type, id=entity_id)

        elif entity_type == EntityType.release:
            release = await svc.get_release(entity_id)
            if release is None:
                raise HTTPException(status_code=404, detail=f"Release {entity_id} not found")
            return EntityResolveResponse(name=release.title, type=entity_type, id=entity_id)

        else:  # master
            master = await svc.get_master(entity_id)
            if master is None:
                raise HTTPException(status_code=404, detail=f"Master {entity_id} not found")
            return EntityResolveResponse(name=master.title, type=entity_type, id=entity_id)
    except DiscogsBreakerOpenError as e:
        # LML#755 R2-3: a saturation-breaker shed on this diagnostic route → 503
        # (transient), not a raw 500. Currently only the release branch's
        # ``get_release`` re-raises the shed, but wrapping the whole dispatch
        # keeps the contract if another branch's fetch ever propagates it too.
        raise HTTPException(
            status_code=503,
            detail="Discogs temporarily shed (rate-saturated); retry shortly.",
        ) from e


@router.get(
    "/tracks/autocomplete",
    response_model=TracksAutocompleteResponse,
    summary="Autocomplete track titles for an artist",
    responses={
        200: {"description": "Track titles returned (may be empty on cache error)"},
        422: {"description": "Missing required artist or q parameter"},
        503: {"description": "Discogs cache not available"},
    },
)
async def autocomplete_tracks(
    artist: str = Query(..., description="Artist name (required)"),
    q: str = Query(..., description="Track title prefix to search for"),
    release: str | None = Query(None, description="Optional release title filter"),
    limit: int = Query(20, ge=1, le=100, description="Maximum number of results"),
    cache: DiscogsCacheService | None = Depends(get_discogs_cache_service),
) -> TracksAutocompleteResponse:
    """Autocomplete track titles from the Discogs cache for a given artist."""
    if cache is None:
        raise HTTPException(
            status_code=503,
            detail=_DISCOGS_CACHE_UNAVAILABLE_DETAIL,
        )

    try:
        results = await cache.autocomplete_tracks(artist, q, release=release, limit=limit)
        return TracksAutocompleteResponse(
            results=results,
            total=len(results),
            artist=artist,
            cached=True,
        )
    except CacheUnavailableError:
        logger.warning("Cache error during track autocomplete, returning empty results")
        return TracksAutocompleteResponse(
            results=[],
            total=0,
            artist=artist,
            cached=True,
        )
