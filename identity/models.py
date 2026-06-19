"""Pydantic response models for identity resolution endpoints."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel

if TYPE_CHECKING:
    from entity.store import Identity


class IdentityResponse(BaseModel):
    """A single resolved artist identity with external identifiers.

    Wire-model counterpart to the ``entity.store.Identity`` DB read-model: it
    carries the same eight artist-identity fields but drops the internal DB
    primary key ``id``. ``from_identity`` is the field-agnostic projection from
    one to the other; the parity test in ``tests/unit/test_identity_models.py``
    fails CI if the two field sets ever diverge (modulo ``id``).
    """

    library_name: str
    discogs_artist_id: int | None = None
    wikidata_qid: str | None = None
    musicbrainz_artist_id: str | None = None
    spotify_artist_id: str | None = None
    apple_music_artist_id: str | None = None
    bandcamp_id: str | None = None
    reconciliation_status: str = "unreconciled"

    @classmethod
    def from_identity(cls, identity: Identity) -> IdentityResponse:
        """Project an entity-store ``Identity`` onto the wire model, dropping ``id``.

        Field-agnostic by design: ``model_validate(..., from_attributes=True)``
        reads every matching attribute off the dataclass, so adding a shared
        identity field needs no edit here — only on the two models, which the
        parity test keeps aligned. The extra ``id`` attribute on the dataclass
        is ignored because it has no corresponding field on this model.
        """
        return cls.model_validate(identity, from_attributes=True)


class BulkIdentityRequest(BaseModel):
    """Request body for bulk identity resolution."""

    names: list[str]


class BulkIdentityResponse(BaseModel):
    """Response for bulk identity resolution.

    Separates successfully resolved identities from unresolved names.
    """

    identities: list[IdentityResponse]
    unresolved: list[str]
