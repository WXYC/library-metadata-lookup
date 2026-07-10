"""``POST /api/v1/cache/refresh-for-identities`` — source-agnostic cache warmer.

LML#525. Takes a batch of ``identity_id`` values (max 50), looks up each
one's per-source ``(source, external_id)`` pairs from
``entity.release_identity``, and fans out per-source release-cache refreshes
(today: Discogs only). For each refreshed Discogs release, walks its artist
credits and refreshes the per-artist cache.

Mirrors ``/api/v1/lookup/bulk``'s dispatcher shape (``lookup/router.py``):
``asyncio.gather`` under a bounded outer semaphore (env
``LML_BULK_MAX_CONCURRENT``, default 10), per-item ``try/except`` so one
identity's failure can't poison siblings, and a ``watch_disconnect``
sentinel race so a client abort cancels the gather and releases
downstream Discogs rate-limit / semaphore permits.

Discogs API concurrency is gated by ``discogs/ratelimit.py``'s per-event-loop
semaphore (``discogs_max_concurrent=5``) + ``AsyncLimiter``
(``discogs_rate_limit=50/min``). The dispatcher inherits both for free
through ``DiscogsService.get_release`` / ``DiscogsService.get_artist_details``.

Sentry instrumentation:

- ``http.server`` span at handler entry (defense-in-depth against
  FastApiIntegration's automatic transaction not landing, per LML#371).
- ``cache.refresh.batch`` span around the gather, carrying batch size and
  outer-concurrency cap.
- Per-item spans in ``cache.dispatch`` (``cache.refresh.identity`` /
  ``.release`` / ``.artist``) — the dispatcher owns those.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from typing import TYPE_CHECKING, Any

import sentry_sdk
from asyncpg.exceptions import PostgresError
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import ValidationError

from cache.dispatch import refresh_identity
from cache.models import (
    BulkCacheRefreshRequest,
    BulkCacheRefreshResponse,
    CacheRefreshResultItem,
)
from core.bulk_concurrency import (
    acquire_bulk_global_permit,
    cancel_and_drain,
    max_concurrency_from_env,
    watch_disconnect,
)
from core.dependencies import get_discogs_service, get_musicbrainz_pg
from discogs.service import DiscogsService
from entity.store import EntityStore
from identity.dependencies import get_entity_store
from streaming.dependencies import (
    get_apple_music_client,
    get_bandcamp_client,
    get_spotify_client,
)

if TYPE_CHECKING:
    from clients.bandcamp import BandcampClient
    from clients.streaming.apple_music import AppleMusicClient
    from clients.streaming.spotify import SpotifyClient
    from entity.sources import PgSourceProtocol

logger = logging.getLogger(__name__)

router = APIRouter(tags=["cache"])

# Per LML#525: cold-cache wall-clock × Railway's ~5min request-timeout ceiling
# pins this cap. 50 ids × (1 release + ~3 artists) × 50 req/min budget ≈ 4 min
# cold-cache wall-clock. Larger caps (the original 200 sketch) yielded ~16 min
# cold-cache — structurally infeasible against Railway's ceiling regardless of
# `watch_disconnect` handling. See the issue's "Latency expectations and cap
# derivation" section.
#
# Downstream callers (most concretely WXYC/Backend-Service#1381's
# rotation-artist-backfill cron migration) should encode this value as a hard
# constant or assert at startup, not expose it as an environment knob —
# recalibration has to land alongside any ingress change.
_REFRESH_INPUT_CAP = 50

# Default outer-gate concurrency. Shared with ``/api/v1/lookup/bulk`` via the
# common ``LML_BULK_MAX_CONCURRENT`` env knob (both endpoints have the same
# outer/inner gate shape and per-item work profile). If observed behavior
# diverges, splitting into ``LML_REFRESH_MAX_CONCURRENT`` with a fallback to
# ``LML_BULK_MAX_CONCURRENT`` is mechanical.
_REFRESH_DEFAULT_CONCURRENCY = 10

_REFRESH_ROUTE = "/api/v1/cache/refresh-for-identities"

_ENTITY_STORE_UNAVAILABLE_DETAIL = (
    "Entity store is not available. Ensure DATABASE_URL_DISCOGS is configured "
    "and the entity schema has been applied."
)

_DISCOGS_SERVICE_UNAVAILABLE_DETAIL = (
    "Discogs service is not available. Ensure DISCOGS_TOKEN (or "
    "DISCOGS_API_KEY / DISCOGS_API_SECRET) is configured."
)


def _require_entity_store(store: EntityStore | None) -> EntityStore:
    if store is None:
        raise HTTPException(status_code=503, detail=_ENTITY_STORE_UNAVAILABLE_DETAIL) from None
    return store


def _require_discogs_service(service: DiscogsService | None) -> DiscogsService:
    if service is None:
        raise HTTPException(status_code=503, detail=_DISCOGS_SERVICE_UNAVAILABLE_DETAIL) from None
    return service


@router.post(
    "/cache/refresh-for-identities",
    response_model=BulkCacheRefreshResponse,
    summary="Warm LML's cache for a batch of release identity_ids",
    description=(
        "For each identity_id in the request, resolves its per-source "
        "external IDs via `entity.release_identity` and fans out the "
        "release-cache refresh to each source's cache layer. After each "
        "refreshed Discogs release, walks the release's artist credits "
        "and refreshes the per-artist cache too — applying the LML#525 "
        "sentinel guard (`artist_id > 0`) at the walk site so VA "
        "compilations with `artist_id = 0` credits don't leak through. "
        "Today only the Discogs leg is wired; other sources return "
        '`release_outcome = "not_implemented"`.'
    ),
    responses={
        200: {"description": "Per-identity verdicts. Tombstones count as success."},
        400: {"description": f"Malformed body or batch above {_REFRESH_INPUT_CAP}-item cap."},
        401: {"description": "Missing or invalid `LML_API_KEY` bearer token."},
        422: {"description": "Empty `identity_ids` list (Pydantic min_length=1)."},
        499: {"description": "Client closed connection before the batch finished."},
        503: {"description": "Entity store or Discogs service not available."},
    },
)
async def handle_refresh_for_identities(
    http_request: Request,
    entity_store: EntityStore | None = Depends(get_entity_store),
    discogs_service: DiscogsService | None = Depends(get_discogs_service),
    mb_pg: PgSourceProtocol | None = Depends(get_musicbrainz_pg),
    spotify_client: SpotifyClient | None = Depends(get_spotify_client),
    apple_music_client: AppleMusicClient | None = Depends(get_apple_music_client),
    bandcamp_client: BandcampClient | None = Depends(get_bandcamp_client),
) -> BulkCacheRefreshResponse:
    """Bulk cache refresh. See route docstring for protocol."""
    # Manual 400-on-overflow before Pydantic so oversize doesn't get 422'd.
    # Same precedent as `/api/v1/lookup/bulk` (lookup/router.py:301-322).
    try:
        body = await http_request.json()
    except (ValueError, TypeError) as e:
        raise HTTPException(status_code=400, detail=f"Malformed JSON body: {e}") from None
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object.")
    ids_raw = body.get("identity_ids")
    if not isinstance(ids_raw, list):
        raise HTTPException(status_code=422, detail="`identity_ids` must be a JSON array.")
    if len(ids_raw) > _REFRESH_INPUT_CAP:
        raise HTTPException(
            status_code=400,
            detail=(f"Batch exceeded the {_REFRESH_INPUT_CAP}-id cap (received {len(ids_raw)})."),
        )

    try:
        request = BulkCacheRefreshRequest.model_validate(body)
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=e.errors()) from None

    store = _require_entity_store(entity_store)
    discogs = _require_discogs_service(discogs_service)

    identity_ids = request.identity_ids
    logger.info("cache refresh start: size=%d", len(identity_ids))

    # One PG read for the whole batch — N×1 round-trips would dominate cold-
    # cache wall-clock at the 50-id cap. Deduplicate the bind list so duplicate
    # identity_ids in the request don't waste a SELECT (the response still
    # carries duplicates; we just don't re-query PG for them).
    try:
        provenance = await store.get_release_identity_provenance_bulk(
            list(dict.fromkeys(identity_ids))
        )
    except (PostgresError, OSError) as e:
        logger.exception("cache refresh: provenance bulk read failed for %d ids", len(identity_ids))
        raise HTTPException(
            status_code=503,
            detail=f"Provenance lookup failed: {type(e).__name__}",
        ) from None

    max_concurrent = max_concurrency_from_env(_REFRESH_DEFAULT_CONCURRENCY)
    semaphore = asyncio.Semaphore(max_concurrent)

    async def _run_one(identity_id: int) -> CacheRefreshResultItem:
        # Batch semaphore OUTER, global permit INNER — the consistent order
        # every bulk-family dispatcher uses (LML#716): the batch semaphore
        # bounds items inside THIS request, the global permit is the
        # cross-request budget shared with /lookup/bulk and identity
        # bulk-resolve.
        async with semaphore, acquire_bulk_global_permit():
            if identity_id not in provenance:
                return CacheRefreshResultItem(
                    identity_id=identity_id,
                    status="not_found",
                    sources=None,
                )
            try:
                return await refresh_identity(
                    identity_id=identity_id,
                    source_pairs=provenance[identity_id],
                    discogs_service=discogs,
                    mb_pg=mb_pg,
                    spotify_client=spotify_client,
                    apple_music_client=apple_music_client,
                    bandcamp_client=bandcamp_client,
                )
            except asyncio.CancelledError:
                # Cancellation always propagates. Without this re-raise, a
                # client-disconnect sentinel win would silently log per-item
                # exceptions instead of unwinding cleanly.
                raise
            except Exception as e:
                # Per-item isolation. Exception class name only — bare str()
                # may leak SQL fragments / upstream error bodies. Full
                # traceback lands in Sentry via the exception breadcrumb.
                logger.exception("cache refresh item failed: identity_id=%d", identity_id)
                return CacheRefreshResultItem(
                    identity_id=identity_id,
                    status="error",
                    sources=None,
                    message=type(e).__name__,
                )

    with sentry_sdk.start_span(op="http.server", name=f"POST {_REFRESH_ROUTE}") as http_span:
        http_span.set_data("http.method", "POST")
        http_span.set_data("http.target", _REFRESH_ROUTE)
        http_span.set_data("lml.cache.size", len(identity_ids))
        http_span.set_data("lml.cache.max_concurrent", max_concurrent)

        with sentry_sdk.start_span(
            op="cache.refresh.batch", name=f"{len(identity_ids)} ids"
        ) as span:
            span.set_data("lml.cache.size", len(identity_ids))
            span.set_data("lml.cache.max_concurrent", max_concurrent)

            gather_future = asyncio.gather(*(_run_one(identity_id) for identity_id in identity_ids))
            sentinel_task = asyncio.create_task(
                watch_disconnect(http_request),
                name="lml.cache.disconnect_sentinel",
            )
            waitables: set[asyncio.Future[Any]] = {gather_future, sentinel_task}

            try:
                done, _pending = await asyncio.wait(waitables, return_when=asyncio.FIRST_COMPLETED)
            except BaseException:
                # Parent cancellation must propagate to both children AND drain
                # them, not just signal. Without the drain, the in-flight items
                # keep holding Discogs semaphore permits while the cancel
                # propagates asynchronously.
                await cancel_and_drain(gather_future)
                await cancel_and_drain(sentinel_task)
                raise

            client_aborted = sentinel_task in done and not gather_future.done()
            span.set_data("lml.cache.client_aborted", client_aborted)

            if client_aborted:
                sentry_sdk.set_tag("lml.client_aborted", "true")
                await cancel_and_drain(gather_future)
                logger.warning(
                    "cache refresh aborted by client: size=%d max_concurrent=%d",
                    len(identity_ids),
                    max_concurrent,
                )
                http_span.set_data("http.status_code", 499)
                raise HTTPException(status_code=499, detail="client disconnected")

            await cancel_and_drain(sentinel_task)
            results = gather_future.result()

        counts = Counter(r.status for r in results)
        http_span.set_data("lml.cache.warmed", counts.get("warmed", 0))
        http_span.set_data("lml.cache.not_found", counts.get("not_found", 0))
        http_span.set_data("lml.cache.not_implemented", counts.get("not_implemented", 0))
        http_span.set_data("lml.cache.error", counts.get("error", 0))
        http_span.set_data("http.status_code", 200)

    logger.info(
        "cache refresh complete: size=%d warmed=%d not_found=%d not_implemented=%d error=%d",
        len(identity_ids),
        counts.get("warmed", 0),
        counts.get("not_found", 0),
        counts.get("not_implemented", 0),
        counts.get("error", 0),
    )

    return BulkCacheRefreshResponse(results=results)
