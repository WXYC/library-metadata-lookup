"""Unit tests for `core.bulk_concurrency.run_bulk_gather` (LML#1033).

Exercises the shared bulk-dispatch orchestration in isolation, independent of
any one FastAPI route. The per-endpoint abort-path suites
(`test_bulk_lookup_endpoint.py`, `test_bulk_resolve_libraries_endpoint.py`,
`test_cache_refresh_endpoint.py`, `test_artist_genres_router.py`) continue to
pin end-to-end behavior through the HTTP layer; these tests pin the extracted
helper's own contract directly.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from core.bulk_concurrency import run_bulk_gather

# WXYC-representative fixture data (see repo CLAUDE.md) standing in for
# generic "bulk items" -- only their count and completion timing matter here.
_WXYC_ARTISTS = ["Stereolab", "Jessica Pratt", "Juana Molina"]


async def _never_disconnects(_request: object) -> None:
    await asyncio.sleep(3600)


async def _immediate_coro(value: str) -> str:
    return value


class TestRunBulkGatherNormalCompletion:
    @pytest.mark.asyncio
    async def test_returns_per_item_results_in_order(self):
        async def _run_one(name: str) -> str:
            await asyncio.sleep(0)
            return f"resolved:{name}"

        settled: list[bool] = []

        results = await run_bulk_gather(
            (_run_one(name) for name in _WXYC_ARTISTS),
            http_request=object(),
            watch_disconnect_fn=_never_disconnects,
            on_abort=lambda: pytest.fail("on_abort must not fire on normal completion"),
            on_race_settled=settled.append,
        )

        assert results == [f"resolved:{name}" for name in _WXYC_ARTISTS]
        assert settled == [False]

    @pytest.mark.asyncio
    async def test_single_coroutine_iterable_works_and_on_race_settled_is_optional(self):
        """A single-element iterable (the `genres_bulk` shape, whose
        per-artist loop stays sequential inside its own coroutine) works the
        same as an N-item generator -- and, since this call omits
        `on_race_settled`, doubles as proof that the callback is optional.
        """

        async def _process_all() -> list[str]:
            return [f"processed:{name}" for name in _WXYC_ARTISTS]

        results = await run_bulk_gather(
            [_process_all()],
            http_request=object(),
            watch_disconnect_fn=_never_disconnects,
            on_abort=lambda: pytest.fail("on_abort must not fire on normal completion"),
        )

        assert results == [[f"processed:{name}" for name in _WXYC_ARTISTS]]


class TestRunBulkGatherClientAbort:
    @pytest.mark.asyncio
    async def test_sentinel_win_raises_499_and_cancels_in_flight_items(self):
        started = 0
        cleaned_up = 0

        async def _slow_run_one(name: str) -> str:
            nonlocal started, cleaned_up
            started += 1
            try:
                await asyncio.sleep(5)
                return name
            finally:
                cleaned_up += 1

        async def _fast_disconnect(_request: object) -> None:
            await asyncio.sleep(0.01)

        abort_calls = 0
        settled: list[bool] = []

        def _on_abort() -> None:
            nonlocal abort_calls
            abort_calls += 1

        captured_tags: dict[str, str] = {}

        with patch(
            "core.bulk_concurrency.sentry_sdk.set_tag", side_effect=captured_tags.__setitem__
        ):
            with pytest.raises(HTTPException) as exc_info:
                await asyncio.wait_for(
                    run_bulk_gather(
                        (_slow_run_one(name) for name in _WXYC_ARTISTS),
                        http_request=object(),
                        watch_disconnect_fn=_fast_disconnect,
                        on_abort=_on_abort,
                        on_race_settled=settled.append,
                    ),
                    timeout=3.0,
                )

        assert exc_info.value.status_code == 499
        assert exc_info.value.detail == "client disconnected"
        assert abort_calls == 1
        assert settled == [True], f"expected on_race_settled(True) before the 499; got {settled}"
        assert captured_tags.get("lml.client_aborted") == "true", (
            f"Expected lml.client_aborted=true tag; got {captured_tags!r}"
        )
        assert started > 0, "test mis-configured: no items ever started"
        assert started == cleaned_up, (
            f"{started - cleaned_up} item(s) started but never cleaned up -- "
            "cancellation did not reach the in-flight items"
        )


class TestRunBulkGatherParentCancellation:
    @pytest.mark.asyncio
    async def test_cancelling_the_caller_drains_both_arms(self):
        """Cancelling the task awaiting `run_bulk_gather` (the `except
        BaseException` branch around `asyncio.wait`, distinct from the
        sentinel winning) must still cancel AND drain both the gather and the
        sentinel, not just signal them.
        """
        item_started = 0
        item_cleaned_up = 0
        sentinel_started = 0
        sentinel_cleaned_up = 0

        async def _slow_run_one(name: str) -> str:
            nonlocal item_started, item_cleaned_up
            item_started += 1
            try:
                await asyncio.sleep(5)
                return name
            finally:
                item_cleaned_up += 1

        async def _slow_disconnect(_request: object) -> None:
            nonlocal sentinel_started, sentinel_cleaned_up
            sentinel_started += 1
            try:
                await asyncio.sleep(5)
            finally:
                sentinel_cleaned_up += 1

        outer = asyncio.ensure_future(
            run_bulk_gather(
                (_slow_run_one(name) for name in _WXYC_ARTISTS),
                http_request=object(),
                watch_disconnect_fn=_slow_disconnect,
                on_abort=lambda: pytest.fail("on_abort must not fire on parent cancellation"),
            )
        )
        # Let both arms actually start before cancelling the caller.
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        outer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await outer

        assert item_started > 0 and sentinel_started > 0, "test mis-configured"
        assert item_started == item_cleaned_up, (
            f"{item_started - item_cleaned_up} in-flight item(s) never unwound on cancellation"
        )
        assert sentinel_started == sentinel_cleaned_up, "the disconnect sentinel never unwound"


class TestRunBulkGatherTieBreak:
    @pytest.mark.asyncio
    async def test_tie_between_gather_and_sentinel_resolves_toward_gather_done(self):
        """When the gather and the sentinel both settle in the same
        `asyncio.wait` wakeup, the tie must resolve toward "work already
        finished", never an abort. Both coroutines below have no internal
        `await`, so under CPython's single-threaded event loop they complete
        before `run_bulk_gather`'s own `asyncio.wait` wakes, producing a
        genuine tie rather than a timing guess.
        """

        async def _immediate_disconnect(_request: object) -> None:
            return None

        settled: list[bool] = []
        abort_calls = 0

        def _on_abort() -> None:
            nonlocal abort_calls
            abort_calls += 1

        results = await run_bulk_gather(
            (_immediate_coro(name) for name in _WXYC_ARTISTS),
            http_request=object(),
            watch_disconnect_fn=_immediate_disconnect,
            on_abort=_on_abort,
            on_race_settled=settled.append,
        )

        assert results == _WXYC_ARTISTS
        assert settled == [False], f"expected the tie to resolve non-aborted; got {settled}"
        assert abort_calls == 0
