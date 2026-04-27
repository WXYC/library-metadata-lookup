"""Pydantic models for the ``POST /api/v1/releases/resolve`` endpoint.

The endpoint takes either a pasted URL or an explicit ``(source, id)`` pair
and returns canonical release metadata, cross-source identifiers, and a
streaming-availability snapshot — all the data tubafrenzy needs to prefill
its rotation-release create form in one round trip.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

from streaming.models import StreamingCheckResponse


class ReleaseResolveRequest(BaseModel):
    """Request body for ``POST /api/v1/releases/resolve``.

    Either ``url`` is set, or both ``source`` and ``id`` are set — never both,
    never neither. Bandcamp ``id`` is the canonical album URL; Discogs ``id``
    is the numeric release or master ID as a string.
    """

    url: str | None = Field(None, description="Discogs or Bandcamp URL to resolve")
    source: Literal["discogs_release", "discogs_master", "bandcamp"] | None = Field(
        None, description="Explicit source — required when url is not given"
    )
    id: str | None = Field(
        None, description="Identifier within the source (release ID, master ID, or Bandcamp URL)"
    )

    @model_validator(mode="after")
    def _validate_oneof(self) -> ReleaseResolveRequest:
        url_given = self.url is not None and self.url.strip() != ""
        explicit_given = (self.source is not None) and (
            self.id is not None and self.id.strip() != ""
        )
        if url_given == explicit_given:
            raise ValueError("provide exactly one of `url` or (`source`, `id`)")
        return self


class CanonicalRelease(BaseModel):
    """Canonical release metadata projected from the source.

    ``label`` is null when self-released (per the Bandcamp self-released case).
    ``catno`` is the catalog number from the first label (Discogs only;
    Bandcamp does not surface a catalog number).

    Genres and styles are intentionally excluded — the rotation-release form
    has no genre field, so surfacing them would just be noise for the music
    director who picks the genre manually.
    """

    artist: str
    title: str
    label: str | None = None
    catno: str | None = None
    year: int | None = None
    country: str | None = None
    formats: list[str] = Field(default_factory=list)


class ReleaseIdentifiers(BaseModel):
    """Cross-source identifiers learned during resolution.

    A pasted Discogs URL fills ``discogs_release_id`` and (when known)
    ``discogs_master_id`` and ``discogs_artist_id``. A pasted Bandcamp URL
    fills ``bandcamp_album_url``. Other identifiers are populated when the
    streaming-availability check finds matches on Spotify / Apple Music.
    """

    discogs_release_id: int | None = None
    discogs_master_id: int | None = None
    discogs_artist_id: int | None = None
    musicbrainz_release_id: str | None = None
    bandcamp_album_url: str | None = None
    spotify_album_id: str | None = None
    apple_music_album_id: str | None = None


class ReleaseResolveResponse(BaseModel):
    """Response body for ``POST /api/v1/releases/resolve``.

    ``source`` is ``"unknown"`` when the input URL did not match any supported
    pattern; ``canonical`` is empty in that case and ``warnings`` explains why.
    Clients should check ``warnings`` regardless of ``source`` before consuming
    ``canonical`` — the orchestrator's contract is "always 200, partial OK".
    """

    source: Literal["discogs_release", "discogs_master", "bandcamp", "unknown"]
    source_id: str
    canonical: CanonicalRelease
    identifiers: ReleaseIdentifiers = Field(default_factory=ReleaseIdentifiers)
    streaming: StreamingCheckResponse | None = Field(
        None,
        description=(
            "Streaming availability across Spotify/Deezer/Apple Music/Bandcamp. "
            "Null when the streaming check could not be performed (e.g. all "
            "configured services failed)."
        ),
    )
    warnings: list[str] = Field(
        default_factory=list,
        description=(
            "Non-fatal issues during resolution — Discogs API rate-limited, "
            "Bandcamp page failed to parse, etc. The form should surface these "
            "to the user but still allow them to save."
        ),
    )
