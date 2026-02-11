"""Tests for uncovered lines in core/search.py."""

from unittest.mock import AsyncMock

import pytest

from core.search import (
    SearchState,
    SearchStrategy,
    SearchStrategyType,
    build_strategies,
    execute_search_pipeline,
    get_search_type_from_state,
    has_artist_or_album_or_song,
    no_results_and_ambiguous_format,
    no_results_and_song_but_no_artist,
    song_not_found_with_artist_and_song,
)
from library.models import LibraryItem
from services.parser import ParsedRequest


def _item(id=1, artist="Artist", title="Album", **kwargs):
    defaults = dict(
        call_letters="A", artist_call_number=1, release_call_number=1,
        genre="Rock", format="CD",
    )
    defaults.update(kwargs)
    return LibraryItem(id=id, artist=artist, title=title, **defaults)


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


# ---------------------------------------------------------------------------
# execute_search_pipeline -- various paths
# ---------------------------------------------------------------------------


class TestExecuteSearchPipeline:
    @pytest.mark.asyncio
    async def test_swapped_interpretation_no_ambiguous_format(self):
        """SWAPPED_INTERPRETATION with non-ambiguous message results in empty."""
        item = _item(id=1)
        search_lib = AsyncMock(return_value=([], False))
        search_alt = AsyncMock(return_value=([], None))
        search_comp = AsyncMock(return_value=([], {}))

        strategies = build_strategies(search_lib, search_alt, search_comp)

        parsed = ParsedRequest(
            artist="Queen", album="The Game", raw_message="Queen The Game"
        )

        state = await execute_search_pipeline(
            parsed, AsyncMock(), "Queen The Game", strategies,
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
            search_lib, search_alt, search_comp, search_song,
        )

        parsed = ParsedRequest(song="Stereolab", raw_message="Stereolab")

        state = await execute_search_pipeline(
            parsed, AsyncMock(), "Stereolab", strategies,
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

        parsed = ParsedRequest(
            artist="Foo", album="Bar", raw_message="Foo - Bar"
        )

        state = await execute_search_pipeline(
            parsed, AsyncMock(), "Foo - Bar", strategies,
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
            artist="Queen", song="We Will Rock You",
            raw_message="Queen - We Will Rock You",
        )

        state = await execute_search_pipeline(
            parsed, AsyncMock(), "Queen - We Will Rock You", strategies,
        )

        assert state.found_on_compilation is True
        assert state.discogs_titles == {1: "Rock Comp"}
