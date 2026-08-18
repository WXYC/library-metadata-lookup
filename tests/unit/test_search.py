"""Tests for ``core/search.py`` — the strategy seam, runner, and budget knobs.

The ``Outcome`` value type and ``_apply`` helper have their own dedicated
tests in ``tests/unit/test_outcome.py``. This file pins the runner, the
strategy factory, and the budget / hard-cap / caller-budget machinery.

Tests inject mocks that return ``Outcome`` instances (post-#399 seam).
The strategy classes themselves live in ``lookup/strategies/`` and adapt
the orchestrator-side tuple-returning execute funcs into Outcome — but
the tests here drive the pipeline at the runner seam, so they construct
strategies with mock Outcome-returning callables.
"""

import asyncio
import time
from dataclasses import dataclass
from unittest.mock import AsyncMock, Mock, patch

import pytest

from core.exceptions import BreakerOpenError
from core.search import (
    DEFAULT_SEARCH_BUDGET_MS,
    DEFAULT_SEARCH_HARD_TIMEOUT_MS,
    TRANSPORT_OVERHEAD_MS,
    Outcome,
    SearchState,
    SearchStrategyType,
    Strategy,
    _log_hard_cap_fired,
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
from discogs.breaker import DiscogsBreakerOpenError
from discogs.service import _retry_budget_deadline_var
from generated.api_models import TrackMatchHint, TrackMatchSource
from lookup.strategies import build_strategies
from services.parser import ParsedRequest
from tests.factories import make_library_item as _item
from tests.factories import make_resolved_release as _rr

# ---------------------------------------------------------------------------
# Test helpers: tiny strategy stand-ins for runner-level tests
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _StubStrategy:
    """Bare-bones :class:`Strategy` for runner unit tests.

    Production strategies live in ``lookup/strategies/`` and adapt the
    orchestrator-side tuple-returning execute funcs into Outcome. The runner
    tests here drive the pipeline directly with a stub that calls the
    injected mock and a stub condition — exercises the runner / _apply
    seam without dragging a real strategy class in.
    """

    name: SearchStrategyType
    condition: object  # Callable[[ParsedRequest, SearchState, str], bool]
    attempt_func: object  # Callable[..., Awaitable[Outcome]]

    def should_attempt(self, parsed, state, raw_message) -> bool:
        return self.condition(parsed, state, raw_message)  # type: ignore[operator]

    async def attempt(self, parsed, state, raw_message) -> Outcome:
        return await self.attempt_func(parsed, state, raw_message)  # type: ignore[no-any-return,operator,misc]


def _build_test_strategies(
    search_lib: AsyncMock,
    search_alt: AsyncMock,
    search_comp: AsyncMock,
    search_song: AsyncMock | None = None,
    search_track: AsyncMock | None = None,
) -> list[Strategy]:
    """Construct the production strategy classes wrapped around mock execute funcs.

    Mirrors the old ``build_strategies(search_lib, search_alt, search_comp,
    search_song)`` test ergonomics. ``db`` is a shared AsyncMock because the
    strategies pass it to their execute funcs (which are mocks themselves
    here, so the db value never matters).
    """
    return build_strategies(
        AsyncMock(),
        search_library_func=search_lib,
        search_alternative_func=search_alt,
        search_compilations_func=search_comp,
        search_song_as_artist_func=search_song,
        search_song_as_track_func=search_track,
    )


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
            AsyncMock(),
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
            AsyncMock(),
            search_library_func=AsyncMock(),
            search_alternative_func=AsyncMock(),
            search_compilations_func=AsyncMock(),
            search_song_as_artist_func=AsyncMock(),
        )
        names = [s.name for s in strategies]
        assert SearchStrategyType.SONG_AS_ARTIST in names

    def test_song_as_track_excluded_without_func(self):
        strategies = build_strategies(
            AsyncMock(),
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
            AsyncMock(),
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
        search_alt = AsyncMock(return_value=([], None, {}))
        search_comp = AsyncMock(return_value=([], {}))

        strategies = _build_test_strategies(search_lib, search_alt, search_comp)

        parsed = ParsedRequest(artist="Queen", album="The Game", raw_message="Queen The Game")

        state = await execute_search_pipeline(
            parsed,
            "Queen The Game",
            strategies,
        )

        assert state.results == []

    @pytest.mark.asyncio
    async def test_song_as_artist_path(self):
        """SONG_AS_ARTIST strategy executes and produces results."""
        item = _item(id=1, artist="Stereolab", title="Dots and Loops")

        search_lib = AsyncMock(return_value=([], False))
        search_alt = AsyncMock(return_value=([], None, {}))
        search_comp = AsyncMock(return_value=([], {}))
        search_song = AsyncMock(return_value=([item], None))

        strategies = _build_test_strategies(search_lib, search_alt, search_comp, search_song)

        parsed = ParsedRequest(song="Stereolab", raw_message="Stereolab")

        state = await execute_search_pipeline(
            parsed,
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
        search_alt = AsyncMock(return_value=([item], None, {}))
        search_comp = AsyncMock(return_value=([], {}))

        strategies = _build_test_strategies(search_lib, search_alt, search_comp)

        parsed = ParsedRequest(artist="Foo", album="Bar", raw_message="Foo - Bar")

        state = await execute_search_pipeline(
            parsed,
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
        search_alt = AsyncMock(return_value=([], None, {}))
        search_comp = AsyncMock(return_value=([item], {1: _rr(1, "Rock Comp")}))

        strategies = _build_test_strategies(search_lib, search_alt, search_comp)

        parsed = ParsedRequest(
            artist="Queen",
            song="We Will Rock You",
            raw_message="Queen - We Will Rock You",
        )

        state = await execute_search_pipeline(
            parsed,
            "Queen - We Will Rock You",
            strategies,
        )

        assert state.found_on_compilation is True
        assert state.discogs_titles == {1: _rr(1, "Rock Comp")}

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
        search_alt = AsyncMock(return_value=([], None, {}))
        search_comp = AsyncMock(
            return_value=(
                [compilation],
                {46602: _rr(46602, "Trax Records 20th Anniversary Collection")},
            )
        )

        strategies = _build_test_strategies(search_lib, search_alt, search_comp)

        parsed = ParsedRequest(
            artist="Adonis",
            song="No Way Back",
            raw_message="No Way Back by Adonis",
        )

        state = await execute_search_pipeline(
            parsed,
            "No Way Back by Adonis",
            strategies,
            song_not_found=True,
        )

        assert state.found_on_compilation is True
        assert state.results == [compilation]
        assert state.discogs_titles == {
            46602: _rr(46602, "Trax Records 20th Anniversary Collection")
        }

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
        search_alt = AsyncMock(return_value=([adult_item], None, {}))
        search_comp = AsyncMock(return_value=([], {}))
        search_song = AsyncMock(return_value=([], None))

        strategies = _build_test_strategies(search_lib, search_alt, search_comp, search_song)

        parsed = ParsedRequest(
            song="Lost Love",
            album="Adult.",
            raw_message="Lost Love, Adult.",
        )

        state = await execute_search_pipeline(
            parsed,
            "Lost Love, Adult.",
            strategies,
        )

        assert state.results == [adult_item]
        assert SearchStrategyType.SWAPPED_INTERPRETATION in state.strategies_tried
        search_song.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_swapped_interpretation_carries_matched_via(self):
        """LML#622: when SWAPPED narrows via a track cross-reference it returns
        a ``matched_via`` map, which the runner projects onto the state. The
        telemetry search_type stays ``alternative``.
        """
        item = _item(id=1, artist="Jefferson Airplane", title="The Worst of Jefferson Airplane")
        hint = TrackMatchHint(
            title="Today", source=TrackMatchSource.discogs_release, confidence=0.85
        )

        search_lib = AsyncMock(return_value=([], False))
        search_alt = AsyncMock(return_value=([item], {1: [hint]}, {}))
        search_comp = AsyncMock(return_value=([], {}))
        search_song = AsyncMock(return_value=([], None))

        strategies = _build_test_strategies(search_lib, search_alt, search_comp, search_song)

        parsed = ParsedRequest(song="Jefferson Airplane", raw_message="Today, Jefferson Airplane")

        state = await execute_search_pipeline(
            parsed,
            "Today, Jefferson Airplane",
            strategies,
        )

        assert state.results == [item]
        assert state.matched_via_by_id == {1: [hint]}
        assert get_search_type_from_state(state) == "alternative"
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
        search_alt = AsyncMock(return_value=([], None, {}))
        search_comp = AsyncMock(return_value=([], {}))
        search_song = AsyncMock(return_value=([], None))
        search_track = AsyncMock(return_value=([confield], {60359: [hint]}, {}))

        strategies = _build_test_strategies(
            search_lib, search_alt, search_comp, search_song, search_track
        )

        parsed = ParsedRequest(song="vi scose poise", raw_message="vi scose poise")

        state = await execute_search_pipeline(
            parsed,
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
        search_alt = AsyncMock(return_value=([], None, {}))
        search_comp = AsyncMock(return_value=([], {}))
        search_song = AsyncMock(return_value=([item], None))
        search_track = AsyncMock(return_value=([], {}, {}))

        strategies = _build_test_strategies(
            search_lib, search_alt, search_comp, search_song, search_track
        )

        parsed = ParsedRequest(song="Stereolab", raw_message="Stereolab")

        state = await execute_search_pipeline(
            parsed,
            "Stereolab",
            strategies,
        )

        assert state.results == [item]
        search_track.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_song_as_track_empty_results_keeps_state_empty(self):
        """When SONG_AS_TRACK also returns empty, state.results stays empty."""
        search_lib = AsyncMock(return_value=([], False))
        search_alt = AsyncMock(return_value=([], None, {}))
        search_comp = AsyncMock(return_value=([], {}))
        search_song = AsyncMock(return_value=([], None))
        search_track = AsyncMock(return_value=([], {}, {}))

        strategies = _build_test_strategies(
            search_lib, search_alt, search_comp, search_song, search_track
        )

        parsed = ParsedRequest(song="nonexistent track", raw_message="nonexistent track")

        state = await execute_search_pipeline(
            parsed,
            "nonexistent track",
            strategies,
        )

        assert state.results == []
        assert state.matched_via_by_id == {}
        search_track.assert_awaited_once()


# ---------------------------------------------------------------------------
# Runner dispatches by interface (LML#391 step 1)
# ---------------------------------------------------------------------------


class TestRunnerIsGeneric:
    """``execute_search_pipeline`` consumes ``_Strategy`` by interface, not by name.

    The pre-#391 runner switched on ``strategy.name`` in a five-arm ``if/elif``,
    so a strategy whose name didn't match any arm would silently no-op. The
    deepened runner has no such switch: a ``_Strategy`` produced by
    ``build_strategies`` or constructed elsewhere drives the pipeline through
    its ``run`` callable directly. This pins generic dispatch — adding a sixth
    strategy (catalog-track-search's planned ``SONG_AS_LABEL``, etc.) does not
    require touching the runner.
    """

    @pytest.mark.asyncio
    async def test_runner_drives_a_custom_strategy_with_no_orchestrator_arm(self):
        custom_item = _item(id=42, artist="Custom", title="Strategy")
        ran: list[bool] = []

        async def custom_attempt(parsed, state, raw_message):
            ran.append(True)
            return Outcome.found([custom_item])

        # ARTIST_ONLY is a SearchStrategyType value the pre-#391 if/elif
        # never dispatched on (no orchestrator arm for it). Post-#391 / #399
        # the runner just calls ``attempt`` regardless of name; the returned
        # Outcome drives ``_apply`` without a per-strategy branch.
        custom = _StubStrategy(
            name=SearchStrategyType.ARTIST_ONLY,
            condition=lambda parsed, state, raw_message: True,
            attempt_func=custom_attempt,
        )

        parsed = ParsedRequest(artist="Custom", raw_message="Custom")
        state = await execute_search_pipeline(parsed, "Custom", [custom])

        assert ran == [True]
        assert state.results == [custom_item]
        assert state.strategies_tried == [SearchStrategyType.ARTIST_ONLY]


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
        search_alt = AsyncMock(return_value=([], None, {}))
        search_comp = AsyncMock(side_effect=must_not_run_compilation)

        strategies = _build_test_strategies(search_lib, search_alt, search_comp)

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
        search_alt = AsyncMock(return_value=([], None, {}))
        search_comp = AsyncMock(return_value=([], {}))
        search_song = AsyncMock(return_value=([], None))
        search_track = AsyncMock(return_value=([track_hit], {60359: [hint]}, {}))

        strategies = _build_test_strategies(
            search_lib, search_alt, search_comp, search_song, search_track
        )

        parsed = ParsedRequest(song="vi scose poise", raw_message="vi scose poise")

        state = await execute_search_pipeline(
            parsed,
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
        search_alt = AsyncMock(return_value=([], None, {}))
        search_comp = AsyncMock(return_value=([], {}))

        strategies = _build_test_strategies(search_lib, search_alt, search_comp)

        parsed = ParsedRequest(
            artist="Juana Molina", album="DOGA", raw_message="Juana Molina - DOGA"
        )
        mock_transaction = Mock()
        mock_scope = Mock()
        mock_scope.transaction = mock_transaction

        with patch("core.search.sentry_sdk.get_current_scope", return_value=mock_scope):
            state = await execute_search_pipeline(
                parsed,
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
        search_alt = AsyncMock(return_value=([], None, {}))
        search_comp = AsyncMock(return_value=([], {}))

        strategies = _build_test_strategies(search_lib, search_alt, search_comp)

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
        search_alt = AsyncMock(return_value=([], None, {}))
        search_comp = AsyncMock(return_value=([], {}))

        strategies = _build_test_strategies(search_lib, search_alt, search_comp)

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

        strategies = _build_test_strategies(search_lib, search_alt, search_comp)

        parsed = ParsedRequest(
            artist="Some Unknown Artist",
            song="Untracked Song",
            raw_message="Some Unknown Artist - Untracked Song",
        )

        state = await execute_search_pipeline(
            parsed,
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
        """When the cap fires, ``hard_cap_fired`` lands on the active transaction."""
        monkeypatch.setenv("LML_SEARCH_HARD_TIMEOUT_MS", "50")
        monkeypatch.setenv("LML_SEARCH_BUDGET_MS", "60000")

        async def slow_empty(*args, **kwargs):
            await asyncio.sleep(0.2)
            return ([], False)

        search_lib = AsyncMock(side_effect=slow_empty)
        search_alt = AsyncMock(return_value=([], None, {}))
        search_comp = AsyncMock(return_value=([], {}))

        strategies = _build_test_strategies(search_lib, search_alt, search_comp)

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
                "Untracked - Song",
                strategies,
                song_not_found=True,
            )

        calls = {c.args[0]: c.args[1] for c in mock_transaction.set_data.call_args_list}
        assert calls.get("hard_cap_fired") is True
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
        assert calls["hard_cap_fired"] is True
        assert calls["hard_cap_skipped_strategies"] == [
            SearchStrategyType.TRACK_ON_COMPILATION.value,
            SearchStrategyType.SONG_AS_TRACK.value,
        ]
        # Pin the elapsed-ms projection — Sentry trace explorer filtering by
        # "how much did we overshoot the cap" is the natural diagnostic for
        # tuning the cap value over time. Rounded to 2 decimals on emit.
        assert calls["hard_cap_elapsed_ms"] == 27_500.5

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
        search_alt = AsyncMock(return_value=([], None, {}))
        search_comp = AsyncMock(return_value=([], {}))
        strategies = _build_test_strategies(search_lib, search_alt, search_comp)

        parsed = ParsedRequest(artist="x", song=None, raw_message="x")

        # 5s outer wait_for: if wait_for-propagation regresses, fail fast
        # instead of stalling CI for 60s waiting on the mock probes.
        start = time.monotonic()
        state = await asyncio.wait_for(
            execute_search_pipeline(parsed, "x", strategies),
            timeout=5.0,
        )
        elapsed = time.monotonic() - start

        assert elapsed < 1.5, f"pipeline should abandon at ~100ms, took {elapsed:.2f}s"
        assert state.timed_out is True
        # All three inner probes cancelled — proof of cancellation propagation.
        assert set(cancelled) == {"a", "b", "c"}
        # The timed-out strategy is recorded as tried.
        assert SearchStrategyType.ARTIST_PLUS_ALBUM in state.strategies_tried

    @pytest.mark.asyncio
    async def test_jitter_floor_handles_overshoot_between_gate_and_wait_for(self, monkeypatch):
        """Plan §3c′ edge case: ``elapsed_ms`` overshoots ``hard_cap_ms`` between
        the loop-level gate check and the ``wait_for`` call.

        Concretely: the strategy's sync ``condition`` callback (or, more
        realistically, scheduler jitter on a loaded host) advances real time
        past the cap *after* the loop-gate check at ``core/search.py`` and
        *before* the ``remaining_budget_seconds`` computation. ``(hard_cap
        - elapsed)`` goes negative; the ``max(0.01, …)`` floor in the source
        clamps ``wait_for`` to a 10ms ceiling. The strategy hangs longer
        than 10ms, ``wait_for`` raises ``TimeoutError``, and the outer
        ``except`` handler records the same end state as a normal cap fire
        (no special-case path).

        This pins the floor's behavior so a future refactor that removes
        the floor (and reintroduces ``wait_for(0)`` or negative-timeout
        surprises) fails loudly here.
        """
        # Hard cap = 50ms; the condition fn does a sync ``time.sleep(0.1)``
        # right before strategy dispatch, blowing the cap by ~50ms by the
        # time ``remaining_budget_seconds`` is computed. The loop-gate at
        # the *top* of the iteration sees ~0ms elapsed and passes; the
        # condition fn then sleeps; remaining computes as negative and the
        # 0.01s floor kicks in.
        monkeypatch.setenv("LML_SEARCH_HARD_TIMEOUT_MS", "50")
        monkeypatch.setenv("LML_SEARCH_BUDGET_MS", "60000")

        # Real-time sleep in the *condition* function — runs between the
        # loop-gate elapsed_ms read and the remaining_budget calculation.
        # ``Strategy.should_attempt`` is a sync callable in the strategy
        # contract; sync time.sleep here directly mimics scheduler jitter.
        def jittery_condition(parsed, state, raw_message):  # noqa: ARG001
            time.sleep(0.1)  # 100ms blocking sleep — blows the 50ms cap
            return True

        async def slow_attempt(parsed, state, raw_message):  # noqa: ARG001
            # Anything > 10ms triggers the 0.01s wait_for floor's TimeoutError.
            await asyncio.sleep(0.05)
            return Outcome.empty()

        # Single-strategy pipeline with the jittery condition. Constructed by
        # hand rather than through build_strategies so we can inject the
        # jittery_condition that triggers the floor.
        strategies = [
            _StubStrategy(
                name=SearchStrategyType.ARTIST_PLUS_ALBUM,
                condition=jittery_condition,
                attempt_func=slow_attempt,
            )
        ]

        parsed = ParsedRequest(
            artist="Overshoot Artist",
            song="Overshoot Song",
            raw_message="Overshoot Artist - Overshoot Song",
        )

        # Outer wait_for safety net: regression must fail fast, not stall CI.
        state = await asyncio.wait_for(
            execute_search_pipeline(
                parsed,
                "Overshoot Artist - Overshoot Song",
                strategies,
                song_not_found=True,
            ),
            timeout=5.0,
        )

        # Same end state as a normal cap fire: timed_out flagged, no results,
        # strategy recorded as tried.
        assert state.timed_out is True
        assert state.results == []
        assert SearchStrategyType.ARTIST_PLUS_ALBUM in state.strategies_tried


class _FutureStreamingBreakerOpenError(BreakerOpenError):
    """Stand-in for a breaker that does not exist yet (LML#1118 regression pin)."""


class TestBreakerShedInRunner:
    """R2-2: a ``DiscogsBreakerOpenError`` raised from a strategy is caught in
    the runner's per-strategy try (the ONE catch boundary) and converted to the
    same cache-only/empty outcome a strategy miss produces — never propagated to
    a 500.
    """

    @pytest.mark.asyncio
    async def test_breaker_shed_from_strategy_degrades_to_empty(self, monkeypatch):
        from discogs.breaker import DiscogsBreakerOpenError

        monkeypatch.setenv("LML_SEARCH_HARD_TIMEOUT_MS", "60000")
        monkeypatch.setenv("LML_SEARCH_BUDGET_MS", "60000")

        async def shed_attempt(parsed, state, raw_message):  # noqa: ARG001
            raise DiscogsBreakerOpenError("Discogs saturation breaker open")

        strategies = [
            _StubStrategy(
                name=SearchStrategyType.TRACK_ON_COMPILATION,
                condition=lambda *_a: True,
                attempt_func=shed_attempt,
            )
        ]
        parsed = ParsedRequest(
            artist="A Guy Called Gerald",
            song="Message to Black Youth",
            raw_message="A Guy Called Gerald - Message to Black Youth",
        )

        # Must NOT raise — the shed degrades to an empty (cache-only) outcome.
        state = await execute_search_pipeline(
            parsed,
            "A Guy Called Gerald - Message to Black Youth",
            strategies,
            song_not_found=True,
        )

        assert state.results == []
        assert SearchStrategyType.TRACK_ON_COMPILATION in state.strategies_tried
        # LML#1126: the shed must be recorded on SearchState so the orchestrator
        # can mark the wire response degraded instead of a byte-identical
        # genuine no-match.
        assert state.upstream_shed is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "breaker_cls",
        [DiscogsBreakerOpenError, _FutureStreamingBreakerOpenError],
        ids=["discogs-breaker", "future-breaker-subclass"],
    )
    async def test_shed_in_one_strategy_does_not_abort_the_cascade(self, monkeypatch, breaker_cls):
        """A shed in an early strategy is contained: later strategies still run
        (the shed is treated as a miss, not a fatal cascade abort).

        Parametrized over ``DiscogsBreakerOpenError`` and a throwaway
        ``BreakerOpenError`` subclass standing in for a breaker introduced
        after this test is written -- YouTube Music, Spotify, whatever #1100's
        registry adds next (LML#1118 regression pin). The runner's per-strategy
        catch is typed on the shared base, not the concrete Discogs type, so a
        future breaker is degraded the same "continue, not break" way with NO
        further change to this "ONE catch boundary" leg -- proven here by
        keeping a second, healthy strategy in the cascade for both cases, not
        just a single-strategy pipeline where a mutated ``continue`` -> ``break``
        would go uncaught. Without this, each new breaker would repeat the #755
        flood's one-miss-at-a-time discovery pattern the 19 hand-patched
        ``except DiscogsBreakerOpenError`` legs already went through.
        """
        monkeypatch.setenv("LML_SEARCH_HARD_TIMEOUT_MS", "60000")
        monkeypatch.setenv("LML_SEARCH_BUDGET_MS", "60000")

        recovered = _item(id=7, artist="Stereolab", title="Aluminum Tunes", genre="Rock")

        async def shed_attempt(parsed, state, raw_message):  # noqa: ARG001
            raise breaker_cls("shed")

        async def healthy_attempt(parsed, state, raw_message):  # noqa: ARG001
            return Outcome.found([recovered])

        strategies = [
            _StubStrategy(
                name=SearchStrategyType.TRACK_ON_COMPILATION,
                condition=lambda *_a: True,
                attempt_func=shed_attempt,
            ),
            _StubStrategy(
                name=SearchStrategyType.SONG_AS_ARTIST,
                condition=lambda *_a: True,
                attempt_func=healthy_attempt,
            ),
        ]
        parsed = ParsedRequest(artist="Stereolab", song="Fuses", raw_message="x")

        state = await execute_search_pipeline(parsed, "x", strategies, song_not_found=True)

        # The later strategy still ran and surfaced its match despite the shed —
        # ``continue`` semantics are preserved.
        assert [i.id for i in state.results] == [7]
        assert SearchStrategyType.SONG_AS_ARTIST in state.strategies_tried
        # LML#1126: a shed that still produced results (via a later cache-only
        # strategy) is still marked — the response is trustworthy but
        # incomplete, not a clean hit.
        assert state.upstream_shed is True

    @pytest.mark.asyncio
    async def test_no_shed_leaves_upstream_shed_false(self, monkeypatch):
        """Regression pin (LML#1126): a genuine miss with no breaker involvement
        must NOT set ``upstream_shed`` — this is what keeps a real no-match
        distinguishable from a shed on the wire."""
        monkeypatch.setenv("LML_SEARCH_HARD_TIMEOUT_MS", "60000")
        monkeypatch.setenv("LML_SEARCH_BUDGET_MS", "60000")

        async def miss_attempt(parsed, state, raw_message):  # noqa: ARG001
            return Outcome.empty()

        strategies = [
            _StubStrategy(
                name=SearchStrategyType.TRACK_ON_COMPILATION,
                condition=lambda *_a: True,
                attempt_func=miss_attempt,
            )
        ]
        parsed = ParsedRequest(artist="Juana Molina", song="La Verdad", raw_message="x")

        state = await execute_search_pipeline(parsed, "x", strategies, song_not_found=True)

        assert state.results == []
        assert state.upstream_shed is False


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
        search_alt = AsyncMock(return_value=([], None, {}))
        search_comp = AsyncMock(side_effect=must_not_run)
        strategies = _build_test_strategies(search_lib, search_alt, search_comp)

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
                "Stereolab - Miss Modular",
                strategies,
                song_not_found=True,
            )

        # Soft budget wins; hard cap silent; results preserved; not timed out.
        assert state.results == [first_hit]
        assert state.timed_out is False
        calls = {c.args[0]: c.args[1] for c in mock_transaction.set_data.call_args_list}
        assert calls.get("search_budget_exceeded") is True
        assert "hard_cap_fired" not in calls


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
        search_alt = AsyncMock(return_value=([], None, {}))
        search_comp = must_not_run_compilation

        strategies = _build_test_strategies(search_lib, search_alt, search_comp)

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
        # LML#944: the same value is ALSO promoted to a measurement (aggregatable
        # across traces via avg/percentile), alongside the set_data above — set_data
        # alone is opaque to the spans/metrics datasets ("Unknown attribute").
        measurement_calls = {
            c.args[0]: c.args[1] for c in mock_transaction.set_measurement.call_args_list
        }
        assert measurement_calls.get("lml.caller_budget_ms") == TRANSPORT_OVERHEAD_MS + 1

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
        search_alt = AsyncMock(return_value=([], None, {}))
        search_comp = AsyncMock(return_value=([], {}))

        strategies = _build_test_strategies(search_lib, search_alt, search_comp)

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
                "Stereolab - Miss Modular",
                strategies,
                song_not_found=True,
                # caller_budget_ms not passed.
            )

        keys = {c.args[0] for c in mock_transaction.set_data.call_args_list}
        assert "lml.caller_budget_ms" not in keys
        # LML#944: the set_measurement promotion sits behind the same
        # `if caller_budget_ms is not None` guard, so it must be equally absent.
        measurement_keys = {c.args[0] for c in mock_transaction.set_measurement.call_args_list}
        assert "lml.caller_budget_ms" not in measurement_keys

    @pytest.mark.asyncio
    async def test_pipeline_caller_budget_cuts_off_empty_results(self, monkeypatch):
        """Caller-budget honored even when no strategy has produced results.

        The env-default soft budget has an `and state.results` safety branch
        (test_budget_exceeded_safety_branch_when_all_empty above) so the
        request keeps grinding when nothing has hit yet. But when the caller
        explicitly sends X-Caller-Budget-Ms, they have already opted into
        "discard whatever comes after my deadline" semantics — LML grinding
        past that is wasted Discogs quota. With a tight caller budget and
        all strategies slow + empty, the pipeline must short-circuit at the
        budget and surface state.timed_out=True.

        Mirrors the WXYC/library-metadata-lookup#337 production tail
        (Rita Villa, The Fly Girlz) where compilation strategies blocked on
        Discogs rate-limit for 20+ s after returning zero results.
        """
        monkeypatch.delenv("LML_SEARCH_BUDGET_MS", raising=False)

        async def slow_empty(*args, **kwargs):
            await asyncio.sleep(0.05)  # 50ms; blows any reasonable budget
            return ([], False)

        async def slow_empty_compilation(*args, **kwargs):
            await asyncio.sleep(0.05)
            return ([], {})

        search_lib = AsyncMock(side_effect=slow_empty)
        search_alt = AsyncMock(return_value=([], None, {}))
        search_comp = AsyncMock(side_effect=slow_empty_compilation)

        strategies = _build_test_strategies(search_lib, search_alt, search_comp)

        parsed = ParsedRequest(
            artist="Rita Villa",
            song="Czardas",
            raw_message="Rita Villa - Czardas",
        )

        # caller_budget_ms = TRANSPORT_OVERHEAD_MS + 1 → effective budget = 1ms.
        state = await execute_search_pipeline(
            parsed,
            "Rita Villa - Czardas",
            strategies,
            song_not_found=True,
            caller_budget_ms=TRANSPORT_OVERHEAD_MS + 1,
        )

        # First strategy's 50ms blew the 1ms effective budget. The empty-results
        # caller-budget gate must fire and skip the subsequent compilation
        # strategy — that's the 20+ s burn we're trying to avoid in prod.
        assert state.results == []
        assert state.timed_out is True
        search_comp.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_pipeline_no_caller_budget_keeps_safety_branch(self, monkeypatch):
        """Without a caller header, the env soft budget's safety branch holds.

        Regression guard: the no-results / empty-state caller-budget gate
        must NOT activate when the caller didn't opt in. Without a header,
        an empty cascade still runs to completion (subject only to the hard
        cap), preserving the WXYC/library-metadata-lookup#340 safety
        contract documented at test_budget_exceeded_safety_branch_when_all_empty.
        """
        monkeypatch.setenv("LML_SEARCH_BUDGET_MS", "1")

        late_hit = _item(id=99, artist="Rita Villa", title="Czardas")

        async def slow_empty(*args, **kwargs):
            await asyncio.sleep(0.02)
            return ([], False)

        async def slow_then_hit(*args, **kwargs):
            await asyncio.sleep(0.02)
            return ([late_hit], {})

        search_lib = AsyncMock(side_effect=slow_empty)
        search_alt = AsyncMock(return_value=([], None, {}))
        search_comp = AsyncMock(side_effect=slow_then_hit)

        strategies = _build_test_strategies(search_lib, search_alt, search_comp)

        parsed = ParsedRequest(
            artist="Rita Villa",
            song="Czardas",
            raw_message="Rita Villa - Czardas",
        )

        state = await execute_search_pipeline(
            parsed,
            "Rita Villa - Czardas",
            strategies,
            song_not_found=True,
            # caller_budget_ms not passed — safety branch must hold.
        )

        # Cascade kept grinding past the 1ms budget because state.results
        # stayed empty, and the second strategy surfaced the late hit.
        assert state.results == [late_hit]
        assert state.timed_out is False


class TestRetryBudgetDeadlineWiring:
    """LML#758 follow-up: the retry-deadline ContextVar wiring itself.

    ``tests/unit/test_discogs_retry_budget_deadline.py`` exercises
    ``_request_with_retry`` against a manually-set ContextVar -- it never
    calls ``execute_search_pipeline``, so it can't catch a regression where
    the pipeline stops setting (or stops resetting) the deadline. These
    tests drive the wiring at the seam: an attempt captures
    ``discogs.service._retry_budget_deadline_var.get()`` from *inside* the
    strategy call, which is the only vantage point that observes the value
    the retry loop would actually see.

    Also pins the fix for the #758 review finding that arming the deadline
    from the *default* env budget (rather than only an explicit caller
    budget/deadline) silently re-imposes the ~4s soft budget on the 429
    retry sleep for no-header callers, eroding the pre-#758 "keep grinding
    to the hard cap on empty results" contract (LML#340/#347).
    """

    @pytest.mark.asyncio
    async def test_deadline_armed_when_caller_budget_present(self, monkeypatch):
        """A caller-supplied budget arms the retry deadline for the duration
        of the pipeline call, and the ContextVar is back to ``None`` once
        the pipeline returns (leak guard)."""
        monkeypatch.delenv("LML_SEARCH_BUDGET_MS", raising=False)

        captured: list[float | None] = []

        async def capture_and_return(*args, **kwargs):
            captured.append(_retry_budget_deadline_var.get())
            return ([], False)

        search_lib = AsyncMock(side_effect=capture_and_return)
        search_alt = AsyncMock(return_value=([], None, {}))
        search_comp = AsyncMock(return_value=([], {}))

        strategies = _build_test_strategies(search_lib, search_alt, search_comp)

        parsed = ParsedRequest(
            artist="Stereolab",
            song="Miss Modular",
            raw_message="Stereolab - Miss Modular",
        )

        assert _retry_budget_deadline_var.get() is None

        await execute_search_pipeline(
            parsed,
            "Stereolab - Miss Modular",
            strategies,
            song_not_found=True,
            caller_budget_ms=3000,
        )

        assert captured, "search_lib strategy never ran"
        assert captured[0] is not None
        # Reset in the pipeline's finally -- no leak into whatever runs next
        # in the same task.
        assert _retry_budget_deadline_var.get() is None

    @pytest.mark.asyncio
    async def test_deadline_not_armed_without_caller_budget(self, monkeypatch):
        """Without a caller-supplied budget, the retry deadline stays
        ``None`` -- the default ~4s soft budget must NOT cap the 429 retry
        sleep, or a no-header warm-cache/write-path caller degrades to
        cache-only early instead of grinding to the hard cap (the pre-#758
        contract)."""
        monkeypatch.delenv("LML_SEARCH_BUDGET_MS", raising=False)

        captured: list[float | None] = []

        async def capture_and_return(*args, **kwargs):
            captured.append(_retry_budget_deadline_var.get())
            return ([], False)

        search_lib = AsyncMock(side_effect=capture_and_return)
        search_alt = AsyncMock(return_value=([], None, {}))
        search_comp = AsyncMock(return_value=([], {}))

        strategies = _build_test_strategies(search_lib, search_alt, search_comp)

        parsed = ParsedRequest(
            artist="Stereolab",
            song="Miss Modular",
            raw_message="Stereolab - Miss Modular",
        )

        await execute_search_pipeline(
            parsed,
            "Stereolab - Miss Modular",
            strategies,
            song_not_found=True,
            # caller_budget_ms not passed.
        )

        assert captured, "search_lib strategy never ran"
        assert captured[0] is None
        assert _retry_budget_deadline_var.get() is None

    @pytest.mark.asyncio
    async def test_deadline_reset_even_when_a_strategy_raises(self, monkeypatch):
        """The reset-in-``finally`` must fire even when a strategy raises an
        exception the pipeline doesn't catch (anything other than
        ``TimeoutError``/``DiscogsBreakerOpenError``) -- otherwise a crashed
        request would leak a stale deadline into whatever runs next on the
        same task (LML#758 leak-guard, mirroring ``_cap_fire_count_var``)."""
        monkeypatch.delenv("LML_SEARCH_BUDGET_MS", raising=False)

        search_lib = AsyncMock(side_effect=RuntimeError("boom"))
        search_alt = AsyncMock(return_value=([], None, {}))
        search_comp = AsyncMock(return_value=([], {}))

        strategies = _build_test_strategies(search_lib, search_alt, search_comp)

        parsed = ParsedRequest(
            artist="Stereolab",
            song="Miss Modular",
            raw_message="Stereolab - Miss Modular",
        )

        with pytest.raises(RuntimeError, match="boom"):
            await execute_search_pipeline(
                parsed,
                "Stereolab - Miss Modular",
                strategies,
                song_not_found=True,
                caller_budget_ms=3000,
            )

        assert _retry_budget_deadline_var.get() is None

    @pytest.mark.asyncio
    async def test_removing_the_deadline_wiring_would_fail_this_test(self, monkeypatch):
        """Direct pin on the exact deadline value the pipeline arms, so
        deleting the ``set_retry_budget_deadline(...)`` call (or wiring it
        to the wrong budget) fails this test instead of passing silently.
        """
        monkeypatch.delenv("LML_SEARCH_BUDGET_MS", raising=False)

        captured: list[float | None] = []
        observed_monotonic: list[float] = []

        async def capture_and_return(*args, **kwargs):
            observed_monotonic.append(time.monotonic())
            captured.append(_retry_budget_deadline_var.get())
            return ([], False)

        search_lib = AsyncMock(side_effect=capture_and_return)
        search_alt = AsyncMock(return_value=([], None, {}))
        search_comp = AsyncMock(return_value=([], {}))

        strategies = _build_test_strategies(search_lib, search_alt, search_comp)

        parsed = ParsedRequest(
            artist="Stereolab",
            song="Miss Modular",
            raw_message="Stereolab - Miss Modular",
        )

        # caller_budget_ms = 3000 -> effective budget = 3000 - TRANSPORT_OVERHEAD_MS.
        await execute_search_pipeline(
            parsed,
            "Stereolab - Miss Modular",
            strategies,
            song_not_found=True,
            caller_budget_ms=3000,
        )

        assert captured[0] is not None
        expected_budget_seconds = (3000 - TRANSPORT_OVERHEAD_MS) / 1000.0
        # The deadline is an absolute monotonic instant ~budget-seconds ahead
        # of when the strategy ran; allow generous slack for test overhead
        # without weakening the assertion into a tautology.
        assert captured[0] == pytest.approx(
            observed_monotonic[0] + expected_budget_seconds, abs=0.5
        )
