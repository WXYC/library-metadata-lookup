"""Data models for the Various Artists disambiguation script."""

from __future__ import annotations

from pydantic import BaseModel


class FlowsheetEntry(BaseModel):
    """A flowsheet entry from FLOWSHEET_ENTRY_PROD with a Various Artists artist name."""

    id: int
    artist_name: str
    song_title: str | None = None
    release_title: str | None = None
    library_release_id: int | None = None
    artist_id: int | None = None


class CompilationRelease(BaseModel):
    """A compilation release from LIBRARY_RELEASE (CALL_LETTERS LIKE 'Z-%')."""

    id: int
    title: str
    library_code_id: int


class LibraryCodeEntry(BaseModel):
    """An entry from the LIBRARY_CODE table."""

    id: int
    presentation_name: str
    call_letters: str
    genre_id: int | None = None


class ResolutionResult(BaseModel):
    """The result of resolving a flowsheet entry's actual artist."""

    entry_id: int
    resolved_artist: str
    confidence: float
    strategy: str
    library_code_id: int | None = None


class CompilationTrackArtist(BaseModel):
    """A single track artist on a compilation release, for COMPILATION_TRACK_ARTIST table."""

    library_release_id: int
    artist_name: str
    track_title: str | None = None
