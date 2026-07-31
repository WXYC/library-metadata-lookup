"""Unit tests for the LML#1022 comprehensive multi-location union.

``lookup/location_union.py`` is the orchestrator-level concurrent probe:
given a track query, it answers "which OTHER WXYC library shelf locations
carry this track?" from the LML#1019 recall index alone -- no live Discogs
call on this path. PG and the library DB are mocked here; the recall index's
own read helper (``entity/compilation_track_location.py``) and its PG
behavior are covered separately.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from entity.compilation_track_location import CompilationTrackLocationRow
from generated.api_models import LibraryLocation
from library.models import LibraryItem
from lookup.location_union import (
    build_also_available_on,
    primary_library_ids_from_results,
    resolve_also_available_on,
    should_run_location_union,
)
from lookup.models import LookupRequest
from services.parser import ParsedRequest


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
    def test_true_when_flag_set_and_song_present(self, monkeypatch):
        monkeypatch.setenv("LML_LOCATION_UNION_ENABLED", "true")
        from config.settings import get_settings

        get_settings.cache_clear()
        request = LookupRequest(artist="Brian Reitzell", song="Ikebana", include_locations=True)
        assert should_run_location_union(request) is True

    def test_false_when_include_locations_not_set(self):
        request = LookupRequest(artist="Brian Reitzell", song="Ikebana")
        assert should_run_location_union(request) is False

    def test_false_when_no_song(self):
        request = LookupRequest(artist="Brian Reitzell", include_locations=True)
        assert should_run_location_union(request) is False

    def test_false_when_kill_switch_disabled(self, monkeypatch):
        monkeypatch.setenv("LML_LOCATION_UNION_ENABLED", "false")
        from config.settings import get_settings

        get_settings.cache_clear()
        try:
            request = LookupRequest(artist="Brian Reitzell", song="Ikebana", include_locations=True)
            assert should_run_location_union(request) is False
        finally:
            monkeypatch.delenv("LML_LOCATION_UNION_ENABLED", raising=False)
            get_settings.cache_clear()


@pytest.mark.asyncio
class TestResolveAlsoAvailableOn:
    async def test_no_pg_returns_empty(self):
        parsed = ParsedRequest(artist="Brian Reitzell", song="Ikebana")
        db = AsyncMock()
        result = await resolve_also_available_on(parsed, None, db)
        assert result == []

    async def test_no_artist_returns_empty_without_querying(self):
        parsed = ParsedRequest(song="Ikebana")
        pg = AsyncMock()
        db = AsyncMock()
        result = await resolve_also_available_on(parsed, pg, db)
        assert result == []
        pg.fetchall.assert_not_called()

    async def test_builds_library_locations_from_recall_index(self, monkeypatch):
        parsed = ParsedRequest(artist="Brian Reitzell", song="Ikebana")
        pg = AsyncMock()
        db = AsyncMock()
        db.get_items_by_ids = AsyncMock(
            return_value={
                60654: LibraryItem(id=60654, title="Lost in Translation", artist="Soundtracks - L")
            }
        )
        monkeypatch.setattr(
            "lookup.location_union.get_compilation_track_locations",
            AsyncMock(return_value=[_row()]),
        )

        result = await resolve_also_available_on(parsed, pg, db)

        assert result == [
            LibraryLocation(
                library_id=60654,
                artist="Soundtracks - L",
                album_title="Lost in Translation",
                track_position="A3",
                track_title="ikebana",
                track_artist="brian reitzell",
                credit_role="primary",
                discogs_release_id=12345,
                artwork_url="https://example.com/lit.jpg",
            )
        ]

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

        result = await resolve_also_available_on(parsed, pg, db)

        # Both tied primaries rank ahead of the extra credit; ties break by id.
        assert [loc.library_id for loc in result] == [1, 2, 3]

    async def test_missing_library_row_degrades_shelf_fields_to_none(self, monkeypatch):
        parsed = ParsedRequest(artist="Squarepusher", song="Tommib")
        pg = AsyncMock()
        db = AsyncMock()
        db.get_items_by_ids = AsyncMock(return_value={})
        monkeypatch.setattr(
            "lookup.location_union.get_compilation_track_locations",
            AsyncMock(return_value=[_row(library_id=999)]),
        )

        result = await resolve_also_available_on(parsed, pg, db)

        assert result[0].library_id == 999
        assert result[0].artist is None
        assert result[0].album_title is None


class TestPrimaryLibraryIdsFromResults:
    def test_prefers_items_with_artwork_when_present(self):
        item_a = LibraryItem(id=1, title="A", artist="Artist A")
        item_b = LibraryItem(id=2, title="B", artist="Artist B")
        ids = primary_library_ids_from_results(
            items_with_artwork=[(item_a, None)],
            library_results=[item_a, item_b],
        )
        assert ids == {1}

    def test_falls_back_to_library_results_when_no_artwork(self):
        item_a = LibraryItem(id=1, title="A", artist="Artist A")
        ids = primary_library_ids_from_results(items_with_artwork=[], library_results=[item_a])
        assert ids == {1}


class TestBuildAlsoAvailableOn:
    def test_excludes_primary_locations(self):
        loc_primary = LibraryLocation(
            library_id=1, track_title="tommib", track_artist="squarepusher"
        )
        loc_other = LibraryLocation(
            library_id=60654, track_title="tommib", track_artist="squarepusher"
        )
        result = build_also_available_on([loc_primary, loc_other], primary_library_ids={1})
        assert result == [loc_other]

    def test_empty_candidates_returns_empty_list(self):
        assert build_also_available_on([], primary_library_ids={1}) == []
