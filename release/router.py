"""Router for the ``POST /api/v1/releases/resolve`` endpoint.

Tubafrenzy's rotation-release create form posts a Discogs/Bandcamp URL here
to prefill the form fields. The endpoint always returns 200 — partial
prefills are surfaced via ``warnings[]`` so the form can show what worked
and let the music director fill in the rest.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends

from release.dependencies import get_resolver_dependencies
from release.models import ReleaseResolveRequest, ReleaseResolveResponse
from release.orchestrator import _ResolverDependencies, resolve_release

logger = logging.getLogger(__name__)

router = APIRouter(tags=["release"])


@router.post(
    "/releases/resolve",
    response_model=ReleaseResolveResponse,
    summary="Resolve a Discogs/Bandcamp URL to canonical release metadata",
    description=(
        "Takes a pasted Discogs release URL or Bandcamp album URL (or an "
        "explicit `(source, id)` pair) and returns canonical release fields, "
        "cross-source identifiers, and streaming availability — everything "
        "tubafrenzy's rotation-release create form needs to prefill."
    ),
    responses={
        200: {"description": "Resolution result (may include warnings on partial success)"},
        422: {"description": "Request body failed validation"},
    },
)
async def handle_resolve(
    request: ReleaseResolveRequest,
    deps: _ResolverDependencies = Depends(get_resolver_dependencies),
) -> ReleaseResolveResponse:
    """Resolve and return. Errors are converted to warnings inside the orchestrator."""
    return await resolve_release(request, deps)
