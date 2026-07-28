"""Spine-level deadline for the lookup hot path (LML#865).

The caller budget (``X-Caller-Budget-Ms``, #345/#403) and the hard timeout
(``LML_SEARCH_HARD_TIMEOUT_MS``, #370) historically wrapped only step 3
(``execute_search_pipeline``). Step 2 — ``resolve_albums_for_track``'s Discogs
pass — ran *before* the bounded region with no deadline at all, so a
non-library artist+song request could grind 60s+ for a caller that hung up at
20s. These tests pin the fix: one deadline, derived at admission, governs the
whole ``perform_lookup`` spine (step 2 + step 3).

The pure-unit block exercises :class:`SpineDeadline`,
:func:`run_within_spine_deadline`, and :func:`project_spine_deadline_telemetry`
in isolation; the integration block drives ``perform_lookup`` with fake slow
step-2 Discogs work to prove the deadline bounds it, frees the Discogs
semaphore on cancellation (mirroring the #370 per-strategy ``wait_for``), and
projects the step-distinguishing telemetry.
"""

import asyncio
import time
from unittest.mock import AsyncMock, Mock, patch

import pytest

from core.search import (
    DEFAULT_SEARCH_HARD_TIMEOUT_MS,
    execute_search_pipeline,
    resolve_search_hard_timeout_ms,
)
from lookup import orchestrator
from lookup.models import LookupRequest
from lookup.orchestrator import perform_lookup
from lookup.spine_deadline import (
    LIMIT_CALLER_BUDGET,
    LIMIT_HARD_CAP,
    SpineDeadline,
    SpineDeadlineExceededError,
    project_spine_deadline_telemetry,
    run_within_spine_deadline,
)
from lookup.strategies import build_strategies
from services.parser import ParsedRequest
from tests.conftest import make_lml_telemetry
from tests.factories import make_library_item

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _strategies(search_lib, search_alt, search_comp):
    """Wrap mock execute funcs in the production strategy classes."""
    return build_strategies(
        AsyncMock(),
        search_library_func=search_lib,
        search_alternative_func=search_alt,
        search_compilations_func=search_comp,
    )


# ---------------------------------------------------------------------------
# SpineDeadline — construction + step_timeout
# ---------------------------------------------------------------------------


class TestSpineDeadlineConstruction:
    def test_no_header_reads_hard_cap_and_no_budget(self, monkeypatch):
        """Without a caller header, only the hard ceiling binds (budget stays None)."""
        monkeypatch.setenv("LML_SEARCH_HARD_TIMEOUT_MS", "150")
        deadline = SpineDeadline.from_caller_budget(None)
        assert deadline.hard_cap_ms == 150
        assert deadline.caller_budget_ms is None
        # Header-gated per #345/#403: the empty-state budget cutoff must not
        # apply without an opt-in header.
        assert deadline.effective_budget_ms is None

    def test_caller_header_sets_effective_budget(self, monkeypatch):
        """An opt-in header derives the effective budget (caller - transport overhead)."""
        monkeypatch.delenv("LML_SEARCH_BUDGET_MS", raising=False)
        monkeypatch.setenv("LML_SEARCH_HARD_TIMEOUT_MS", "25000")
        deadline = SpineDeadline.from_caller_budget(300)
        # 300 - 200ms transport overhead, clamped under the 4000ms env default.
        assert deadline.effective_budget_ms == 100
        assert deadline.caller_budget_ms == 300

    def test_from_caller_budget_always_reads_hard_cap_default(self, monkeypatch):
        """The hard cap is universal — read even when the header is absent."""
        monkeypatch.delenv("LML_SEARCH_HARD_TIMEOUT_MS", raising=False)
        deadline = SpineDeadline.from_caller_budget(None)
        assert deadline.hard_cap_ms == DEFAULT_SEARCH_HARD_TIMEOUT_MS
        assert deadline.hard_cap_ms == resolve_search_hard_timeout_ms()

    def test_step_timeout_hard_cap_binds_without_header(self, monkeypatch):
        """No header: step_timeout tracks the hard-cap remaining, marked hard_cap."""
        monkeypatch.setenv("LML_SEARCH_HARD_TIMEOUT_MS", "150")
        deadline = SpineDeadline.from_caller_budget(None)
        timeout_s, limit = deadline.step_timeout()
        assert limit == LIMIT_HARD_CAP
        # ~0.15s minus a sliver of elapsed; comfortably above the 0.01 floor.
        assert 0.10 < timeout_s <= 0.15

    def test_step_timeout_budget_binds_when_tighter(self, monkeypatch):
        """Header budget below the hard cap binds and is marked caller_budget."""
        monkeypatch.delenv("LML_SEARCH_BUDGET_MS", raising=False)
        monkeypatch.setenv("LML_SEARCH_HARD_TIMEOUT_MS", "25000")
        deadline = SpineDeadline.from_caller_budget(300)  # effective 100ms
        timeout_s, limit = deadline.step_timeout()
        assert limit == LIMIT_CALLER_BUDGET
        assert 0.05 < timeout_s <= 0.10

    def test_step_timeout_floored_at_hundredth_second(self):
        """An already-exhausted deadline floors the step timeout at 0.01s (mirrors step 3)."""
        deadline = SpineDeadline(
            start=time.monotonic() - 10.0,  # 10s in the past — hard past any cap
            hard_cap_ms=150,
            caller_budget_ms=None,
            effective_budget_ms=None,
        )
        timeout_s, limit = deadline.step_timeout()
        assert timeout_s == pytest.approx(0.01)
        assert limit == LIMIT_HARD_CAP

    def test_elapsed_ms_advances(self):
        deadline = SpineDeadline(
            start=time.monotonic() - 0.5,
            hard_cap_ms=25000,
            caller_budget_ms=None,
            effective_budget_ms=None,
        )
        assert deadline.elapsed_ms() >= 500.0


# ---------------------------------------------------------------------------
# SpineDeadline.remaining / tail_exhausted / clamp_probe_timeout_s (LML#930)
# ---------------------------------------------------------------------------


class TestSpineDeadlineRemaining:
    """The signed, un-floored remaining-budget query the tail gate reads."""

    def test_remaining_goes_negative_past_budget(self):
        """Unlike step_timeout (floored at 0.01s), remaining is signed so an
        exhausted deadline reports how far *past* budget it is."""
        deadline = SpineDeadline(
            start=time.monotonic() - 1.0,  # 1s elapsed
            hard_cap_ms=25000,
            caller_budget_ms=500,
            effective_budget_ms=300,
        )
        remaining_ms, limit = deadline.remaining()
        assert remaining_ms < 0  # ~ 300 - 1000 = -700ms
        assert remaining_ms == pytest.approx(-700, abs=50)
        assert limit == LIMIT_CALLER_BUDGET

    def test_remaining_reads_hard_cap_without_header(self):
        """No caller budget → remaining tracks the hard-cap ceiling."""
        deadline = SpineDeadline(
            start=time.monotonic(),
            hard_cap_ms=25000,
            caller_budget_ms=None,
            effective_budget_ms=None,
        )
        remaining_ms, limit = deadline.remaining()
        assert remaining_ms == pytest.approx(25000, abs=50)
        assert limit == LIMIT_HARD_CAP

    def test_remaining_budget_binds_when_tighter_than_hard_cap(self):
        deadline = SpineDeadline(
            start=time.monotonic(),
            hard_cap_ms=25000,
            caller_budget_ms=800,
            effective_budget_ms=600,
        )
        remaining_ms, limit = deadline.remaining()
        assert remaining_ms == pytest.approx(600, abs=50)
        assert limit == LIMIT_CALLER_BUDGET


class TestTailExhausted:
    """The between-tail-step shed gate (LML#930)."""

    def test_fresh_budget_not_exhausted(self):
        deadline = SpineDeadline(
            start=time.monotonic(),
            hard_cap_ms=25000,
            caller_budget_ms=4000,
            effective_budget_ms=3800,
        )
        exhausted, remaining_ms, limit = deadline.tail_exhausted()
        assert exhausted is False
        assert remaining_ms > 0
        assert limit == LIMIT_CALLER_BUDGET

    def test_past_budget_exhausted(self):
        deadline = SpineDeadline(
            start=time.monotonic() - 10.0,  # long past any caller budget
            hard_cap_ms=25000,
            caller_budget_ms=500,
            effective_budget_ms=300,
        )
        exhausted, remaining_ms, limit = deadline.tail_exhausted()
        assert exhausted is True
        assert remaining_ms < 0
        assert limit == LIMIT_CALLER_BUDGET

    def test_headerless_not_exhausted_within_hard_cap(self):
        """No budget header + inside the 25s ceiling → tail runs (unshed)."""
        deadline = SpineDeadline(
            start=time.monotonic(),
            hard_cap_ms=25000,
            caller_budget_ms=None,
            effective_budget_ms=None,
        )
        exhausted, _remaining_ms, limit = deadline.tail_exhausted()
        assert exhausted is False
        assert limit == LIMIT_HARD_CAP

    def test_headerless_exhausted_when_hard_cap_blown(self):
        """No budget header but the universal hard cap is blown → tail sheds."""
        deadline = SpineDeadline(
            start=time.monotonic() - 10.0,
            hard_cap_ms=150,
            caller_budget_ms=None,
            effective_budget_ms=None,
        )
        exhausted, _remaining_ms, limit = deadline.tail_exhausted()
        assert exhausted is True
        assert limit == LIMIT_HARD_CAP

    def test_sub_floor_budget_is_exhausted(self):
        """A remaining budget at/under the 0.01s step floor sheds immediately —
        the same floor step_timeout clamps a started step's wait_for to."""
        deadline = SpineDeadline(
            start=time.monotonic(),
            hard_cap_ms=25000,
            caller_budget_ms=6,
            effective_budget_ms=6,  # < 10ms floor
        )
        exhausted, remaining_ms, _limit = deadline.tail_exhausted()
        assert exhausted is True
        assert remaining_ms <= 10.0


class TestClampProbeTimeout:
    """The per-item Apple-probe wait_for clamp (LML#930, fork-2 'clamp hot I/O')."""

    def test_no_reduction_when_budget_ample(self):
        deadline = SpineDeadline(
            start=time.monotonic(),
            hard_cap_ms=25000,
            caller_budget_ms=5200,
            effective_budget_ms=5000,  # 5s > 4s base
        )
        assert deadline.clamp_probe_timeout_s(4.0) == pytest.approx(4.0, abs=0.05)

    def test_clamps_to_remaining_when_tight(self):
        deadline = SpineDeadline(
            start=time.monotonic(),
            hard_cap_ms=25000,
            caller_budget_ms=700,
            effective_budget_ms=500,  # 0.5s < 4s base
        )
        clamped = deadline.clamp_probe_timeout_s(4.0)
        assert 0.4 < clamped <= 0.5

    def test_floored_when_exhausted(self):
        deadline = SpineDeadline(
            start=time.monotonic() - 10.0,
            hard_cap_ms=25000,
            caller_budget_ms=500,
            effective_budget_ms=300,  # remaining negative
        )
        assert deadline.clamp_probe_timeout_s(4.0) == pytest.approx(0.01)

    def test_headerless_returns_base_unchanged(self):
        deadline = SpineDeadline(
            start=time.monotonic(),
            hard_cap_ms=25000,
            caller_budget_ms=None,
            effective_budget_ms=None,  # remaining = 25s hard cap
        )
        assert deadline.clamp_probe_timeout_s(4.0) == pytest.approx(4.0, abs=0.05)


# ---------------------------------------------------------------------------
# run_within_spine_deadline
# ---------------------------------------------------------------------------


class TestRunWithinSpineDeadline:
    @pytest.mark.asyncio
    async def test_fast_coro_returns_value(self):
        deadline = SpineDeadline.from_caller_budget(None)

        async def fast():
            return "ok"

        assert await run_within_spine_deadline(fast(), deadline, step="album_lookup") == "ok"

    @pytest.mark.asyncio
    async def test_slow_coro_raises_spine_deadline_exceeded(self):
        deadline = SpineDeadline(
            start=time.monotonic(),
            hard_cap_ms=25000,
            caller_budget_ms=300,
            effective_budget_ms=20,  # ~0.02s window
        )

        async def slow():
            await asyncio.sleep(5)

        with pytest.raises(SpineDeadlineExceededError) as excinfo:
            await run_within_spine_deadline(slow(), deadline, step="album_lookup")
        assert excinfo.value.limit == LIMIT_CALLER_BUDGET
        assert excinfo.value.step == "album_lookup"
        assert excinfo.value.elapsed_ms >= 0

    @pytest.mark.asyncio
    async def test_cancellation_frees_semaphore(self):
        """A cancelled step-2 coro runs its finally, freeing the Discogs permit (#370 parity)."""
        deadline = SpineDeadline(
            start=time.monotonic(),
            hard_cap_ms=50,  # ~0.05s window
            caller_budget_ms=None,
            effective_budget_ms=None,
        )
        sem = asyncio.Semaphore(1)

        async def holds_permit():
            await sem.acquire()
            try:
                await asyncio.sleep(5)
            finally:
                sem.release()

        with pytest.raises(SpineDeadlineExceededError):
            await run_within_spine_deadline(holds_permit(), deadline, step="album_lookup")
        # give the cancellation a beat to run the finally
        await asyncio.sleep(0)
        assert not sem.locked()


# ---------------------------------------------------------------------------
# project_spine_deadline_telemetry
# ---------------------------------------------------------------------------


class TestSpineDeadlineTelemetry:
    def test_hard_cap_projection(self):
        """A hard-cap-origin cap hit fires the existing hard_cap_fired key + a step marker."""
        transaction = Mock()
        scope = Mock()
        scope.transaction = transaction
        exc = SpineDeadlineExceededError(
            limit=LIMIT_HARD_CAP, elapsed_ms=27_500.4, step="album_lookup"
        )
        with patch("lookup.spine_deadline.sentry_sdk.get_current_scope", return_value=scope):
            project_spine_deadline_telemetry(exc)
        calls = {c.args[0]: c.args[1] for c in transaction.set_data.call_args_list}
        assert calls["hard_cap_fired"] is True
        assert calls["hard_cap_step"] == "album_lookup"

    def test_caller_budget_projection(self):
        """A budget-origin cap hit fires the existing search_budget_exceeded key + a step marker."""
        transaction = Mock()
        scope = Mock()
        scope.transaction = transaction
        exc = SpineDeadlineExceededError(
            limit=LIMIT_CALLER_BUDGET, elapsed_ms=120.0, step="album_lookup"
        )
        with patch("lookup.spine_deadline.sentry_sdk.get_current_scope", return_value=scope):
            project_spine_deadline_telemetry(exc)
        calls = {c.args[0]: c.args[1] for c in transaction.set_data.call_args_list}
        assert calls["search_budget_exceeded"] is True
        assert calls["search_budget_step"] == "album_lookup"

    def test_no_transaction_is_noop(self):
        scope = Mock()
        scope.transaction = None
        exc = SpineDeadlineExceededError(limit=LIMIT_HARD_CAP, elapsed_ms=1.0, step="album_lookup")
        with patch("lookup.spine_deadline.sentry_sdk.get_current_scope", return_value=scope):
            # Must not raise.
            project_spine_deadline_telemetry(exc)


# ---------------------------------------------------------------------------
# execute_search_pipeline — pipeline_start_offset_ms
# ---------------------------------------------------------------------------


class TestPipelineStartOffset:
    @pytest.mark.asyncio
    async def test_offset_charges_prior_spine_time_against_hard_cap(self, monkeypatch):
        """An offset already past the hard cap short-circuits step 3 on the first iteration."""
        monkeypatch.setenv("LML_SEARCH_HARD_TIMEOUT_MS", "100")
        monkeypatch.setenv("LML_SEARCH_BUDGET_MS", "60000")

        async def must_not_run(*args, **kwargs):
            raise AssertionError("strategy past the spine hard cap must not run")

        search_lib = AsyncMock(side_effect=must_not_run)
        search_alt = AsyncMock(side_effect=must_not_run)
        search_comp = AsyncMock(side_effect=must_not_run)
        strategies = _strategies(search_lib, search_alt, search_comp)

        parsed = ParsedRequest(artist="Some Artist", song="Some Song", raw_message="x")
        state = await execute_search_pipeline(
            parsed,
            "x",
            strategies,
            song_not_found=True,
            pipeline_start_offset_ms=200.0,  # step 2 already blew the 100ms cap
        )

        search_lib.assert_not_awaited()
        assert state.timed_out is True
        assert state.results == []

    @pytest.mark.asyncio
    async def test_default_offset_zero_leaves_found_path_intact(self, monkeypatch):
        """The default offset (0.0) keeps a normal found result unchanged."""
        monkeypatch.delenv("LML_SEARCH_HARD_TIMEOUT_MS", raising=False)
        monkeypatch.delenv("LML_SEARCH_BUDGET_MS", raising=False)
        item = make_library_item(id=1, artist="Stereolab", title="Aluminum Tunes")

        search_lib = AsyncMock(return_value=([item], False))
        search_alt = AsyncMock(return_value=([], None, {}))
        search_comp = AsyncMock(return_value=([], {}))
        strategies = _strategies(search_lib, search_alt, search_comp)

        parsed = ParsedRequest(
            artist="Stereolab", album="Aluminum Tunes", raw_message="Stereolab - Aluminum Tunes"
        )
        state = await execute_search_pipeline(parsed, "Stereolab - Aluminum Tunes", strategies)

        assert len(state.results) == 1
        assert state.timed_out is False


# ---------------------------------------------------------------------------
# perform_lookup — spine deadline bounds step 2 (integration)
# ---------------------------------------------------------------------------


class TestSpineDeadlineBoundsStepTwo:
    @pytest.mark.asyncio
    async def test_caller_budget_bounds_slow_step2(
        self, mock_library_db, mock_discogs_service, monkeypatch
    ):
        """AC #1: caller budget B, step-2 Discogs leg exceeds B → empty timed-out within ~B."""
        monkeypatch.delenv("LML_SEARCH_BUDGET_MS", raising=False)
        monkeypatch.setenv("LML_SEARCH_HARD_TIMEOUT_MS", "25000")

        async def slow_step2(*args, **kwargs):
            await asyncio.sleep(5)
            return [("Jimmy Scott", "Falling in Love Is Wonderful")]

        monkeypatch.setattr(
            "lookup.orchestrator.lookup_releases_by_track", AsyncMock(side_effect=slow_step2)
        )

        request = LookupRequest(
            artist="Jimmy Scott", song="Sycamore Trees", raw_message="Jimmy Scott - Sycamore Trees"
        )
        started = time.monotonic()
        response = await perform_lookup(
            request,
            mock_library_db,
            mock_discogs_service,
            make_lml_telemetry(),
            caller_budget_ms=300,  # effective ~100ms
        )
        elapsed = time.monotonic() - started

        assert response.song_not_found is True
        assert response.timeout is True
        assert response.results == []
        assert elapsed < 1.0  # returned near the budget, not after the 5s grind

    @pytest.mark.asyncio
    async def test_hard_cap_bounds_slow_step2_and_frees_semaphore(
        self, mock_library_db, mock_discogs_service, monkeypatch
    ):
        """AC #2: no header, hard cap bounds slow step 2 and cancellation frees the permit."""
        monkeypatch.setenv("LML_SEARCH_HARD_TIMEOUT_MS", "150")

        sem = asyncio.Semaphore(1)

        async def slow_step2_holding_permit(*args, **kwargs):
            await sem.acquire()
            try:
                await asyncio.sleep(5)
                return [("Jimmy Scott", "Falling in Love Is Wonderful")]
            finally:
                sem.release()

        monkeypatch.setattr(
            "lookup.orchestrator.lookup_releases_by_track",
            AsyncMock(side_effect=slow_step2_holding_permit),
        )

        request = LookupRequest(
            artist="Jimmy Scott", song="Sycamore Trees", raw_message="Jimmy Scott - Sycamore Trees"
        )
        started = time.monotonic()
        response = await perform_lookup(
            request,
            mock_library_db,
            mock_discogs_service,
            make_lml_telemetry(),
            # No caller_budget_ms — the hard ceiling must still bound step 2.
        )
        elapsed = time.monotonic() - started
        await asyncio.sleep(0)  # let the cancellation run the finally

        assert response.timeout is True
        assert response.results == []
        assert elapsed < 1.0
        assert not sem.locked()  # the cancelled step-2 work freed its Discogs permit

    @pytest.mark.asyncio
    async def test_hard_cap_projects_step_marker(
        self, mock_library_db, mock_discogs_service, monkeypatch
    ):
        """AC #4: a step-2-originated hard-cap hit is distinguishable in Sentry via hard_cap_step."""
        monkeypatch.setenv("LML_SEARCH_HARD_TIMEOUT_MS", "100")

        async def slow_step2(*args, **kwargs):
            await asyncio.sleep(5)
            return []

        monkeypatch.setattr(
            "lookup.orchestrator.lookup_releases_by_track", AsyncMock(side_effect=slow_step2)
        )

        transaction = Mock()
        scope = Mock()
        scope.transaction = transaction

        request = LookupRequest(
            artist="Jimmy Scott", song="Sycamore Trees", raw_message="Jimmy Scott - Sycamore Trees"
        )
        with patch("lookup.spine_deadline.sentry_sdk.get_current_scope", return_value=scope):
            await perform_lookup(
                request,
                mock_library_db,
                mock_discogs_service,
                make_lml_telemetry(),
            )

        calls = {c.args[0]: c.args[1] for c in transaction.set_data.call_args_list}
        assert calls.get("hard_cap_fired") is True
        assert calls.get("hard_cap_step") == "album_lookup"

    @pytest.mark.asyncio
    async def test_step3_offset_reflects_step2_elapsed(
        self, mock_library_db, mock_discogs_service, monkeypatch
    ):
        """The step-2 wall time is charged into step 3's clock via the offset (headline wiring).

        Guards the LML#865 promise that the hard cap + caller budget bound
        ``step2 + step3`` *together*: ``_step_search_pipeline`` must call
        ``execute_search_pipeline`` with ``pipeline_start_offset_ms`` equal to the
        spine time already spent. Zeroing that offset (a fresh full-budget step-3
        clock — the ~2x-budget grind #865 set out to bound) is caught here; the
        callee-side honoring of the offset is covered separately in
        ``TestPipelineStartOffset``.
        """
        monkeypatch.delenv("LML_SEARCH_HARD_TIMEOUT_MS", raising=False)
        monkeypatch.delenv("LML_SEARCH_BUDGET_MS", raising=False)

        # Step 2 consumes measurable wall time but returns cleanly (no trip).
        async def slow_but_ok_step2(*args, **kwargs):
            await asyncio.sleep(0.1)
            return []

        monkeypatch.setattr(
            "lookup.orchestrator.lookup_releases_by_track", AsyncMock(side_effect=slow_but_ok_step2)
        )
        # Keep the real step-3 cascade fast + empty so we only observe the offset.
        mock_discogs_service.search.return_value = None

        spy = AsyncMock(wraps=orchestrator.execute_search_pipeline)
        monkeypatch.setattr("lookup.orchestrator.execute_search_pipeline", spy)

        request = LookupRequest(
            artist="Jimmy Scott", song="Sycamore Trees", raw_message="Jimmy Scott - Sycamore Trees"
        )
        await perform_lookup(
            request,
            mock_library_db,
            mock_discogs_service,
            make_lml_telemetry(),
            caller_budget_ms=5000,
        )

        assert spy.await_count == 1
        offset = spy.await_args.kwargs["pipeline_start_offset_ms"]
        # Step 2 slept 100ms, so at least that much spine time is charged to step 3.
        assert offset >= 50.0

    @pytest.mark.asyncio
    async def test_fast_step2_returns_normally_without_timeout(
        self, mock_library_db, mock_discogs_service, monkeypatch
    ):
        """Regression: a fast step 2 is untouched — normal result, timeout falsy."""
        monkeypatch.delenv("LML_SEARCH_HARD_TIMEOUT_MS", raising=False)
        monkeypatch.delenv("LML_SEARCH_BUDGET_MS", raising=False)

        async def fast_step2(*args, **kwargs):
            return [("Jimmy Scott", "Falling in Love Is Wonderful")]

        monkeypatch.setattr(
            "lookup.orchestrator.lookup_releases_by_track", AsyncMock(side_effect=fast_step2)
        )
        mock_discogs_service.search.return_value = None

        request = LookupRequest(
            artist="Jimmy Scott", song="Sycamore Trees", raw_message="Jimmy Scott - Sycamore Trees"
        )
        response = await perform_lookup(
            request,
            mock_library_db,
            mock_discogs_service,
            make_lml_telemetry(),
            caller_budget_ms=5000,
        )

        assert not response.timeout
