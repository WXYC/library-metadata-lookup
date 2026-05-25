"""Tests for uncovered lines in core/search.py."""

import asyncio
import time
from unittest.mock import AsyncMock, Mock, patch

import pytest

from core.search import (
    DEFAULT_SEARCH_BUDGET_MS,
    DEFAULT_SEARCH_HARD_TIMEOUT_MS,
    TRANSPORT_OVERHEAD_MS,
    SearchState,
    SearchStrategyType,
    _log_hard_cap_fired,
    build_strategies,
    execute_search_pipeline,
    get_search_type_from_state,
    has_artist_or_album_or_song,
    no_results_and_ambiguous_format,
    no_results_and_song_but_no_artist,
    no_results_and_song_but_no_artist_track_fallback,
    resolve_effective_search_budget_ms,
    resolve_search_budget_ms,
    resolve_search_hard_timeout_ms,
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


# ---------------------------------------------------------------------------
# Search budget — A3 / LML#340
# ---------------------------------------------------------------------------


class TestSearchBudget:
    """Wall-clock budget short-circuits long pipelines once we already have results.

    Today every strategy gets to run to completion before the next is even
    considered. With BS's 5s LML timeout that converts to "user sees nothing"
    instead of "user sees what LML found in the first 4s". The budget gates
    every subsequent strategy on (elapsed_ms <= budget OR state.results is
    empty) — the second clause is the safety branch that keeps the all-empty
    case running the full pipeline.
    """

    def test_resolve_search_budget_ms_default(self, monkeypatch):
        """Returns DEFAULT_SEARCH_BUDGET_MS when no env var set."""
        monkeypatch.delenv("LML_SEARCH_BUDGET_MS", raising=False)
        assert resolve_search_budget_ms() == DEFAULT_SEARCH_BUDGET_MS

    def test_resolve_search_budget_ms_env_override(self, monkeypatch):
        """LML_SEARCH_BUDGET_MS env var overrides the constant."""
        monkeypatch.setenv("LML_SEARCH_BUDGET_MS", "1500")
        assert resolve_search_budget_ms() == 1500

    def test_resolve_search_budget_ms_invalid_falls_back(self, monkeypatch):
        """Unparseable env value falls back to the default rather than crashing.

        Operator typos shouldn't 500 every /lookup. The fallback is louder than
        silent — surfaces should log at WARN — but the request stays alive.
        """
        monkeypatch.setenv("LML_SEARCH_BUDGET_MS", "not-a-number")
        assert resolve_search_budget_ms() == DEFAULT_SEARCH_BUDGET_MS

    def test_resolve_search_budget_ms_negative_falls_back(self, monkeypatch):
        """Negative values fall back to the default with a WARN.

        A negative budget would make `elapsed_ms > budget_ms` true on the very
        first iteration, short-circuiting every request that produced results
        from strategy 1 — almost certainly not what the operator intended.
        """
        monkeypatch.setenv("LML_SEARCH_BUDGET_MS", "-500")
        assert resolve_search_budget_ms() == DEFAULT_SEARCH_BUDGET_MS

    def test_resolve_search_budget_ms_zero_falls_back(self, monkeypatch):
        """Zero is also a misconfiguration; falls back to the default.

        With budget=0 the gate fires on the second iteration after any prior
        strategy returned results, because `elapsed_ms > 0` after any await.
        Operators reaching for `0` typically mean "disable" — we redirect to
        the default and WARN. Disabling for real means setting the budget
        well above the request timeout.
        """
        monkeypatch.setenv("LML_SEARCH_BUDGET_MS", "0")
        assert resolve_search_budget_ms() == DEFAULT_SEARCH_BUDGET_MS

    @pytest.mark.asyncio
    async def test_budget_exceeded_skips_remaining_strategies(self, monkeypatch):
        """Slow strategy + prior results → subsequent strategies skipped, telemetry set.

        Budget set to 1ms so any await crosses it. First strategy returns
        results quickly (via the artist+album path), second strategy is the
        slow one that crosses the budget, third strategy must not run.
        """
        monkeypatch.setenv("LML_SEARCH_BUDGET_MS", "1")

        first_hit = _item(id=1, artist="Stereolab", title="Dots and Loops")

        async def slow_first(*args, **kwargs):
            # Slow ARTIST_PLUS_ALBUM blows the 1ms budget *before* the next
            # iteration's budget gate fires. Returns results so the gate's
            # safety branch (state.results empty → keep running) doesn't apply.
            await asyncio.sleep(0.05)
            return ([first_hit], False)

        async def must_not_run_compilation(*args, **kwargs):
            raise AssertionError("compilation strategy should be skipped by budget")

        search_lib = AsyncMock(side_effect=slow_first)
        search_alt = AsyncMock(return_value=([], None))
        search_comp = AsyncMock(side_effect=must_not_run_compilation)

        strategies = build_strategies(search_lib, search_alt, search_comp)

        # song_not_found=True keeps TRACK_ON_COMPILATION's condition true even
        # after ARTIST_PLUS_ALBUM populates state.results — without the budget
        # gate the compilation strategy *would* run.
        parsed = ParsedRequest(
            artist="Stereolab",
            song="Miss Modular",
            raw_message="Stereolab - Miss Modular",
        )
        mock_transaction = Mock()
        mock_scope = Mock()
        mock_scope.transaction = mock_transaction

        with patch("core.search.sentry_sdk.get_current_scope", return_value=mock_scope):
            state = await execute_search_pipeline(
                parsed,
                AsyncMock(),
                "Stereolab - Miss Modular",
                strategies,
                song_not_found=True,
            )

        # Compilation strategy never ran.
        search_comp.assert_not_awaited()
        assert SearchStrategyType.TRACK_ON_COMPILATION not in state.strategies_tried
        # State preserves the prior strategy's results.
        assert state.results == [first_hit]
        # Telemetry projected onto the active Sentry transaction.
        calls = {c.args[0]: c.args[1] for c in mock_transaction.set_data.call_args_list}
        assert calls.get("search_budget_exceeded") is True
        assert SearchStrategyType.TRACK_ON_COMPILATION.value in calls.get(
            "search_strategies_skipped", []
        )

    @pytest.mark.asyncio
    async def test_budget_exceeded_safety_branch_when_all_empty(self, monkeypatch):
        """All prior strategies empty → budget does not short-circuit.

        The safety branch is the load-bearing half of the budget design: if
        every strategy has produced nothing, the user is going to get an
        empty response anyway — better to keep running than to short-circuit
        on an irrelevant timer.
        """
        monkeypatch.setenv("LML_SEARCH_BUDGET_MS", "1")

        async def slow_empty(*args, **kwargs):
            await asyncio.sleep(0.02)  # 20ms, blows the 1ms budget
            return ([], False)

        track_hit = _item(id=60359, artist="Autechre", title="Confield")
        hint = TrackMatchHint(
            title="vi scose poise",
            artist_credit=None,
            position="3",
            confidence=0.92,
            source=TrackMatchSource.discogs_release,
        )

        search_lib = AsyncMock(side_effect=slow_empty)
        search_alt = AsyncMock(return_value=([], None))
        search_comp = AsyncMock(return_value=([], {}))
        search_song = AsyncMock(return_value=([], None))
        search_track = AsyncMock(return_value=([track_hit], {60359: [hint]}))

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

        # The slow first strategy blew the budget, but state.results is empty
        # so subsequent strategies still ran and SONG_AS_TRACK surfaced a hit.
        assert state.results == [track_hit]
        search_track.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_budget_breach_when_within_limit(self, monkeypatch):
        """Default budget is generous enough that fast pipelines aren't affected.

        Sanity check: with the default 4s budget and fast mock strategies, the
        budget gate never fires and no telemetry is emitted.
        """
        monkeypatch.delenv("LML_SEARCH_BUDGET_MS", raising=False)

        hit = _item(id=1, artist="Juana Molina", title="DOGA")
        search_lib = AsyncMock(return_value=([hit], False))
        search_alt = AsyncMock(return_value=([], None))
        search_comp = AsyncMock(return_value=([], {}))

        strategies = build_strategies(search_lib, search_alt, search_comp)

        parsed = ParsedRequest(
            artist="Juana Molina", album="DOGA", raw_message="Juana Molina - DOGA"
        )
        mock_transaction = Mock()
        mock_scope = Mock()
        mock_scope.transaction = mock_transaction

        with patch("core.search.sentry_sdk.get_current_scope", return_value=mock_scope):
            state = await execute_search_pipeline(
                parsed,
                AsyncMock(),
                "Juana Molina - DOGA",
                strategies,
            )

        assert state.results == [hit]
        calls = {c.args[0]: c.args[1] for c in mock_transaction.set_data.call_args_list}
        assert "search_budget_exceeded" not in calls

    @pytest.mark.asyncio
    async def test_budget_breach_no_active_transaction_is_noop(self, monkeypatch):
        """Telemetry projection no-ops when there is no active Sentry transaction.

        Mirrors `_log_album_title_fallback` — observability never breaks
        /lookup. Confirms the breach path doesn't raise when Sentry has
        nothing to attach to.
        """
        monkeypatch.setenv("LML_SEARCH_BUDGET_MS", "1")

        first_hit = _item(id=1, artist="Stereolab", title="Dots and Loops")

        async def slow_first(*args, **kwargs):
            await asyncio.sleep(0.02)
            return ([first_hit], False)

        search_lib = AsyncMock(side_effect=slow_first)
        search_alt = AsyncMock(return_value=([], None))
        search_comp = AsyncMock(return_value=([], {}))

        strategies = build_strategies(search_lib, search_alt, search_comp)

        parsed = ParsedRequest(
            artist="Stereolab",
            song="Miss Modular",
            raw_message="Stereolab - Miss Modular",
        )

        mock_scope = Mock()
        mock_scope.transaction = None  # no active transaction

        with patch("core.search.sentry_sdk.get_current_scope", return_value=mock_scope):
            state = await execute_search_pipeline(
                parsed,
                AsyncMock(),
                "Stereolab - Miss Modular",
                strategies,
                song_not_found=True,
            )

        # No crash; pipeline still surfaces the prior strategy's results.
        assert state.results == [first_hit]

    @pytest.mark.asyncio
    async def test_budget_breach_swallows_sentry_sdk_errors(self, monkeypatch):
        """Sentry SDK raising inside the projection path doesn't break /lookup.

        The docstring on `_log_search_budget_exceeded` promises any SDK error
        is swallowed. Pin that promise: if `get_current_scope` itself raises,
        the pipeline still returns prior results normally.
        """
        monkeypatch.setenv("LML_SEARCH_BUDGET_MS", "1")

        first_hit = _item(id=1, artist="Stereolab", title="Dots and Loops")

        async def slow_first(*args, **kwargs):
            await asyncio.sleep(0.02)
            return ([first_hit], False)

        search_lib = AsyncMock(side_effect=slow_first)
        search_alt = AsyncMock(return_value=([], None))
        search_comp = AsyncMock(return_value=([], {}))

        strategies = build_strategies(search_lib, search_alt, search_comp)

        parsed = ParsedRequest(
            artist="Stereolab",
            song="Miss Modular",
            raw_message="Stereolab - Miss Modular",
        )

        with patch(
            "core.search.sentry_sdk.get_current_scope",
            side_effect=RuntimeError("sentry exploded"),
        ):
            state = await execute_search_pipeline(
                parsed,
                AsyncMock(),
                "Stereolab - Miss Modular",
                strategies,
                song_not_found=True,
            )

        # No crash; pipeline still surfaces the prior strategy's results.
        assert state.results == [first_hit]


# ---------------------------------------------------------------------------
# Search hard timeout — LML#370
# ---------------------------------------------------------------------------


class TestSearchHardTimeoutResolver:
    """Env-var resolver for the cascade-exhaustion hard cap.

    The soft budget (LML#340) only short-circuits when ``state.results`` is
    non-empty — by design, so the all-empty-cascade case keeps grinding for a
    better answer. The 2026-05-24 outliers (tail 414s) showed that the
    "keep grinding" branch needs a ceiling: a separate hard cap, defaulting
    to ~25s (5s under BS's 30s AbortController), that fires regardless of
    ``state.results``. Callers cannot raise the hard cap via header — it's a
    safety floor, not a budget. The resolver therefore takes zero parameters,
    unlike :func:`resolve_effective_search_budget_ms`.
    """

    def test_resolve_search_hard_timeout_ms_default(self, monkeypatch):
        """Returns DEFAULT_SEARCH_HARD_TIMEOUT_MS when no env var set."""
        monkeypatch.delenv("LML_SEARCH_HARD_TIMEOUT_MS", raising=False)
        assert resolve_search_hard_timeout_ms() == DEFAULT_SEARCH_HARD_TIMEOUT_MS

    def test_resolve_search_hard_timeout_ms_env_override(self, monkeypatch):
        """LML_SEARCH_HARD_TIMEOUT_MS env var overrides the constant."""
        monkeypatch.setenv("LML_SEARCH_HARD_TIMEOUT_MS", "15000")
        assert resolve_search_hard_timeout_ms() == 15000

    def test_resolve_search_hard_timeout_ms_invalid_falls_back(self, monkeypatch):
        """Unparseable env value falls back to the default rather than crashing.

        Operator typos shouldn't 500 every /lookup. The fallback is louder
        than silent — surfaces should log at WARN — but the request stays
        alive.
        """
        monkeypatch.setenv("LML_SEARCH_HARD_TIMEOUT_MS", "not-a-number")
        assert resolve_search_hard_timeout_ms() == DEFAULT_SEARCH_HARD_TIMEOUT_MS

    def test_resolve_search_hard_timeout_ms_negative_falls_back(self, monkeypatch):
        """Negative values fall back to the default with a WARN.

        A negative hard cap would short-circuit every request before any
        strategy ran. Operators reaching for negative numbers almost
        certainly meant "disable" — to actually disable, set the cap well
        above the request timeout (e.g. 600000).
        """
        monkeypatch.setenv("LML_SEARCH_HARD_TIMEOUT_MS", "-500")
        assert resolve_search_hard_timeout_ms() == DEFAULT_SEARCH_HARD_TIMEOUT_MS

    def test_resolve_search_hard_timeout_ms_zero_falls_back(self, monkeypatch):
        """Zero is also a misconfiguration; falls back to the default.

        With cap=0 the gate fires on the very first iteration before any
        strategy runs. Operators reaching for ``0`` typically mean "disable"
        — we redirect to the default and WARN.
        """
        monkeypatch.setenv("LML_SEARCH_HARD_TIMEOUT_MS", "0")
        assert resolve_search_hard_timeout_ms() == DEFAULT_SEARCH_HARD_TIMEOUT_MS


class TestSearchHardTimeoutLoopGate:
    """Loop-level hard-cap gate fires regardless of ``state.results`` (LML#370).

    The soft-budget gate from LML#340 is gated on ``state.results`` being
    non-empty — by design, so cascade-exhaustion queries keep grinding for a
    better answer. The hard cap is the safety floor that catches them when
    they grind too long.
    """

    @pytest.mark.asyncio
    async def test_hard_cap_fires_with_empty_results(self, monkeypatch):
        """Every strategy returns empty + first strategy slow → loop short-circuits.

        Mirrors the 2026-05-24 414s outlier shape: cascade exhaustion on a
        query no strategy can satisfy. With a 50 ms hard cap and a 200 ms
        first strategy, the loop should set ``timed_out=True`` and exit
        without running the later strategies.
        """
        monkeypatch.setenv("LML_SEARCH_HARD_TIMEOUT_MS", "50")
        # Keep the soft budget out of the way: large enough that it doesn't
        # fire first, and irrelevant anyway since state.results stays empty.
        monkeypatch.setenv("LML_SEARCH_BUDGET_MS", "60000")

        async def slow_empty(*args, **kwargs):
            await asyncio.sleep(0.2)  # 200ms — blows the 50ms cap
            return ([], False)

        async def must_not_run(*args, **kwargs):
            raise AssertionError("strategy past hard cap must not run")

        search_lib = AsyncMock(side_effect=slow_empty)
        search_alt = AsyncMock(side_effect=must_not_run)
        search_comp = AsyncMock(side_effect=must_not_run)

        strategies = build_strategies(search_lib, search_alt, search_comp)

        parsed = ParsedRequest(
            artist="Some Unknown Artist",
            song="Untracked Song",
            raw_message="Some Unknown Artist - Untracked Song",
        )

        state = await execute_search_pipeline(
            parsed,
            AsyncMock(),
            "Some Unknown Artist - Untracked Song",
            strategies,
            song_not_found=True,
        )

        # Later strategies skipped on the empty-results path (the gate that
        # LML#340 alone doesn't cover).
        search_alt.assert_not_awaited()
        search_comp.assert_not_awaited()
        assert state.timed_out is True
        assert state.results == []

    @pytest.mark.asyncio
    async def test_hard_cap_telemetry_projected_onto_sentry(self, monkeypatch):
        """When the cap fires, ``lml.hard_cap_fired`` lands on the active transaction."""
        monkeypatch.setenv("LML_SEARCH_HARD_TIMEOUT_MS", "50")
        monkeypatch.setenv("LML_SEARCH_BUDGET_MS", "60000")

        async def slow_empty(*args, **kwargs):
            await asyncio.sleep(0.2)
            return ([], False)

        search_lib = AsyncMock(side_effect=slow_empty)
        search_alt = AsyncMock(return_value=([], None))
        search_comp = AsyncMock(return_value=([], {}))

        strategies = build_strategies(search_lib, search_alt, search_comp)

        parsed = ParsedRequest(
            artist="Untracked",
            song="Song",
            raw_message="Untracked - Song",
        )
        mock_transaction = Mock()
        mock_scope = Mock()
        mock_scope.transaction = mock_transaction

        with patch("core.search.sentry_sdk.get_current_scope", return_value=mock_scope):
            await execute_search_pipeline(
                parsed,
                AsyncMock(),
                "Untracked - Song",
                strategies,
                song_not_found=True,
            )

        calls = {c.args[0]: c.args[1] for c in mock_transaction.set_data.call_args_list}
        assert calls.get("lml.hard_cap_fired") is True
        # Strategies past the cap recorded as skipped (telemetry diagnostic).
        skipped = calls.get("hard_cap_skipped_strategies", [])
        assert SearchStrategyType.SWAPPED_INTERPRETATION.value in skipped
        assert SearchStrategyType.TRACK_ON_COMPILATION.value in skipped


class TestLogHardCapFired:
    """Sentry projection helper for the hard-cap-fired event."""

    def test_projects_keys_onto_active_transaction(self):
        mock_transaction = Mock()
        mock_scope = Mock()
        mock_scope.transaction = mock_transaction

        with patch("core.search.sentry_sdk.get_current_scope", return_value=mock_scope):
            _log_hard_cap_fired(
                elapsed_ms=27_500.5,
                skipped=[SearchStrategyType.TRACK_ON_COMPILATION, SearchStrategyType.SONG_AS_TRACK],
                hard_cap_ms=25_000,
            )

        calls = {c.args[0]: c.args[1] for c in mock_transaction.set_data.call_args_list}
        assert calls["lml.hard_cap_fired"] is True
        assert calls["hard_cap_skipped_strategies"] == [
            SearchStrategyType.TRACK_ON_COMPILATION.value,
            SearchStrategyType.SONG_AS_TRACK.value,
        ]

    def test_noop_when_no_active_transaction(self):
        """No transaction → silently no-op; do not raise."""
        mock_scope = Mock()
        mock_scope.transaction = None

        with patch("core.search.sentry_sdk.get_current_scope", return_value=mock_scope):
            # Must not raise; nothing to assert beyond that.
            _log_hard_cap_fired(elapsed_ms=10.0, skipped=[], hard_cap_ms=25_000)

    def test_swallows_sdk_errors(self):
        """Any SDK-side exception is swallowed so observability cannot break /lookup."""
        with patch(
            "core.search.sentry_sdk.get_current_scope",
            side_effect=RuntimeError("sentry exploded"),
        ):
            # Must not raise.
            _log_hard_cap_fired(elapsed_ms=10.0, skipped=[], hard_cap_ms=25_000)


class TestPerStrategyWaitFor:
    """Per-strategy ``asyncio.wait_for`` ceiling (LML#370).

    The loop-level hard cap fires *between* strategies. The per-strategy
    wait_for fires *inside* a single strategy that's hanging too long —
    e.g. the 414s outlier where one Discogs-cascade strategy went past the
    cap on its own. wait_for is what propagates ``CancelledError`` into
    in-flight ``asyncio.gather()`` probes, which is what actually frees the
    Discogs semaphore on cap-fire.
    """

    @pytest.mark.asyncio
    async def test_wait_for_cancels_inflight_gather(self, monkeypatch):
        """Load-bearing: cancellation propagates into nested asyncio.gather probes.

        Without this, the fix improves per-request wall time but does not
        free the Discogs semaphore — the queue grows monotonically under
        concurrent load (see [[project_lml_cascade_mechanism]]).
        """
        monkeypatch.setenv("LML_SEARCH_HARD_TIMEOUT_MS", "100")
        monkeypatch.setenv("LML_SEARCH_BUDGET_MS", "60000")
        cancelled: list[str] = []

        async def slow_probe(name: str) -> None:
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                cancelled.append(name)
                raise  # re-raise so gather() sees the cancellation

        async def fan_out_strategy(*_args, **_kwargs):
            await asyncio.gather(slow_probe("a"), slow_probe("b"), slow_probe("c"))
            return ([], False)  # unreachable

        search_lib = AsyncMock(side_effect=fan_out_strategy)
        search_alt = AsyncMock(return_value=([], None))
        search_comp = AsyncMock(return_value=([], {}))
        strategies = build_strategies(search_lib, search_alt, search_comp)

        parsed = ParsedRequest(artist="x", song=None, raw_message="x")

        start = time.monotonic()
        state = await execute_search_pipeline(parsed, AsyncMock(), "x", strategies)
        elapsed = time.monotonic() - start

        assert elapsed < 1.5, f"pipeline should abandon at ~100ms, took {elapsed:.2f}s"
        assert state.timed_out is True
        # All three inner probes cancelled — proof of cancellation propagation.
        assert set(cancelled) == {"a", "b", "c"}
        # The timed-out strategy is recorded as both tried AND timed out.
        assert SearchStrategyType.ARTIST_PLUS_ALBUM in state.strategies_tried
        assert SearchStrategyType.ARTIST_PLUS_ALBUM in state.strategies_timed_out

    @pytest.mark.asyncio
    async def test_strategies_timed_out_populated(self, monkeypatch):
        """``strategies_timed_out`` records which strategy hit its ceiling."""
        monkeypatch.setenv("LML_SEARCH_HARD_TIMEOUT_MS", "100")
        monkeypatch.setenv("LML_SEARCH_BUDGET_MS", "60000")

        async def hangs(*_args, **_kwargs):
            await asyncio.sleep(60)
            return ([], False)

        search_lib = AsyncMock(side_effect=hangs)
        search_alt = AsyncMock(return_value=([], None))
        search_comp = AsyncMock(return_value=([], {}))
        strategies = build_strategies(search_lib, search_alt, search_comp)

        parsed = ParsedRequest(artist="x", song=None, raw_message="x")
        state = await execute_search_pipeline(parsed, AsyncMock(), "x", strategies)

        assert state.strategies_timed_out == [SearchStrategyType.ARTIST_PLUS_ALBUM]
        assert state.timed_out is True


class TestHardCapSoftBudgetIndependence:
    """The hard cap (LML#370) and soft budget (LML#340) are layered knobs.

    Soft budget keeps its "have results, next strategy marginal" semantics;
    hard cap is a separate higher safety floor. Neither subsumes the other.
    """

    @pytest.mark.asyncio
    async def test_soft_budget_still_wins_when_results_exist(self, monkeypatch):
        """Soft budget short-circuits with results; hard cap stays silent.

        Pre-condition: hard cap is much higher than the soft budget; first
        strategy returns results but blows the soft budget. The soft gate
        must fire and ``timed_out`` must stay False — we're not in a
        cascade-exhaustion scenario, just a "got an answer, skip the
        marginal next strategy" scenario.
        """
        monkeypatch.setenv("LML_SEARCH_BUDGET_MS", "1")
        monkeypatch.setenv("LML_SEARCH_HARD_TIMEOUT_MS", "60000")

        first_hit = _item(id=1, artist="Stereolab", title="Dots and Loops")

        async def slow_first(*args, **kwargs):
            await asyncio.sleep(0.05)
            return ([first_hit], False)

        async def must_not_run(*args, **kwargs):
            raise AssertionError("compilation strategy should be skipped by soft budget")

        search_lib = AsyncMock(side_effect=slow_first)
        search_alt = AsyncMock(return_value=([], None))
        search_comp = AsyncMock(side_effect=must_not_run)
        strategies = build_strategies(search_lib, search_alt, search_comp)

        parsed = ParsedRequest(
            artist="Stereolab",
            song="Miss Modular",
            raw_message="Stereolab - Miss Modular",
        )
        mock_transaction = Mock()
        mock_scope = Mock()
        mock_scope.transaction = mock_transaction

        with patch("core.search.sentry_sdk.get_current_scope", return_value=mock_scope):
            state = await execute_search_pipeline(
                parsed,
                AsyncMock(),
                "Stereolab - Miss Modular",
                strategies,
                song_not_found=True,
            )

        # Soft budget wins; hard cap silent; results preserved; not timed out.
        assert state.results == [first_hit]
        assert state.timed_out is False
        calls = {c.args[0]: c.args[1] for c in mock_transaction.set_data.call_args_list}
        assert calls.get("search_budget_exceeded") is True
        assert "lml.hard_cap_fired" not in calls


# ---------------------------------------------------------------------------
# Caller-budget header — A8 / LML#345
# ---------------------------------------------------------------------------


class TestCallerBudget:
    """Per-request caller budget via the X-Caller-Budget-Ms header.

    BS aborts at 5s; LML's env-default budget is 4s. A request that knows it
    has only 3s left (BS retry, fast-path caller, etc.) should be able to
    say "I'm going to give up at 3s — don't grind past 2.8s." The header
    plus the TRANSPORT_OVERHEAD_MS slack lets LML return slightly before
    the caller times out.

    Contract (per LML#345):
      - Header absent or zero/negative → env default (unchanged from A3).
      - Header present and < env default → effective = header − transport overhead.
      - Header present and >= env default → effective = env default (clamp).
    """

    def test_resolve_effective_budget_no_caller_header(self, monkeypatch):
        """No caller header → env default; same as the A3 contract."""
        monkeypatch.delenv("LML_SEARCH_BUDGET_MS", raising=False)
        assert resolve_effective_search_budget_ms(None) == DEFAULT_SEARCH_BUDGET_MS

    def test_resolve_effective_budget_caller_smaller_than_env(self, monkeypatch):
        """Caller budget tighter than env → effective = caller − transport overhead."""
        monkeypatch.delenv("LML_SEARCH_BUDGET_MS", raising=False)
        # Caller says "3000ms"; LML must return ~200ms earlier so the caller
        # sees the response before its own timeout fires.
        assert resolve_effective_search_budget_ms(3000) == 3000 - TRANSPORT_OVERHEAD_MS

    def test_resolve_effective_budget_caller_larger_than_env_clamps(self, monkeypatch):
        """Caller budget looser than env → clamp to env (don't grant more than the operator gave)."""
        monkeypatch.setenv("LML_SEARCH_BUDGET_MS", "2000")
        # Caller says "10000ms" but operator capped at 2000ms; we keep the cap.
        assert resolve_effective_search_budget_ms(10000) == 2000

    def test_resolve_effective_budget_caller_equal_to_env_returns_env(self, monkeypatch):
        """When caller − overhead equals env, the tighter one wins (still env-minus-zero)."""
        monkeypatch.setenv("LML_SEARCH_BUDGET_MS", "4000")
        # Caller header equals env; effective is min(4000-200, 4000) = 3800.
        assert resolve_effective_search_budget_ms(4000) == 4000 - TRANSPORT_OVERHEAD_MS

    def test_resolve_effective_budget_caller_zero_or_negative_falls_back(self, monkeypatch):
        """Caller header <= 0 is treated as misconfiguration; fall back to env."""
        monkeypatch.delenv("LML_SEARCH_BUDGET_MS", raising=False)
        assert resolve_effective_search_budget_ms(0) == DEFAULT_SEARCH_BUDGET_MS
        assert resolve_effective_search_budget_ms(-500) == DEFAULT_SEARCH_BUDGET_MS

    def test_resolve_effective_budget_caller_below_transport_overhead(self, monkeypatch):
        """If caller − overhead would be negative, fall back to env.

        A caller advertising a 100ms budget when transport overhead is 200ms
        is asking for a negative budget. That short-circuits the safety
        branch of the budget gate (`elapsed_ms > budget AND state.results`
        is always true after the first strategy completes). Treat as
        misconfiguration; fall back so the request stays alive.
        """
        monkeypatch.delenv("LML_SEARCH_BUDGET_MS", raising=False)
        assert resolve_effective_search_budget_ms(150) == DEFAULT_SEARCH_BUDGET_MS
        assert resolve_effective_search_budget_ms(TRANSPORT_OVERHEAD_MS) == DEFAULT_SEARCH_BUDGET_MS

    @pytest.mark.asyncio
    async def test_pipeline_honors_caller_budget(self, monkeypatch):
        """Caller-budget arg, when small enough to fire, projects telemetry + skips strategies.

        Mirrors `test_budget_exceeded_skips_remaining_strategies` from the A3
        test class but with the budget flowing through the caller arg rather
        than env. Confirms the wiring: `execute_search_pipeline(..., caller_budget_ms=X)`
        produces the same gate behavior as `LML_SEARCH_BUDGET_MS=X-200`.
        """
        monkeypatch.delenv("LML_SEARCH_BUDGET_MS", raising=False)

        first_hit = _item(id=1, artist="Stereolab", title="Dots and Loops")

        async def slow_first(*args, **kwargs):
            await asyncio.sleep(0.05)
            return ([first_hit], False)

        must_not_run_compilation = AsyncMock(
            side_effect=AssertionError("compilation strategy should be skipped by budget")
        )

        search_lib = AsyncMock(side_effect=slow_first)
        search_alt = AsyncMock(return_value=([], None))
        search_comp = must_not_run_compilation

        strategies = build_strategies(search_lib, search_alt, search_comp)

        parsed = ParsedRequest(
            artist="Stereolab",
            song="Miss Modular",
            raw_message="Stereolab - Miss Modular",
        )
        mock_transaction = Mock()
        mock_scope = Mock()
        mock_scope.transaction = mock_transaction

        # caller_budget_ms = 201 → effective budget = 1 (201 - TRANSPORT_OVERHEAD_MS).
        # Tightest valid effective budget; any await trips the gate.
        with patch("core.search.sentry_sdk.get_current_scope", return_value=mock_scope):
            state = await execute_search_pipeline(
                parsed,
                AsyncMock(),
                "Stereolab - Miss Modular",
                strategies,
                song_not_found=True,
                caller_budget_ms=TRANSPORT_OVERHEAD_MS + 1,
            )

        search_comp.assert_not_awaited()
        assert state.results == [first_hit]
        # Telemetry pins both the budget-exceeded flag AND the caller-budget value,
        # so trace explorer queries can distinguish header-driven from env-driven cutoffs.
        calls = {c.args[0]: c.args[1] for c in mock_transaction.set_data.call_args_list}
        assert calls.get("search_budget_exceeded") is True
        assert calls.get("lml.caller_budget_ms") == TRANSPORT_OVERHEAD_MS + 1

    @pytest.mark.asyncio
    async def test_pipeline_no_caller_budget_does_not_set_caller_attr(self, monkeypatch):
        """When the caller doesn't send the header, the lml.caller_budget_ms attr is not set.

        Sentry attribute hygiene: only set the attribute on traces where the
        caller actually opted into the contract. Saves trace-explorer noise.
        """
        monkeypatch.setenv("LML_SEARCH_BUDGET_MS", "10000")

        first_hit = _item(id=1, artist="Stereolab", title="Dots and Loops")

        async def fast_first(*args, **kwargs):
            return ([first_hit], False)

        search_lib = AsyncMock(side_effect=fast_first)
        search_alt = AsyncMock(return_value=([], None))
        search_comp = AsyncMock(return_value=([], {}))

        strategies = build_strategies(search_lib, search_alt, search_comp)

        parsed = ParsedRequest(
            artist="Stereolab",
            song="Miss Modular",
            raw_message="Stereolab - Miss Modular",
        )
        mock_transaction = Mock()
        mock_scope = Mock()
        mock_scope.transaction = mock_transaction

        with patch("core.search.sentry_sdk.get_current_scope", return_value=mock_scope):
            await execute_search_pipeline(
                parsed,
                AsyncMock(),
                "Stereolab - Miss Modular",
                strategies,
                song_not_found=True,
                # caller_budget_ms not passed.
            )

        keys = {c.args[0] for c in mock_transaction.set_data.call_args_list}
        assert "lml.caller_budget_ms" not in keys
