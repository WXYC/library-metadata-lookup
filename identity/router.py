"""Identity resolution REST API router.

Provides:

- ``GET /identity/resolve`` and ``POST /identity/bulk`` — query the
  ``entity.identity`` table in the discogs-cache database. Used by
  semantic-index.

- ``POST /api/v1/identity/bulk-resolve-libraries`` — the cross-cache-identity
  contract endpoint added by WXYC/library-metadata-lookup#272 (per the
  2026-05-09 pivot, BS#800). Backend POSTs library rows; LML composes
  per-source provenance into a single verdict per row using the §3.4.1.1
  rules. Composition lives in ``identity/bulk_resolve.py``.
"""

import logging

from asyncpg.exceptions import PostgresError
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import ValidationError

from generated.api_models import (
    BulkResolveLibrariesRequest,
    BulkResolveLibrariesResponse,
)
from identity.bulk_resolve import compilation_result, compose_for_identity
from identity.dependencies import get_entity_store
from identity.models import (
    BulkIdentityRequest,
    BulkIdentityResponse,
    IdentityResponse,
)
from scripts.entity_resolution.store import EntityStore, Identity

# Lazy imports inside the handler:
#   wxyc_etl.text.is_compilation_artist — required at handler scope only.

# Per api.yaml: max 1,000 inputs per request; over-cap returns 413.
_BULK_RESOLVE_INPUT_CAP = 1000

logger = logging.getLogger(__name__)

router = APIRouter(tags=["identity"])

# Separate router for the `/api/v1/identity/...` surface so `main.py` can
# attach the LML bearer auth dep here without affecting the open
# `/identity/resolve` and `/identity/bulk` routes (consumed by
# semantic-index). The bulk-resolve-libraries endpoint is the only member
# at the moment; future cross-cache-identity endpoints land here too.
api_v1_router = APIRouter(tags=["lookup"])

_ENTITY_STORE_UNAVAILABLE_DETAIL = (
    "Entity store is not available. Ensure DATABASE_URL_DISCOGS is configured "
    "and the entity schema has been applied."
)


def _identity_to_response(identity: Identity) -> IdentityResponse:
    """Convert an EntityStore Identity dataclass to a Pydantic response model."""
    return IdentityResponse(
        library_name=identity.library_name,
        discogs_artist_id=identity.discogs_artist_id,
        wikidata_qid=identity.wikidata_qid,
        musicbrainz_artist_id=identity.musicbrainz_artist_id,
        spotify_artist_id=identity.spotify_artist_id,
        apple_music_artist_id=identity.apple_music_artist_id,
        bandcamp_id=identity.bandcamp_id,
        reconciliation_status=identity.reconciliation_status,
    )


def _require_entity_store(store: EntityStore | None) -> EntityStore:
    """Raise 503 if the entity store is not available."""
    if store is None:
        raise HTTPException(status_code=503, detail=_ENTITY_STORE_UNAVAILABLE_DETAIL) from None
    return store


@router.get(
    "/resolve",
    response_model=IdentityResponse,
    summary="Resolve a single artist name to external identifiers",
    responses={
        200: {"description": "Identity found"},
        404: {"description": "No identity found for the given name"},
        503: {"description": "Entity store not available"},
    },
)
async def resolve_identity(
    name: str = Query(..., description="Artist name to resolve"),
    entity_store: EntityStore | None = Depends(get_entity_store),
) -> IdentityResponse:
    """Look up a single artist name in the entity identity store."""
    store = _require_entity_store(entity_store)
    try:
        identity = await store.get_identity(name)
    except (PostgresError, OSError):
        logger.exception("Entity store query failed for name=%r", name)
        raise HTTPException(status_code=503, detail=_ENTITY_STORE_UNAVAILABLE_DETAIL) from None
    if identity is None:
        raise HTTPException(status_code=404, detail=f"No identity found for '{name}'")
    return _identity_to_response(identity)


@router.post(
    "/bulk",
    response_model=BulkIdentityResponse,
    summary="Resolve multiple artist names to external identifiers",
    responses={
        200: {"description": "Bulk resolution completed"},
        503: {"description": "Entity store not available"},
    },
)
async def bulk_resolve_identities(
    request: BulkIdentityRequest,
    entity_store: EntityStore | None = Depends(get_entity_store),
) -> BulkIdentityResponse:
    """Resolve multiple artist names in a single request.

    Returns resolved identities and a list of names that could not be found.
    Designed for batch consumers like semantic-index (20-30K artists).
    """
    store = _require_entity_store(entity_store)

    identities: list[IdentityResponse] = []
    unresolved: list[str] = []

    for name in request.names:
        try:
            identity = await store.get_identity(name)
        except (PostgresError, OSError):
            # Fail closed on partial PG failure: the caller cannot distinguish
            # "name had no identity" from "PG died before this name was tried".
            logger.exception("Entity store query failed mid-bulk for name=%r", name)
            raise HTTPException(status_code=503, detail=_ENTITY_STORE_UNAVAILABLE_DETAIL) from None
        if identity is not None:
            identities.append(_identity_to_response(identity))
        else:
            unresolved.append(name)

    return BulkIdentityResponse(identities=identities, unresolved=unresolved)


@api_v1_router.post(
    "/identity/bulk-resolve-libraries",
    response_model=BulkResolveLibrariesResponse,
    summary="Bulk-resolve cross-cache identity for library rows",
    responses={
        200: {"description": "Verdicts for every request input, in input order."},
        401: {"description": "Missing or invalid `LML_API_KEY` bearer token."},
        413: {"description": "Batch exceeded the 1,000-input cap."},
        503: {"description": "Entity store not available."},
    },
)
async def bulk_resolve_libraries(
    http_request: Request,
    entity_store: EntityStore | None = Depends(get_entity_store),
) -> BulkResolveLibrariesResponse:
    """Compose cross-cache-identity verdicts for a batch of library rows.

    Per the 2026-05-09 architecture pivot
    (WXYC/Backend-Service#800), LML is the sole composer; Backend writes
    the response verbatim into `library_identity` /
    `library_identity_source`. Composition follows §3.4.1.1 Rules 2-6
    (Rule 1 — manual override — is Backend's responsibility).

    Per-input handling:

    - V/A rows (detected via ``wxyc_etl.text.is_compilation_artist`` on
      ``artist_name``) return ``kind: compilation`` with empty
      ``tracks: []`` — full track resolution lands in
      WXYC/library-metadata-lookup#271.
    - Otherwise we look up ``artist_name`` in ``entity.identity`` and
      compose per-source provenance via ``identity.bulk_resolve``.
    - When the lookup misses we return ``kind: unresolved`` so Backend
      can cache the no-match verdict (with TTL) and avoid re-asking.

    Response order matches request input order, as per the api.yaml
    contract.
    """
    # Lazy import — wxyc_etl is a Rust extension; importing at module load
    # would couple every importer of this module (e.g. unit tests that
    # don't touch this endpoint) to the wxyc_etl ABI.
    from wxyc_etl.text import is_compilation_artist

    # Parse + cap-check manually rather than via a typed body parameter:
    # the codegen-derived `BulkResolveLibrariesRequest` carries
    # `max_length=1000` (extracted from api.yaml's `maxItems: 1000`), which
    # would let Pydantic short-circuit over-cap requests as 422. The
    # api.yaml contract documents 413 explicitly for cap exceedance, so we
    # check the raw input count first and raise 413; only then do we run
    # full per-input validation.
    try:
        body = await http_request.json()
    except (ValueError, TypeError) as e:
        raise HTTPException(status_code=400, detail=f"Malformed JSON body: {e}") from None
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object.")
    inputs_raw = body.get("inputs")
    if not isinstance(inputs_raw, list):
        raise HTTPException(status_code=422, detail="`inputs` must be a JSON array.")
    if len(inputs_raw) > _BULK_RESOLVE_INPUT_CAP:
        raise HTTPException(
            status_code=413,
            detail=(
                f"Batch exceeded the {_BULK_RESOLVE_INPUT_CAP}-input cap "
                f"(received {len(inputs_raw)})."
            ),
        )

    try:
        request = BulkResolveLibrariesRequest.model_validate(body)
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=e.errors()) from None

    store = _require_entity_store(entity_store)

    results = []
    for input_row in request.inputs:
        if is_compilation_artist(input_row.artist_name):
            results.append(compilation_result(input_row.library_id))
            continue

        try:
            identity: Identity | None = await store.get_identity(input_row.artist_name)
        except (PostgresError, OSError):
            # Fail closed on partial PG failure — the caller cannot
            # distinguish "row had no identity" from "PG died before this
            # row was tried". Same posture as ``/identity/bulk``.
            logger.exception(
                "Entity store query failed mid-bulk-resolve for library_id=%d artist_name=%r",
                input_row.library_id,
                input_row.artist_name,
            )
            raise HTTPException(status_code=503, detail=_ENTITY_STORE_UNAVAILABLE_DETAIL) from None

        try:
            result = await compose_for_identity(input_row.library_id, identity, store)
        except (PostgresError, OSError):
            logger.exception(
                "Provenance fetch failed mid-bulk-resolve for library_id=%d",
                input_row.library_id,
            )
            raise HTTPException(status_code=503, detail=_ENTITY_STORE_UNAVAILABLE_DETAIL) from None
        results.append(result)

    return BulkResolveLibrariesResponse(results=results)
