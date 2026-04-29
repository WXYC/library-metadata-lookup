"""Lookup API router."""

import logging

import httpx
import sentry_sdk
from fastapi import APIRouter, Depends, HTTPException
from posthog import Posthog

from core.dependencies import (
    get_apple_music_http_client,
    get_discogs_cache_service,
    get_discogs_service,
    get_library_db,
    get_musicbrainz_pg,
    get_posthog_client,
)
from core.telemetry import RequestTelemetry, get_cache_stats, init_cache_stats
from discogs.cache_service import DiscogsCacheService
from discogs.memory_cache import set_skip_cache
from discogs.service import DiscogsService
from identity.dependencies import get_entity_store
from library.db import LibraryDB
from lookup.models import LookupRequest, LookupResponse
from lookup.orchestrator import perform_lookup
from scripts.entity_resolution.sources import PgSource
from scripts.entity_resolution.store import EntityStore

logger = logging.getLogger(__name__)

router = APIRouter(tags=["lookup"])


def _project_cache_stats_to_transaction(stats: dict | None) -> None:
    """Attach numeric cache_stats fields to the current Sentry transaction.

    Each field becomes a `lml.cache.<key>` data attribute on the transaction
    so the data joins against the trace in Sentry's trace explorer. No-op
    when there is no active transaction (Sentry not initialized, or call
    happening outside a request span).

    Non-numeric values (strings, None) are skipped — the cache_stats schema
    is all-numeric today, but keep the projection defensive in case that
    ever drifts.
    """
    if not stats:
        return
    transaction = sentry_sdk.get_current_scope().transaction
    if transaction is None:
        return
    for key, value in stats.items():
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            transaction.set_data(f"lml.cache.{key}", value)


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

    The caller (request-o-matic) handles parsing and Slack posting.
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
    discogs_cache: DiscogsCacheService | None = Depends(get_discogs_cache_service),
    mb_pg: PgSource | None = Depends(get_musicbrainz_pg),
    entity_store: EntityStore | None = Depends(get_entity_store),
    posthog_client: Posthog | None = Depends(get_posthog_client),
    http_client: httpx.AsyncClient = Depends(get_apple_music_http_client),
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
            entity_store=entity_store,
            discogs_cache=discogs_cache,
            mb_pg=mb_pg,
            http_client=http_client,
        )

        # Attach cache stats
        response.cache_stats = get_cache_stats()

        # Project cache_stats onto the active Sentry transaction so it joins
        # against the trace in Sentry's trace explorer (alongside latency,
        # status, etc.). No-op when there is no active transaction (no
        # SENTRY_DSN configured, or running outside a request span).
        _project_cache_stats_to_transaction(response.cache_stats)

        # Send telemetry
        if posthog_client:
            results = response.results or []
            telemetry.send_to_posthog(
                posthog_client,
                {
                    "results_count": len(results),
                    "search_type": response.search_type,
                    "had_artist": bool(request.artist),
                    "had_album": bool(request.album),
                    "had_song": bool(request.song),
                    "reconciled_identity_count": sum(
                        1 for r in results if r.reconciled_identity is not None
                    ),
                },
            )

        return response

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Lookup failed: {e}")
        raise HTTPException(status_code=500, detail="Internal server error") from e
