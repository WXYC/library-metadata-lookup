"""Tests for ``lookup.result_items.build_result_items`` (moved verbatim out of the orchestrator)."""

from generated.api_models import ArtistMatchHint, ReconciledIdentity, TrackMatchHint
from lookup.orchestrator import LookupState
from lookup.result_items import build_result_items
from tests.factories import make_library_item

CREDIT = "Afel Bocoum, Damon Albarn, Toumani Diabate and friends"


def test_empty_state_builds_no_items():
    assert build_result_items(LookupState(), {}, artist=None) == []


def test_library_results_carry_matched_via_none_when_untagged():
    state = LookupState(library_results=[make_library_item()])
    items = build_result_items(state, {}, artist=None)
    assert len(items) == 1
    assert items[0].library_item.artist == "Stereolab"
    assert items[0].reconciled_identity is None
    assert items[0].matched_via is None


def test_matched_via_propagates_by_item_id():
    hint = TrackMatchHint(title="Cybele's Reverie", source="discogs_release")
    state = LookupState(
        library_results=[make_library_item(id=1), make_library_item(id=2)],
        matched_via_by_id={2: [hint]},
    )
    items = build_result_items(state, {}, artist=None)
    assert [i.matched_via for i in items] == [None, [hint]]


def test_matched_via_propagates_on_artwork_path():
    hint = TrackMatchHint(title="Cybele's Reverie", source="discogs_release")
    state = LookupState(
        items_with_artwork=[(make_library_item(id=2), None)], matched_via_by_id={2: [hint]}
    )
    assert build_result_items(state, {}, artist=None)[0].matched_via == [hint]


def test_identity_binds_by_filed_artist():
    identity = ReconciledIdentity(discogs_artist_id=1234)
    state = LookupState(
        library_results=[make_library_item(id=1), make_library_item(id=2, artist="Cat Power")]
    )
    items = build_result_items(state, {"Stereolab": identity}, artist=None)
    assert items[0].reconciled_identity == identity
    assert items[1].reconciled_identity is None


def test_items_with_artwork_take_precedence_and_synthesized_item_is_external():
    synthesized = make_library_item(id=0, artist="Juana Molina", title="DOGA")
    state = LookupState(
        library_results=[make_library_item()],
        items_with_artwork=[(synthesized, None)],
        matched_via_by_id={0: [TrackMatchHint(title="x", source="discogs_release")]},
    )
    items = build_result_items(state, {}, artist=None)
    assert len(items) == 1
    assert items[0].library_item.artist == "Juana Molina"
    assert items[0].library_item.call_number == "(external)"
    assert items[0].matched_via is None


def test_alternate_credit_row_is_tagged_and_the_artists_own_row_is_not():
    own = make_library_item(id=1, artist="Afel Bocoum", title="Alfalfa")
    mali = make_library_item(
        id=2, artist="Damon Albarn", title="Mali Music", alternate_artist_name=CREDIT
    )
    state = LookupState(library_results=[own, mali])
    items = build_result_items(state, {}, artist="Afel Bocoum")
    assert items[0].matched_via_alias is None
    assert items[1].matched_via_alias == [
        ArtistMatchHint(matched_variant=CREDIT, source="wxyc_library_alt")
    ]


def test_synthesized_row_is_never_tagged():
    synthesized = make_library_item(
        id=0, artist="Damon Albarn", title="Mali Music", alternate_artist_name=CREDIT
    )
    state = LookupState(items_with_artwork=[(synthesized, None)])
    assert build_result_items(state, {}, artist="Afel Bocoum")[0].matched_via_alias is None
