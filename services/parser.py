"""Minimal parser models for the lookup service.

The lookup service receives pre-parsed requests from request-o-matic,
so it only needs the ParsedRequest model shape, not the Groq parsing logic.
"""

from enum import StrEnum

from pydantic import BaseModel


class MessageType(StrEnum):
    REQUEST = "request"
    DJ_MESSAGE = "dj_message"
    FEEDBACK = "feedback"
    OTHER = "other"


class ParsedRequest(BaseModel):
    song: str | None = None
    album: str | None = None
    artist: str | None = None
    library_artist: str | None = None
    """The library-fuzzy spelling correction for ``artist`` (the second channel
    of the two-channel seam, WXYC/library-metadata-lookup#626).

    ``artist`` always holds the *typed* value and flows to every Discogs-facing
    path (the Discogs probes, ``validate_release_for_track``, the library-miss
    Discogs probe). ``library_artist`` carries the corrected name and is read
    ONLY by the library-side legs — the ``db.search`` queries and the
    ``artist_matches_item`` match-backs. ``None`` when no correction was applied;
    the library legs fall back to ``artist`` in that case. Keeping the two apart
    stops a *distinct* non-library artist whose name is one edit from a library
    artist ("Plug" vs library "Plugz") from being snapped into the library's
    vocabulary for Discogs resolution."""

    is_request: bool = True
    message_type: MessageType = MessageType.REQUEST
    raw_message: str = ""
