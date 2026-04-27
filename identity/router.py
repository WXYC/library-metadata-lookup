"""Identity resolution REST API router.

Provides ``GET /identity/resolve`` and ``POST /identity/bulk`` endpoints
that query the ``entity.identity`` table in the discogs-cache database.
"""

import logging

from asyncpg.exceptions import PostgresError
from fastapi import APIRouter, Depends, HTTPException, Query

from identity.dependencies import get_entity_store
from identity.models import (
    BulkIdentityRequest,
    BulkIdentityResponse,
    IdentityResponse,
)
from scripts.entity_resolution.store import EntityStore, Identity

logger = logging.getLogger(__name__)

router = APIRouter(tags=["identity"])

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
