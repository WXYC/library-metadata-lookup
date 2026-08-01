"""Router for the streaming availability check endpoint."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException
from wxyc_fastapi.observability import RequestTelemetry, init_cache_stats

from clients.bandcamp import BandcampClient
from clients.streaming.apple_music import AppleMusicClient
from clients.streaming.deezer import DeezerClient
from clients.streaming.spotify import SpotifyClient
from core.dependencies import get_streaming_posthog_client
from core.observability import observability_guard, project_capped
from core.search import resolve_positive_int_env
from streaming.dependencies import (
    get_apple_music_client,
    get_bandcamp_client,
    get_deezer_client,
    get_spotify_client,
)
from streaming.models import (
    StreamingCheckRequest,
    StreamingCheckResponse,
    StreamingCheckSources,
)
from streaming.orchestrator import check_streaming_availability

if TYPE_CHECKING:
    from posthog import Posthog

logger = logging.getLogger(__name__)

router = APIRouter(tags=["streaming"])

# The streaming services dispatched by ``check_streaming_availability``, in a
# fixed order. Derived from ``StreamingCheckSources`` (pydantic preserves
# field-declaration order) so the telemetry service list can't drift from the
# response shape. Adding a provider still requires editing the orchestrator
# (its ``clients`` dict + kwargs + the ``_EXPECTED_SERVICE_FIELDS`` guard, which
# halts import on drift) — but this list then updates automatically rather than
# being a separate literal to forget. Doubles as the ``RequestTelemetry``
# ``api_call_keys`` (stable ``api_calls`` shape, all zero until LML#641
# instruments the clients) and as the verdict-iteration order.
_STREAMING_SERVICES: tuple[str, ...] = tuple(StreamingCheckSources.model_fields)

# Process-wide cap on concurrent `/api/v1/streaming-check` requests (LML#753).
# Each request fans out four external streaming-service calls in one gather
# (`check_streaming_availability`), so N concurrent requests hold 4xN in-flight
# external HTTP calls on the shared event loop with no cross-request bound.
# This endpoint borrows no asyncpg pool connection (no `pool.acquire`, no
# `lml_cache` writes -- persistence is the caller's job, the LML#376 "treat
# `None` as do-not-persist" verdict contract), so it is deliberately NOT folded
# into the LML#716 bulk-family pool-derived budget -- that would queue
# flowsheet adds behind pool-bound bulk-drain work they don't share. One
# budget per contended resource: this cap protects loop time + sockets, not
# the discogs-cache pool. Excess requests QUEUE on the semaphore -- no
# 429/503 shedding; callers (tubafrenzy / Backend-Service) see latency, never
# a new error mode. Default 8 is a literal, not pool-derived (there is no pool
# to derive it from): organic flowsheet-add peaks are single-digit concurrent,
# and 8 leaves headroom above that without letting a caller storm (the
# 2026-07-06 flowsheet-backfill-flood shape) occupy the loop unbounded.
# Per-worker caveat: if LML#747 (`UVICORN_WORKERS > 1`) ships, this becomes a
# per-worker bound -- same caveat `LML_LOOKUP_MAX_CONCURRENT` and
# `LML_BULK_GLOBAL_MAX_CONCURRENT` carry.
_STREAMING_CHECK_MAX_CONCURRENT_ENV_VAR = "LML_STREAMING_CHECK_MAX_CONCURRENT"
_STREAMING_CHECK_MAX_CONCURRENT_DEFAULT = 8

_streaming_check_semaphore: asyncio.Semaphore | None = None
"""Lazily constructed on the first request -- mirrors `lookup.router.
_lookup_semaphore` (LML#706 PR3): not a loop constraint (3.10+ binds a
semaphore's loop only at the first contended acquire), but so the env is read
at request time (the no-redeploy Railway lever) and tests can reset the global
between event loops (the suite-wide autouse fixture in `tests/conftest.py`)."""


def _get_streaming_check_semaphore() -> asyncio.Semaphore:
    """Lazily build the process-global `/streaming-check` in-flight semaphore.

    Sized from `LML_STREAMING_CHECK_MAX_CONCURRENT` (read once, at first
    construction; unparseable/zero/negative values WARN and fall back to the
    literal default -- a 0 cap would deadlock every request).
    """
    global _streaming_check_semaphore
    if _streaming_check_semaphore is None:
        cap = resolve_positive_int_env(
            _STREAMING_CHECK_MAX_CONCURRENT_ENV_VAR, _STREAMING_CHECK_MAX_CONCURRENT_DEFAULT
        )
        _streaming_check_semaphore = asyncio.Semaphore(cap)
    return _streaming_check_semaphore


def _project_inflight_capped(wait_ms: float) -> None:
    """Project cap engagement onto Sentry for a request that queued.

    Two channels, mirroring `lookup.router._project_inflight_capped` (the
    LML#683 lesson: `set_data` alone reads back as "Unknown attribute" in the
    spans dataset, so it can't back a query or an alert):

    * `sentry_sdk.set_tag("lml.streaming_check.inflight_capped", "true")` --
      the filterable engagement flag, set only on requests that found the cap
      saturated.
    * `set_measurement("lml.streaming_check.inflight_wait_ms", ...)` -- the
      quantitative series (p95 queue wait) that decides whether the default
      cap is tuned right.

    Observability must not break the request path; failures log and continue.
    """
    with observability_guard("project inflight_capped onto Sentry transaction", logger):
        project_capped(
            "lml.streaming_check.inflight_capped",
            "lml.streaming_check.inflight_wait_ms",
            wait_ms,
            also_set_data=False,
        )


def _summary_properties(
    response: StreamingCheckResponse | None,
    *,
    error_type: str | None = None,
) -> dict[str, object]:
    """Per-service verdict + roll-up for the summary event, one stable shape.

    The orchestrator fans out all services concurrently in one gather, so
    per-service *timing* isn't separable at this layer, but each service's
    *outcome* is. Both the success and total-failure paths emit this same shape
    so PostHog sees one schema (LML#639):

    - Success (``response`` set, ``error_type`` None): each service is
      ``matched`` (a ``SourceMatch`` is present), ``errored`` (the service raised
      — listed in ``errored_sources``; a 200 with ``on_streaming=None`` per
      LML#376), or ``absent`` (dispatched-and-no-match, or not dispatched —
      indistinguishable from the response alone; both mean "no URL").
    - Total failure (``response`` None, ``error_type`` set): ``check_streaming_
      availability`` itself raised, so no result exists — every verdict is
      ``unknown`` and ``on_streaming`` is None. This is the 500 path.
    """
    # Total-failure path: no result exists, so every verdict is ``unknown``.
    # Early-return keeps the success branch below free of ``if response`` noise.
    if response is None:
        return {
            "outcome": "error",
            "error_type": error_type,
            "on_streaming": None,
            "errored_sources": [],
            "errored_count": 0,
            "match_count": 0,
            **{f"verdict_{service}": "unknown" for service in _STREAMING_SERVICES},
        }

    errored = set(response.errored_sources)
    props: dict[str, object] = {
        "outcome": "success",
        "error_type": None,
        "on_streaming": response.on_streaming,
        "errored_sources": response.errored_sources,
        "errored_count": len(response.errored_sources),
    }
    match_count = 0
    for service in _STREAMING_SERVICES:
        if getattr(response.sources, service) is not None:
            verdict = "matched"
            match_count += 1
        elif service in errored:
            verdict = "errored"
        else:
            verdict = "absent"
        props[f"verdict_{service}"] = verdict
    props["match_count"] = match_count
    return props


def _emit_summary(
    posthog_client: Posthog | None,
    telemetry: RequestTelemetry,
    response: StreamingCheckResponse | None,
    *,
    error_type: str | None = None,
) -> None:
    """Best-effort summary emit, used on BOTH the success and failure paths.

    Building the properties (``_summary_properties``) is done *inside* the swallow
    too, not just the send — so the whole telemetry step (incl. deriving verdicts)
    can never change the handler's outcome: on success the response still returns,
    on failure the 500 still raises (LML#639). No-op when telemetry is disabled.
    """
    if not posthog_client:
        return
    try:
        telemetry.send_to_posthog(
            posthog_client, _summary_properties(response, error_type=error_type)
        )
    except Exception:
        logger.warning("streaming-check telemetry emit failed", exc_info=True)


@router.post(
    "/streaming-check",
    response_model=StreamingCheckResponse,
    summary="Check streaming availability for an album",
    description=(
        "Checks Spotify, Deezer, Apple Music, and Bandcamp for a given artist+title. "
        "Returns whether the album is available on streaming platforms and URLs for each match."
    ),
    responses={
        200: {"description": "Streaming availability result"},
        500: {"description": "Internal server error"},
    },
)
async def handle_streaming_check(
    request: StreamingCheckRequest,
    spotify: SpotifyClient | None = Depends(get_spotify_client),
    deezer: DeezerClient = Depends(get_deezer_client),
    apple_music: AppleMusicClient | None = Depends(get_apple_music_client),
    bandcamp: BandcampClient = Depends(get_bandcamp_client),
    posthog_client: Posthog | None = Depends(get_streaming_posthog_client),
) -> StreamingCheckResponse:
    """Check streaming availability across all configured services."""
    # Per-request telemetry (LML#639). The cache-stats bracket and api_calls map
    # are seeded with a stable, all-zero shape: the streaming-check path is
    # application-cache-free and the clients record no API calls yet (LML#641).
    init_cache_stats()
    telemetry = RequestTelemetry(
        api_call_keys=list(_STREAMING_SERVICES),
        distinct_id="library-metadata-lookup-service",
        event_prefix="streaming_check",
    )

    # LML#753: bound in-flight `/streaming-check` requests process-wide. The
    # `locked()` pre-check is exact on this repo's Python (>=3.12) -- no await
    # separates it from the `async with` acquire below, and `locked()` returns
    # True when the value is exhausted OR waiters exist, the same predicate
    # `acquire()` uses to decide whether to park.
    semaphore = _get_streaming_check_semaphore()
    capped_on_arrival = semaphore.locked()
    wait_start = time.perf_counter()

    async with semaphore:
        if capped_on_arrival:
            _project_inflight_capped((time.perf_counter() - wait_start) * 1000.0)

        try:
            with telemetry.track_step("availability"):
                response = await check_streaming_availability(
                    request.artist,
                    request.title,
                    spotify=spotify,
                    deezer=deezer,
                    apple_music=apple_music,
                    bandcamp=bandcamp,
                )
        except HTTPException:
            raise
        except Exception as e:
            logger.exception("Streaming check failed for %s - %s", request.artist, request.title)
            # Report the total failure before raising. The emit is itself
            # swallowed, so it can neither suppress nor alter the 500 (LML#639).
            _emit_summary(posthog_client, telemetry, None, error_type=type(e).__name__)
            raise HTTPException(status_code=500, detail="Streaming check failed") from e

    _emit_summary(posthog_client, telemetry, response)
    return response
