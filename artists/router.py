"""Artist-search-alias REST API router.

`POST /api/v1/artists/search-aliases/bulk` — composes per-source artist-
name variants for a batch of WXYC canonical artist names. Backend-
Service's artist-search-alias-consumer ETL (BS PR 4 of the artist-search-
alias plan) is the consumer; the response shape is the input substrate
for the alias-aware catalog search LATERAL JOIN (BS PR 5).

Auth: bearer `LML_API_KEY`. Mounted under the same `Depends(require_lml_key)`
posture as `bulk-resolve-libraries`.
"""

from __future__ import annotations

import asyncio
import logging

import sentry_sdk
from asyncpg.exceptions import PostgresError
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import ValidationError
from starlette.requests import ClientDisconnect

from artists.composer import ArtistSearchAliasesComposer
from core.dependencies import get_discogs_cache_service_from_pool
from discogs.cache_service import CacheUnavailableError, DiscogsCacheService
from entity.store import EntityStore
from generated.api_models import (
    ArtistSearchAliasesBulkRequest,
    ArtistSearchAliasesBulkResponse,
)
from identity.dependencies import get_entity_store

logger = logging.getLogger(__name__)


def _resolve_input_cap() -> int:
    """Read the names array `max_length` from the generated Pydantic model.

    Drift guard: api.yaml's `maxItems` for `names` drives the generated
    `ArtistSearchAliasesBulkRequest.names` max_length. The route enforces
    the cap as 413 before Pydantic validation; sourcing the literal from
    the model means a future api.yaml change that regenerates the model
    automatically updates the manual 413 gate.

    Raises at module import time if `max_length` cannot be resolved
    (e.g., a future Pydantic minor bump moves the constraint off
    `.metadata`). Fail loud — a silent fallback to the historical cap of
    1000 would mask any api.yaml reduction (501-N requests would return
    422 instead of the documented 413 until someone noticed).
    """
    for meta in ArtistSearchAliasesBulkRequest.model_fields["names"].metadata:
        max_length = getattr(meta, "max_length", None)
        if max_length is not None:
            return int(max_length)
    raise RuntimeError(
        "Cannot resolve `max_length` from ArtistSearchAliasesBulkRequest.names "
        "metadata — Pydantic constraint shape likely changed. The route's 413 "
        "gate must agree with the generated model; fix or pin the constraint "
        "before this module can import."
    )


# Per api.yaml: max names per request; over-cap returns 413.
_BULK_INPUT_CAP = _resolve_input_cap()

_ROUTE_PATH = "/api/v1/artists/search-aliases/bulk"

_ENTITY_STORE_UNAVAILABLE_DETAIL = (
    "Entity store is not available. Ensure DATABASE_URL_DISCOGS is configured "
    "and the entity schema has been applied."
)
_DISCOGS_CACHE_UNAVAILABLE_DETAIL = (
    "Discogs cache is not available. Ensure DATABASE_URL_DISCOGS is configured."
)


router = APIRouter(tags=["artists"])


@router.post(
    "/artists/search-aliases/bulk",
    response_model=ArtistSearchAliasesBulkResponse,
    summary="Bulk artist-search-alias variants",
    responses={
        200: {"description": "Composed variants per input name."},
        400: {"description": "Malformed JSON body."},
        401: {"description": "Missing or invalid `LML_API_KEY` bearer token."},
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
    # Manual 413 check before Pydantic validation (mirrors `bulk-resolve-
    # libraries`): the generated request model has `max_length=1000`
    # from api.yaml's `maxItems`, but Pydantic's enforcement is 422.
    # The api.yaml contract documents 413 for cap exceedance, so we read
    # the raw count first and raise 413; only then validate.
    try:
        body = await http_request.json()
    except (ValueError, TypeError) as e:
        raise HTTPException(status_code=400, detail=f"Malformed JSON body: {e}") from None
    except ClientDisconnect:
        # Client closed the connection before the body completed. There is
        # no point sending a 5xx — the client is gone — but we want this to
        # land as a 4xx in logs (the route did its job; the failure is the
        # client's). Treat as 400 to keep error-class accounting clean.
        raise HTTPException(
            status_code=400,
            detail="Client disconnected before request body completed.",
        ) from None
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object.")
    names_raw = body.get("names")
    if not isinstance(names_raw, list):
        raise HTTPException(status_code=422, detail="`names` must be a JSON array.")
    if len(names_raw) > _BULK_INPUT_CAP:
        raise HTTPException(
            status_code=413,
            detail=(f"Batch exceeded the {_BULK_INPUT_CAP}-name cap (received {len(names_raw)})."),
        )

    try:
        request = ArtistSearchAliasesBulkRequest.model_validate(body)
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=e.errors()) from None

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
            raise HTTPException(status_code=503, detail=_DISCOGS_CACHE_UNAVAILABLE_DETAIL) from None
        except (PostgresError, OSError):
            # Phase 1: entity-store PG failure (raw asyncpg error — not
            # wrapped). Phase 2 errors are wrapped above; if a PostgresError
            # reaches here, it came from `bulk_resolve_library_names`.
            logger.exception(
                "Entity store query failed during artist-search-alias compose (names=%d)",
                names_count,
            )
            http_span.set_data("http.status_code", 503)
            raise HTTPException(status_code=503, detail=_ENTITY_STORE_UNAVAILABLE_DETAIL) from None
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
