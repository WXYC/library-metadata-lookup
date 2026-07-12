"""Artist REST API router.

`POST /api/v1/artists/search-aliases/bulk` — composes per-source artist-
name variants for a batch of WXYC canonical artist names. Backend-
Service's artist-search-alias-consumer ETL (BS PR 4 of the artist-search-
alias plan) is the consumer; the response shape is the input substrate
for the alias-aware catalog search LATERAL JOIN (BS PR 5).

`POST /api/v1/artists/resolve/bulk` — bulk bare-name artist resolution
under the LML#759 verify-before-mint model (tier orchestration in
`artists/resolver.py`). Backend-Service's concerts pipeline (BS#1614) is
the consumer.

Auth: bearer `LML_API_KEY`. Mounted under the same `Depends(require_lml_key)`
posture as `bulk-resolve-libraries`.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from typing import TYPE_CHECKING

import sentry_sdk
from fastapi import APIRouter, Depends, HTTPException, Request
from wxyc_fastapi.observability import RequestTelemetry, init_cache_stats

from artists.composer import ArtistSearchAliasesComposer
from artists.resolver import BareNameArtistResolver, InvalidNameError
from core.bulk_body import TRANSIENT_PG_ERRORS, parse_bulk_body, resolve_input_cap
from core.dependencies import (
    get_artist_resolve_posthog_client,
    get_discogs_cache_service_from_pool,
    get_discogs_service,
)
from discogs.cache_service import CacheUnavailableError, DiscogsCacheService
from discogs.service import BREAKER_OPEN_STAT_KEY, DiscogsService
from entity.store import EntityStore
from generated.api_models import (
    ArtistResolveBulkRequest,
    ArtistResolveBulkResponse,
    ArtistSearchAliasesBulkRequest,
    ArtistSearchAliasesBulkResponse,
)
from identity.dependencies import get_entity_store

if TYPE_CHECKING:
    from posthog import Posthog

logger = logging.getLogger(__name__)


# Per api.yaml: max names per request; over-cap returns 413.
_BULK_INPUT_CAP = resolve_input_cap(ArtistSearchAliasesBulkRequest, "names")
_RESOLVE_INPUT_CAP = resolve_input_cap(ArtistResolveBulkRequest, "names")

_ROUTE_PATH = "/api/v1/artists/search-aliases/bulk"
_RESOLVE_ROUTE_PATH = "/api/v1/artists/resolve/bulk"

_ENTITY_STORE_UNAVAILABLE_DETAIL = (
    "Entity store is not available. Ensure DATABASE_URL_DISCOGS is configured "
    "and the entity schema has been applied."
)
_DISCOGS_CACHE_UNAVAILABLE_DETAIL = (
    "Discogs cache is not available. Ensure DATABASE_URL_DISCOGS is configured."
)
# Mid-flight variants: the dependency gates above prove the wiring existed at
# request start, so a failure DURING the request is most often a transient
# pool/connection loss (deploy-window teardown), with schema drift a distant
# second. Distinct from the gate details so on-call doesn't chase
# DATABASE_URL_DISCOGS for a blip that retrying clears.
_ENTITY_STORE_QUERY_FAILED_DETAIL = (
    "Entity store query failed — likely a transient connection loss (retrying "
    "may succeed) or the entity schema has not been applied."
)
_DISCOGS_CACHE_QUERY_FAILED_DETAIL = (
    "Discogs cache query failed — likely a transient connection loss; retrying may succeed."
)


router = APIRouter(tags=["artists"])


@router.post(
    "/artists/search-aliases/bulk",
    response_model=ArtistSearchAliasesBulkResponse,
    summary="Bulk artist-search-alias variants",
    responses={
        200: {"description": "Composed variants per input name."},
        400: {"description": "Malformed JSON body."},
        401: {"description": "Missing `Authorization` header."},
        403: {"description": "Present but invalid/malformed `LML_API_KEY` bearer token."},
        413: {"description": "Batch exceeded the per-request name cap."},
        422: {"description": "Request body failed Pydantic validation."},
        503: {"description": "Entity store or Discogs cache not available."},
    },
)
async def search_aliases_bulk(
    http_request: Request,
    entity_store: EntityStore | None = Depends(get_entity_store),
    discogs_cache: DiscogsCacheService | None = Depends(get_discogs_cache_service_from_pool),
) -> ArtistSearchAliasesBulkResponse:
    """Compose artist-search-alias variants for a batch of input names.

    Two-phase batched composer: 1-3 PG round-trips to `entity.identity`
    for name resolution, plus 5 PG round-trips to the discogs-cache for
    the set of resolved artists. No Discogs API escalation; no
    `asyncio.gather` over per-name leaves (LML#370/#372 cascade-cascade
    avoidance).
    """
    request = await parse_bulk_body(
        http_request, ArtistSearchAliasesBulkRequest, _BULK_INPUT_CAP, field="names"
    )

    if entity_store is None:
        raise HTTPException(status_code=503, detail=_ENTITY_STORE_UNAVAILABLE_DETAIL)
    if discogs_cache is None:
        raise HTTPException(status_code=503, detail=_DISCOGS_CACHE_UNAVAILABLE_DETAIL)

    composer = ArtistSearchAliasesComposer(
        entity_store=entity_store,
        discogs_cache=discogs_cache,
    )

    names_count = len(request.names)
    logger.info("artist-search-alias bulk start: names=%d", names_count)

    # Explicit `http.server` span (mirrors `bulk-resolve-libraries` —
    # defense-in-depth against the FastApi auto-instrumentation not
    # landing for this route). Query in trace explorer with
    # `op:http.server span.description:*search-aliases/bulk*`.
    with sentry_sdk.start_span(
        op="http.server",
        name=f"POST {_ROUTE_PATH}",
    ) as http_span:
        http_span.set_data("http.method", "POST")
        http_span.set_data("http.target", _ROUTE_PATH)
        http_span.set_data("lml.search_aliases.names", names_count)

        try:
            response = await composer.compose(request.names)
        except asyncio.CancelledError:
            # Client aborted mid-compose. Pin 499 on the span so the
            # trace explorer query
            # `op:http.server http.status_code:499 description:*search-aliases/bulk*`
            # surfaces aborts in Sentry — same observability gap LML#430
            # paid down for the sibling bulk-resolve-libraries route.
            # CancelledError is re-raised (not converted to HTTPException):
            # the asyncio cancellation contract requires this, and the
            # client is gone anyway.
            http_span.set_data("http.status_code", 499)
            logger.warning(
                "artist-search-alias bulk aborted by client: names=%d",
                names_count,
            )
            raise
        except CacheUnavailableError:
            # Phase 2: discogs-cache pool/PG failure. Distinct detail string
            # from the entity-store path so the on-call sees which
            # subsystem actually failed.
            logger.exception(
                "Discogs cache unavailable during artist-search-alias compose (names=%d)",
                names_count,
            )
            http_span.set_data("http.status_code", 503)
            raise HTTPException(
                status_code=503, detail=_DISCOGS_CACHE_QUERY_FAILED_DETAIL
            ) from None
        except TRANSIENT_PG_ERRORS:
            # Phase 1: entity-store PG failure (raw asyncpg error — not
            # wrapped). Phase 2 errors are wrapped above; if a PostgresError
            # reaches here, it came from `bulk_resolve_library_names`.
            # InterfaceError: asyncpg's client-side pool-lifecycle errors
            # subclass neither PostgresError nor OSError — same transient
            # 503 class as the resolve route's arm.
            logger.exception(
                "Entity store query failed during artist-search-alias compose (names=%d)",
                names_count,
            )
            http_span.set_data("http.status_code", 503)
            raise HTTPException(status_code=503, detail=_ENTITY_STORE_QUERY_FAILED_DETAIL) from None
        except HTTPException:
            # HTTPException carries its own status code, set by whoever
            # raised it (either us above, or future inner code). Don't
            # blanket-stamp 500 — that would lie to Sentry's
            # http.status_code aggregation about the real response code.
            # Re-raise unchanged.
            raise
        except Exception:
            # Pin 500 on the span so unhandled-exception traces still carry
            # http.status_code for per-route status aggregation. Re-raise
            # so FastAPI's default handler produces the response.
            http_span.set_data("http.status_code", 500)
            raise

        http_span.set_data("lml.search_aliases.resolved", len(response.artists))
        http_span.set_data("lml.search_aliases.missing", len(response.missing))
        http_span.set_data("http.status_code", 200)

    logger.info(
        "artist-search-alias bulk complete: names=%d resolved=%d missing=%d",
        names_count,
        len(response.artists),
        len(response.missing),
    )
    return response


def _emit_resolve_summary(
    posthog_client: Posthog | None,
    telemetry: RequestTelemetry,
    summary: dict,
) -> None:
    """Best-effort aggregate emit — one `artist_resolve_completed` event per
    SUCCESSFULLY COMPLETED request, no per-name events. The entire emit path
    is inside the swallow so it can never change the handler's outcome (the
    swallow mirrors streaming-check's LML#639 posture; unlike streaming-check,
    error exits deliberately emit nothing — the Sentry span's status pins
    carry error-class observability for this offline-consumer route).

    No summary key may shadow a framework property (``api_calls``, ``steps``,
    ``cache``, ``total_duration_ms``): ``ResolveStats`` names its probe
    counter ``discogs_api_calls`` for exactly this reason, so the summary
    passes through verbatim.
    """
    if not posthog_client:
        return
    try:
        telemetry.send_to_posthog(posthog_client, dict(summary))
    except Exception:
        logger.warning("artist-resolve telemetry emit failed", exc_info=True)


@router.post(
    "/artists/resolve/bulk",
    response_model=ArtistResolveBulkResponse,
    summary="Bulk bare-name artist resolution",
    responses={
        200: {"description": "Per-name verdicts, index-aligned with the request."},
        400: {"description": "Malformed JSON body."},
        401: {"description": "Missing `Authorization` header."},
        403: {"description": "Present but invalid/malformed `LML_API_KEY` bearer token."},
        413: {"description": "Batch exceeded the per-request name cap."},
        422: {
            "description": "Request body failed Pydantic validation, or a name is "
            "semantically unresolvable (blank, NUL, unencodable, empty identity-match "
            "form, bare parenthesized number)."
        },
        503: {"description": "Entity store or Discogs cache not available."},
    },
)
async def resolve_bulk(
    http_request: Request,
    entity_store: EntityStore | None = Depends(get_entity_store),
    discogs_cache: DiscogsCacheService | None = Depends(get_discogs_cache_service_from_pool),
    discogs_service: DiscogsService | None = Depends(get_discogs_service),
    posthog_client: Posthog | None = Depends(get_artist_resolve_posthog_client),
) -> ArtistResolveBulkResponse:
    """Resolve bare artist names to Discogs artist identities (LML#759).

    Verify-before-mint: verdict semantics and tier orchestration live in
    `artists/resolver.py`. A missing Discogs service (no token) or an
    LML#755 breaker shed degrades per-name to `escalation_unavailable` —
    the PG tiers keep answering, never a batch-level 503.
    """
    request = await parse_bulk_body(
        http_request, ArtistResolveBulkRequest, _RESOLVE_INPUT_CAP, field="names"
    )

    if entity_store is None:
        raise HTTPException(status_code=503, detail=_ENTITY_STORE_UNAVAILABLE_DETAIL)
    if discogs_cache is None:
        raise HTTPException(status_code=503, detail=_DISCOGS_CACHE_UNAVAILABLE_DETAIL)

    resolver = BareNameArtistResolver(
        entity_store=entity_store,
        discogs_cache=discogs_cache,
        discogs_service=discogs_service,
    )

    names = [n.root for n in request.names]
    dry_run = bool(request.dry_run)
    logger.info("artist-resolve bulk start: names=%d dry_run=%s", len(names), dry_run)

    # Per-request telemetry bracket: `search_artists` records API calls into
    # the cache-stats context (the breaker-shed counter seeds to 0 so it is
    # a stable, alertable series rather than a key that appears only on shed
    # requests — the LML#544 shape rule), and RequestTelemetry carries the
    # aggregate event. Explicit `http.server` span mirrors the siblings —
    # query with `op:http.server span.description:*artists/resolve/bulk*`.
    init_cache_stats(extra_keys=(BREAKER_OPEN_STAT_KEY,))
    telemetry = RequestTelemetry(
        api_call_keys=["discogs"],
        distinct_id="library-metadata-lookup-service",
        event_prefix="artist_resolve",
    )
    with sentry_sdk.start_span(
        op="http.server",
        name=f"POST {_RESOLVE_ROUTE_PATH}",
    ) as http_span:
        http_span.set_data("http.method", "POST")
        http_span.set_data("http.target", _RESOLVE_ROUTE_PATH)
        http_span.set_data("lml.artist_resolve.names", len(names))
        http_span.set_data("lml.artist_resolve.dry_run", dry_run)

        try:
            results, stats = await resolver.resolve(names, dry_run=dry_run)
        except asyncio.CancelledError:
            # Client aborted mid-resolve. Pin 499 for the trace explorer,
            # re-raise per the asyncio cancellation contract (see sibling).
            http_span.set_data("http.status_code", 499)
            logger.warning("artist-resolve bulk aborted by client: names=%d", len(names))
            raise
        except InvalidNameError as e:
            # The resolver's input-validation contract: semantically
            # unresolvable names (blank, NUL, lone surrogates, empty
            # identity-match form, bare parenthesized numbers) are caller
            # errors, never in-band verdicts. Pydantic can't express these
            # (constr(min_length=1) admits whitespace), so the resolver
            # raises and the route maps to 422 per api.yaml. The dedicated
            # subclass — never bare ValueError — keeps a server-side
            # ValueError from masquerading as caller error.
            http_span.set_data("http.status_code", 422)
            raise HTTPException(status_code=422, detail=str(e)) from None
        except CacheUnavailableError:
            # Tier-2 candidate queries: discogs-cache pool/PG failure.
            logger.exception(
                "Discogs cache unavailable during artist-resolve (names=%d)", len(names)
            )
            http_span.set_data("http.status_code", 503)
            raise HTTPException(
                status_code=503, detail=_DISCOGS_CACHE_QUERY_FAILED_DETAIL
            ) from None
        except TRANSIENT_PG_ERRORS:
            # Tier-1 read or write-back: raw entity-store PG failure.
            # InterfaceError (asyncpg's client-side pool/connection
            # lifecycle errors) subclasses neither PostgresError nor
            # OSError — without it a pool-teardown race during deploy
            # would read as an application 500 instead of the transient
            # 503 the sibling failure modes emit. The 503 classification
            # hides nothing from Sentry: `logger.exception` is ERROR-level,
            # which the default LoggingIntegration captures with the stack
            # trace, so genuine server bugs in this class still page.
            logger.exception(
                "Entity store query failed during artist-resolve (names=%d)", len(names)
            )
            http_span.set_data("http.status_code", 503)
            raise HTTPException(status_code=503, detail=_ENTITY_STORE_QUERY_FAILED_DETAIL) from None
        except HTTPException:
            # Carries its own status code — don't blanket-stamp 500.
            raise
        except Exception:
            http_span.set_data("http.status_code", 500)
            raise

        # Mirror the resolver's probe count into the framework per-service
        # map so cross-event `api_calls.discogs` slicing (the sibling
        # *_completed convention) includes this route. Same semantics as
        # `discogs_api_calls`: probes dispatched, including failed returns.
        telemetry.api_calls["discogs"] = stats.discogs_api_calls

        # Every ResolveStats field lands verbatim as a span key, PostHog
        # property, and log field — one enumeration, derived from the
        # dataclass, so a new counter can't be dropped from one surface.
        summary = {**dataclasses.asdict(stats), "dry_run": dry_run}
        for key, value in summary.items():
            if key in ("names", "dry_run"):
                # Already pinned pre-try from the request, so error exits
                # carry them too; re-writing from stats would put two
                # provenances behind one span key.
                continue
            http_span.set_data(f"lml.artist_resolve.{key}", value)
        http_span.set_data("http.status_code", 200)

    _emit_resolve_summary(posthog_client, telemetry, summary)
    logger.info(
        "artist-resolve bulk complete: %s",
        " ".join(f"{key}={value}" for key, value in summary.items()),
    )
    return ArtistResolveBulkResponse(results=results)
