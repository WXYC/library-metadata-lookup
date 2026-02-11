"""Helper functions for Discogs lookups.

These functions accept an optional DiscogsService instance. When provided
(e.g., from FastAPI dependency injection), the service's cache is used.
When omitted, a cacheless service is created as a fallback.
"""

import logging

from discogs.models import DiscogsSearchRequest
from discogs.service import DiscogsService

logger = logging.getLogger(__name__)


def _get_service() -> DiscogsService | None:
    """Get a cacheless DiscogsService instance if token is configured.

    Prefer passing a service instance from dependency injection to benefit
    from the PostgreSQL cache.
    """
    from config.settings import get_settings

    settings = get_settings()
    if not settings.discogs_token:
        return None
    return DiscogsService(settings.discogs_token)


async def lookup_releases_by_track(
    track: str,
    artist: str | None = None,
    limit: int = 20,
    service: DiscogsService | None = None,
) -> list[tuple[str, str]]:
    """Look up all releases containing a track using Discogs.

    For Various Artists / compilation releases, validates the tracklist
    to ensure the track by the artist actually exists on the release.

    Args:
        track: Track title
        artist: Optional artist name
        limit: Maximum number of results
        service: Optional DiscogsService instance (with cache). If not provided,
            creates a cacheless fallback service.

    Returns:
        List of (artist, album) tuples for releases containing the track.
    """
    if service is None:
        service = _get_service()
    if not service:
        return []

    response = await service.search_releases_by_track(track, artist, limit)

    # Validate that the track actually exists on each release
    releases = []
    for release_info in response.releases:
        if artist and release_info.release_id:
            is_valid = await service.validate_track_on_release(
                release_info.release_id, track, artist
            )
            if not is_valid:
                logger.info(
                    f"Skipping '{release_info.album}' - track/artist not validated on release"
                )
                continue

        releases.append((release_info.artist, release_info.album))

    return releases


async def lookup_releases_by_artist(
    artist: str,
    limit: int = 10,
    service: DiscogsService | None = None,
) -> list[tuple[str, str]]:
    """Look up releases by an artist using Discogs.

    Args:
        artist: Artist name to search for
        limit: Maximum number of results
        service: Optional DiscogsService instance (with cache). If not provided,
            creates a cacheless fallback service.

    Returns:
        List of (artist, album) tuples for releases by or featuring the artist.
    """
    if service is None:
        service = _get_service()
    if not service:
        return []

    request = DiscogsSearchRequest(artist=artist)
    response = await service.search(request, limit=limit)

    return [(r.artist or "", r.album or "") for r in response.results]
