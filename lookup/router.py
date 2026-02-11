"""Lookup API router."""

import logging

from fastapi import APIRouter, Depends, HTTPException
from posthog import Posthog

from core.dependencies import get_discogs_service, get_library_db, get_posthog_client
from core.telemetry import RequestTelemetry, get_cache_stats, init_cache_stats
from discogs.memory_cache import set_skip_cache
from discogs.service import DiscogsService
from library.db import LibraryDB
from lookup.models import LookupRequest, LookupResponse
from lookup.orchestrator import perform_lookup

logger = logging.getLogger(__name__)

router = APIRouter(tags=["lookup"])


@router.post(
    "/lookup",
    response_model=LookupResponse,
    summary="Look up a song/artist/album in the library catalog",
    description="""
    Performs a comprehensive library catalog lookup with Discogs cross-referencing.

    This endpoint:
    1. Corrects artist spelling using fuzzy matching
    2. Resolves album names from Discogs if only a song is provided
    3. Searches the library catalog with multiple fallback strategies
    4. Validates fallback results against Discogs tracklists
    5. Fetches album artwork from Discogs
    6. Returns enriched results with metadata

    The caller (request-parser) handles parsing and Slack posting.
    """,
    responses={
        200: {"description": "Lookup completed successfully"},
        400: {"description": "Invalid request"},
        500: {"description": "Internal server error"},
    },
)
async def handle_lookup(
    request: LookupRequest,
    db: LibraryDB = Depends(get_library_db),
    discogs_service: DiscogsService | None = Depends(get_discogs_service),
    posthog_client: Posthog | None = Depends(get_posthog_client),
    skip_cache: bool = False,
):
    """Process a lookup request."""
    # Initialize telemetry
    init_cache_stats()
    if skip_cache:
        set_skip_cache(True)
    telemetry = RequestTelemetry()

    try:
        response = await perform_lookup(
            request=request,
            db=db,
            discogs_service=discogs_service,
            telemetry=telemetry,
        )

        # Attach cache stats
        response.cache_stats = get_cache_stats()

        # Send telemetry
        if posthog_client:
            telemetry.send_to_posthog(
                posthog_client,
                {
                    "results_count": len(response.results),
                    "search_type": response.search_type,
                    "had_artist": bool(request.artist),
                    "had_album": bool(request.album),
                    "had_song": bool(request.song),
                },
            )

        return response

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Lookup failed: {e}")
        raise HTTPException(status_code=500, detail="Internal server error") from e
