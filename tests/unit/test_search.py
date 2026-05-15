"""Tests for uncovered lines in core/search.py."""

import asyncio
from unittest.mock import AsyncMock, Mock, patch

import pytest

from core.search import (
    DEFAULT_SEARCH_BUDGET_MS,
    SearchState,
    SearchStrategyType,
    build_strategies,
    execute_search_pipeline,
    get_search_type_from_state,
    has_artist_or_album_or_song,
    no_results_and_ambiguous_format,
    no_results_and_song_but_no_artist,
    no_results_and_song_but_no_artist_track_fallback,
    resolve_search_budget_ms,
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
