"""Models for the lookup API contract."""

from pydantic import BaseModel

from discogs.models import DiscogsSearchResult
from library.models import LibraryItem


class LookupRequest(BaseModel):
    """Request body for the POST /lookup endpoint."""

    artist: str | None = None
    song: str | None = None
    album: str | None = None
    raw_message: str


class LookupResultItem(BaseModel):
    """A single lookup result: library item paired with optional artwork."""

    library_item: LibraryItem
    artwork: DiscogsSearchResult | None = None


class LookupResponse(BaseModel):
    """Response from the lookup service."""

    results: list[LookupResultItem] = []
    search_type: str = "none"
    song_not_found: bool = False
    found_on_compilation: bool = False
    context_message: str | None = None
    corrected_artist: str | None = None
    cache_stats: dict | None = None
