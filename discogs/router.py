"""FastAPI router for Discogs API endpoints."""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query

from core.dependencies import get_discogs_cache_service, get_discogs_service
from discogs.cache_service import CacheUnavailableError, DiscogsCacheService
from discogs.markup_parser import DiscogsServiceResolver, parse_async
from discogs.models import (
    ArtistDetails,
    EntityResolveResponse,
    EntityType,
    ReleaseMetadataResponse,
    SearchByTrackResponse,
    SearchByTrackResult,
    TrackReleasesResponse,
    TracksAutocompleteResponse,
)
from discogs.service import DiscogsService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/discogs", tags=["discogs"])


def _require_service(service: DiscogsService | None) -> DiscogsService:
    """Raise 503 if service is not available."""
    if service is None:
        raise HTTPException(
            status_code=503,
            detail="Discogs service is not configured. Set DISCOGS_TOKEN environment variable.",
        )
    return service


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
    service: DiscogsService | None = Depends(get_discogs_service),
) -> TrackReleasesResponse:
    """Find all releases containing a specific track."""
    svc = _require_service(service)
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
    service: DiscogsService | None = Depends(get_discogs_service),
) -> ReleaseMetadataResponse:
    """Get full metadata for a release by ID."""
    svc = _require_service(service)
    result = await svc.get_release(release_id)

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
    service: DiscogsService | None = Depends(get_discogs_service),
) -> ArtistDetails:
    """Get full details for an artist by Discogs ID."""
    svc = _require_service(service)
    result = await svc.get_artist_details(artist_id)

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
    service: DiscogsService | None = Depends(get_discogs_service),
) -> EntityResolveResponse:
    """Resolve a Discogs entity (artist, release, or master) to its name."""
    svc = _require_service(service)

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


@router.get(
    "/search-by-track",
    response_model=SearchByTrackResponse,
    summary="Find Discogs releases by track title (album-by-track search)",
    description="""
    Fuzzy-match a track title against ``release_track.title`` and return
    the parent releases. This is the LML-side primitive that Backend-Service
    wraps to deliver dj-site catalog search by track title (e.g. searching
    "vi scose poise" returns the *Confield* release_id).

    Returns one row per ``release_id``; multiple pressings of the same
    album appear as distinct rows. Bridge to library rows on the caller
    side via ``library.canonical_entity_id = 'discogs:' || release_id``.

    The default ``score_threshold`` of 0.3 catches partial-track-title
    queries ("scose poise" → "VI Scose Poise"); raise to 0.5+ to require
    near-exact matches.
    """,
    responses={
        200: {"description": "Matched releases returned (may be empty on cache error)"},
        422: {"description": "Missing required q parameter"},
        503: {"description": "Discogs cache not available"},
    },
)
async def search_by_track(
    q: str = Query(..., description="Track title query (need not be exact)"),
    limit: int = Query(50, ge=1, le=200, description="Maximum number of results"),
    score_threshold: float = Query(
        0.3,
        ge=0,
        le=1,
        description="Minimum trigram similarity in [0, 1]; default 0.3 = pg_trgm default",
    ),
    cache: DiscogsCacheService | None = Depends(get_discogs_cache_service),
) -> SearchByTrackResponse:
    """Fuzzy-search Discogs cache tracklists for a track-title match."""
    if cache is None:
        raise HTTPException(
            status_code=503,
            detail="Discogs cache is not available. Set DATABASE_URL_DISCOGS.",
        )

    try:
        rows = await cache.search_albums_by_track_title(
            q, limit=limit, score_threshold=score_threshold
        )
    except CacheUnavailableError:
        logger.warning("Cache error during search-by-track, returning empty results")
        return SearchByTrackResponse(results=[], total=0, query=q)

    return SearchByTrackResponse(
        results=[SearchByTrackResult(**r) for r in rows],
        total=len(rows),
        query=q,
    )


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
            detail="Discogs cache is not available. Set DATABASE_URL_DISCOGS.",
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
