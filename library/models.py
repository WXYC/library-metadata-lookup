from __future__ import annotations

import re
from typing import TYPE_CHECKING

from pydantic import BaseModel, computed_field

if TYPE_CHECKING:
    from generated.api_models import LibraryCatalogItem

# Rock and Soundtracks are each split into 26 lettered shelf bins, with
# release numbers restarting in each bin; every other compilation genre
# shelves under a single bin. See LibraryItem.call_number.
_COMPILATION_LETTERED_GENRES = {"Rock", "Soundtracks"}

# A trailing " - <letter>" bin heading, e.g. "Various Artists - Rock - M" or
# "Soundtracks - M".
_COMPILATION_BIN_SUFFIX = re.compile(r" - ([A-Za-z])$")


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
    def _is_compilation(self) -> bool:
        """True when `call_letters` is the structural Various-Artists marker.

        `V/A` is the form Backend's library-etl writes into library.db; `Z-`
        is the raw tubafrenzy code (`Z--` single-bin, `Z-<letter>` lettered)
        that a tubafrenzy-shaped export can still carry. Detection is
        structural only -- never by artist name -- per LML#1427.
        """
        if not self.call_letters:
            return False
        normalized = self.call_letters.strip().upper()
        return normalized == "V/A" or normalized.startswith("Z-")

    def _compilation_bin_letter(self) -> str | None:
        """The Rock/Soundtracks shelf-bin letter, or None if unrecoverable.

        A compilation is filed by title, so there is no per-artist shelf
        number and `artist_call_number` is always 0 on these rows -- it must
        never render. The bin letter is a different story: Rock and
        Soundtracks split into 26 lettered bins with release numbers that
        restart per bin, so dropping the letter is ambiguous, not just ugly
        (up to 23 Rock bins can share one release number). The raw `Z-`
        tubafrenzy code carries the letter at a fixed offset. The modern
        `V/A` form does not carry it anywhere on the row itself -- it
        survives only as a bin heading baked into `artist`
        ("Various Artists - Rock - M", "Soundtracks - M") -- so recovering it
        from the name is a deliberate, narrowly-scoped exception: only after
        the structural V/A gate above, and only for the two lettered genres.
        """
        normalized = (self.call_letters or "").strip().upper()
        if normalized.startswith("Z-"):
            return normalized[2] if len(normalized) > 2 and normalized[2] != "-" else None
        if self.genre not in _COMPILATION_LETTERED_GENRES or not self.artist:
            return None
        match = _COMPILATION_BIN_SUFFIX.search(self.artist.strip())
        return match.group(1).upper() if match else None

    def _compilation_call_number(self) -> str:
        bin_letter = self._compilation_bin_letter()
        if self.genre == "Soundtracks" and bin_letter:
            artist_half = bin_letter
        elif self.genre == "Rock" and bin_letter:
            artist_half = f"V/A {bin_letter}"
        else:
            artist_half = "V/A"

        letters = artist_half
        if self.release_call_number is not None:
            letters = f"{artist_half}-{self.release_call_number}"

        parts = []
        if self.genre:
            parts.append(self.genre)
        if self.format:
            parts.append(self.format)
        parts.append(letters)
        return " ".join(parts)

    @property
    def call_number(self) -> str:
        """Full call number for shelf lookup: <Genre> <Format> <Letters> <ArtistNum>/<ReleaseNum>

        A compilation (`call_letters` structurally `V/A` or a raw `Z-` code;
        see `_is_compilation`) does not fit that pattern -- it is filed by
        title, so `artist_call_number` is meaningless (always 0) and must be
        dropped rather than rendered as "V/A 0/<n>". `_compilation_call_number`
        renders the shelf form instead, composed as an artist-half (the
        letters, e.g. "V/A" / "V/A M" / "M") joined to a release-half (the
        release number) -- the same composition LML#1373 will extend with a
        per-release volume letter (`<ReleaseNum>-<VolumeLetter>`).
        """
        if self._is_compilation:
            return self._compilation_call_number()
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
