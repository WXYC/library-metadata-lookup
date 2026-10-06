from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, Field, computed_field

if TYPE_CHECKING:
    from generated.api_models import LibraryCatalogItem

# Rock and Soundtracks are each split into 26 lettered shelf bins, with
# release numbers restarting in each bin; every other compilation genre
# shelves under a single "V/A" bin. Maps each lettered genre to how its bin
# letter renders in the call number's artist half (tubafrenzy's
# ArtistLibraryCode.getCallLettersAndNumbers). See LibraryItem.call_number.
_COMPILATION_BIN_FORMS = {"Rock": "V/A {}", "Soundtracks": "{}"}


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
    # The Rock/Soundtracks compilation bin letter (Backend's structural
    # `genre_artist_crossreference.code_comp_letter`: one upper-case letter or
    # NULL). Optional column; absent from library.db files predating
    # WXYC/discogs-etl#440 and NULL until Backend-Service#2834's backfill is in
    # the file. Excluded from the wire: consumers render `call_number`.
    artist_comp_letter: str | None = Field(default=None, exclude=True)
    on_streaming: bool | None = None

    def _compilation_bin_letter(self, letters: str) -> str | None:
        """The shelf-bin letter for a compilation row, or None if unrecoverable.

        The raw tubafrenzy `Z-<letter>` code carries it at index 2 (`Z--` has
        none), taken as-is the way the Java's `substring(2, 3)` does. The `V/A`
        form Backend's export writes has lost it from `call_letters`; it lives in
        `artist_comp_letter`. The artist name is never read: renaming an artist
        must not move its shelf.
        """
        if letters.startswith("Z-"):
            return letters[2:3].strip("-").upper() or None
        return (self.artist_comp_letter or "").strip().upper() or None

    def _compilation_shelf(self, letters: str) -> str:
        """The `<artist-half>-<release-half>` locator for a compilation row."""
        form = _COMPILATION_BIN_FORMS.get(self.genre or "")
        bin_letter = self._compilation_bin_letter(letters) if form else None
        artist_half = form.format(bin_letter) if form and bin_letter else "V/A"
        # LML#1373 extends the release half with the per-release volume letter
        # (`<ReleaseNum>-<VolumeLetter>`).
        release_half = "" if self.release_call_number is None else f"-{self.release_call_number}"
        return artist_half + release_half

    @computed_field  # type: ignore[prop-decorator]
    @property
    def call_number(self) -> str:
        """Full call number for shelf lookup: <Genre> <Format> <Letters> <ArtistNum>/<ReleaseNum>

        A compilation does not fit that pattern. It is detected structurally,
        never by artist name: `call_letters` is `V/A` (case- and
        whitespace-insensitive, the form Backend's library-etl writes) or
        starts with the raw tubafrenzy `Z-` (case-sensitive, as in
        tubafrenzy's `isVariousArtists`). Call letters render upper-case, as
        in tubafrenzy's `getCallLettersAndNumbers`. A compilation shelf is filed by
        title, so `artist_call_number` (always 0) must not render as
        "V/A 0/<n>"; the shelf form is `<Genre> <Format> V/A-<ReleaseNum>`,
        or `Rock <Format> V/A <Bin>-<ReleaseNum>` / `Soundtracks <Format>
        <Bin>-<ReleaseNum>` for the two genres split into lettered bins, where
        release numbers restart per bin and the letter is what disambiguates
        the locator (LML#1427). Backend-Service's `computeCallNumber`
        (Backend-Service#2822) renders the same rule and must match it
        character for character.
        """
        parts = [part for part in (self.genre, self.format) if part]
        letters = (self.call_letters or "").strip()
        if letters.upper() == "V/A" or letters.startswith("Z-"):
            parts.append(self._compilation_shelf(letters))
            return " ".join(parts)
        artist_half = [self.call_letters.upper()] if self.call_letters else []
        if self.artist_call_number is not None:
            artist_half.append(str(self.artist_call_number))
        if self.release_call_number is not None:
            if artist_half:
                artist_half[-1] = f"{artist_half[-1]}/{self.release_call_number}"
            else:
                artist_half = [str(self.release_call_number)]
        return " ".join(parts + artist_half)

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
