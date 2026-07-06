"""Contract guard for the row-less result shape (LML#627).

A *row-less* result is a `LookupResultItem` whose `library_item.id == 0` (no
WXYC catalog row) paired with a real Discogs identity in `artwork`
(`release_id > 0`, non-empty `release_url`). It is the shape the library-miss
Discogs search (LML#583, orchestrator Step 3a) emits: the album isn't shelved
in the WXYC library, but Discogs confidently identifies the release, so the
response still carries the Discogs link, artwork, and (downstream) the
streaming buttons Backend-Service + iOS render.

Two facts the #625 grill verified end-to-end and that this file PINS:

1. The shape is **already schema-valid and already rendered** — `api.yaml`'s
   `LookupResultItem.artwork` (a `DiscogsMatchResult`) is optional and
   independent of `library_item`, and `LibraryCatalogItem` has no `album_id`
   (library identity is just `id`). BS gates its Discogs link on
   `discogs_url`/`isSyntheticArtwork()`, never on `album_id`; iOS gates on
   `metadata.discogsURL != nil`. So `library_item.id == 0` + a present
   `artwork.release_id > 0` needs no consumer change.

2. This row-less *Discogs-identity* shape (`release_id > 0`, `release_url != ""`)
   is **distinct from** the BS#1185 streaming-only *sentinel*
   (`DiscogsSearchResult(release_id=0, release_url="")`, built at
   `orchestrator.py` ~L3064). The sentinel tells BS "skip extractAlbumMetadata,
   I carry only streaming URLs"; the row-less identity tells BS "here is a real
   Discogs release with no catalog row." The discriminator is
   `release_id > 0 && album_id is null` — unambiguous against the sentinel and
   backward-compatible with any consumer keying "has Discogs identity" on
   `release_id != 0`.

Nothing pins (1)+(2) today: a future refactor of the BS `isSyntheticArtwork` /
iOS `discogsURL != nil` gate — or of LML's Step-6 construction — could silently
regress the row-less case with no failing test. This file is that test. It is a
guard, not a red-then-green TDD driver: the construction already supports the
shape, so these assertions pass against current code; the point is that they
must KEEP passing.

Companion to `test_library_miss_discogs.py` (which covers the Step-3a wiring +
the LML#400 contamination floor); here the lens is strictly the *wire contract*
of the emitted item and its serialization against the generated API models.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from discogs.models import DiscogsSearchResponse
from generated.api_models import DiscogsMatchResult, LibraryCatalogItem, LookupResultItem
from lookup.models import LookupRequest, LookupResponse
from lookup.orchestrator import perform_lookup
from tests.conftest import make_lml_telemetry
from tests.factories import make_discogs_result, make_match_result

# A real Discogs release id, paired with the canonical release-URL shape. Using
# a concrete id (rather than a placeholder) keeps the contract assertion honest:
# this is what a resolved row-less release actually looks like on the wire.
ROWLESS_RELEASE_ID = 4163711
ROWLESS_RELEASE_URL = f"https://discogs.com/release/{ROWLESS_RELEASE_ID}"


def _build_rowless_response_via_perform_lookup(library_db, discogs_service):
    """Drive `perform_lookup` down the Step-3a row-less path and return the response.

    Library search misses (empty + no similar artist); Discogs confidently
    identifies the (artist, album) pair, so Step 3a synthesizes the
    `LibraryItem(id=0)` + real `DiscogsSearchResult` pair and the response
    build (`_build_result_items` in `lookup/orchestrator.py`) produces the
    `LookupResultItem` we assert on. `get_release` returns None so the artwork
    enrichment pass is a no-op and the resolved `release_id`/`release_url`
    survive untouched onto the wire.
    """
    library_db.search.return_value = []
    library_db.find_similar_artist.return_value = None
    discogs_service.search.return_value = DiscogsSearchResponse(
        results=[
            make_discogs_result(
                release_id=ROWLESS_RELEASE_ID,
                artist="Sessa",
                album="Grandeza",
                artwork_url="https://img.discogs.com/cover.jpg",
            )
        ]
    )
    discogs_service.get_release = AsyncMock(return_value=None)

    request = LookupRequest(
        artist="Sessa",
        album="Grandeza",
        raw_message="Sessa - Grandeza",
    )
    return perform_lookup(request, library_db, discogs_service, telemetry=make_lml_telemetry())


class TestRowlessResultContract:
    """The emitted row-less item is the real-Discogs-identity shape, not the sentinel."""

    @pytest.mark.asyncio
    async def test_rowless_result_has_discogs_identity_shape(
        self, mock_library_db, mock_discogs_service
    ):
        """`perform_lookup` emits library_item.id==0 + artwork.release_id>0 + release_url!=""."""
        response = await _build_rowless_response_via_perform_lookup(
            mock_library_db, mock_discogs_service
        )

        assert isinstance(response, LookupResponse)
        assert len(response.results) == 1
        item = response.results[0]

        # Row-less: no WXYC catalog row.
        assert item.library_item.id == 0

        # Real Discogs identity carried in artwork — NOT the BS#1185 sentinel.
        assert item.artwork is not None
        assert item.artwork.release_id == ROWLESS_RELEASE_ID
        assert item.artwork.release_id > 0
        assert item.artwork.release_url != ""
        assert str(ROWLESS_RELEASE_ID) in item.artwork.release_url

    @pytest.mark.asyncio
    async def test_rowless_result_is_distinguishable_from_bs1185_sentinel(
        self, mock_library_db, mock_discogs_service
    ):
        """The discriminator (release_id>0) cleanly separates this shape from the sentinel.

        BS#1185 streaming-only sentinel: ``DiscogsSearchResult(release_id=0,
        release_url="")``. A consumer keying "has Discogs identity" on
        ``release_id != 0`` must classify the row-less result as identity-bearing
        and the sentinel as not. Pin both directions so a future construction
        change that leaked the sentinel's ``release_id=0`` onto the row-less path
        (or vice versa) fails here.
        """
        response = await _build_rowless_response_via_perform_lookup(
            mock_library_db, mock_discogs_service
        )
        artwork = response.results[0].artwork
        assert artwork is not None

        # The row-less identity is on the identity-bearing side of the discriminator.
        is_synthetic_sentinel = artwork.release_id == 0 and artwork.release_url == ""
        assert is_synthetic_sentinel is False

        # And the sentinel itself lands on the other side — the predicate actually
        # discriminates, rather than being vacuously true.
        sentinel = make_match_result(release_id=0, release_url="")
        sentinel_is_synthetic = sentinel.release_id == 0 and sentinel.release_url == ""
        assert sentinel_is_synthetic is True
        assert sentinel.release_id != artwork.release_id

    @pytest.mark.asyncio
    async def test_rowless_result_serializes_against_generated_models(
        self, mock_library_db, mock_discogs_service
    ):
        """The emitted item round-trips through the generated API models intact.

        Serialize the `LookupResultItem` to JSON and re-validate it against the
        generated `LookupResultItem`. This is the parity check: the shape is not
        just convenient in-process, it survives the wire contract that
        Backend-Service decodes.
        """
        response = await _build_rowless_response_via_perform_lookup(
            mock_library_db, mock_discogs_service
        )
        item = response.results[0]

        payload = item.model_dump(mode="json")
        assert payload["library_item"]["id"] == 0
        assert payload["artwork"]["release_id"] == ROWLESS_RELEASE_ID
        assert payload["artwork"]["release_url"] != ""

        # Re-validation against the generated model must not raise — the shape is
        # contract-valid, not merely producible.
        roundtripped = LookupResultItem.model_validate(payload)
        assert roundtripped.library_item.id == 0
        assert roundtripped.artwork is not None
        assert roundtripped.artwork.release_id == ROWLESS_RELEASE_ID
        assert roundtripped.artwork.release_url != ""

    def test_library_identity_is_id_only_no_album_id(self):
        """`LibraryCatalogItem` has no `album_id` — library identity is just `id`.

        The #627 discriminator is ``release_id > 0 && album_id == null``. The
        ``album_id == null`` half is structural: the catalog item type carries no
        such field at all, so a row-less result is unambiguously "Discogs
        identity, no catalog row." Pin it so a schema regen that introduced an
        ``album_id`` (re-opening the ambiguity #627 closes) trips this guard.
        """
        assert "album_id" not in LibraryCatalogItem.model_fields
        assert "id" in LibraryCatalogItem.model_fields

        # The row-less catalog item is constructible with id=0 and no album_id —
        # mirroring the "(external)" item the response build (_build_result_items
        # in lookup/orchestrator.py) produces.
        rowless_catalog_item = LibraryCatalogItem(
            id=0,
            artist="Sessa",
            title="Grandeza",
            call_number="(external)",
            library_url="",
        )
        assert rowless_catalog_item.id == 0

    def test_rowless_item_constructs_directly_from_domain_conversion(self):
        """The response-build building blocks compose into the row-less shape directly.

        A focused unit check on the construction primitives the orchestrator uses
        (`LibraryCatalogItem` for the id=0 row + `DiscogsSearchResult.to_match_result`
        for the artwork), independent of the full `perform_lookup` wiring. Mirrors
        the response build (`_build_result_items` in `lookup/orchestrator.py`):
        id=0 items get the "(external)" catalog item, and the real Discogs
        identity rides in `artwork`.
        """
        catalog_item = LibraryCatalogItem(
            id=0,
            artist="Sessa",
            title="Grandeza",
            call_number="(external)",
            library_url="",
        )
        artwork = make_discogs_result(
            release_id=ROWLESS_RELEASE_ID,
            artist="Sessa",
            album="Grandeza",
        ).to_match_result()

        item = LookupResultItem(library_item=catalog_item, artwork=artwork)

        assert isinstance(item.artwork, DiscogsMatchResult)
        assert item.library_item.id == 0
        assert item.artwork.release_id == ROWLESS_RELEASE_ID
        assert item.artwork.release_url == ROWLESS_RELEASE_URL
        # Not the sentinel.
        assert not (item.artwork.release_id == 0 and item.artwork.release_url == "")
