"""Unit tests for the comprehensive multi-location union, folded transparently
into ``results`` (supersedes the original LML#1022 separate-field design).

``lookup/location_union.py`` is the orchestrator-level concurrent probe: given
a track query, it answers "which OTHER WXYC library shelf locations carry this
track?" from the LML#1019 recall index alone -- no live Discogs call on this
path -- and converts survivors into ordinary ``LookupResultItem``s the
orchestrator appends to ``results``. PG and the library DB are mocked here;
the recall index's own read helper (``entity/compilation_track_location.py``)
and its PG behavior are covered separately.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from entity.compilation_track_location import CompilationTrackLocationRow
from generated.api_models import TrackMatchSource
from library.models import LibraryItem
from lookup.location_union import (
    ResolvedLocation,
    build_location_result_items,
    primary_library_ids_from_results,
    resolve_track_shelf_locations,
    should_run_location_union,
)
from lookup.models import LookupRequest
from services.parser import ParsedRequest
from tests.factories import make_catalog_item, make_library_item


def _row(
    *,
    library_id: int = 60654,
    track_position: str = "A3",
    track_artist: str = "brian reitzell",
    track_title: str = "ikebana",
    credit_role: str = "primary",
    discogs_release_id: int = 12345,
    artwork_url: str | None = "https://example.com/lit.jpg",
) -> CompilationTrackLocationRow:
    return CompilationTrackLocationRow(
        library_id=library_id,
        track_position=track_position,
        track_artist=track_artist,
        track_title=track_title,
        credit_role=credit_role,
        discogs_release_id=discogs_release_id,
        artwork_url=artwork_url,
    )


class TestShouldRunLocationUnion:
    def test_true_when_song_present_and_kill_switch_on(self, monkeypatch):
        monkeypatch.setenv("LML_LOCATION_UNION_ENABLED", "true")
        from config.settings import get_settings

        get_settings.cache_clear()
        request = LookupRequest(artist="Brian Reitzell", song="Ikebana")
        assert should_run_location_union(request) is True

    def test_false_when_no_song(self):
        request = LookupRequest(artist="Brian Reitzell")
        assert should_run_location_union(request) is False

    def test_false_when_kill_switch_disabled(self, monkeypatch):
        monkeypatch.setenv("LML_LOCATION_UNION_ENABLED", "false")
        from config.settings import get_settings

        get_settings.cache_clear()
        try:
            request = LookupRequest(artist="Brian Reitzell", song="Ikebana")
            assert should_run_location_union(request) is False
        finally:
            monkeypatch.delenv("LML_LOCATION_UNION_ENABLED", raising=False)
            get_settings.cache_clear()

    def test_no_request_flag_gates_the_union(self):
        """There is no request-level opt-in anymore -- a bare song-bearing
        request runs the union whenever the kill switch is on (server-side
        gating only, per the location-union-transparent-results plan)."""
        assert "include_locations" not in LookupRequest.model_fields
        request = LookupRequest(artist="Brian Reitzell", song="Ikebana")
        assert should_run_location_union(request) is True


@pytest.mark.asyncio
class TestResolveTrackShelfLocations:
    async def test_no_pg_returns_empty(self):
        parsed = ParsedRequest(artist="Brian Reitzell", song="Ikebana")
        db = AsyncMock()
        result = await resolve_track_shelf_locations(parsed, None, db)
        assert result == []

    async def test_no_artist_returns_empty_without_querying(self):
        parsed = ParsedRequest(song="Ikebana")
        pg = AsyncMock()
        db = AsyncMock()
        result = await resolve_track_shelf_locations(parsed, pg, db)
        assert result == []
        pg.fetchall.assert_not_called()

    async def test_retains_the_joined_shelf_item(self, monkeypatch):
        """The probe must retain the joined LibraryItem (not discard it into a
        bare LibraryLocation) so the converter can call to_catalog_item()."""
        parsed = ParsedRequest(artist="Brian Reitzell", song="Ikebana")
        pg = AsyncMock()
        db = AsyncMock()
        lit_item = LibraryItem(id=60654, title="Lost in Translation", artist="Soundtracks - L")
        db.get_items_by_ids = AsyncMock(return_value={60654: lit_item})
        row = _row()
        monkeypatch.setattr(
            "lookup.location_union.get_compilation_track_locations",
            AsyncMock(return_value=[row]),
        )

        result = await resolve_track_shelf_locations(parsed, pg, db)

        assert result == [ResolvedLocation(row=row, shelf_item=lit_item)]

    async def test_ranks_by_credit_tier_then_title_ratio_then_library_id(self, monkeypatch):
        parsed = ParsedRequest(artist="Squarepusher", song="Tommib")
        pg = AsyncMock()
        db = AsyncMock()
        db.get_items_by_ids = AsyncMock(return_value={})
        rows = [
            _row(
                library_id=3, credit_role="extra", track_artist="squarepusher", track_title="tommib"
            ),
            _row(
                library_id=2,
                credit_role="primary",
                track_artist="squarepusher",
                track_title="tommib",
            ),
            _row(
                library_id=1,
                credit_role="primary",
                track_artist="squarepusher",
                track_title="tommib",
            ),
        ]
        monkeypatch.setattr(
            "lookup.location_union.get_compilation_track_locations",
            AsyncMock(return_value=rows),
        )

        result = await resolve_track_shelf_locations(parsed, pg, db)

        # Both tied primaries rank ahead of the extra credit; ties break by id.
        assert [loc.row.library_id for loc in result] == [1, 2, 3]

    async def test_missing_library_row_yields_none_shelf_item(self, monkeypatch):
        """A recall-index row with no matching library row (or the whole join
        failing) surfaces as shelf_item=None -- the converter's job to skip,
        not this probe's."""
        parsed = ParsedRequest(artist="Squarepusher", song="Tommib")
        pg = AsyncMock()
        db = AsyncMock()
        db.get_items_by_ids = AsyncMock(return_value={})
        row = _row(library_id=999)
        monkeypatch.setattr(
            "lookup.location_union.get_compilation_track_locations",
            AsyncMock(return_value=[row]),
        )

        result = await resolve_track_shelf_locations(parsed, pg, db)

        assert result == [ResolvedLocation(row=row, shelf_item=None)]

    async def test_whole_join_failure_degrades_every_row_to_none_shelf_item(self, monkeypatch):
        """A PG exception on the shelf-metadata join degrades to `{}` -- every
        ranked row still comes back, all with shelf_item=None, rather than
        the whole probe raising."""
        parsed = ParsedRequest(artist="Squarepusher", song="Tommib")
        pg = AsyncMock()
        db = AsyncMock()
        db.get_items_by_ids = AsyncMock(side_effect=RuntimeError("pg down"))
        row = _row(library_id=1)
        monkeypatch.setattr(
            "lookup.location_union.get_compilation_track_locations",
            AsyncMock(return_value=[row]),
        )

        result = await resolve_track_shelf_locations(parsed, pg, db)

        assert result == [ResolvedLocation(row=row, shelf_item=None)]


class TestPrimaryLibraryIdsFromResults:
    def test_reads_ids_off_the_built_result_items(self):
        from lookup.models import LookupResultItem

        result_items = [
            LookupResultItem(library_item=make_catalog_item(id=1)),
            LookupResultItem(library_item=make_catalog_item(id=2)),
        ]
        assert primary_library_ids_from_results(result_items) == {1, 2}

    def test_empty_results_yields_empty_set(self):
        assert primary_library_ids_from_results([]) == set()

    def test_tolerates_synthetic_external_cache_rows_with_id_zero(self):
        """A synthetic external-cache row (build_external_catalog_item) carries
        library id 0 -- included harmlessly, since no real location row ever
        carries id 0."""
        from lookup.models import LookupResultItem

        result_items = [
            LookupResultItem(library_item=make_catalog_item(id=0)),
            LookupResultItem(library_item=make_catalog_item(id=5)),
        ]
        assert primary_library_ids_from_results(result_items) == {0, 5}


class TestBuildLocationResultItems:
    def test_excludes_locations_already_in_the_response(self):
        primary_item = make_library_item(id=5, artist="Squarepusher", title="Go Plastic")
        other_item = LibraryItem(id=60654, title="Lost in Translation", artist="Soundtracks - L")
        candidates = [
            ResolvedLocation(
                row=_row(library_id=5, credit_role="primary"), shelf_item=primary_item
            ),
            ResolvedLocation(
                row=_row(library_id=60654, credit_role="extra"), shelf_item=other_item
            ),
        ]

        result = build_location_result_items(candidates, primary_library_ids={5})

        assert [item.library_item.id for item in result] == [60654]

    def test_skips_candidates_with_no_shelf_item(self):
        candidates = [
            ResolvedLocation(row=_row(library_id=999), shelf_item=None),
        ]
        assert build_location_result_items(candidates, primary_library_ids=set()) == []

    def test_empty_candidates_returns_empty_list(self):
        assert build_location_result_items([], primary_library_ids={1}) == []

    def test_converts_to_a_result_item_tagged_via_matched_via(self):
        row = _row(
            library_id=60654,
            track_position="A3",
            track_artist="brian reitzell",
            track_title="ikebana",
            discogs_release_id=12345,
            artwork_url="https://example.com/lit.jpg",
        )
        shelf_item = LibraryItem(
            id=60654,
            title="Lost in Translation",
            artist="Soundtracks - L",
            genre="Soundtrack",
            format="CD",
        )
        candidates = [ResolvedLocation(row=row, shelf_item=shelf_item)]

        result = build_location_result_items(candidates, primary_library_ids=set())

        assert len(result) == 1
        item = result[0]
        # library_item comes from the joined shelf item, not a bare id.
        assert item.library_item.id == 60654
        assert item.library_item.artist == "Soundtracks - L"
        assert item.library_item.title == "Lost in Translation"
        # artwork is precomputed from the recall-index row -- no live Discogs call.
        assert item.artwork is not None
        assert item.artwork.release_id == 12345
        assert item.artwork.release_url == "https://www.discogs.com/release/12345"
        assert item.artwork.artwork_url == "https://example.com/lit.jpg"
        assert item.artwork.album == "Lost in Translation"
        assert item.artwork.artist == "Soundtracks - L"
        # matched_via carries the per-track credit, tagged discogs_release --
        # the recall index is Discogs-release-derived, and this is the second
        # matched_via producer alongside SONG_AS_TRACK (no new enum value).
        assert item.matched_via is not None
        assert len(item.matched_via) == 1
        hint = item.matched_via[0]
        assert hint.title == "ikebana"
        assert hint.artist_credit == "brian reitzell"
        assert hint.position == "A3"
        assert hint.source == TrackMatchSource.discogs_release

    def test_preserves_probe_rank_order(self):
        """The converter must not re-sort -- ranking is entirely the probe's
        job (credit-tier -> title-ratio -> library_id)."""
        shelf_a = LibraryItem(id=1, title="A", artist="Artist A")
        shelf_b = LibraryItem(id=2, title="B", artist="Artist B")
        candidates = [
            ResolvedLocation(row=_row(library_id=1), shelf_item=shelf_a),
            ResolvedLocation(row=_row(library_id=2), shelf_item=shelf_b),
        ]

        result = build_location_result_items(candidates, primary_library_ids=set())

        assert [item.library_item.id for item in result] == [1, 2]
