"""Lookup API router."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING

import httpx
import sentry_sdk
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import ValidationError
from wxyc_fastapi.observability import RequestTelemetry, get_cache_stats, init_cache_stats

from core.dependencies import (
    get_apple_music_http_client,
    get_discogs_cache_service,
    get_discogs_service,
    get_library_db,
    get_musicbrainz_pg,
    get_posthog_client,
)
from discogs.cache_service import DiscogsCacheService
from discogs.memory_cache import set_skip_cache
from discogs.service import DiscogsService
from generated.api_models import CacheStats
from identity.dependencies import get_entity_store
from library.db import LibraryDB
from lookup.models import (
    BulkLookupRequest,
    BulkLookupResponse,
    BulkLookupResultItem,
    LookupRequest,
    LookupResponse,
)
from lookup.orchestrator import perform_lookup
from scripts.entity_resolution.sources import PgSource
from scripts.entity_resolution.store import EntityStore

if TYPE_CHECKING:
    from posthog import Posthog

logger = logging.getLogger(__name__)

router = APIRouter(tags=["lookup"])

# LML#368: bulk endpoint accepts 1-100 items per request. Above the cap we
# return 400 (per the ticket's acceptance criteria — not 413, which the
# `/identity/bulk-resolve-libraries` precedent uses for its 1000-cap). The
# cap is constant; if it needs to grow we change it here.
_BULK_LOOKUP_INPUT_CAP = 100

# Default in-flight concurrency cap for bulk items. Tunable via env so we can
# react to per-deploy hardware without a code change. Bounded at the floor so
# accidental `0` or negative values can't hang the request.
_BULK_LOOKUP_DEFAULT_CONCURRENCY = 10


def _project_cache_stats_to_transaction(stats: dict | None) -> None:
    """Attach numeric cache_stats fields to the current Sentry transaction.

    Each field becomes a `lml.cache.<key>` data attribute on the transaction
    so the data joins against the trace in Sentry's trace explorer. No-op
    when there is no active transaction (Sentry not initialized, or call
    happening outside a request span).

    Non-numeric values (strings, None) are skipped — the cache_stats schema
    is all-numeric today, but keep the projection defensive in case that
    ever drifts.

    Observability must not break the request path: any exception raised by
    the Sentry SDK or by an unexpected stats shape is caught and logged at
    WARNING. The request keeps going.
    """
    if not stats:
        return
    try:
        transaction = sentry_sdk.get_current_scope().transaction
        if transaction is None:
            return
        for key, value in stats.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                transaction.set_data(f"lml.cache.{key}", value)
    except Exception as e:
        logger.warning("Failed to project cache_stats onto Sentry transaction: %s", e)


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
    x_caller_budget_ms: int | None = Header(
        default=None,
        alias="X-Caller-Budget-Ms",
        description=(
            "Optional per-request budget in ms (A8 / LML#345). When set, the search "
            "pipeline uses min(header − transport overhead, LML_SEARCH_BUDGET_MS) "
            "as its wall-clock cutoff so LML returns slightly before the caller "
            "times out. Non-positive values fall back to the env default."
        ),
    ),
):
    """Process a lookup request."""
    init_cache_stats()
    if skip_cache:
        set_skip_cache(True)
    telemetry = RequestTelemetry(
        api_call_keys=["discogs"],
        distinct_id="library-metadata-lookup-service",
        event_prefix="lookup",
    )

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
            caller_budget_ms=x_caller_budget_ms,
        )

        # Attach cache stats. wxyc-shared#86 has shipped the typed CacheStats
        # Pydantic model, so the response field is no longer dict-shaped —
        # convert before assignment. Keep the raw dict around for the Sentry
        # projection (which iterates via `.items()`).
        stats = get_cache_stats()
        response.cache_stats = CacheStats(**stats) if stats else None

        # Project cache_stats onto the active Sentry transaction so it joins
        # against the trace in Sentry's trace explorer (alongside latency,
        # status, etc.). No-op when there is no active transaction (no
        # SENTRY_DSN configured, or running outside a request span).
        _project_cache_stats_to_transaction(stats)

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


def _max_concurrency_from_env() -> int:
    """Resolve the bulk-item concurrency cap from env, floored at 1."""
    raw = os.getenv("LML_BULK_MAX_CONCURRENT")
    if raw is None or raw == "":
        return _BULK_LOOKUP_DEFAULT_CONCURRENCY
    try:
        return max(1, int(raw))
    except ValueError:
        logger.warning(
            "Invalid LML_BULK_MAX_CONCURRENT=%r, falling back to %d",
            raw,
            _BULK_LOOKUP_DEFAULT_CONCURRENCY,
        )
        return _BULK_LOOKUP_DEFAULT_CONCURRENCY


@router.post(
    "/lookup/bulk",
    response_model=BulkLookupResponse,
    summary="Bulk variant of /lookup — amortizes cold-cache cost across items",
    description="""
    Runs ``perform_lookup`` for each item in the request body under a bounded
    semaphore (env: ``LML_BULK_MAX_CONCURRENT``, default 10). Results are
    returned in input order. Per-item failures are isolated and surfaced as
    ``status: error`` so one item cannot poison the batch.

    Amortizes per-row cold-cache cost two ways: one HTTP roundtrip per N
    items instead of N roundtrips, and the in-process TTL/LRU caches stay
    warm across all N items in a batch.
    """,
    responses={
        200: {"description": "Per-item verdicts in input order."},
        400: {"description": f"Malformed body or batch above {_BULK_LOOKUP_INPUT_CAP}-item cap."},
        422: {"description": "Request body failed Pydantic validation (e.g. empty items array)."},
    },
)
async def handle_bulk_lookup(
    http_request: Request,
    db: LibraryDB = Depends(get_library_db),
    discogs_service: DiscogsService | None = Depends(get_discogs_service),
    discogs_cache: DiscogsCacheService | None = Depends(get_discogs_cache_service),
    mb_pg: PgSource | None = Depends(get_musicbrainz_pg),
    entity_store: EntityStore | None = Depends(get_entity_store),
    posthog_client: Posthog | None = Depends(get_posthog_client),
    http_client: httpx.AsyncClient = Depends(get_apple_music_http_client),
    x_caller_budget_ms: int | None = Header(
        default=None,
        alias="X-Caller-Budget-Ms",
        description=(
            "Optional per-request budget in ms. Forwarded to each item's "
            "perform_lookup call; the per-item budget is independent of the "
            "batch (each item sees the same caller-supplied ceiling)."
        ),
    ),
) -> BulkLookupResponse:
    """Bulk lookup. See route docstring for protocol."""
    # Manual parse + cap-check so oversize gets 400 (per LML#368 acceptance
    # criteria) rather than the 422 Pydantic would emit via max_length.
    try:
        body = await http_request.json()
    except (ValueError, TypeError) as e:
        raise HTTPException(status_code=400, detail=f"Malformed JSON body: {e}") from None
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object.")
    items_raw = body.get("items")
    if not isinstance(items_raw, list):
        raise HTTPException(status_code=422, detail="`items` must be a JSON array.")
    if len(items_raw) > _BULK_LOOKUP_INPUT_CAP:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Batch exceeded the {_BULK_LOOKUP_INPUT_CAP}-item cap (received {len(items_raw)})."
            ),
        )

    try:
        request = BulkLookupRequest.model_validate(body)
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=e.errors()) from None

    # One cache_stats context for the whole batch — the in-process caches are
    # shared, so cumulative hit/miss counters reflect the batch's aggregate
    # behavior (the property that motivates this endpoint).
    init_cache_stats()

    max_concurrent = _max_concurrency_from_env()
    semaphore = asyncio.Semaphore(max_concurrent)

    async def _run_one(index: int, item: LookupRequest) -> BulkLookupResultItem:
        async with semaphore:
            telemetry = RequestTelemetry(
                api_call_keys=["discogs"],
                distinct_id="library-metadata-lookup-service",
                event_prefix="lookup.bulk",
            )
            try:
                with sentry_sdk.start_span(op="lml.bulk.item", name=f"item {index}"):
                    lookup_response = await perform_lookup(
                        request=item,
                        db=db,
                        discogs_service=discogs_service,
                        telemetry=telemetry,
                        entity_store=entity_store,
                        discogs_cache=discogs_cache,
                        mb_pg=mb_pg,
                        http_client=http_client,
                        caller_budget_ms=x_caller_budget_ms,
                    )
            except Exception as e:
                # Per-item isolation: one failure must not poison siblings.
                # Log the traceback for observability but surface only the
                # exception class + message to the caller — no internals.
                logger.exception("bulk lookup item %d failed", index)
                return BulkLookupResultItem(
                    index=index,
                    status="error",
                    lookup=None,
                    message=f"{type(e).__name__}: {e}",
                )
            has_results = bool(lookup_response.results)
            return BulkLookupResultItem(
                index=index,
                status="match" if has_results else "no_match",
                lookup=lookup_response,
            )

    with sentry_sdk.start_span(op="lml.bulk.batch", name=f"{len(request.items)} items") as span:
        if span is not None:
            span.set_data("lml.bulk.size", len(request.items))
            span.set_data("lml.bulk.max_concurrent", max_concurrent)
        results = await asyncio.gather(*(_run_one(i, item) for i, item in enumerate(request.items)))

    # Project the batch's aggregate cache_stats onto the active Sentry
    # transaction so the join against the trace works the same as per-item
    # /lookup. Sentry SDK errors must not break the request path.
    stats = get_cache_stats()
    _project_cache_stats_to_transaction(stats)

    # Telemetry: one PostHog event for the batch summarizing per-item outcomes.
    if posthog_client:
        match_count = sum(1 for r in results if r.status == "match")
        no_match_count = sum(1 for r in results if r.status == "no_match")
        error_count = sum(1 for r in results if r.status == "error")
        try:
            posthog_client.capture(
                "lookup.bulk",
                distinct_id="library-metadata-lookup-service",
                properties={
                    "batch_size": len(request.items),
                    "match_count": match_count,
                    "no_match_count": no_match_count,
                    "error_count": error_count,
                    "max_concurrent": max_concurrent,
                },
            )
        except Exception:
            logger.warning("PostHog capture failed for lookup.bulk", exc_info=True)

    return BulkLookupResponse(results=results)
