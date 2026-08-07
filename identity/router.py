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
from fastapi import APIRouter, Depends, HTTPException, Query, Request

from core.bulk_body import (
    TRANSIENT_PG_ERRORS,
    parse_bulk_body,
    resolve_input_cap,
)
from core.bulk_concurrency import (
    acquire_bulk_global_permit,
    max_concurrency_from_env,
    run_bulk_gather,
    watch_disconnect,
)
from core.dependencies import discogs_pool_max_size
from discogs.ratelimit import set_discogs_low_priority
from entity.store import EntityStore, Identity
from generated.api_models import (
    BulkResolveInput,
    BulkResolveLibrariesRequest,
    BulkResolveLibrariesResponse,
    BulkResolveResult,
    ReleaseIdentityResolveRequest,
    ReleaseIdentityResolveResponse,
    TracksContractVersion,
)
from identity.bulk_resolve import compilation_result, compose_for_identity
from identity.dependencies import get_entity_store, require_entity_store
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

# Per api.yaml: max inputs per request; over-cap returns 413. Read from the
# generated model's JSON schema so a regenerated `maxItems` moves the 413
# gate in lockstep (LML#767 drift guard — the artists routes' pattern).
_BULK_RESOLVE_INPUT_CAP = resolve_input_cap(BulkResolveLibrariesRequest, "inputs")


def _bulk_resolve_default_concurrency() -> int:
    """Default ceiling on concurrent per-input lookups (LML#278).

    Each per-input coroutine holds at most one pooled connection at a time —
    ``resolve_library_name``'s three legs and ``compose_for_identity``'s
    provenance read are sequential awaits within a single coroutine, never
    concurrent — so a semaphore sized to the discogs-cache pool's ``max_size``
    saturates the pool without coroutines queueing on ``pool.acquire()`` (which
    would otherwise block up to the pool's 10 s acquire timeout). The default
    therefore IS the pool max, per the issue's "default to the asyncpg pool's
    max_size" acceptance criterion.

    Delegates to :func:`core.dependencies.discogs_pool_max_size` — the same
    accessor the pool is built from (LML#706) — so raising or lowering the pool
    moves this cap with it: a hardcoded ``5`` would silently under-parallelize a
    widened pool, or (worse) admit more coroutines than a narrowed pool has
    connections. Still overridable at runtime via the shared
    ``LML_BULK_MAX_CONCURRENT`` knob (see ``core.bulk_concurrency``), which — when
    set — supersedes this pool-derived default entirely.

    This per-request gate deliberately coexists with the LML#716 global
    permit (``acquire_bulk_global_permit``): this one is the within-request
    fairness bound (one request self-limits to the pool width), the global
    permit is the cross-request budget — without it, N concurrent requests
    would multiply this cap N-fold against the shared pool.
    """
    return discogs_pool_max_size()


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
    store = require_entity_store(entity_store)
    try:
        identity = await store.get_identity(name)
    except TRANSIENT_PG_ERRORS:
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
    store = require_entity_store(entity_store)

    identities: list[IdentityResponse] = []
    unresolved: list[str] = []

    for name in request.names:
        try:
            identity = await store.get_identity(name)
        except TRANSIENT_PG_ERRORS:
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
      ``artist_name``) return ``kind: compilation``.
    - Otherwise we look up ``artist_name`` in ``entity.identity`` and
      compose per-source provenance via ``identity.bulk_resolve``.
    - When the lookup misses we return ``kind: unresolved`` so Backend
      can cache the no-match verdict (with TTL) and avoid re-asking.

    ``include_tracks`` (1.31.0, wxyc-shared#297/#307) gates the
    ``(tracks_attempted, tracks)`` pair on both resolved kinds; ``None``
    and ``false`` are one state. When understood-true, the response
    carries ``tracks_contract_version: 1`` so a consumer can tell "this
    producer understood the flag" from "this producer predates it"
    (wxyc-shared#303, Q1 option A). Until #1021/#1138 wire real
    emission, flag-on rows carry ``(false, [])`` — asked, not yet
    visited — and the consumer keeps re-asking.

    Response order matches request input order, as per the api.yaml
    contract.
    """
    # Lazy import — wxyc_etl is a Rust extension; importing at module load
    # would couple every importer of this module (e.g. unit tests that
    # don't touch this endpoint) to the wxyc_etl ABI.
    from wxyc_etl.text import is_compilation_artist

    # Shared bulk envelope (LML#767): raw-JSON parse, ClientDisconnect -> 400,
    # non-object -> 400, manual over-cap -> 413 before Pydantic (whose
    # `max_length` would 422 an oversize batch, but api.yaml documents 413),
    # then model_validate. An absent/wrong-type `inputs` field falls through
    # to Pydantic's structured errors() rather than a bare-string 422.
    request = await parse_bulk_body(
        http_request, BulkResolveLibrariesRequest, _BULK_RESOLVE_INPUT_CAP, field="inputs"
    )

    # None and False are one state by contract (the field carries no
    # schema-level default for openapi-typescript optionality reasons).
    include_tracks = bool(request.include_tracks)

    store = require_entity_store(entity_store)

    # LML#927: this route is part of the bulk family (shares the LML#716
    # global permit with `/lookup/bulk` and cache refresh), so it is always
    # low priority at the Discogs semaphore gate too -- mirrors the
    # unconditional placement in `lookup.router.handle_bulk_lookup`.
    set_discogs_low_priority(True)

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

    # Per-input lookups dispatch concurrently under a semaphore (LML#278).
    # Worst case drops from ~3,000 sequential PG round-trips for a 1,000-row
    # miss-heavy batch to ~``ceil(N / max_concurrent)`` waves. The semaphore is
    # sized to the discogs-cache pool's ``max_size`` (see
    # ``_bulk_resolve_default_concurrency``) so we parallelize without exhausting
    # the asyncpg pool. ``min(..., inputs_count)`` avoids over-allocating permits
    # for small batches; ``max(1, ...)`` keeps ``Semaphore(0)`` (which would
    # deadlock any acquire) off the table for an empty-input request.
    max_concurrent = max_concurrency_from_env(_bulk_resolve_default_concurrency())
    semaphore = asyncio.Semaphore(max(1, min(max_concurrent, inputs_count)))

    async def _resolve_one(input_row: BulkResolveInput) -> BulkResolveResult:
        """Compose one verdict, gated by the shared pool semaphore.

        Holds a single permit across the whole per-input path so the in-flight
        connection count never exceeds the pool. A ``PostgresError`` / ``OSError``
        raised here surfaces as ``HTTPException(503)``; ``asyncio.gather`` (default
        ``return_exceptions=False``) propagates the first such failure to the
        awaiting handler, which fails the whole batch closed — never a 200 with
        partial results. The caller cannot distinguish "row had no identity"
        from "PG died before this row was tried", so the fail-closed posture is
        load-bearing (same contract as ``/identity/bulk``).
        """
        # Per-request semaphore OUTER, global permit INNER — the consistent
        # order every bulk-family dispatcher uses (LML#716). Both gates stay:
        # the per-request semaphore is the within-request fairness bound
        # (sized to the pool so one request self-limits), the global permit
        # is the cross-request budget shared with /lookup/bulk and cache
        # refresh, so unbounded concurrent bulk-resolve requests can't
        # multiply against the shared pool.
        async with semaphore, acquire_bulk_global_permit():
            if is_compilation_artist(input_row.artist_name):
                return compilation_result(input_row.library_id, include_tracks=include_tracks)

            try:
                # Three-leg fall-through lookup (issues #274 / #276): exact
                # match first (handles the 99.8% dominant case where Backend's
                # `library.artist_name` shape equals storage), then
                # case-insensitive `LOWER()` (catches pure case drift), then
                # canonical form (catches diacritic / `&`-vs-`and` / smart-quote
                # / etc. divergence when storage happens to be canonical).
                # Strictly ≥ legacy `get_identity()` hit rate.
                identity: Identity | None = await store.resolve_library_name(input_row.artist_name)
            except TRANSIENT_PG_ERRORS:
                logger.exception(
                    "Entity store query failed mid-bulk-resolve for library_id=%d artist_name=%r",
                    input_row.library_id,
                    input_row.artist_name,
                )
                raise HTTPException(
                    status_code=503, detail=_ENTITY_STORE_UNAVAILABLE_DETAIL
                ) from None

            try:
                # `compose_for_identity` issues its own
                # `get_latest_provenance_by_source` query — that second round-trip
                # is covered by the same permit, so a resolved input never holds
                # two pool connections at once.
                return await compose_for_identity(
                    input_row.library_id, identity, store, include_tracks=include_tracks
                )
            except TRANSIENT_PG_ERRORS:
                logger.exception(
                    "Provenance fetch failed mid-bulk-resolve for library_id=%d",
                    input_row.library_id,
                )
                raise HTTPException(
                    status_code=503, detail=_ENTITY_STORE_UNAVAILABLE_DETAIL
                ) from None

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
        http_span.set_data("lml.bulk_resolve.max_concurrent", max_concurrent)

        def _on_abort() -> None:
            # 499 = Nginx "client closed request" — nobody reads the body, but
            # it pins the span/log for triage.
            http_span.set_data("http.status_code", 499)
            logger.warning("bulk resolve aborted by client: inputs=%d", inputs_count)

        # Race the gather against a client-disconnect sentinel (LML#700,
        # hoisted into core.bulk_concurrency.run_bulk_gather by LML#1033).
        # uvicorn does not propagate a client socket close into the handler,
        # so a plain `await asyncio.gather(...)` would run the in-flight
        # per-input tasks — and the discogs-cache pool permits they hold
        # (bounded by `max_concurrent`) — to completion against a client that
        # is already gone; the sentinel lets a disconnect cancel the
        # outstanding gather promptly instead. `gather` preserves input order
        # (results[i] <-> inputs[i]) regardless of completion order,
        # satisfying the api.yaml ordering contract.
        #
        # Spawning the sentinel must happen *after* `await http_request.json()`
        # above has fully consumed the request body — `watch_disconnect` awaits
        # `request.receive()`, which would otherwise swallow the `http.request`
        # body messages the JSON parser needs.
        try:
            results = await run_bulk_gather(
                (_resolve_one(r) for r in request.inputs),
                http_request=http_request,
                watch_disconnect_fn=watch_disconnect,
                on_abort=_on_abort,
                sentinel_task_name="lml.bulk_resolve.disconnect_sentinel",
            )
        except asyncio.CancelledError:
            # Server-driven cancellation surfaced through the gather (shutdown /
            # outer timeout), or a child observed a cancel — distinct from the
            # client-disconnect branch above, which the sentinel handles. Pin 499
            # and re-raise per the asyncio cancellation contract; the client is
            # gone so there is nobody to read a 499 response.
            http_span.set_data("http.status_code", 499)
            logger.warning("bulk resolve aborted (cancelled): inputs=%d", inputs_count)
            raise
        except HTTPException as exc:
            # Fail-closed 503 (or any explicit status) from a per-input failure
            # -- or the 499 `run_bulk_gather` itself raises on abort, whose
            # `on_abort` callback above already pinned the span/log, so this
            # `set_data` call is a harmless no-op re-write to the same value.
            # Pin the status on the span before re-raising so the trace carries
            # the real outcome rather than an unset code.
            http_span.set_data("http.status_code", exc.status_code)
            raise

        # Exit signal pairs with the entry log so operators can confirm
        # response delivery and read off the per-kind verdict breakdown
        # without correlating to a Sentry trace. The kind enum values
        # (`single_artist`, `compilation`, `unresolved`) come from
        # `BulkResolveResultKind` and are what the LML#355 audit needed when
        # it had to fall back to local-cache simulation.
        kinds = Counter(r.kind.value for r in results)
        logger.info(
            "bulk resolve complete: inputs=%d single_artist=%d compilation=%d unresolved=%d"
            " include_tracks=%s",
            inputs_count,
            kinds.get("single_artist", 0),
            kinds.get("compilation", 0),
            kinds.get("unresolved", 0),
            include_tracks,
        )
        http_span.set_data("lml.bulk_resolve.single_artist", kinds.get("single_artist", 0))
        http_span.set_data("lml.bulk_resolve.compilation", kinds.get("compilation", 0))
        http_span.set_data("lml.bulk_resolve.unresolved", kinds.get("unresolved", 0))
        http_span.set_data("lml.bulk_resolve.include_tracks", include_tracks)
        http_span.set_data("http.status_code", 200)

    return BulkResolveLibrariesResponse(
        results=results,
        # The producer-echoed capability marker (wxyc-shared#303 Q1 option A):
        # present exactly when the flag was understood-true. When None, the
        # wire spells it "tracks_contract_version": null (no exclude_none on
        # this response_model) — consumers use the null-tolerant check; the
        # nullability doc fix is wxyc-shared#313.
        tracks_contract_version=TracksContractVersion.integer_1 if include_tracks else None,
    )


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
    store = require_entity_store(entity_store)

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
        except TRANSIENT_PG_ERRORS:
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
