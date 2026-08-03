"""Lookup API router."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter
from typing import TYPE_CHECKING

import sentry_sdk
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from wxyc_fastapi.observability import (
    RequestTelemetry,
    get_cache_stats,
    get_cache_stats_recorder,
    init_cache_stats,
)

from clients.bandcamp import BandcampClient
from clients.streaming.apple_music import AppleMusicClient
from clients.streaming.spotify import SpotifyClient
from config.settings import Settings, get_settings
from core.bulk_body import parse_bulk_body
from core.bulk_concurrency import (
    ClientDisconnectedWhileQueuedError,
    acquire_bulk_global_permit,
    cancel_and_drain,
    is_low_priority_caller_class,
    max_concurrency_from_env,
    maybe_acquire_bulk_global_permit_or_reap,
    resolve_caller_class,
    run_bulk_gather,
    watch_disconnect,
)
from core.dependencies import (
    discogs_pool_max_size,
    get_discogs_cache_pg,
    get_discogs_cache_service,
    get_discogs_service,
    get_library_db,
    get_musicbrainz_pg,
    get_posthog_client,
)
from core.event_loop_lag import get_event_loop_lag_ms
from core.observability import observability_guard, project_capped
from core.search import SEARCH_API_CALL_CAP_FIRED_STAT_KEY, resolve_positive_int_env
from discogs.cache_service import DiscogsCacheService
from discogs.memory_cache import set_skip_cache
from discogs.ratelimit import set_discogs_low_priority
from discogs.service import (
    BREAKER_OPEN_STAT_KEY,
    DISCOGS_BULK_RESERVED_CAPPED_STAT_KEY,
    DiscogsService,
)
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
from lookup.admission import ADMISSION_WOULD_SHED_STAT_KEY
from lookup.caller_reason import record_caller_reason_tag
from lookup.endpoint_family import (
    ENDPOINT_FAMILY_LOOKUP,
    ENDPOINT_FAMILY_LOOKUP_BULK,
    record_endpoint_family_tag,
    record_low_priority_tag,
)
from lookup.enrichment import SKIPPED_PREFETCH_STAT_KEY
from lookup.location_union import (
    LOCATION_UNION_INDEX_DEGRADED_STAT_KEY,
    LOCATION_UNION_INDEX_HIT_STAT_KEY,
    LOCATION_UNION_INDEX_MISS_STAT_KEY,
)
from lookup.models import (
    BulkLookupRequest,
    BulkLookupResponse,
    BulkLookupResultItem,
    LookupRequest,
    LookupResponse,
)
from lookup.orchestrator import perform_lookup
from lookup.rowless import NONLIBRARY_RELEASE_SURFACED_STAT_KEY
from lookup.server_timing_legs import EVENT_LOOP_LAG_STAT_KEY, event_loop_lag_extra_leg
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
# Ceiling 8: above the warm path's needs (~5 ms hits never stack that deep at
# production arrival rates) and low enough that a single-lookup cold storm
# can't re-enter the pool-starvation regime.
#
# But 8 is only the CEILING, not the effective default. The reopen (LML#706)
# showed the residual cold tail is a cap-wider-than-pool hazard: the cap
# defaulted to 8 while the discogs-cache pool defaults to 5, so up to 3
# admitted lookups sat on ``pool.acquire()`` at once — moving the queue off the
# (measured, bounded) semaphore onto acquire, where the wait attaches to
# whatever PG span is open (a 1.4 ms ``entity.identity`` SELECT measured at
# 5 s). The discogs-cache pool is THE binding constraint for a `/lookup`: the
# trigram release match, the streaming-URL cache read, AND every identity
# mint/lookup borrow that one pool (the WXYC#395 shared seam — the entity store
# reuses the discogs pool, it is not an owned `LML_PG_POOL_MAX_SIZE` pool;
# that knob sizes only the off-hot-path musicbrainz pool). Profiling ruled out
# ``library/db.py`` as the loop-blocker (fallback paths are 5-9 ms, off-loop,
# <2 ms loop lag at ×8), leaving this config incoherence as the fitting
# mechanism. So the effective default is the ceiling CLAMPED to the discogs
# pool — the cap can never *silently* exceed the pool it contends for. An
# explicit ``LML_LOOKUP_MAX_CONCURRENT`` still overrides upward (the
# no-redeploy Railway lever), but emits a guard warning.
_LOOKUP_MAX_CONCURRENT_ENV_VAR = "LML_LOOKUP_MAX_CONCURRENT"
_LOOKUP_MAX_CONCURRENT_CEILING = 8


def _lookup_default_max_concurrent(pool_max: int | None = None) -> int:
    """Effective default ``/lookup`` in-flight cap: the ceiling, clamped to the pool.

    ``min(_LOOKUP_MAX_CONCURRENT_CEILING, discogs_pool_max_size())`` — read at
    semaphore-construction time so ``LML_DISCOGS_POOL_MAX_SIZE`` is honoured
    without a redeploy, mirroring the cap's own env read. Every `/lookup`
    borrows the discogs-cache pool for its cache reads, trigram match, and
    identity mint (WXYC#395), so that pool is the binding constraint (LML#706).

    ``pool_max`` lets the caller pass a pool size it already read, so the
    semaphore builder can size both the clamp and its guard from one env read
    without this helper reading it a second time. Left ``None`` (the test/probe
    path), it reads the env itself.
    """
    if pool_max is None:
        pool_max = discogs_pool_max_size()
    return min(_LOOKUP_MAX_CONCURRENT_CEILING, pool_max)


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
    cap would deadlock every request forever). The fallback is the pool-clamped
    default (:func:`_lookup_default_max_concurrent`), not a bare 8, so an unset
    cap can never exceed the pool it contends for (LML#706). An explicit
    override *may* exceed the pool; the guard below makes that mismatch loud
    rather than silent (LML#706 AC#4).
    """
    global _lookup_semaphore
    if _lookup_semaphore is None:
        # One read of the pool size backs both the clamped default and the
        # guard, so they can't disagree about which pool the cap is measured
        # against (the clamp keeps the default within it; the guard flags an
        # explicit override that escapes it). ``_lookup_default_max_concurrent``
        # stays the single definition of the clamp — passed the pool we already
        # read so it doesn't read the env a second time.
        pool_max = discogs_pool_max_size()
        cap = resolve_positive_int_env(
            _LOOKUP_MAX_CONCURRENT_ENV_VAR, _lookup_default_max_concurrent(pool_max)
        )
        if cap > pool_max:
            logger.warning(
                "LML_LOOKUP_MAX_CONCURRENT=%d exceeds the discogs-cache pool (%d): "
                "excess in-flight lookups will queue on pool.acquire() and inflate "
                "trivial PG spans (LML#706). Raise LML_DISCOGS_POOL_MAX_SIZE to "
                "match, or lower the cap.",
                cap,
                pool_max,
            )
        _lookup_semaphore = asyncio.Semaphore(cap)
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
      cap (``min(8, LML_DISCOGS_POOL_MAX_SIZE)``) is tuned right.

    Observability must not break the request path; failures log and continue.
    """
    with observability_guard("project inflight_capped onto Sentry transaction", logger):
        project_capped("lml.lookup.inflight_capped", "lml.lookup.inflight_wait_ms", wait_ms)


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
    # LML#755 Discogs saturation breaker: shed counter seeds to 0 so
    # breaker-open time is an alertable baseline series on the #683 surface.
    BREAKER_OPEN_STAT_KEY,
    # LML#927 bulk sub-semaphore reservation: seeds to 0 so bulk-reservation
    # pressure is an alertable baseline series on the same #683 surface.
    DISCOGS_BULK_RESERVED_CAPPED_STAT_KEY,
    # LML#907 event-loop-lag gauge: seeded to 0 so the series is present even
    # when the sampler is off / unsampled (the stamp is skipped, leaving the 0).
    EVENT_LOOP_LAG_STAT_KEY,
    # LML#507: top-1 prefetch skip counter, seeded to 0 so the skip rate is an
    # alertable baseline series on the #683 cache.* surface.
    SKIPPED_PREFETCH_STAT_KEY,
    # LML#930 PR2: admission-shed would-shed counter, seeded to 0 so the rate
    # is an alertable baseline series even in shadow mode (enforce off).
    ADMISSION_WOULD_SHED_STAT_KEY,
    # LML#1026 recall-index authority verdicts in TRACK_ON_COMPILATION: all
    # three seed to 0 so hit-rate is dashboardable and a sustained nonzero
    # degraded rate (comp lookups running without the index -- discogs-cache
    # PG outage class) is an alertable baseline series on the #683 surface.
    LOCATION_UNION_INDEX_HIT_STAT_KEY,
    LOCATION_UNION_INDEX_MISS_STAT_KEY,
    LOCATION_UNION_INDEX_DEGRADED_STAT_KEY,
)
"""LML-specific keys seeded into every request's cache_stats dict so PostHog
and Sentry payload shapes stay stable. Used at BOTH ``handle_lookup`` and
``handle_bulk_lookup`` so the two endpoints emit identical shapes. See
``init_cache_stats`` and LML#544 round 2 for the shape-stability rationale.
Adding a new key here is the single point of update; LML#681's row-less flag
observability (flag tags, ``nonlibrary_release_surfaced``, the #632
hit/miss/unavailable counters) was the most recent addition.

LML#1036 seeding-decision survey: this is one of four ``init_cache_stats(...)``
call sites in the repo, and the other three deliberately seed a narrower (or
empty) set rather than this one -- surveyed and confirmed intentional, not
drift:

* ``artists/router.py``'s ``resolve_bulk`` seeds only ``(BREAKER_OPEN_STAT_KEY,)``
  because that route's payload never touches the release-resolution cache,
  the event-loop-lag gauge, or any of this tuple's other lookup-pipeline-
  specific counters -- seeding them would add permanently-zero noise to a
  shape those subsystems never apply to. The Discogs saturation-breaker
  counter IS relevant there (escalation can shed on that route too), so it
  alone is seeded.
* ``streaming/router.py``'s ``handle_streaming_check`` calls bare
  ``init_cache_stats()`` (no extras) because that path is application-cache-
  free end to end (LML#639/#641) -- there is no LML-specific key for it to
  stabilize.

Each site's own call comment carries its local rationale; this note just
records that the three-way split was reviewed together and left as-is."""


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
    with observability_guard("record LML flag tags into cache_stats", logger):
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


def _record_event_loop_lag() -> None:
    """Stamp the current event-loop-lag gauge onto ``cache_stats`` (LML#907).

    Called once per ``cache_stats`` context, right after ``_record_lml_flag_tags``
    at BOTH ``handle_lookup`` and ``handle_bulk_lookup``. What it records is a
    per-request *sample* of the process-global gauge (``core.event_loop_lag``),
    not this request's own tail — aggregated avg/p95 across requests in Sentry /
    PostHog, it tracks loop health over time and is the before/after metric for
    Lever A' (#904) and Lever B (#747).

    Gated on ``lml_event_loop_lag_gauge`` (the Railway kill switch) and a no-op
    when the gauge is unsampled (sampler off, or the first ~interval of process
    life), leaving the seeded ``0``. Observability must not break the request
    path: any exception is swallowed at WARNING (matches ``_record_lml_flag_tags``).
    """
    with observability_guard("record event-loop lag into cache_stats", logger):
        if not get_settings().lml_event_loop_lag_gauge:
            return
        lag_ms = get_event_loop_lag_ms()
        if lag_ms is None:
            return
        get_cache_stats_recorder().record(EVENT_LOOP_LAG_STAT_KEY, lag_ms)


def init_lookup_observability(
    family: str,
    caller_reason: str | None,
    *,
    skip_cache: bool = False,
    extra_keys: tuple[str, ...] = _LML_CACHE_STATS_EXTRA_KEYS,
) -> None:
    """Shared telemetry preamble for `/lookup` and `/lookup/bulk` (LML#1036).

    Runs the same six-call sequence both handlers ran inline at request
    entry, in the order `handle_lookup` established:

    1. ``init_cache_stats(extra_keys=extra_keys)`` — seed the cache_stats
       context so PostHog/Sentry payload shapes stay stable (LML#544 round 2).
    2. ``_record_lml_flag_tags()`` — LML#681 row-less-family flag state, once
       per context.
    3. ``_record_event_loop_lag()`` — LML#907 event-loop-lag gauge sample,
       once per context.
    4. ``record_endpoint_family_tag(family)`` — LML#944 traffic-class tag.
    5. ``record_caller_reason_tag(caller_reason)`` — LML#931 caller-reason tag.
    6. ``set_skip_cache(True)`` when ``skip_cache`` is truthy.

    Both handlers called this same six-call block verbatim before this
    extraction; the "once per cache_stats context" contract steps 2 and 3
    depend on (LML#681 — `_record_lml_flag_tags` is an additive-only
    recorder, so calling it more than once per context would double-count)
    is preserved because this helper itself is still called exactly once per
    handler invocation, same as the inline block was.

    `extra_keys` defaults to the shared `_LML_CACHE_STATS_EXTRA_KEYS` set
    both existing call sites already passed identically; a future sibling
    endpoint with a different seeding decision passes its own tuple (see the
    LML#1036 seeding-decision survey in `_LML_CACHE_STATS_EXTRA_KEYS`'s
    docstring above).

    `record_low_priority_tag` is deliberately NOT part of this preamble: at
    `handle_lookup` it must run AFTER `resolve_caller_class` resolves
    `low_priority` — a value this function has no access to — and at
    `handle_bulk_lookup` it is called unconditionally with `True`. Both call
    sites invoke it separately, next to their own call to this helper; see
    `lookup.endpoint_family.record_low_priority_tag`'s call-site-timing
    docstring for why moving it here would be wrong.
    """
    init_cache_stats(extra_keys=extra_keys)
    _record_lml_flag_tags()
    _record_event_loop_lag()
    record_endpoint_family_tag(family)
    record_caller_reason_tag(caller_reason)
    if skip_cache:
        set_skip_cache(True)


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
    with observability_guard("project cache_stats onto Sentry transaction", logger):
        transaction = sentry_sdk.get_current_scope().transaction
        if transaction is None:
            return
        for key, value in stats.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                transaction.set_data(f"lml.cache.{key}", value)
                transaction.set_measurement(f"lml.cache.{key}", value)


def _emit_server_timing_header(
    http_response: Response,
    telemetry: RequestTelemetry,
    extra: dict[str, float] | None = None,
) -> None:
    """Surface the per-stage RequestTelemetry timings as a ``Server-Timing`` header.

    Out-of-band instrumentation — the LML half of the cross-repo Server-Timing
    trace (Backend-Service#881, Epic G: enrichment-pipeline observability). The
    durations ``track_step`` already captured — plus any derived ``extra`` legs
    (on ``/lookup`` the ``discogs`` = ``pg_time_ms + api_time_ms`` split) and one
    live ``total`` — are serialized onto the HTTP response so a caller
    (request-o-matic's ``lookup`` CLI) can attribute a slow lookup to a named
    server stage. No ``api.yaml`` / ``CacheStats`` change: the JSON body is
    byte-identical whether or not this header is set.

    Gated on ``LML_EMIT_SERVER_TIMING`` (default on; the Railway kill switch).
    Observability must not break the request path — any failure (a settings read
    or the serialize) logs at WARNING and the response ships without the header.
    """
    with observability_guard("emit Server-Timing header", logger):
        if not get_settings().lml_emit_server_timing:
            return
        http_response.headers["Server-Timing"] = telemetry.as_server_timing(extra=extra)


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
    http_request: Request,
    http_response: Response,
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
    x_caller_reason: str | None = Header(
        default=None,
        alias="X-Caller-Reason",
        description=(
            "Optional caller-supplied traffic-class label forwarded by "
            "Backend-Service (BS#1843), e.g. proxy-library-search or "
            "catalog-popularity-freetext-resolve (LML#931). Purely "
            "observational: tagged onto the Sentry transaction and the "
            "PostHog completion event. Absent on callers that predate "
            "BS#1843 -- treated as a safe no-op, never fabricated."
        ),
    ),
    x_caller_class: str | None = Header(
        default=None,
        alias="X-Caller-Class",
        description=(
            "Optional caller-declared traffic class, 1-5 (LML#928; forwarded by "
            "Backend-Service per BS#1843). Class 5 (batch/cron/backfill) "
            "additionally routes this request onto the low-priority lane -- the "
            "LML#716/#924 process-global bulk permit (LML_BULK_GLOBAL_MAX_CONCURRENT) "
            "`/lookup/bulk` items already share. Missing or invalid values "
            "(including 1-4) are a no-op: today's lane placement is unchanged. "
            "Down-rank only -- never trusted to grant a protected lane beyond the "
            "caller's entitlement, since this header is honored only on the "
            "authenticated Backend-Service-to-LML channel."
        ),
    ),
):
    """Process a lookup request."""
    # LML#1036: shared telemetry preamble (cache_stats seed, LML#681 flag
    # tags, LML#907 event-loop-lag stamp, endpoint-family + caller-reason
    # Sentry tags, skip_cache honor) -- see `init_lookup_observability`.
    init_lookup_observability(
        ENDPOINT_FAMILY_LOOKUP,
        x_caller_reason,
        skip_cache=skip_cache,
        extra_keys=_LML_CACHE_STATS_EXTRA_KEYS,
    )

    try:
        # LML#928/#953: class 5 (batch/cron/backfill) additionally routes this
        # request onto the low-priority lane -- the same LML#716/#924
        # process-global bulk permit `/lookup/bulk` items already share.
        # Down-rank only: classes 1-4, absent, and invalid values resolve to a
        # no-op (today's interactive-only behavior, unchanged). Resolved here,
        # before ANY permit is touched -- LML#953 requires the bulk-global
        # wait to happen BEFORE (outside) the interactive
        # `LML_LOOKUP_MAX_CONCURRENT` semaphore below, so a class-5 request
        # parked on a saturated bulk budget never pins an interactive slot
        # while it waits (the #951 review's priority-inversion finding).
        # Deadlock-free: acquisition is strictly ordered bulk-global permit ->
        # interactive lookup semaphore and never the reverse. The class-5 path
        # deliberately holds the bulk permit while parked on the lookup
        # semaphore -- that is the intended forward edge, not a cycle. Safety
        # comes from the reverse never happening: nothing that already holds the
        # lookup semaphore ever waits on the bulk-global permit, so no acquire
        # cycle can form.
        caller_class = resolve_caller_class(x_caller_class)
        low_priority = is_low_priority_caller_class(caller_class)
        # LML#930 PR2: tag the resolved traffic class now that it's known --
        # must run here (after resolve_caller_class), not at the earlier
        # _record_event_loop_lag call site, where low_priority is undefined.
        record_low_priority_tag(low_priority)
        # LML#927: propagate the same down-rank-only signal to the Discogs
        # semaphore gate, deeper than the bulk-global permit above -- so a
        # class-5 request that gets past admission control still can't
        # occupy more than the reserved share of the shared 5-permit Discogs
        # semaphore. Set before any permit is touched; the value propagates
        # into `perform_lookup`'s task via the inherited context.
        set_discogs_low_priority(low_priority)

        async with maybe_acquire_bulk_global_permit_or_reap(
            low_priority, http_request
        ) as bulk_wait_ms:
            # LML#706 PR3: bound in-flight lookups process-wide. On this repo's
            # Python (>=3.12) the `locked()` pre-check is EXACT, not racy: there is
            # no await between the check and the acquire (one event-loop slice),
            # and 3.12's `locked()` returns True when the value is exhausted OR
            # waiters exist — the same predicate `acquire()` uses to decide whether
            # to park. So `capped_on_arrival` ⇔ this request actually queued.
            semaphore = _get_lookup_semaphore()
            capped_on_arrival = semaphore.locked()
            wait_start = time.perf_counter()

            # LML#715: reap callers that disconnect WHILE QUEUED. Starlette runs a
            # non-streaming handler to completion after the client's socket closes,
            # so a request whose caller already timed out and retried (the #706
            # cold-storm shape) would otherwise keep its FIFO slot, later acquire a
            # permit, and run a full cold lookup nobody receives — retry-amplified
            # zombie work that spends the scarce cap. Race the permit acquire
            # against the same `watch_disconnect` sentinel the /lookup/bulk and
            # identity bulk-resolve paths use; on disconnect, cancel the acquire so
            # the queue slot frees and no lookup runs. This guards the QUEUED phase
            # ONLY — once the permit is held the sentinel is cancelled and
            # perform_lookup runs to completion (a disconnect mid-lookup is not
            # reaped; that phase already holds its resources).
            #
            # NOT `core.bulk_concurrency.run_bulk_gather` (LML#1033): that helper
            # races a batch `asyncio.gather` and raises `HTTPException(499)`; this
            # race is over a single semaphore acquire and returns a 499 response
            # body instead (`ClientDisconnectedWhileQueuedError` below), with its
            # own permit-leak-safety shape. Deliberately left alone — LML#1033's
            # documented non-goal.
            #
            # Cancelling `semaphore.acquire()` is permit-safe on this repo's Python
            # (>=3.12): if the acquire is cancelled AFTER the permit was granted
            # (the acquire/sentinel tie), Semaphore.acquire returns the permit and
            # re-raises, so `cancel_and_drain` cannot strand a slot.
            acquire_future = asyncio.ensure_future(semaphore.acquire())
            sentinel_task = asyncio.create_task(
                watch_disconnect(http_request), name="lml.lookup.disconnect_sentinel"
            )
            # A single guaranteed-release scope guards the permit from the moment it
            # could be held. `cancel_and_drain` is a no-op on an already-done future,
            # so it does NOT return a granted permit; and the sentinel cleanup on
            # the success path can re-raise (a `receive()` error) before the lookup.
            # Both are permit leaks on the process-global cap if release is tied to
            # a narrower `try`. Track "did we end up holding a permit" explicitly and
            # release it once in the outer `finally`, whatever path we leave by.
            permit_held = False
            try:
                try:
                    done, _pending = await asyncio.wait(
                        {acquire_future, sentinel_task}, return_when=asyncio.FIRST_COMPLETED
                    )
                except BaseException:
                    # Handler cancellation can arrive with the acquire already
                    # granted (grant + cancel in the same loop cycle). Drain the
                    # acquire, then flag the granted permit so the outer `finally`
                    # releases it — a still-pending acquire drains as CANCELLED and
                    # never took a permit, so it stays False.
                    await cancel_and_drain(acquire_future)
                    permit_held = acquire_future.done() and not acquire_future.cancelled()
                    await cancel_and_drain(sentinel_task)
                    raise

                # Tie broken toward "acquired": if both completed, the acquire holds
                # a permit, so proceed rather than reap. `acquire_future.done()` is
                # exact here — no await separates the wait() return from this check,
                # so the single event loop cannot flip the state underneath us.
                if sentinel_task in done and not acquire_future.done():
                    # Client departed while queued. Free the never-taken slot and
                    # short-circuit; nobody reads the body. The tag mirrors the bulk
                    # path so `lml.client_aborted:true` surfaces reaped requests in
                    # the Sentry trace explorer.
                    await cancel_and_drain(acquire_future)
                    sentry_sdk.set_tag("lml.client_aborted", "true")
                    logger.warning(
                        "lookup reaped: client disconnected while queued on the in-flight cap"
                    )
                    http_response.status_code = 499
                    return LookupResponse(results=[], search_type="none")

                # Permit held from here — the outer `finally` owns its release, so a
                # raising sentinel drain (below) or perform_lookup error can't strand
                # it.
                permit_held = True
                # Stop watching for disconnect; the permit-held phase is not reaped.
                await cancel_and_drain(sentinel_task)
                # `queue_wait` Server-Timing leg (LML#907 follow-up, extended by
                # LML#953): the sum of BOTH gates' measured wait -- the
                # bulk-global wait (`bulk_wait_ms`, 0.0 unless this is a capped
                # class-5 request) plus this semaphore's own wait -- always
                # computed (0.0 when uncontended on both) so it always renders
                # via `extra` below; `total` (built after this point) never
                # includes it.
                queue_wait_ms = bulk_wait_ms
                if capped_on_arrival:
                    interactive_wait_ms = (time.perf_counter() - wait_start) * 1000.0
                    _project_inflight_capped(interactive_wait_ms)
                    queue_wait_ms += interactive_wait_ms
                # Debit the TOTAL queue wait (bulk-global + interactive) from the
                # caller's budget so the A8 / LML#345 contract ("LML returns
                # slightly before the caller times out") holds under saturation
                # on EITHER gate — the pipeline's budget clock only starts
                # inside perform_lookup. Floor at 1: the budget resolver treats
                # non-positive as "unset → env default", which would hand the
                # MOST delayed caller the LARGEST budget. A 1ms budget
                # short-circuits the pipeline almost immediately — the cheap
                # outcome the (likely already timed-out) caller would want.
                if queue_wait_ms > 0 and x_caller_budget_ms is not None and x_caller_budget_ms > 0:
                    x_caller_budget_ms = max(1, x_caller_budget_ms - int(queue_wait_ms))
                # Constructed inside the permit so telemetry's total-duration
                # series keeps meaning "lookup work", not "queue wait + work" —
                # the wait is reported separately via the Sentry measurement.
                telemetry = RequestTelemetry(
                    api_call_keys=["discogs"],
                    distinct_id="library-metadata-lookup-service",
                    event_prefix="lookup",
                    # Emit only the `lookup_completed` summary, not the ~9
                    # per-step `lookup_<step>` events (wxyc-fastapi>=1.3.0). No
                    # insight or alert reads the per-step events; the summary
                    # already carries every step timing under `steps`. This is
                    # /lookup's ~7x PostHog fan-out — a Backend-Service backfill
                    # (2026-08-01) multiplied it into a ~440k events/day spike.
                    emit_step_events=False,
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
            finally:
                if permit_held:
                    semaphore.release()

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

        # Surface the per-stage telemetry as a Server-Timing header (BS#881).
        # The derived `discogs` leg is `pg_time_ms + api_time_ms`; guard the
        # None-context case (get_cache_stats() can return None) so it degrades
        # to 0 rather than TypeError, mirroring the CacheStats guard above.
        pg_ms = stats.get("pg_time_ms", 0) if stats else 0
        api_ms = stats.get("api_time_ms", 0) if stats else 0
        # `queue_wait` nets out of the middleware-timed `lml_wall` leg to
        # isolate DI/(de)serialization overhead; `event_loop_lag` is omitted
        # (not zeroed) when unsampled — see `lookup.server_timing_legs`.
        extra: dict[str, float] = {"discogs": pg_ms + api_ms, "queue_wait": queue_wait_ms}
        extra.update(event_loop_lag_extra_leg())
        _emit_server_timing_header(http_response, telemetry, extra=extra)

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
                    "endpoint_family": ENDPOINT_FAMILY_LOOKUP,
                    "low_priority": low_priority,
                    **({"caller_reason": x_caller_reason} if x_caller_reason is not None else {}),
                },
            )

        return response

    except HTTPException:
        raise
    except ClientDisconnectedWhileQueuedError:
        # LML#953: client departed while parked on a saturated bulk-global
        # budget -- mirrors the in-flight cap's own reap branch above (the
        # tag/log/499 shape), just for the OUTER gate instead of the inner
        # one. The permit was never taken; nobody reads the body.
        sentry_sdk.set_tag("lml.client_aborted", "true")
        logger.warning("lookup reaped: client disconnected while queued on the bulk-global permit")
        http_response.status_code = 499
        return LookupResponse(results=[], search_type="none")
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
    enforced guarantee as the ``bandcamp`` pin (``allow_release_resolution_fallback``
    is caller-controlled since LML#920 — see the query flag below). Callers
    that want the bio cache populated at bulk scale should use the offline
    warmer (#548).
    """,
    responses={
        200: {"description": "Per-item verdicts in input order."},
        400: {"description": f"Malformed body or batch above {_BULK_LOOKUP_INPUT_CAP}-item cap."},
        422: {"description": "Request body failed Pydantic validation (e.g. empty items array)."},
    },
)
async def handle_bulk_lookup(
    http_request: Request,
    http_response: Response,
    db: LibraryDB = Depends(get_library_db),
    discogs_service: DiscogsService | None = Depends(get_discogs_service),
    discogs_cache: DiscogsCacheService | None = Depends(get_discogs_cache_service),
    mb_pg: PgSource | None = Depends(get_musicbrainz_pg),
    entity_store: EntityStore | None = Depends(get_entity_store),
    discogs_cache_pg: PgSource | None = Depends(get_discogs_cache_pg),
    posthog_client: Posthog | None = Depends(get_posthog_client),
    apple_music: AppleMusicClient | None = Depends(get_apple_music_client),
    spotify: SpotifyClient | None = Depends(get_spotify_client),
    # Bandcamp is resolved but forwarded to perform_lookup ONLY when
    # lml_bulk_bandcamp_streaming_warm is on (default off); otherwise it is
    # pinned to None at the call site below, exactly as before. See the call
    # site for the flag-gate + rate-limit rationale.
    bandcamp: BandcampClient | None = Depends(get_bandcamp_client),
    settings: Settings = Depends(get_settings),
    skip_cache: bool = False,
    allow_release_resolution_fallback: bool = Query(
        False,
        description=(
            "Per-caller opt-in to non-library release resolution — the LML#671 "
            "bulk kill switch. Default False keeps the offline drain unchanged; "
            "true restores `/lookup`'s default resolution (LML#583/#652, BS#1815)."
        ),
    ),
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
    x_caller_reason: str | None = Header(
        default=None,
        alias="X-Caller-Reason",
        description=(
            "Optional caller-supplied traffic-class label forwarded by "
            "Backend-Service (BS#1843), e.g. proxy-library-search or "
            "catalog-popularity-freetext-resolve (LML#931). Purely "
            "observational: tagged onto the Sentry transaction and the "
            "PostHog completion event. Absent on callers that predate "
            "BS#1843 -- treated as a safe no-op, never fabricated."
        ),
    ),
    x_caller_class: str | None = Header(
        default=None,
        alias="X-Caller-Class",
        description=(
            "Optional caller-declared traffic class, 1-5 (LML#928; forwarded by "
            "Backend-Service per BS#1843). Accepted for parity with `/lookup` "
            "and documentation, but NOT branched on here: every item on this "
            "route already runs under the low-priority lane "
            "(`acquire_bulk_global_permit`, LML#716/#924) unconditionally, so "
            "no class value -- including 1-4 -- can escalate a bulk caller out "
            "of it. Down-rank only, never up-rank."
        ),
    ),
) -> BulkLookupResponse:
    """Bulk lookup. See route docstring for protocol."""
    # Shared bulk envelope (LML#767): raw-JSON parse, ClientDisconnect -> 400,
    # non-object -> 400, manual over-cap before Pydantic, then model_validate.
    # `cap_status=400` preserves this route's LML#368 over-cap contract (400,
    # not the family's default 413). An absent/wrong-type `items` field falls
    # through to Pydantic's structured errors() rather than a bare 422.
    request = await parse_bulk_body(
        http_request, BulkLookupRequest, _BULK_LOOKUP_INPUT_CAP, field="items", cap_status=400
    )

    # Entry signal (LML#371). Fires synchronously before any awaits, so a
    # handler that later hangs past the caller's AbortController still leaves
    # a trace — uvicorn's access log and Sentry's automatic `http.server`
    # transaction only commit on response completion.
    logger.info("bulk lookup start: size=%d", len(request.items))

    # Bulk does streaming-URL cache read-fill ONLY — never spawns the background
    # warm (LML#706). A 35k-album drain returns fast per item, so an enqueued
    # warm tail would decouple from the request and starve the live /lookup
    # path's own warms. Same ContextVar-propagation mechanism as `skip_cache`
    # below, and the same posture as the unconditional `bandcamp=None` pin —
    # `allow_release_resolution_fallback` is caller-controlled since LML#920.
    set_suppress_streaming_warm(True)

    # LML#927: every item on this route is always low priority at the
    # Discogs semaphore gate too -- mirrors the unconditional bulk-global
    # permit placement below (LML#716/#924); no `X-Caller-Class` value can
    # escalate a bulk caller out of it (down-rank only, never up-rank).
    set_discogs_low_priority(True)

    # One cache_stats context for the whole batch: the in-process caches are
    # shared, so aggregate counters reflect the batch's behavior — the
    # property that motivates this endpoint. LML#1036: shared telemetry
    # preamble (see `init_lookup_observability`) -- also honors the same
    # `skip_cache` query flag the per-item route accepts (backed by a
    # ContextVar, so setting once at the batch top propagates into each
    # `_run_one` task via the inherited task context) and records the flag
    # tag once for the whole batch (shared context), so the bulk value stays
    # 0/1 instead of summing across items.
    init_lookup_observability(
        ENDPOINT_FAMILY_LOOKUP_BULK,
        x_caller_reason,
        skip_cache=skip_cache,
        extra_keys=_LML_CACHE_STATS_EXTRA_KEYS,
    )
    # LML#930 PR2: every item on this route is always low priority (mirrors
    # the unconditional set_discogs_low_priority(True) above). Not part of
    # the shared preamble -- unlike `handle_lookup` (where `low_priority` is
    # resolved later from `resolve_caller_class`), here it's unconditionally
    # True, so this call just sits next to the preamble instead of inside it.
    record_low_priority_tag(True)

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
            # Hard-pin warm_cache off (LML#742), pairing with the unconditional
            # `bandcamp=None` pin below (`allow_release_resolution_fallback` is
            # caller-controlled since LML#920, no longer a hard pin here): a
            # truthy per-item flag would schedule the `_warm_bio_cache`
            # per-bio-ref live Discogs fan-out for every batch item. Unlike
            # `bandcamp`, warm_cache is read from the request *inside*
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
                        # Bandcamp on the bulk path is gated by the dedicated
                        # lml_bulk_bandcamp_streaming_warm flag (default off,
                        # extends #1052). With the flag OFF this is None — exactly
                        # the LML#573 PR-3 posture: its client is rate-limited to
                        # 1 req/s, so a per-item live album probe would serialize
                        # the 35k-album drain into hours of requests against
                        # Bandcamp (and starve the shared singleton for the live
                        # /lookup path), so the post-process skips the Bandcamp
                        # leg and the search-URL fallback applies. With the flag
                        # ON, the injected client flows through so a cold miss
                        # schedules the bounded background warm (the post-process
                        # exempts Bandcamp from the bulk suppression), resolving
                        # the DIRECT album URL into the cache for subsequent
                        # lookups. Enable only after the BS#642 drain completes.
                        bandcamp=bandcamp if settings.lml_bulk_bandcamp_streaming_warm else None,
                        discogs_cache_pg=discogs_cache_pg,
                        caller_budget_ms=x_caller_budget_ms,
                        # Caller-controlled since LML#920 (default False, the
                        # LML#671 bulk kill switch): the enrichment worker
                        # (Backend-Service#1815) opts in via the query flag to
                        # restore the resolution `/lookup` gets by default.
                        allow_release_resolution_fallback=allow_release_resolution_fallback,
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

            def _on_abort() -> None:
                # Exit log fires for the abort branch too — operators see the
                # batch size + abort reason without digging into Sentry.
                logger.warning(
                    "bulk lookup aborted by client: size=%d max_concurrent=%d",
                    len(request.items),
                    max_concurrent,
                )
                # 499 = Nginx "client closed request". Skip PostHog: partial
                # counts would skew batch-completion analytics.
                http_span.set_data("http.status_code", 499)

            # Race the gather against a client-disconnect sentinel (LML#1033:
            # core.bulk_concurrency.run_bulk_gather). If the client departs
            # first, the gather is cancelled so Discogs semaphore permits free
            # for the next caller; without this, queue depth grows
            # monotonically across batches.
            #
            # Spawning the sentinel must happen *after* `parse_bulk_body`
            # above has fully consumed the request body — `watch_disconnect`
            # awaits `request.receive()`, which would otherwise swallow
            # `http.request` body messages the body parser needs.
            results = await run_bulk_gather(
                (_run_one(i, item) for i, item in enumerate(request.items)),
                http_request=http_request,
                watch_disconnect_fn=watch_disconnect,
                on_abort=_on_abort,
                sentinel_task_name="lml.bulk.disconnect_sentinel",
                # Span data carries the bulk-specific context; the Sentry tag
                # `run_bulk_gather` sets on abort is global-scope. Key-name
                # asymmetry intentional.
                on_race_settled=lambda aborted: span.set_data("lml.bulk.client_aborted", aborted),
            )

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
                    "endpoint_family": ENDPOINT_FAMILY_LOOKUP_BULK,
                    "low_priority": True,
                    **({"caller_reason": x_caller_reason} if x_caller_reason is not None else {}),
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

    # Batch-level Server-Timing (BS#881): one `total` for the whole request,
    # no per-item step timings or derived `discogs` leg — per-item stages aren't
    # meaningful in a single per-HTTP-request header. `batch_telemetry` tracks no
    # steps, so with no `extra` this is total-only. Degrade-safe by construction.
    _emit_server_timing_header(http_response, batch_telemetry)

    return BulkLookupResponse(results=results)
