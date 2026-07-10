"""Lookup API router."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter
from typing import TYPE_CHECKING, Any

import sentry_sdk
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import ValidationError
from wxyc_fastapi.observability import (
    RequestTelemetry,
    get_cache_stats,
    get_cache_stats_recorder,
    init_cache_stats,
)

from clients.bandcamp import BandcampClient
from clients.streaming.apple_music import AppleMusicClient
from clients.streaming.spotify import SpotifyClient
from config.settings import get_settings
from core.bulk_concurrency import (
    acquire_bulk_global_permit,
    cancel_and_drain,
    max_concurrency_from_env,
    watch_disconnect,
)
from core.dependencies import (
    get_discogs_cache_pg,
    get_discogs_cache_service,
    get_discogs_service,
    get_library_db,
    get_musicbrainz_pg,
    get_posthog_client,
)
from core.search import SEARCH_API_CALL_CAP_FIRED_STAT_KEY, resolve_positive_int_env
from discogs.cache_service import DiscogsCacheService
from discogs.memory_cache import set_skip_cache
from discogs.service import DiscogsService
from entity.release_resolution_cache import (
    RELEASE_RESOLUTION_CACHE_HIT_STAT_KEY,
    RELEASE_RESOLUTION_CACHE_MISS_STAT_KEY,
    RELEASE_RESOLUTION_CACHE_UNAVAILABLE_STAT_KEY,
)
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
from lookup.rowless import NONLIBRARY_RELEASE_SURFACED_STAT_KEY
from lookup.streaming_url_postprocess import set_suppress_streaming_warm
from streaming.dependencies import (
    get_apple_music_client,
    get_bandcamp_client,
    get_spotify_client,
)

if TYPE_CHECKING:
    from posthog import Posthog

logger = logging.getLogger(__name__)

router = APIRouter(tags=["lookup"])

# 400 (not 413) for oversize is the endpoint's contract; sibling
# `/identity/bulk-resolve-libraries` chose 413 — keep them separate.
_BULK_LOOKUP_INPUT_CAP = 100

_BULK_LOOKUP_DEFAULT_CONCURRENCY = 10

# Process-wide cap on concurrent single `/lookup` requests (LML#706 PR3). The
# #706 collapse compounded because single `/lookup` had NO in-flight bound:
# cold requests piled up (Little's law), each holding the one event loop
# through seconds of external I/O and contending for the 5-connection asyncpg
# pool, inflating even trivial PG spans to tens of seconds. Excess requests
# QUEUE on the semaphore — no 429/503 shedding; callers see latency, never a
# new error mode. Deliberately separate from `LML_BULK_MAX_CONCURRENT`: the
# bulk knob bounds items INSIDE one batch (per-request semaphore); this one
# bounds SINGLE-`/lookup` requests across the process. Scope note: the
# bulk-family consumers (`/lookup/bulk`, identity bulk-resolve, cache
# refresh) are bounded by their own cross-request budget, the LML#716 global
# permit (`LML_BULK_GLOBAL_MAX_CONCURRENT` in core/bulk_concurrency.py) —
# peak discogs-pool contention when both gates saturate is
# LML_LOOKUP_MAX_CONCURRENT + LML_BULK_GLOBAL_MAX_CONCURRENT, so size the
# pool against that SUM. `/streaming-check` sits under neither cap (it
# borrows no pool connection); its loop-time residual is LML#753.
# Default 8: above the warm path's needs (~5 ms hits never stack
# that deep at production arrival rates) and low enough that a single-lookup
# cold storm can't re-enter the pool-starvation regime.
_LOOKUP_MAX_CONCURRENT_ENV_VAR = "LML_LOOKUP_MAX_CONCURRENT"
_LOOKUP_DEFAULT_MAX_CONCURRENT = 8

_lookup_semaphore: asyncio.Semaphore | None = None
"""Lazily constructed on the first request — NOT because a semaphore needs a
running loop (3.10+ binds a loop only at the first contended acquire), but so
the env is read at request time (the no-redeploy Railway lever, mirroring the
LML#706 streaming-warm semaphore) and so tests can reset the global between
event loops (the suite-wide autouse fixture in ``tests/conftest.py``)."""


def _get_lookup_semaphore() -> asyncio.Semaphore:
    """Lazily build the process-global `/lookup` in-flight semaphore.

    Sized from ``LML_LOOKUP_MAX_CONCURRENT`` (read once, at first
    construction; unparseable/zero/negative values WARN and fall back — a 0
    cap would deadlock every request forever).
    """
    global _lookup_semaphore
    if _lookup_semaphore is None:
        _lookup_semaphore = asyncio.Semaphore(
            resolve_positive_int_env(_LOOKUP_MAX_CONCURRENT_ENV_VAR, _LOOKUP_DEFAULT_MAX_CONCURRENT)
        )
    return _lookup_semaphore


def _project_inflight_capped(wait_ms: float) -> None:
    """Project cap engagement onto Sentry for a request that queued.

    Two channels, following the LML#683 lesson (recorded on
    ``_project_cache_stats_to_transaction`` below): ``set_data`` alone reads
    back as "Unknown attribute" in the spans dataset, so it can't back a
    query or an alert. Therefore:

    * ``sentry_sdk.set_tag("lml.lookup.inflight_capped", "true")`` — the
      filterable engagement flag (mirrors ``lml.client_aborted``). Set only
      on requests that found the cap saturated, so uncontended traffic stays
      untagged.
    * ``set_measurement("lml.lookup.inflight_wait_ms", ...)`` — the
      quantitative series (p95 queue wait) that decides whether the default
      cap of 8 is tuned right.

    Observability must not break the request path; failures log and continue.
    """
    try:
        sentry_sdk.set_tag("lml.lookup.inflight_capped", "true")
        scope = sentry_sdk.get_current_scope()
        if scope.transaction is not None:
            scope.transaction.set_measurement("lml.lookup.inflight_wait_ms", wait_ms)
            scope.transaction.set_data("lml.lookup.inflight_wait_ms", wait_ms)
    except Exception as e:
        logger.warning("Failed to project inflight_capped onto Sentry transaction: %s", e)


# LML#681 flag-tag keys. Recorded once per cache_stats context at the router
# (see ``_record_lml_flag_tags``) as a clean 0/1, so flag-on vs flag-off is
# sliceable in both PostHog and the Sentry transaction. The key names mirror the
# Settings field names so the dimension is self-describing.
LML_RESOLVE_NONLIBRARY_RELEASE_STAT_KEY = "lml_resolve_nonlibrary_release"
LML_RESOLVE_COMPILATION_RELEASE_STAT_KEY = "lml_resolve_compilation_release"

_LML_CACHE_STATS_EXTRA_KEYS: tuple[str, ...] = (
    "memory_cache_inflight_join",
    "memory_cache_inflight_retry_after_cancel",
    "memory_cache_write_failed",
    SEARCH_API_CALL_CAP_FIRED_STAT_KEY,
    # LML#681 pre-flip observability for LML_RESOLVE_NONLIBRARY_RELEASE.
    LML_RESOLVE_NONLIBRARY_RELEASE_STAT_KEY,
    LML_RESOLVE_COMPILATION_RELEASE_STAT_KEY,
    NONLIBRARY_RELEASE_SURFACED_STAT_KEY,
    RELEASE_RESOLUTION_CACHE_HIT_STAT_KEY,
    RELEASE_RESOLUTION_CACHE_MISS_STAT_KEY,
    RELEASE_RESOLUTION_CACHE_UNAVAILABLE_STAT_KEY,
)
"""LML-specific keys seeded into every request's cache_stats dict so PostHog
and Sentry payload shapes stay stable. Used at BOTH ``handle_lookup`` and
``handle_bulk_lookup`` so the two endpoints emit identical shapes. See
``init_cache_stats`` and LML#544 round 2 for the shape-stability rationale.
Adding a new key here is the single point of update; LML#681's row-less flag
observability (flag tags, ``nonlibrary_release_surfaced``, the #632
hit/miss/unavailable counters) was the most recent addition."""


def _record_lml_flag_tags() -> None:
    """Record the row-less-family feature-flag state as 0/1 cache_stats keys.

    Called once per ``cache_stats`` context, right after ``init_cache_stats`` at
    BOTH ``handle_lookup`` and ``handle_bulk_lookup`` (LML#681). Recording once
    at the router — reading the process-constant flag — keeps the tag a clean
    ``0``/``1`` even on ``/lookup/bulk``, where one ``cache_stats`` context is
    shared across the whole batch and ``record`` is additive-only: recording the
    flag per-request from inside the orchestrator would sum to the batch size.

    Observability must not break the request path, so any recorder/SDK exception
    is swallowed (matches ``_project_cache_stats_to_transaction``). ``record`` is
    a no-op when ``init_cache_stats`` wasn't called for the context.
    """
    try:
        settings = get_settings()
        recorder = get_cache_stats_recorder()
        recorder.record(
            LML_RESOLVE_NONLIBRARY_RELEASE_STAT_KEY,
            1 if settings.lml_resolve_nonlibrary_release else 0,
        )
        recorder.record(
            LML_RESOLVE_COMPILATION_RELEASE_STAT_KEY,
            1 if settings.lml_resolve_compilation_release else 0,
        )
    except Exception as e:
        logger.warning("Failed to record LML flag tags into cache_stats: %s", e)


# Canonical full path for the bulk endpoint. Referenced by the explicit
# `http.server` span (name + `http.target` data field) so the two stay in
# lockstep; the FastAPI route decorator below uses the relative `/lookup/bulk`
# form because the router prefix `/api/v1` is applied at mount time.
_BULK_LOOKUP_ROUTE = "/api/v1/lookup/bulk"


def _project_cache_stats_to_transaction(stats: dict | None) -> None:
    """Attach numeric cache_stats fields to the current Sentry transaction.

    Each field is attached two ways, because the two serve different consumers:

    - ``set_data`` — a ``lml.cache.<key>`` span-data attribute, visible when you
      open a single trace in Sentry's trace explorer (per-request drill-down,
      alongside latency/status).
    - ``set_measurement`` — a ``lml.cache.<key>`` transaction *measurement*. Span
      ``data`` set via ``set_data`` is opaque to the spans/metrics datasets (it
      reads back as "Unknown attribute"), so it cannot back a metric alert.
      Measurements are aggregatable (avg/percentile/threshold) and are what the
      LML#683 row-less-flag degradation alerts query (e.g. the Discogs call-rate
      guard on ``lml.cache.api_calls``). traces_sample_rate is 1.0 (see
      ``init_sentry`` in ``main.py``), so the measurement series covers every
      request, not a sampled fraction.

    No-op when there is no active transaction (Sentry not initialized, or call
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
                transaction.set_measurement(f"lml.cache.{key}", value)
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
    discogs_cache_pg: PgSource | None = Depends(get_discogs_cache_pg),
    posthog_client: Posthog | None = Depends(get_posthog_client),
    apple_music: AppleMusicClient | None = Depends(get_apple_music_client),
    spotify: SpotifyClient | None = Depends(get_spotify_client),
    bandcamp: BandcampClient | None = Depends(get_bandcamp_client),
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
    # LML#681: tag the per-request payload with the row-less-family flag state
    # (0/1) once per context so the flip is sliceable in PostHog/Sentry.
    _record_lml_flag_tags()
    if skip_cache:
        set_skip_cache(True)

    try:
        # LML#706 PR3: bound in-flight lookups process-wide. On this repo's
        # Python (>=3.12) the `locked()` pre-check is EXACT, not racy: there is
        # no await between the check and the acquire (one event-loop slice),
        # and 3.12's `locked()` returns True when the value is exhausted OR
        # waiters exist — the same predicate `acquire()` uses to decide whether
        # to park. So `capped_on_arrival` ⇔ this request actually queued.
        semaphore = _get_lookup_semaphore()
        capped_on_arrival = semaphore.locked()
        wait_start = time.perf_counter()
        async with semaphore:
            if capped_on_arrival:
                wait_ms = (time.perf_counter() - wait_start) * 1000.0
                _project_inflight_capped(wait_ms)
                # Debit the queue wait from the caller's budget so the A8 /
                # LML#345 contract ("LML returns slightly before the caller
                # times out") holds under saturation — the pipeline's budget
                # clock only starts inside perform_lookup. Floor at 1: the
                # budget resolver treats non-positive as "unset → env
                # default", which would hand the MOST delayed caller the
                # LARGEST budget. A 1ms budget short-circuits the pipeline
                # almost immediately — the cheap outcome the (likely already
                # timed-out) caller would want.
                if x_caller_budget_ms is not None and x_caller_budget_ms > 0:
                    x_caller_budget_ms = max(1, x_caller_budget_ms - int(wait_ms))
            # Constructed inside the permit so telemetry's total-duration
            # series keeps meaning "lookup work", not "queue wait + work" —
            # the wait is reported separately via the Sentry measurement.
            telemetry = RequestTelemetry(
                api_call_keys=["discogs"],
                distinct_id="library-metadata-lookup-service",
                event_prefix="lookup",
            )
            response = await perform_lookup(
                request=request,
                db=db,
                discogs_service=discogs_service,
                telemetry=telemetry,
                entity_store=entity_store,
                discogs_cache=discogs_cache,
                mb_pg=mb_pg,
                apple_music=apple_music,
                spotify=spotify,
                bandcamp=bandcamp,
                discogs_cache_pg=discogs_cache_pg,
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

    Per-item ``extended`` is honored (LML#685): each item is a full
    ``LookupRequest`` forwarded intact to ``perform_lookup`` (sole exception:
    the ``warm_cache`` pin below), so an item with
    ``extended: true`` gets the same extended-only ``DiscogsMatchResult`` fields
    on its top-1 match as the single ``/lookup`` route — byte-identical contract
    (the 8 ``album_metadata`` columns BS#1442 backfills — ``discogs_artist_id``,
    ``label``, ``full_release_date``, ``genres``, ``styles``, ``tracklist``,
    ``artist_image_url``, ``profile_tokens`` — plus ``writer_credits``, LML#699).

    ``extended`` is Discogs-ceiling-safe: it is CACHE-ONLY on this path. It
    plucks fields from the release/artist objects ``fetch_top1_release_details``
    already fetched (which run regardless of ``extended``, feeding the base
    ``release_year`` / ``artist_bio`` / ``wikipedia_url`` scalars), plus a
    cache-only bio parse and a local MusicBrainz PG query — never a new Discogs
    fetch. So flipping ``extended`` on adds ZERO incremental Discogs calls.

    Note the one bio-warm fan-out is gated on the SEPARATE ``warm_cache`` flag,
    NOT ``extended``: ``warm_cache: true`` schedules ``_warm_bio_cache``, which
    fires per-bio-ref live Discogs calls. On this path the flag is hard-pinned
    off (LML#742) — a per-item ``warm_cache: true`` is discarded (with a
    server-side warning log) before the item reaches ``perform_lookup``, same
    enforced guarantee as the ``bandcamp`` / ``allow_release_resolution_fallback``
    pins. Callers that want the bio cache populated at bulk scale should use
    the offline warmer (#548).
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
    discogs_cache_pg: PgSource | None = Depends(get_discogs_cache_pg),
    posthog_client: Posthog | None = Depends(get_posthog_client),
    apple_music: AppleMusicClient | None = Depends(get_apple_music_client),
    spotify: SpotifyClient | None = Depends(get_spotify_client),
    # No Bandcamp dependency on the bulk path — the Bandcamp leg is disabled
    # here (bandcamp=None at the perform_lookup call below), so resolving its
    # client would be dead work. See the call site for the rate-limit rationale.
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

    # Bulk does streaming-URL cache read-fill ONLY — never spawns the background
    # warm (LML#706). A 35k-album drain returns fast per item, so an enqueued
    # warm tail would decouple from the request and starve the live /lookup
    # path's own warms. Same ContextVar-propagation mechanism as `skip_cache`,
    # and the same posture as `bandcamp=None` / `allow_release_resolution_fallback
    # =False` below: the offline warmer (#548) is the right tier for bulk fill.
    set_suppress_streaming_warm(True)

    # One cache_stats context for the whole batch: the in-process caches are
    # shared, so aggregate counters reflect the batch's behavior — the property
    # that motivates this endpoint.
    # Pre-declare LML-specific cache-stats keys so PostHog/Sentry payload shapes
    # are stable even on requests with zero in-flight joins or zero L1 write
    # failures (LML#544 round 2). Without extra_keys, the keys would only
    # appear in payloads from the first request that records them.
    init_cache_stats(extra_keys=_LML_CACHE_STATS_EXTRA_KEYS)
    # LML#681: record the flag tag once for the whole batch (shared context),
    # so the bulk value stays 0/1 instead of summing across items.
    _record_lml_flag_tags()

    max_concurrent = max_concurrency_from_env(_BULK_LOOKUP_DEFAULT_CONCURRENCY)
    semaphore = asyncio.Semaphore(max_concurrent)
    batch_telemetry = RequestTelemetry(
        api_call_keys=["discogs"],
        distinct_id="library-metadata-lookup-service",
        event_prefix="lookup.bulk",
    )

    async def _run_one(index: int, item: LookupRequest) -> BulkLookupResultItem:
        # Batch semaphore OUTER, global permit INNER — the one consistent
        # order every bulk-family dispatcher uses (LML#716). The per-batch
        # semaphore bounds items inside THIS request; the global permit is
        # the cross-request budget shared with identity bulk-resolve and
        # cache refresh, so N concurrent batches can't multiply into N x
        # LML_BULK_MAX_CONCURRENT in-flight items against the shared pool.
        async with semaphore, acquire_bulk_global_permit():
            # Hard-pin warm_cache off (LML#742), completing the set with
            # `bandcamp=None` / `allow_release_resolution_fallback=False` below:
            # a truthy per-item flag would schedule the `_warm_bio_cache`
            # per-bio-ref live Discogs fan-out for every batch item. Unlike its
            # two siblings, warm_cache is read from the request *inside*
            # perform_lookup (whose signature is frozen for LML#722), so the pin
            # is a copied item rather than a kwarg. The offline warmer (#548)
            # remains the right tier for bulk cache population.
            if item.warm_cache:
                # Warn per pinned item: unlike the sibling pins (server-side
                # kwargs), this discards a caller-supplied field, and post-pin
                # the Sentry `lml.lookup.warm_cache` tag records the pinned
                # False — without this line a misconfigured caller is
                # indistinguishable from a compliant one in every telemetry
                # surface. Volume is bounded by the batch cap, and only misuse
                # triggers it.
                logger.warning(
                    "bulk item %d: per-item warm_cache=true pinned off (LML#742); "
                    "use the offline warmer (#548) for bulk bio-cache population",
                    index,
                )
                item = item.model_copy(update={"warm_cache": False})
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
                        spotify=spotify,
                        # Bandcamp is intentionally NOT forwarded on the bulk
                        # path (LML#573 PR-3): its client is rate-limited to
                        # 1 req/s, so a per-item live album probe would serialize
                        # the 35k-album drain into hours of requests against
                        # Bandcamp (and starve the shared singleton for the live
                        # /lookup path). Passing None makes the post-process skip
                        # the Bandcamp leg here; the search-URL fallback still
                        # applies, and the offline warmer (#548) is the right tier
                        # for bulk Bandcamp cache population.
                        bandcamp=None,
                        discogs_cache_pg=discogs_cache_pg,
                        caller_budget_ms=x_caller_budget_ms,
                        # The 35k-album bulk drain must never trigger the LML#604
                        # lazy release-resolution fan-out (per-row Discogs probe).
                        allow_release_resolution_fallback=False,
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
