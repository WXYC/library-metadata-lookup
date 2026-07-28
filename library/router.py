"""Library router with dependency injection."""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query

from core.dependencies import get_library_db
from library.db import LibraryDB
from library.models import LibrarySearchResponse
from lookup.endpoint_family import (
    ENDPOINT_FAMILY_LIBRARY_SEARCH,
    record_endpoint_family_tag,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/library", tags=["library"])


@router.get(
    "/search",
    response_model=LibrarySearchResponse,
    summary="Search library catalog",
    description="""
    Search the music library catalog using full-text search or filters.

    You can search by:
    - `q`: Full-text search across artist and title
    - `artist`: Filter by artist name
    - `title`: Filter by album title

    Example request:
    ```
    GET /api/v1/library/search?q=Queen+Bohemian+Rhapsody&limit=5
    ```
    """,
    responses={
        200: {"description": "Search results returned"},
        400: {"description": "Invalid request (no search parameters)"},
        503: {"description": "Library service unavailable"},
        500: {"description": "Internal server error"},
    },
)
async def search_library(
    q: str | None = Query(None, description="Full-text search query"),
    artist: str | None = Query(None, description="Filter by artist name"),
    title: str | None = Query(None, description="Filter by album title"),
    limit: int = Query(10, ge=1, le=100, description="Max results"),
    db: LibraryDB = Depends(get_library_db),
):
    """Search the library catalog."""
    # LML#929: label this request's Sentry transaction with the local-catalog
    # traffic class so the #931 observability residual can slice protected-search
    # latency during a /lookup/bulk flood. Emitted at handler entry so every
    # search request (including the 400 below) carries the label. This path
    # invokes zero Discogs/Apple/streaming/enrichment code (guarded by
    # tests/unit/test_library_router.py) — the label is the only cross-module
    # touch, and record_endpoint_family_tag swallows its own errors.
    record_endpoint_family_tag(ENDPOINT_FAMILY_LIBRARY_SEARCH)

    if not q and not artist and not title:
        raise HTTPException(
            status_code=400,
            detail="At least one of q, artist, or title must be provided",
        )

    try:
        results = await db.search(query=q, artist=artist, title=title, limit=limit)

        return LibrarySearchResponse(
            results=results,
            total=len(results),
            query=q or f"artist={artist}, title={title}",
        )
    except Exception as e:
        logger.error(f"Library search failed: {e}")
        raise HTTPException(status_code=500, detail="Internal server error") from e
