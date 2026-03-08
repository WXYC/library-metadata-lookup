from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, computed_field

if TYPE_CHECKING:
    from generated.api_models import LibraryCatalogItem


class LibrarySearchRequest(BaseModel):
    """Request to search the library catalog."""

    query: str | None = None
    artist: str | None = None
    title: str | None = None
    limit: int = 10


class LibraryItem(BaseModel):
    """A single item from the library catalog."""

    id: int
    title: str | None = None
    artist: str | None = None
    call_letters: str | None = None
    artist_call_number: int | None = None
    release_call_number: int | None = None
    genre: str | None = None
    format: str | None = None
    alternate_artist_name: str | None = None

    @property
    def call_number(self) -> str:
        """Full call number for shelf lookup: <Genre> <Format> <Letters> <ArtistNum>/<ReleaseNum>"""
        parts = []
        if self.genre:
            parts.append(self.genre)
        if self.format:
            parts.append(self.format)
        if self.call_letters:
            parts.append(self.call_letters)
        if self.artist_call_number is not None:
            parts.append(str(self.artist_call_number))
        if self.release_call_number is not None:
            parts[-1] = f"{parts[-1]}/{self.release_call_number}"
        return " ".join(parts)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def library_url(self) -> str:
        """URL to view this release in the WXYC library."""
        return f"http://www.wxyc.info/wxycdb/libraryRelease?id={self.id}"

    def to_catalog_item(self) -> LibraryCatalogItem:
        """Convert to the API contract model (generated from wxyc-shared/api.yaml)."""
        from generated.api_models import LibraryCatalogItem

        return LibraryCatalogItem(
            id=self.id,
            title=self.title,
            artist=self.artist,
            call_letters=self.call_letters,
            artist_call_number=self.artist_call_number,
            release_call_number=self.release_call_number,
            genre=self.genre,
            format=self.format,
            call_number=self.call_number,
            library_url=self.library_url,
        )


class LibrarySearchResponse(BaseModel):
    """Response containing library search results."""

    results: list[LibraryItem]
    total: int
    query: str | None = None
