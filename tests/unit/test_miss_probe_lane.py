"""The step-3a lane selector (``lookup/strategies/miss_probe_lane.py``)."""

import pytest

from core.search import SEARCH_TYPE_FALLBACK
from lookup.strategies.miss_probe_lane import MissProbeLane, miss_probe_lane
from services.parser import MessageType, ParsedRequest
from tests.factories import make_library_item

_HALO = make_library_item(id=1, artist="Juana Molina", title="Halo")
_DOGA = make_library_item(id=2, artist="Juana Molina", title="DOGA")


@pytest.mark.parametrize(
    ("song", "album", "rows", "search_type", "lane"),
    [
        pytest.param(None, "DOGA", [], "none", MissProbeLane.LIBRARY_MISS, id="no-rows"),
        pytest.param(
            None, "Halo Deluxe Tour Edition", [_HALO], SEARCH_TYPE_FALLBACK,
            MissProbeLane.SERVE_BLOCKED, id="songless-token-subset-row",
        ),
        pytest.param(
            "la paradoja", "DOGA", [_HALO, _DOGA], SEARCH_TYPE_FALLBACK, None,
            id="a-row-clears-the-album",
        ),
        pytest.param("la paradoja", "Zzyzx Road", [_HALO], "direct", None, id="direct-match"),
    ],
)  # fmt: skip
def test_lane_is_chosen_from_the_rows_in_hand(song, album, rows, search_type, lane):
    parsed = ParsedRequest(
        artist="Juana Molina",
        album=album,
        song=song,
        message_type=MessageType.REQUEST,
        is_request=True,
    )

    assert miss_probe_lane(parsed, rows, search_type) is lane


@pytest.mark.parametrize(
    ("lane", "matched", "outcome"),
    [
        (MissProbeLane.LIBRARY_MISS, True, "library_miss_discogs_match"),
        (MissProbeLane.LIBRARY_MISS, False, "library_miss_no_discogs_match"),
        (MissProbeLane.SERVE_BLOCKED, True, "serve_blocked_fallback_discogs_match"),
        (MissProbeLane.SERVE_BLOCKED, False, "serve_blocked_fallback_no_discogs_match"),
    ],
)
def test_outcome_values_are_the_four_the_trace_slices_key_on(lane, matched, outcome):
    assert lane.outcome(matched=matched) == outcome
