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
    # The cataloger's SECOND name for this release's artist, and not a
    # synonym for ``artist`` -- ``artist`` is the name the record is FILED
    # under, this is how THIS release is billed. Empty on ~92% of catalog
    # rows. When present it is usually a collaboration or cross-reference
    # credit ("Marvin Gaye" -> "Marvin Gaye and Tammi Terrell", "The Ex" ->
    # "The Ex with Brader Musiki"), sometimes a spelling variant ("B.J.
    # Thomas" -> "bj thomas"), and sometimes the Discogs canonical or legal
    # name with its disambiguation suffix ("skee mask" -> "Bryan Muller",
    # "Ear (11)"). On a compilation row it inverts: ``artist`` is a shelf
    # heading ("Various Artists - Africa", "Soundtracks - G") and this holds
    # the performer.
    #
    # Consequence: which of the two a reader wants depends on the QUESTION,
    # and the three current readers deliberately disagree.
    # ``lookup/enrichment/item.py``'s ``row_artist`` and ``lookup/artwork.py``'s
    # ``track_artist`` lead with this one (they score against Discogs, which
    # indexes billing credits); ``lookup/enrichment/search_urls.py``'s
    # ``search_artist_for`` leads with ``artist`` and inverts only on
    # compilations, because a streaming service indexes the performing name.
    # Each carries its own rationale; this note is the shared substrate so a
    # correction to what the FIELD holds has one home. LML#1284 had to make
    # exactly that correction mid-PR.
    alternate_artist_name: str | None = None
    label: str | None = None
    # Pipe-joined (" | ") PRESENTATION_NAMEs of any WXYC catalog LIBRARY_CODEs
    # cataloger-cross-referenced to this row's own code (e.g. a release filed
    # under a band name carries a member's personal name). Optional column;
    # absent from library.db files predating WXYC/discogs-etl#334.
    cross_reference_names: str | None = None
    on_streaming: bool | None = None

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
        """Per-release permalink for viewing this release in the WXYC library.

        Points at the dj-site legacy front door
        (``{dj_site_base_url}/dashboard/album/legacy/{id}``), which resolves this
        legacy library id -- ``self.id`` is the tubafrenzy ``LIBRARY_RELEASE.ID``,
        NOT the Backend-Service serial -- to the canonical serial route server-side
        and 308-redirects (WXYC/dj-site#1050). The lazy ``get_settings()`` import
        mirrors ``library/db.py`` and avoids an import cycle; it is ``lru_cache``d,
        so calling it per render is cheap.
        """
        from config.settings import get_settings

        base_url = get_settings().dj_site_base_url.rstrip("/")
        return f"{base_url}/dashboard/album/legacy/{self.id}"

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
            label=self.label,
            call_number=self.call_number,
            library_url=self.library_url,
            on_streaming=self.on_streaming,
        )


class LibrarySearchResponse(BaseModel):
    """Response containing library search results."""

    results: list[LibraryItem]
    total: int
    query: str | None = None
