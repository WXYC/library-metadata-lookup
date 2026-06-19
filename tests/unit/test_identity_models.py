"""Unit tests for identity/models.py wire models and Identity↔IdentityResponse parity.

`Identity` (``entity/store.py``, a dataclass DB read-model) and
`IdentityResponse` (``identity/models.py``, the public wire model) carry the same
eight artist-identity fields; `Identity` adds only the internal DB primary key
`id`. They are projected onto each other by ``IdentityResponse.from_identity``,
which is field-agnostic (``model_validate(..., from_attributes=True)``).

The parity test below fails if the two field sets ever diverge (modulo `id`), so
adding a source identifier to one without the other breaks CI rather than
silently dropping the field from API responses (audit finding #2).
"""

from __future__ import annotations

import dataclasses

from entity.store import Identity
from identity.models import IdentityResponse


def test_identity_response_fields_match_identity_minus_id():
    """The wire model's fields must equal the dataclass's fields minus `id`.

    This is the structural drift guard: if a future change adds a shared
    identity field to `Identity` but forgets `IdentityResponse` (or vice
    versa), this assertion fails at CI time instead of the field silently
    never reaching the API response.
    """
    identity_fields = {f.name for f in dataclasses.fields(Identity)} - {"id"}
    response_fields = set(IdentityResponse.model_fields)
    assert identity_fields == response_fields


class TestFromIdentity:
    """`IdentityResponse.from_identity` projects an Identity onto the wire model."""

    def test_fully_populated_identity_round_trips(self):
        """Every shared field carries across; the internal `id` is dropped."""
        identity = Identity(
            id=99,
            library_name="Stereolab",
            discogs_artist_id=2154,
            wikidata_qid="Q484464",
            musicbrainz_artist_id="d4133898-91ea-48ea-8820-1b85825901fe",
            spotify_artist_id="1p6GVMFhLhSrRE7qgy8aAS",
            apple_music_artist_id="5765873",
            bandcamp_id="stereolab",
            reconciliation_status="reconciled",
        )

        response = IdentityResponse.from_identity(identity)

        assert response.library_name == "Stereolab"
        assert response.discogs_artist_id == 2154
        assert response.wikidata_qid == "Q484464"
        assert response.musicbrainz_artist_id == "d4133898-91ea-48ea-8820-1b85825901fe"
        assert response.spotify_artist_id == "1p6GVMFhLhSrRE7qgy8aAS"
        assert response.apple_music_artist_id == "5765873"
        assert response.bandcamp_id == "stereolab"
        assert response.reconciliation_status == "reconciled"
        # The internal DB primary key is not part of the wire contract.
        assert not hasattr(response, "id")

    def test_defaults_are_preserved(self):
        """An Identity built with only a name keeps the dataclass defaults."""
        identity = Identity(id=1, library_name="Sessa")

        response = IdentityResponse.from_identity(identity)

        assert response.library_name == "Sessa"
        assert response.reconciliation_status == "unreconciled"
        assert response.discogs_artist_id is None
        assert response.wikidata_qid is None
        assert response.musicbrainz_artist_id is None
        assert response.spotify_artist_id is None
        assert response.apple_music_artist_id is None
        assert response.bandcamp_id is None
