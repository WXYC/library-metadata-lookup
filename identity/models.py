"""Pydantic response models for identity resolution endpoints."""

from pydantic import BaseModel


class IdentityResponse(BaseModel):
    """A single resolved artist identity with external identifiers."""

    library_name: str
    discogs_artist_id: int | None = None
    wikidata_qid: str | None = None
    musicbrainz_artist_id: str | None = None
    spotify_artist_id: str | None = None
    apple_music_artist_id: str | None = None
    bandcamp_id: str | None = None
    reconciliation_status: str = "unreconciled"


class BulkIdentityRequest(BaseModel):
    """Request body for bulk identity resolution."""

    names: list[str]


class BulkIdentityResponse(BaseModel):
    """Response for bulk identity resolution.

    Separates successfully resolved identities from unresolved names.
    """

    identities: list[IdentityResponse]
    unresolved: list[str]
