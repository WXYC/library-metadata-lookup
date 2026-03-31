"""FastAPI router for Discogs API endpoints."""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query

from core.dependencies import get_discogs_service
from discogs.models import (
    ArtistDetails,
    DiscogsSearchRequest,
    DiscogsSearchResponse,
    EntityResolveResponse,
    EntityType,
    ReleaseMetadataResponse,
    TrackReleasesResponse,
)
from discogs.service import DiscogsService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/discogs", tags=["discogs"])


def _require_service(service: DiscogsService | None) -> DiscogsService:
    """Raise 503 if service is not available."""
    if service is None:
        raise HTTPException(
            status_code=503,
            detail="Discogs service is not configured. Set DISCOGS_TOKEN or DISCOGS_API_KEY + DISCOGS_API_SECRET.",
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


@router.post(
    "/search",
    response_model=DiscogsSearchResponse,
    summary="Search Discogs releases",
    responses={
        200: {"description": "Search results returned"},
        400: {"description": "No search parameters provided"},
        503: {"description": "Discogs service not configured"},
    },
)
async def search_releases(
    request: DiscogsSearchRequest,
    limit: int = Query(5, ge=1, le=50, description="Maximum number of results"),
    service: DiscogsService | None = Depends(get_discogs_service),
) -> DiscogsSearchResponse:
    """Search Discogs for releases matching the criteria."""
    svc = _require_service(service)

    if not request.artist and not request.album and not request.track:
        raise HTTPException(
            status_code=400,
            detail="At least one of artist, album, or track must be provided",
        )

    return await svc.search(request, limit=limit)


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
