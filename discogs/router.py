"""FastAPI router for Discogs API endpoints."""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query

from core.dependencies import get_discogs_service
from discogs.models import (
    DiscogsSearchRequest,
    DiscogsSearchResponse,
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
