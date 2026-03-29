"""API contract models for the lookup endpoint.

These are re-exports from the generated api.yaml models (wxyc-shared).
Internal models (LibraryItem, DiscogsSearchResult) remain in their
respective modules for domain logic.

LookupResultItem is overridden here to use SerializeAsAny so that
EnrichedDiscogsMatchResult (a subclass of DiscogsMatchResult) is
serialized with all its fields, not just the base class fields.
"""

from pydantic import SerializeAsAny

from generated.api_models import (
    DiscogsMatchResult,
    LibraryCatalogItem,
    LookupRequest,
    LookupResponse,
    SearchType,
)
from generated.api_models import (
    LookupResultItem as _GeneratedLookupResultItem,
)


class LookupResultItem(_GeneratedLookupResultItem):
    """Override to serialize artwork subclasses with all fields."""

    artwork: SerializeAsAny[DiscogsMatchResult] | None = None


__all__ = [
    "DiscogsMatchResult",
    "LibraryCatalogItem",
    "LookupRequest",
    "LookupResponse",
    "LookupResultItem",
    "SearchType",
]
