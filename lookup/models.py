"""API contract models for the lookup endpoint.

These are re-exports from the generated api.yaml models (wxyc-shared).
Internal models (LibraryItem, DiscogsSearchResult) remain in their
respective modules for domain logic.

LookupResultItem and LookupResponse are overridden here to use SerializeAsAny
so that EnrichedDiscogsMatchResult (a subclass of DiscogsMatchResult) is
serialized with all its fields, not just the base class fields.

LookupRequest and LookupResponse are extended with mojibake-cleanup
Phase 1.5 fields (``include_external_caches`` / ``external_source``) ahead
of the api.yaml regen so the lossy-mojibake matcher can opt in without
waiting on the wxyc-shared release cycle. Fields default to opt-out so
existing callers see no behavior change.
"""

from typing import Literal

from pydantic import BaseModel, Field, SerializeAsAny

from generated.api_models import (
    DiscogsMatchResult,
    LibraryCatalogItem,
    SearchType,
)
from generated.api_models import (
    LookupRequest as _GeneratedLookupRequest,
)
from generated.api_models import (
    LookupResponse as _GeneratedLookupResponse,
)
from generated.api_models import (
    LookupResultItem as _GeneratedLookupResultItem,
)


class LookupRequest(_GeneratedLookupRequest):
    """Override to add the Phase 1.5 ``include_external_caches`` opt-in.

    Defaults to ``False`` so existing callers (Backend-Service, tubafrenzy
    autocomplete, streaming-check) see no behavior change. The
    lossy-mojibake matcher passes ``True`` to widen the search to the
    discogs-cache + musicbrainz-cache PostgreSQL DBs when the WXYC library
    catalog has no hit.
    """

    include_external_caches: bool = Field(
        False,
        description=(
            "When True and the library returns no results, fall back to "
            "fuzzy artist-name search against discogs-cache and then "
            "musicbrainz-cache. Used by the mojibake-recovery matcher."
        ),
    )


class LookupResultItem(_GeneratedLookupResultItem):
    """Override to serialize artwork subclasses with all fields."""

    artwork: SerializeAsAny[DiscogsMatchResult] | None = None


class LookupResponse(_GeneratedLookupResponse):
    """Override so results use our LookupResultItem with SerializeAsAny."""

    results: list[LookupResultItem] | None = None  # type: ignore[assignment]
    external_source: Literal["library", "discogs", "musicbrainz"] | None = Field(
        None,
        description=(
            "Provenance for the returned results: 'library' when the WXYC "
            "catalog produced them, 'discogs'/'musicbrainz' when an external "
            "cache fallback did, None when no results were found. Always "
            "None for legacy callers that don't set include_external_caches."
        ),
    )


BulkLookupResultStatus = Literal["match", "no_match", "error"]


class BulkLookupResultItem(BaseModel):
    """Per-item verdict for the bulk lookup endpoint.

    ``lookup`` carries the full per-item ``LookupResponse`` so callers see the
    same shape as the single-item endpoint when the lookup completed, regardless
    of whether it produced results. ``status`` is a fast signal:

    - ``match``    — ``lookup.results`` is non-empty
    - ``no_match`` — ``lookup.results`` is empty (search ran, found nothing)
    - ``error``    — ``perform_lookup`` raised; ``lookup`` is None, ``message`` set
    """

    index: int = Field(..., description="Zero-based index into the request `items` array.")
    status: BulkLookupResultStatus
    lookup: LookupResponse | None = None
    message: str | None = None


class BulkLookupRequest(BaseModel):
    """Bulk variant of ``LookupRequest``. Items run concurrently under a
    bounded semaphore; results return in input order."""

    items: list[LookupRequest] = Field(..., min_length=1)


class BulkLookupResponse(BaseModel):
    results: list[BulkLookupResultItem]


__all__ = [
    "BulkLookupRequest",
    "BulkLookupResponse",
    "BulkLookupResultItem",
    "BulkLookupResultStatus",
    "DiscogsMatchResult",
    "LibraryCatalogItem",
    "LookupRequest",
    "LookupResponse",
    "LookupResultItem",
    "SearchType",
]
