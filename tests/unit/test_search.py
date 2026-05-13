"""Tests for uncovered lines in core/search.py."""

from unittest.mock import AsyncMock

import pytest

from core.search import (
    SearchState,
    SearchStrategyType,
    build_strategies,
    execute_search_pipeline,
    get_search_type_from_state,
    has_artist_or_album_or_song,
    no_results_and_ambiguous_format,
    no_results_and_song_but_no_artist,
    no_results_and_song_but_no_artist_track_fallback,
    song_not_found_with_artist_and_song,
)
from generated.api_models import TrackMatchHint, TrackMatchSource
from services.parser import ParsedRequest
from tests.factories import make_library_item as _item

# ---------------------------------------------------------------------------
# get_search_type_from_state
# ---------------------------------------------------------------------------


class TestGetSearchTypeFromState:
    def test_compilation(self):
        state = SearchState()
        state.found_on_compilation = True
        assert get_search_type_from_state(state) == "compilation"

    def test_no_strategies_tried(self):
        state = SearchState()
        state.strategies_tried = []
        assert get_search_type_from_state(state) == "none"

    def test_direct(self):
        state = SearchState()
        state.strategies_tried = [SearchStrategyType.ARTIST_PLUS_ALBUM]
        state.song_not_found = False
        assert get_search_type_from_state(state) == "direct"

    def test_fallback(self):
        state = SearchState()
        state.strategies_tried = [SearchStrategyType.ARTIST_PLUS_ALBUM]
        state.song_not_found = True
        assert get_search_type_from_state(state) == "fallback"

    def test_alternative(self):
        state = SearchState()
        state.strategies_tried = [
            SearchStrategyType.ARTIST_PLUS_ALBUM,
            SearchStrategyType.SWAPPED_INTERPRETATION,
        ]
        assert get_search_type_from_state(state) == "alternative"

    def test_track_on_compilation(self):
        state = SearchState()
        state.strategies_tried = [SearchStrategyType.TRACK_ON_COMPILATION]
        assert get_search_type_from_state(state) == "compilation"

    def test_song_as_artist(self):
        state = SearchState()
        state.strategies_tried = [SearchStrategyType.SONG_AS_ARTIST]
        assert get_search_type_from_state(state) == "song_as_artist"

    def test_song_as_track_maps_to_compilation(self):
        """SONG_AS_TRACK reuses the existing 'compilation' search_type.

        The api.yaml v1.3.0 SearchType enum has no `song_as_track` value;
        the precise provenance lives on `matched_via.source` per plan §5.1.
        Track-driven matches are compilation-class from the caller's lens.
        """
        state = SearchState()
        state.strategies_tried = [
            SearchStrategyType.SONG_AS_ARTIST,
            SearchStrategyType.SONG_AS_TRACK,
        ]
        assert get_search_type_from_state(state) == "compilation"


# ---------------------------------------------------------------------------
# Condition functions
# ---------------------------------------------------------------------------


class TestConditions:
    def test_has_artist_or_album_or_song_artist(self):
        parsed = ParsedRequest(artist="Queen", raw_message="Queen")
        state = SearchState()
        assert has_artist_or_album_or_song(parsed, state, "Queen") is True

    def test_has_artist_or_album_or_song_albums(self):
        parsed = ParsedRequest(raw_message="test")
        state = SearchState(albums_for_search=["The Game"])
        assert has_artist_or_album_or_song(parsed, state, "test") is True

    def test_has_artist_or_album_or_song_none(self):
        parsed = ParsedRequest(raw_message="test")
        state = SearchState()
        assert has_artist_or_album_or_song(parsed, state, "test") is False

    def test_no_results_and_ambiguous_format_match(self):
        parsed = ParsedRequest(raw_message="Foo - Bar")
        state = SearchState()
        assert no_results_and_ambiguous_format(parsed, state, "Foo - Bar") is True

    def test_no_results_and_ambiguous_format_has_results(self):
        parsed = ParsedRequest(raw_message="Foo - Bar")
        state = SearchState(results=[_item()])
        assert no_results_and_ambiguous_format(parsed, state, "Foo - Bar") is False

    def test_song_not_found_with_artist_and_song(self):
        parsed = ParsedRequest(artist="Queen", song="Song", raw_message="test")
        state = SearchState(song_not_found=True)
        assert song_not_found_with_artist_and_song(parsed, state, "test") is True

    def test_no_results_and_song_but_no_artist(self):
        parsed = ParsedRequest(song="Stereolab", raw_message="Stereolab")
        state = SearchState()
        assert no_results_and_song_but_no_artist(parsed, state, "Stereolab") is True

    def test_no_results_and_song_but_no_artist_has_artist(self):
        parsed = ParsedRequest(artist="X", song="Y", raw_message="test")
        state = SearchState()
        assert no_results_and_song_but_no_artist(parsed, state, "test") is False

    # SONG_AS_TRACK condition — must fire only after SONG_AS_ARTIST returned empty.

    def test_track_fallback_when_song_as_artist_failed(self):
        """Condition fires after SONG_AS_ARTIST ran and produced nothing."""
        parsed = ParsedRequest(song="vi scose poise", raw_message="vi scose poise")
        state = SearchState(strategies_tried=[SearchStrategyType.SONG_AS_ARTIST])
        assert (
            no_results_and_song_but_no_artist_track_fallback(parsed, state, "vi scose poise")
            is True
        )

    def test_track_fallback_blocked_when_song_as_artist_not_tried(self):
        """Without SONG_AS_ARTIST in strategies_tried, the track fallback must wait.

        This preserves the strategy cascade ordering: SONG_AS_ARTIST gets first
        crack at song-only queries (it covers the parser-misread-artist case),
        and SONG_AS_TRACK only steps in if that failed.
        """
        parsed = ParsedRequest(song="vi scose poise", raw_message="vi scose poise")
        state = SearchState()
        assert (
            no_results_and_song_but_no_artist_track_fallback(parsed, state, "vi scose poise")
            is False
        )

    def test_track_fallback_blocked_when_results_present(self):
        parsed = ParsedRequest(song="X", raw_message="X")
        state = SearchState(
            results=[_item()],
            strategies_tried=[SearchStrategyType.SONG_AS_ARTIST],
        )
        assert no_results_and_song_but_no_artist_track_fallback(parsed, state, "X") is False

    def test_track_fallback_blocked_when_artist_parsed(self):
        parsed = ParsedRequest(artist="Autechre", song="vi scose poise", raw_message="test")
        state = SearchState(strategies_tried=[SearchStrategyType.SONG_AS_ARTIST])
        assert no_results_and_song_but_no_artist_track_fallback(parsed, state, "test") is False

    def test_track_fallback_blocked_when_no_song(self):
        parsed = ParsedRequest(raw_message="test")
        state = SearchState(strategies_tried=[SearchStrategyType.SONG_AS_ARTIST])
        assert no_results_and_song_but_no_artist_track_fallback(parsed, state, "test") is False


# ---------------------------------------------------------------------------
# build_strategies
# ---------------------------------------------------------------------------


class TestBuildStrategies:
    def test_basic_strategies(self):
        strategies = build_strategies(
            search_library_func=AsyncMock(),
            search_alternative_func=AsyncMock(),
            search_compilations_func=AsyncMock(),
        )
        names = [s.name for s in strategies]
        assert SearchStrategyType.ARTIST_PLUS_ALBUM in names
        assert SearchStrategyType.SWAPPED_INTERPRETATION in names
        assert SearchStrategyType.TRACK_ON_COMPILATION in names
        assert SearchStrategyType.SONG_AS_ARTIST not in names

    def test_includes_song_as_artist(self):
        strategies = build_strategies(
            search_library_func=AsyncMock(),
            search_alternative_func=AsyncMock(),
            search_compilations_func=AsyncMock(),
            search_song_as_artist_func=AsyncMock(),
        )
        names = [s.name for s in strategies]
        assert SearchStrategyType.SONG_AS_ARTIST in names

    def test_song_as_track_excluded_without_func(self):
        strategies = build_strategies(
            search_library_func=AsyncMock(),
            search_alternative_func=AsyncMock(),
            search_compilations_func=AsyncMock(),
            search_song_as_artist_func=AsyncMock(),
        )
        names = [s.name for s in strategies]
        assert SearchStrategyType.SONG_AS_TRACK not in names

    def test_song_as_track_appended_after_song_as_artist(self):
        """SONG_AS_TRACK must fire AFTER SONG_AS_ARTIST in the cascade.

        Strategy execution order is array position, so SONG_AS_TRACK's index
        must be strictly greater than SONG_AS_ARTIST's. This enforces the
        condition's `SONG_AS_ARTIST in strategies_tried` precondition.
        """
        strategies = build_strategies(
            search_library_func=AsyncMock(),
            search_alternative_func=AsyncMock(),
            search_compilations_func=AsyncMock(),
            search_song_as_artist_func=AsyncMock(),
            search_song_as_track_func=AsyncMock(),
        )
        names = [s.name for s in strategies]
        assert SearchStrategyType.SONG_AS_TRACK in names
        assert names.index(SearchStrategyType.SONG_AS_TRACK) > names.index(
            SearchStrategyType.SONG_AS_ARTIST
        )


# ---------------------------------------------------------------------------
# execute_search_pipeline -- various paths
# ---------------------------------------------------------------------------


class TestExecuteSearchPipeline:
    @pytest.mark.asyncio
    async def test_swapped_interpretation_no_ambiguous_format(self):
        """SWAPPED_INTERPRETATION with non-ambiguous message results in empty."""
        search_lib = AsyncMock(return_value=([], False))
        search_alt = AsyncMock(return_value=([], None))
        search_comp = AsyncMock(return_value=([], {}))

        strategies = build_strategies(search_lib, search_alt, search_comp)

        parsed = ParsedRequest(artist="Queen", album="The Game", raw_message="Queen The Game")

        state = await execute_search_pipeline(
            parsed,
            AsyncMock(),
            "Queen The Game",
            strategies,
        )

        assert state.results == []

    @pytest.mark.asyncio
    async def test_song_as_artist_path(self):
        """SONG_AS_ARTIST strategy executes and produces results."""
        item = _item(id=1, artist="Stereolab", title="Dots and Loops")

        search_lib = AsyncMock(return_value=([], False))
        search_alt = AsyncMock(return_value=([], None))
        search_comp = AsyncMock(return_value=([], {}))
        search_song = AsyncMock(return_value=([item], None))

        strategies = build_strategies(
            search_lib,
            search_alt,
            search_comp,
            search_song,
        )

        parsed = ParsedRequest(song="Stereolab", raw_message="Stereolab")

        state = await execute_search_pipeline(
            parsed,
            AsyncMock(),
            "Stereolab",
            strategies,
        )

        assert len(state.results) == 1
        assert state.song_not_found is False

    @pytest.mark.asyncio
    async def test_swapped_interpretation_with_results(self):
        """SWAPPED_INTERPRETATION produces results and clears song_not_found."""
        item = _item(id=1, artist="Foo", title="Bar")

        search_lib = AsyncMock(return_value=([], True))  # no results, song_not_found
        search_alt = AsyncMock(return_value=([item], None))
        search_comp = AsyncMock(return_value=([], {}))

        strategies = build_strategies(search_lib, search_alt, search_comp)

        parsed = ParsedRequest(artist="Foo", album="Bar", raw_message="Foo - Bar")

        state = await execute_search_pipeline(
            parsed,
            AsyncMock(),
            "Foo - Bar",
            strategies,
        )

        assert len(state.results) == 1
        assert state.song_not_found is False

    @pytest.mark.asyncio
    async def test_compilation_search_path(self):
        """TRACK_ON_COMPILATION sets found_on_compilation and discogs_titles."""
        item = _item(id=1, artist="Various", title="Rock Comp")

        search_lib = AsyncMock(return_value=([], True))  # song_not_found
        search_alt = AsyncMock(return_value=([], None))
        search_comp = AsyncMock(return_value=([item], {1: "Rock Comp"}))

        strategies = build_strategies(search_lib, search_alt, search_comp)

        parsed = ParsedRequest(
            artist="Queen",
            song="We Will Rock You",
            raw_message="Queen - We Will Rock You",
        )

        state = await execute_search_pipeline(
            parsed,
            AsyncMock(),
            "Queen - We Will Rock You",
            strategies,
        )

        assert state.found_on_compilation is True
        assert state.discogs_titles == {1: "Rock Comp"}

    @pytest.mark.asyncio
    async def test_compilation_search_when_song_not_found_from_album_resolution(self):
        """song_not_found from album resolution triggers compilation search.

        When resolve_albums_for_track() sets song_not_found=True (Discogs found
        only VA releases) but the artist has zero library entries so
        ARTIST_PLUS_ALBUM returns ([], False), the song_not_found flag must
        propagate into the pipeline so TRACK_ON_COMPILATION still runs.
        """
        compilation = _item(
            id=46602,
            artist="Various Artists - Electronic - T",
            title="Trax Records 20th Anniversary Collection",
        )

        # ARTIST_PLUS_ALBUM: artist not in library at all -> ([], False)
        search_lib = AsyncMock(return_value=([], False))
        search_alt = AsyncMock(return_value=([], None))
        search_comp = AsyncMock(
            return_value=([compilation], {46602: "Trax Records 20th Anniversary Collection"})
        )

        strategies = build_strategies(search_lib, search_alt, search_comp)

        parsed = ParsedRequest(
            artist="Adonis",
            song="No Way Back",
            raw_message="No Way Back by Adonis",
        )

        state = await execute_search_pipeline(
            parsed,
            AsyncMock(),
            "No Way Back by Adonis",
            strategies,
            song_not_found=True,
        )

        assert state.found_on_compilation is True
        assert state.results == [compilation]
        assert state.discogs_titles == {46602: "Trax Records 20th Anniversary Collection"}

    @pytest.mark.asyncio
    async def test_comma_format_triggers_swapped_interpretation(self):
        """Comma-separated 'song, artist' triggers SWAPPED_INTERPRETATION.

        Regression test: "Lost Love, Adult." was parsed as song="Lost Love",
        album="Adult.", artist=None. ARTIST_PLUS_ALBUM found nothing, and
        SWAPPED_INTERPRETATION was skipped because detect_ambiguous_format
        didn't recognize comma format. The request fell through to
        SONG_AS_ARTIST which returned wrong results.
        """
        adult_item = _item(id=13766, artist="Adult.", title="Resuscitation")

        search_lib = AsyncMock(return_value=([], False))
        search_alt = AsyncMock(return_value=([adult_item], None))
        search_comp = AsyncMock(return_value=([], {}))
        search_song = AsyncMock(return_value=([], None))

        strategies = build_strategies(search_lib, search_alt, search_comp, search_song)

        parsed = ParsedRequest(
            song="Lost Love",
            album="Adult.",
            raw_message="Lost Love, Adult.",
        )

        state = await execute_search_pipeline(
            parsed,
            AsyncMock(),
            "Lost Love, Adult.",
            strategies,
        )

        assert state.results == [adult_item]
        assert SearchStrategyType.SWAPPED_INTERPRETATION in state.strategies_tried
        search_song.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_song_as_track_runs_after_song_as_artist_empty(self):
        """SONG_AS_TRACK fires for song-only queries after SONG_AS_ARTIST returns empty.

        End-to-end pipeline: ARTIST_PLUS_ALBUM (empty), SONG_AS_ARTIST (empty),
        SONG_AS_TRACK matches via Discogs cross-reference and populates
        state.matched_via_by_id with a TrackMatchHint per matched row.
        """
        confield = _item(id=60359, artist="Autechre", title="Confield")
        hint = TrackMatchHint(
            title="vi scose poise",
            artist_credit=None,
            position="3",
            confidence=0.92,
            source=TrackMatchSource.discogs_release,
        )

        search_lib = AsyncMock(return_value=([], False))
        search_alt = AsyncMock(return_value=([], None))
        search_comp = AsyncMock(return_value=([], {}))
        search_song = AsyncMock(return_value=([], None))
        search_track = AsyncMock(return_value=([confield], {60359: [hint]}))

        strategies = build_strategies(
            search_lib,
            search_alt,
            search_comp,
            search_song,
            search_song_as_track_func=search_track,
        )

        parsed = ParsedRequest(song="vi scose poise", raw_message="vi scose poise")

        state = await execute_search_pipeline(
            parsed,
            AsyncMock(),
            "vi scose poise",
            strategies,
        )

        assert state.results == [confield]
        assert SearchStrategyType.SONG_AS_ARTIST in state.strategies_tried
        assert SearchStrategyType.SONG_AS_TRACK in state.strategies_tried
        assert state.matched_via_by_id == {60359: [hint]}
        search_track.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_song_as_track_skipped_when_song_as_artist_succeeds(self):
        """SONG_AS_ARTIST winning short-circuits before SONG_AS_TRACK runs."""
        item = _item(id=1, artist="Stereolab", title="Dots and Loops")

        search_lib = AsyncMock(return_value=([], False))
        search_alt = AsyncMock(return_value=([], None))
        search_comp = AsyncMock(return_value=([], {}))
        search_song = AsyncMock(return_value=([item], None))
        search_track = AsyncMock(return_value=([], {}))

        strategies = build_strategies(
            search_lib,
            search_alt,
            search_comp,
            search_song,
            search_song_as_track_func=search_track,
        )

        parsed = ParsedRequest(song="Stereolab", raw_message="Stereolab")

        state = await execute_search_pipeline(
            parsed,
            AsyncMock(),
            "Stereolab",
            strategies,
        )

        assert state.results == [item]
        search_track.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_song_as_track_empty_results_keeps_state_empty(self):
        """When SONG_AS_TRACK also returns empty, state.results stays empty."""
        search_lib = AsyncMock(return_value=([], False))
        search_alt = AsyncMock(return_value=([], None))
        search_comp = AsyncMock(return_value=([], {}))
        search_song = AsyncMock(return_value=([], None))
        search_track = AsyncMock(return_value=([], {}))

        strategies = build_strategies(
            search_lib,
            search_alt,
            search_comp,
            search_song,
            search_song_as_track_func=search_track,
        )

        parsed = ParsedRequest(song="nonexistent track", raw_message="nonexistent track")

        state = await execute_search_pipeline(
            parsed,
            AsyncMock(),
            "nonexistent track",
            strategies,
        )

        assert state.results == []
        assert state.matched_via_by_id == {}
        search_track.assert_awaited_once()
