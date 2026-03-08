"""API contract models for the lookup endpoint.

These are re-exports from the generated api.yaml models (wxyc-shared).
Internal models (LibraryItem, DiscogsSearchResult) remain in their
respective modules for domain logic.
"""

from generated.api_models import (
    DiscogsMatchResult,
    LibraryCatalogItem,
    LookupRequest,
    LookupResponse,
    LookupResultItem,
    SearchType,
)

__all__ = [
    "DiscogsMatchResult",
    "LibraryCatalogItem",
    "LookupRequest",
    "LookupResponse",
    "LookupResultItem",
    "SearchType",
]
