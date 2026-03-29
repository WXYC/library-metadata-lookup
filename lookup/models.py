"""API contract models for the lookup endpoint.

These are re-exports from the generated api.yaml models (wxyc-shared).
Internal models (LibraryItem, DiscogsSearchResult) remain in their
respective modules for domain logic.

LookupResultItem and LookupResponse are overridden here to use SerializeAsAny
so that EnrichedDiscogsMatchResult (a subclass of DiscogsMatchResult) is
serialized with all its fields, not just the base class fields.
"""

from pydantic import SerializeAsAny

from generated.api_models import (
    DiscogsMatchResult,
    LibraryCatalogItem,
    LookupRequest,
    SearchType,
)
from generated.api_models import (
    LookupResponse as _GeneratedLookupResponse,
)
from generated.api_models import (
    LookupResultItem as _GeneratedLookupResultItem,
)


class LookupResultItem(_GeneratedLookupResultItem):
    """Override to serialize artwork subclasses with all fields."""

    artwork: SerializeAsAny[DiscogsMatchResult] | None = None


class LookupResponse(_GeneratedLookupResponse):
    """Override so results use our LookupResultItem with SerializeAsAny."""

    results: list[LookupResultItem] | None = None  # type: ignore[assignment]


__all__ = [
    "DiscogsMatchResult",
    "LibraryCatalogItem",
    "LookupRequest",
    "LookupResponse",
    "LookupResultItem",
    "SearchType",
]
