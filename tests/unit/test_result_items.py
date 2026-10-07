"""Tests for ``lookup.result_items.build_result_items`` (moved verbatim out of the orchestrator)."""

from library.models import LibraryItem
from lookup.orchestrator import LookupState
from lookup.result_items import build_result_items


def _item(id: int = 1) -> LibraryItem:
    return LibraryItem(id=id, artist="Stereolab", title="Aluminum Tunes", genre="Rock")


def test_empty_state_builds_no_items():
    assert build_result_items(LookupState(), {}) == []


def test_library_results_carry_matched_via_none_when_untagged():
    state = LookupState(library_results=[_item()])
    items = build_result_items(state, {})
    assert len(items) == 1
    assert items[0].library_item.artist == "Stereolab"
    assert items[0].reconciled_identity is None
    assert items[0].matched_via is None


def test_items_with_artwork_take_precedence_and_synthesized_item_is_external():
    synthesized = LibraryItem(id=0, artist="Juana Molina", title="DOGA", genre="Rock")
    state = LookupState(
        library_results=[_item()],
        items_with_artwork=[(synthesized, None)],
    )
    items = build_result_items(state, {})
    assert len(items) == 1
    assert items[0].library_item.artist == "Juana Molina"
    assert items[0].library_item.call_number == "(external)"
    assert items[0].matched_via is None
