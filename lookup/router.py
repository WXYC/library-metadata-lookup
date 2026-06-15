"""Lookup API router."""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from typing import TYPE_CHECKING, Any

import sentry_sdk
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import ValidationError
from wxyc_fastapi.observability import RequestTelemetry, get_cache_stats, init_cache_stats

from clients.streaming.apple_music import AppleMusicClient
from core.bulk_concurrency import (
    cancel_and_drain,
    max_concurrency_from_env,
    watch_disconnect,
)
from core.dependencies import (
    get_discogs_cache_service,
    get_discogs_service,
    get_library_db,
    get_musicbrainz_pg,
    get_posthog_client,
)
from discogs.cache_service import DiscogsCacheService
from discogs.memory_cache import set_skip_cache
from discogs.service import DiscogsService
from entity.sources import PgSource
from entity.store import EntityStore
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
from streaming.dependencies import get_apple_music_client

if TYPE_CHECKING:
    from posthog import Posthog

logger = logging.getLogger(__name__)

router = APIRouter(tags=["lookup"])

# 400 (not 413) for oversize is the endpoint's contract; sibling
# `/identity/bulk-resolve-libraries` chose 413 — keep them separate.
_BULK_LOOKUP_INPUT_CAP = 100

_BULK_LOOKUP_DEFAULT_CONCURRENCY = 10

_LML_CACHE_STATS_EXTRA_KEYS: tuple[str, ...] = (
    "memory_cache_inflight_join",
    "memory_cache_inflight_retry_after_cancel",
    "memory_cache_write_failed",
    "search_api_call_cap_fired",
)
"""LML-specific keys seeded into every request's cache_stats dict so PostHog
and Sentry payload shapes stay stable. Used at BOTH ``handle_lookup`` and
``handle_bulk_lookup`` so the two endpoints emit identical shapes. See
``init_cache_stats`` and LML#544 round 2 for the shape-stability rationale.
Adding a new key here is the single point of update; LML#543's
``search_api_call_cap_fired`` was the most recent addition."""

# Canonical full path for the bulk endpoint. Referenced by the explicit
# `http.server` span (name + `http.target` data field) so the two stay in
# lockstep; the FastAPI route decorator below uses the relative `/lookup/bulk`
# form because the router prefix `/api/v1` is applied at mount time.
_BULK_LOOKUP_ROUTE = "/api/v1/lookup/bulk"


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
    apple_music: AppleMusicClient | None = Depends(get_apple_music_client),
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
    # Pre-declare LML-specific cache-stats keys so PostHog/Sentry payload shapes
    # are stable even on requests with zero in-flight joins or zero L1 write
    # failures (LML#544 round 2). Without extra_keys, the keys would only
    # appear in payloads from the first request that records them.
    init_cache_stats(extra_keys=_LML_CACHE_STATS_EXTRA_KEYS)
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
            apple_music=apple_music,
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
    apple_music: AppleMusicClient | None = Depends(get_apple_music_client),
    skip_cache: bool = False,
    x_caller_budget_ms: int | None = Header(
        default=None,
        alias="X-Caller-Budget-Ms",
        description=(
            "Optional per-request budget in ms. Forwarded to each item's "
            "perform_lookup call; the per-item budget is independent of the "
            "batch (each item sees the same caller-supplied ceiling). The "
            "`LML#345 follow-up will redefine this as a batch-level budget."
        ),
    ),
) -> BulkLookupResponse:
    """Bulk lookup. See route docstring for protocol."""
    # Manual cap-check: 400 (not Pydantic's 422) for oversize batches.
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

    # Entry signal (LML#371). Fires synchronously before any awaits, so a
    # handler that later hangs past the caller's AbortController still leaves
    # a trace — uvicorn's access log and Sentry's automatic `http.server`
    # transaction only commit on response completion.
    logger.info("bulk lookup start: size=%d", len(request.items))

    # Honor the same `skip_cache` query flag the per-item route accepts. Backed
    # by a ContextVar, so setting once at the batch top propagates into each
    # `_run_one` task via the inherited task context.
    if skip_cache:
        set_skip_cache(True)

    # One cache_stats context for the whole batch: the in-process caches are
    # shared, so aggregate counters reflect the batch's behavior — the property
    # that motivates this endpoint.
    # Pre-declare LML-specific cache-stats keys so PostHog/Sentry payload shapes
    # are stable even on requests with zero in-flight joins or zero L1 write
    # failures (LML#544 round 2). Without extra_keys, the keys would only
    # appear in payloads from the first request that records them.
    init_cache_stats(extra_keys=_LML_CACHE_STATS_EXTRA_KEYS)

    max_concurrent = max_concurrency_from_env(_BULK_LOOKUP_DEFAULT_CONCURRENCY)
    semaphore = asyncio.Semaphore(max_concurrent)
    batch_telemetry = RequestTelemetry(
        api_call_keys=["discogs"],
        distinct_id="library-metadata-lookup-service",
        event_prefix="lookup.bulk",
    )

    async def _run_one(index: int, item: LookupRequest) -> BulkLookupResultItem:
        async with semaphore:
            # Per-item telemetry instance: required by perform_lookup's signature
            # and avoids the `_current_step` race that would happen with a shared
            # instance across concurrent items.
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
                        apple_music=apple_music,
                        caller_budget_ms=x_caller_budget_ms,
                    )
            except Exception as e:
                # Per-item isolation: one failure must not poison siblings.
                # The error class is surfaced for caller-side classification,
                # but the exception's str() may include SQL fragments, file
                # paths, or upstream error bodies — strip to the class name to
                # keep internals out of the response. Full traceback lands in
                # Sentry via `logger.exception`.
                logger.exception("bulk lookup item %d failed", index)
                return BulkLookupResultItem(
                    index=index,
                    status="error",
                    lookup=None,
                    message=type(e).__name__,
                )
            return BulkLookupResultItem(
                index=index,
                status="match" if lookup_response.results else "no_match",
                lookup=lookup_response,
            )

    # Explicit `http.server` span (LML#371). Defense-in-depth against the
    # FastApiIntegration's automatic transaction not landing for this endpoint
    # — that's the gap that left 22:21-22:34 on 2026-05-24 with 0 spans for 26
    # in-flight bulk requests despite the handler running. With this wrap, any
    # Sentry trace explorer query for `op:http.server span.description:*lookup/bulk*`
    # will surface bulk-route traffic.
    with sentry_sdk.start_span(op="http.server", name=f"POST {_BULK_LOOKUP_ROUTE}") as http_span:
        http_span.set_data("http.method", "POST")
        http_span.set_data("http.target", _BULK_LOOKUP_ROUTE)
        http_span.set_data("lml.bulk.size", len(request.items))
        http_span.set_data("lml.bulk.max_concurrent", max_concurrent)

        with sentry_sdk.start_span(op="lml.bulk.batch", name=f"{len(request.items)} items") as span:
            span.set_data("lml.bulk.size", len(request.items))
            span.set_data("lml.bulk.max_concurrent", max_concurrent)

            # Race the gather against a client-disconnect sentinel. If the
            # client departs first, cancel the gather so Discogs semaphore
            # permits free for the next caller; without this, queue depth grows
            # monotonically across batches.
            #
            # Spawning the sentinel must happen *after* `await
            # http_request.json()` above has fully consumed the request body —
            # watch_disconnect awaits `request.receive()`, which would
            # otherwise swallow `http.request` body messages the body parser
            # needs.
            gather_future = asyncio.gather(
                *(_run_one(i, item) for i, item in enumerate(request.items))
            )
            sentinel_task = asyncio.create_task(
                watch_disconnect(http_request),
                name="lml.bulk.disconnect_sentinel",
            )
            waitables: set[asyncio.Future[Any]] = {gather_future, sentinel_task}

            try:
                done, _pending = await asyncio.wait(waitables, return_when=asyncio.FIRST_COMPLETED)
            except BaseException:
                # Parent task cancellation must propagate to both children AND
                # the children must be drained, not just signalled — otherwise
                # the semaphore permits this PR exists to release are still
                # held while the cancellation propagates asynchronously.
                await cancel_and_drain(gather_future)
                await cancel_and_drain(sentinel_task)
                raise

            client_aborted = sentinel_task in done and not gather_future.done()
            span.set_data("lml.bulk.client_aborted", client_aborted)

            if client_aborted:
                # Tag is global-scope (filterable across all routes); span data
                # carries the bulk-specific context. Key-name asymmetry
                # intentional.
                sentry_sdk.set_tag("lml.client_aborted", "true")
                await cancel_and_drain(gather_future)
                # Exit log fires for the abort branch too — operators see the
                # batch size + abort reason without digging into Sentry.
                logger.warning(
                    "bulk lookup aborted by client: size=%d max_concurrent=%d",
                    len(request.items),
                    max_concurrent,
                )
                http_span.set_data("http.status_code", 499)
                # 499 = Nginx "client closed request". Skip PostHog: partial
                # counts would skew batch-completion analytics.
                raise HTTPException(status_code=499, detail="client disconnected")

            await cancel_and_drain(sentinel_task)
            results = gather_future.result()

        _project_cache_stats_to_transaction(get_cache_stats())

        counts = Counter(r.status for r in results)

        if posthog_client:
            batch_telemetry.send_to_posthog(
                posthog_client,
                {
                    "batch_size": len(request.items),
                    "match_count": counts["match"],
                    "no_match_count": counts["no_match"],
                    "error_count": counts["error"],
                    "max_concurrent": max_concurrent,
                },
            )

        # Exit signal pairs with the entry log so operators can confirm
        # response delivery and read off the status breakdown without
        # correlating to a Sentry trace.
        logger.info(
            "bulk lookup complete: size=%d match=%d no_match=%d error=%d",
            len(request.items),
            counts["match"],
            counts["no_match"],
            counts["error"],
        )
        http_span.set_data("http.status_code", 200)

    return BulkLookupResponse(results=results)
