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

import asyncio
import logging
from collections import Counter

import sentry_sdk
from asyncpg.exceptions import PostgresError
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import ValidationError

from entity.store import EntityStore, Identity
from generated.api_models import (
    BulkResolveLibrariesRequest,
    BulkResolveLibrariesResponse,
    ReleaseIdentityResolveRequest,
    ReleaseIdentityResolveResponse,
)
from identity.bulk_resolve import compilation_result, compose_for_identity
from identity.dependencies import get_entity_store
from identity.models import (
    BulkIdentityRequest,
    BulkIdentityResponse,
    IdentityResponse,
)
from identity.release_validation import (
    InvalidReleaseExternalIdError,
    validate_and_canonicalize_external_id,
)

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
    return IdentityResponse.from_identity(identity)


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
            identities.append(IdentityResponse.from_identity(identity))
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

    # Entry signal (LML#430, sibling-of-#371). Fires before the per-input PG
    # loop, so a handler that hangs inside the loop still leaves a trace —
    # uvicorn's access log and Sentry's automatic `http.server` transaction
    # only commit on response completion, and the LML#355 audit hit exactly
    # that gap (zero spans for 60 batches over the 2026-05-20 prod dry-run
    # despite the handler running). Note: `await http_request.json()` above
    # ran before this point, so the "before any awaits" guarantee is for the
    # downstream loop only; a hang during JSON parse is not covered here, but
    # body parse is bounded by request size and was not the LML#355 failure
    # mode.
    inputs_count = len(request.inputs)
    logger.info("bulk resolve start: inputs=%d", inputs_count)

    # Explicit `http.server` span (LML#430). Defense-in-depth against the
    # FastApiIntegration's automatic transaction not landing for this endpoint
    # — `op:http.server span.description:*bulk-resolve-libraries*` queries in
    # the trace explorer will surface bulk-resolve traffic even when the
    # automatic instrumentation misses. Mirrors PR #417's pattern for
    # `/api/v1/lookup/bulk`.
    with sentry_sdk.start_span(
        op="http.server",
        name="POST /api/v1/identity/bulk-resolve-libraries",
    ) as http_span:
        http_span.set_data("http.method", "POST")
        http_span.set_data("http.target", "/api/v1/identity/bulk-resolve-libraries")
        http_span.set_data("lml.bulk_resolve.inputs", inputs_count)

        results = []
        try:
            for input_row in request.inputs:
                if is_compilation_artist(input_row.artist_name):
                    results.append(compilation_result(input_row.library_id))
                    continue

                try:
                    # Three-leg fall-through lookup (issues #274 / #276): exact
                    # match first (handles the 99.8% dominant case where
                    # Backend's `library.artist_name` shape equals storage),
                    # then case-insensitive `LOWER()` (catches pure case
                    # drift), then canonical form (catches diacritic /
                    # `&`-vs-`and` / smart-quote / etc. divergence when
                    # storage happens to be canonical). Strictly ≥ legacy
                    # `get_identity()` hit rate.
                    identity: Identity | None = await store.resolve_library_name(
                        input_row.artist_name
                    )
                except (PostgresError, OSError):
                    # Fail closed on partial PG failure — the caller cannot
                    # distinguish "row had no identity" from "PG died before
                    # this row was tried". Same posture as ``/identity/bulk``.
                    logger.exception(
                        "Entity store query failed mid-bulk-resolve for "
                        "library_id=%d artist_name=%r",
                        input_row.library_id,
                        input_row.artist_name,
                    )
                    http_span.set_data("http.status_code", 503)
                    raise HTTPException(
                        status_code=503, detail=_ENTITY_STORE_UNAVAILABLE_DETAIL
                    ) from None

                try:
                    result = await compose_for_identity(input_row.library_id, identity, store)
                except (PostgresError, OSError):
                    logger.exception(
                        "Provenance fetch failed mid-bulk-resolve for library_id=%d",
                        input_row.library_id,
                    )
                    http_span.set_data("http.status_code", 503)
                    raise HTTPException(
                        status_code=503, detail=_ENTITY_STORE_UNAVAILABLE_DETAIL
                    ) from None
                results.append(result)
        except asyncio.CancelledError:
            # Client aborted mid-loop. The sequential per-input loop has no
            # gather/sentinel structure (unlike PR #417's `/lookup/bulk`
            # shape), so CancelledError raised at the next `await` point is
            # the only abort signal we get. Pin 499 on the span before
            # re-raising so the audit-style query
            # `op:http.server http.status_code:499` surfaces these in Sentry,
            # and emit a warn log so Railway also carries the record.
            # CancelledError is re-raised (not converted to HTTPException) —
            # the asyncio cancellation contract requires this, and the client
            # is gone anyway so there is nobody to read a 499 response.
            http_span.set_data("http.status_code", 499)
            logger.warning(
                "bulk resolve aborted by client: inputs=%d processed=%d",
                inputs_count,
                len(results),
            )
            raise

        # Exit signal pairs with the entry log so operators can confirm
        # response delivery and read off the per-kind verdict breakdown
        # without correlating to a Sentry trace. The kind enum values
        # (`single_artist`, `compilation`, `unresolved`) come from
        # `BulkResolveResultKind` and are what the LML#355 audit needed when
        # it had to fall back to local-cache simulation.
        kinds = Counter(r.kind.value for r in results)
        logger.info(
            "bulk resolve complete: inputs=%d single_artist=%d compilation=%d unresolved=%d",
            inputs_count,
            kinds.get("single_artist", 0),
            kinds.get("compilation", 0),
            kinds.get("unresolved", 0),
        )
        http_span.set_data("lml.bulk_resolve.single_artist", kinds.get("single_artist", 0))
        http_span.set_data("lml.bulk_resolve.compilation", kinds.get("compilation", 0))
        http_span.set_data("lml.bulk_resolve.unresolved", kinds.get("unresolved", 0))
        http_span.set_data("http.status_code", 200)

    return BulkResolveLibrariesResponse(results=results)


@api_v1_router.post(
    "/identity/resolve",
    response_model=ReleaseIdentityResolveResponse,
    summary="Mint or resolve a stable identity for a release",
    responses={
        200: {"description": "Identity resolved (minted or pre-existing)."},
        401: {"description": "Missing or invalid `LML_API_KEY` bearer token."},
        422: {"description": "Sentinel input rejected before any DB write."},
        503: {"description": "Entity store not available."},
    },
)
async def resolve_release_identity(
    request: ReleaseIdentityResolveRequest,
    entity_store: EntityStore | None = Depends(get_entity_store),
) -> ReleaseIdentityResolveResponse:
    """Mint or look up an ``entity.release_identity`` row from `(source, external_id)`.

    Idempotent — same input always returns the same ``identity_id``; only the
    first call mints. Pydantic rejects unknown ``kind`` / ``source`` upstream
    (→ 422). Per-source sentinel rules (Discogs id ≤ 0, malformed Bandcamp URL)
    run via ``validate_and_canonicalize_external_id`` before any DB write, so
    no poisoned rows can be created.

    Observability: emits entry/exit INFO logs and wraps the store call in an
    explicit ``http.server`` span. Same posture as ``bulk_resolve_libraries``
    above — the FastApiIntegration's automatic transaction is not reliable for
    ``/api/v1/`` handlers (LML#355 audit), and the explicit span keeps trace-
    explorer queries like ``op:http.server span.description:*identity/resolve*``
    accurate when the auto-instrumentation gaps out. The entry log fires
    before the span opens so a hang inside the store call still leaves a log
    trace; the exit log fires after the span closes so its timestamp reflects
    span completion.
    """
    store = _require_entity_store(entity_store)

    try:
        canonical_external_id = validate_and_canonicalize_external_id(
            request.source.value, request.external_id
        )
    except InvalidReleaseExternalIdError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None

    source_value = request.source.value
    logger.info("release-identity resolve start: source=%s", source_value)

    with sentry_sdk.start_span(
        op="http.server",
        name="POST /api/v1/identity/resolve",
    ) as http_span:
        http_span.set_data("http.method", "POST")
        http_span.set_data("http.target", "/api/v1/identity/resolve")
        http_span.set_data("lml.identity_resolve.source", source_value)

        try:
            identity_id, minted = await store.mint_or_get_release_identity(
                source=source_value, external_id=canonical_external_id
            )
        except (PostgresError, OSError):
            logger.exception(
                "release-identity resolve failed for source=%r external_id=%r",
                source_value,
                canonical_external_id,
            )
            http_span.set_data("http.status_code", 503)
            raise HTTPException(status_code=503, detail=_ENTITY_STORE_UNAVAILABLE_DETAIL) from None

        http_span.set_data("lml.identity_resolve.minted", minted)
        http_span.set_data("http.status_code", 200)

    logger.info(
        "release-identity resolve complete: source=%s minted=%s identity_id=%d",
        source_value,
        minted,
        identity_id,
    )

    return ReleaseIdentityResolveResponse(
        identity_id=identity_id,
        kind=request.kind,
        minted=minted,
    )
