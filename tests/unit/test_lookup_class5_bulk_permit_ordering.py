"""Unit tests for LML#953: class-5 `/lookup` must not pin an interactive slot
while parked on the bulk-global lane.

LML#928 routed a class-5 `/lookup` request onto the low-priority lane by
acquiring the LML#716/#924 process-global bulk permit AROUND `perform_lookup`
-- but that acquisition happened while the request still held the interactive
`LML_LOOKUP_MAX_CONCURRENT` permit (acquired near the top of `handle_lookup`).
Under compound saturation this is a priority inversion: a batch/cron job
firing >= LML_LOOKUP_MAX_CONCURRENT concurrent class-5 requests takes every
interactive slot, then only LML_BULK_GLOBAL_MAX_CONCURRENT of them progress at
a time -- the rest sit parked holding an interactive slot doing no work, so a
concurrent class-1 request finds zero free slots.

The fix (this file) moves the bulk-global-permit acquisition BEFORE (outside)
the interactive semaphore, races that wait against client disconnect (mirrors
the #706/#715 in-flight-cap pattern), and debits the bulk-global wait from the
caller budget / `queue_wait` Server-Timing leg the same way the interactive
wait already was.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from config.settings import get_settings
from core import bulk_concurrency as bc
from core.dependencies import get_discogs_service, get_library_db, get_posthog_client
from discogs.service import DiscogsService
from library.db import LibraryDB
from lookup import router as router_mod
from lookup.models import LookupResponse
from main import app
from tests.unit.conftest import override_deps

LOOKUP_BODY = {
    "artist": "Chuquimamani-Condori",
    "album": "Edits",
    "raw_message": "Chuquimamani-Condori - Edits",
}


async def _wait_until(predicate, *, timeout_s: float = 5.0, message: str = "condition"):
    """Poll ``predicate`` on the loop until true, failing loudly at the deadline.

    Mirrors the identical helper in ``test_lookup_inflight_cap.py`` -- this
    repo's tests deliberately don't import fixtures across test modules, so
    small helpers like this are duplicated per file rather than shared.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while not predicate():
        if loop.time() > deadline:
            pytest.fail(f"timed out after {timeout_s}s waiting for: {message}")
        await asyncio.sleep(0.005)


def _bulk_global_has_waiters() -> bool:
    sem = bc._get_bulk_global_semaphore()
    return bool(getattr(sem, "_waiters", None))


def _lookup_semaphore_has_waiters() -> bool:
    sem = router_mod._lookup_semaphore
    return bool(sem is not None and getattr(sem, "_waiters", None))


def _empty_response() -> LookupResponse:
    return LookupResponse(results=[], search_type="none")


def _parse_server_timing(value: str) -> dict[str, float | None]:
    """Local copy of the helper duplicated across this suite's router tests."""
    parsed: dict[str, float | None] = {}
    for entry in value.split(","):
        entry = entry.strip()
        if not entry:
            continue
        name, _, params = entry.partition(";")
        dur: float | None = None
        for param in params.split(";"):
            param = param.strip()
            if param.startswith("dur="):
                dur = float(param[len("dur=") :])
        parsed[name.strip()] = dur
    return parsed


@pytest.fixture
def app_client(mock_settings):
    with override_deps(
        app,
        {
            get_library_db: AsyncMock(spec=LibraryDB),
            get_discogs_service: AsyncMock(spec=DiscogsService),
            get_posthog_client: None,
            get_settings: mock_settings,
        },
    ):
        yield app


class TestClassFiveDoesNotPinInteractiveSlot:
    """The core LML#953 regression: a class-5 request parked on a saturated
    bulk-global budget must not hold an interactive `LML_LOOKUP_MAX_CONCURRENT`
    permit while it waits."""

    @pytest.mark.asyncio
    async def test_parked_class_five_does_not_hold_interactive_permit(
        self, app_client, monkeypatch
    ):
        monkeypatch.setenv("LML_BULK_GLOBAL_MAX_CONCURRENT", "1")
        monkeypatch.setenv("LML_LOOKUP_MAX_CONCURRENT", "8")

        release_bulk = asyncio.Event()

        async def _occupy_bulk_permit():
            async with bc.acquire_bulk_global_permit():
                await release_bulk.wait()

        occupier = asyncio.create_task(_occupy_bulk_permit())
        await _wait_until(lambda: bc._get_bulk_global_semaphore().locked())

        with patch(
            "lookup.router.perform_lookup",
            new_callable=AsyncMock,
            return_value=_empty_response(),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app_client), base_url="http://test"
            ) as ac:
                class_five = asyncio.create_task(
                    ac.post(
                        "/api/v1/lookup",
                        json=LOOKUP_BODY,
                        headers={"X-Caller-Class": "5"},
                    )
                )
                await _wait_until(
                    _bulk_global_has_waiters,
                    message="class-5 request to park on the bulk-global permit",
                )

                # The interactive semaphore must be untouched: the class-5
                # request hasn't reached that stage yet, so the process-wide
                # cap still shows its full unclaimed value.
                assert router_mod._get_lookup_semaphore()._value == 8
                assert not _lookup_semaphore_has_waiters()

                release_bulk.set()
                resp = await asyncio.wait_for(class_five, timeout=3.0)
                await asyncio.wait_for(occupier, timeout=3.0)

        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_class_one_not_starved_by_parked_class_five(self, app_client, monkeypatch):
        """The acceptance-criteria regression test (LML#953): N > cap
        concurrent class-5 single lookups parked on a saturated bulk budget
        must not block a concurrent class-1 `/lookup` from acquiring an
        interactive slot.

        Cap the interactive semaphore at 2 and park 3 class-5 requests on a
        bulk-global budget of 1 (occupied by a fourth holder). Under the OLD
        ordering, the 3 parked class-5 requests would have claimed both
        interactive slots and left none for a concurrent class-1 request,
        which would then time out below. Under the fix, the class-5 requests
        never reach the interactive semaphore while parked, so the class-1
        request completes promptly.
        """
        monkeypatch.setenv("LML_BULK_GLOBAL_MAX_CONCURRENT", "1")
        monkeypatch.setenv("LML_LOOKUP_MAX_CONCURRENT", "2")

        release_bulk = asyncio.Event()

        async def _occupy_bulk_permit():
            async with bc.acquire_bulk_global_permit():
                await release_bulk.wait()

        occupier = asyncio.create_task(_occupy_bulk_permit())
        await _wait_until(lambda: bc._get_bulk_global_semaphore().locked())

        with patch(
            "lookup.router.perform_lookup",
            new_callable=AsyncMock,
            return_value=_empty_response(),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app_client), base_url="http://test"
            ) as ac:
                five_tasks = [
                    asyncio.create_task(
                        ac.post(
                            "/api/v1/lookup",
                            json=LOOKUP_BODY,
                            headers={"X-Caller-Class": "5"},
                        )
                    )
                    for _ in range(3)
                ]
                await _wait_until(
                    lambda: (
                        len(getattr(bc._get_bulk_global_semaphore(), "_waiters", []) or []) >= 3
                    ),
                    message="3 class-5 requests to park on the bulk-global permit",
                )

                one_resp = await asyncio.wait_for(
                    ac.post(
                        "/api/v1/lookup",
                        json=LOOKUP_BODY,
                        headers={"X-Caller-Class": "1"},
                    ),
                    timeout=1.0,
                )

                release_bulk.set()
                five_resps = await asyncio.gather(*five_tasks)
                await asyncio.wait_for(occupier, timeout=3.0)

        assert one_resp.status_code == 200
        assert all(r.status_code == 200 for r in five_resps)


class TestClassFiveBulkStageDisconnect:
    """LML#953 sub-effect 2: the disconnect sentinel must cover the
    bulk-global wait, not just the interactive-semaphore wait."""

    @pytest.mark.asyncio
    async def test_disconnect_while_parked_on_bulk_stage_returns_499_and_runs_no_lookup(
        self, app_client, monkeypatch
    ):
        monkeypatch.setenv("LML_BULK_GLOBAL_MAX_CONCURRENT", "1")
        monkeypatch.setenv("LML_LOOKUP_MAX_CONCURRENT", "8")

        release_bulk = asyncio.Event()

        async def _occupy_bulk_permit():
            async with bc.acquire_bulk_global_permit():
                await release_bulk.wait()

        occupier = asyncio.create_task(_occupy_bulk_permit())
        await _wait_until(lambda: bc._get_bulk_global_semaphore().locked())

        started = 0

        async def fake_lookup(request, **kwargs):
            nonlocal started
            started += 1
            return _empty_response()

        async def _disconnect_after_parked(_request):
            await _wait_until(
                _bulk_global_has_waiters, message="request to park on the bulk-global permit"
            )

        with (
            patch("lookup.router.perform_lookup", new_callable=AsyncMock, side_effect=fake_lookup),
            patch.object(bc, "watch_disconnect", _disconnect_after_parked),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app_client), base_url="http://test"
            ) as ac:
                resp = await asyncio.wait_for(
                    ac.post(
                        "/api/v1/lookup",
                        json=LOOKUP_BODY,
                        headers={"X-Caller-Class": "5"},
                    ),
                    timeout=3.0,
                )

        assert resp.status_code == 499
        assert started == 0, "a reaped class-5 request must never run perform_lookup"

        release_bulk.set()
        await asyncio.wait_for(occupier, timeout=3.0)

    @pytest.mark.asyncio
    async def test_disconnect_while_parked_on_bulk_stage_sets_sentry_tag(
        self, app_client, monkeypatch
    ):
        import sentry_sdk

        monkeypatch.setenv("LML_BULK_GLOBAL_MAX_CONCURRENT", "1")
        monkeypatch.setenv("LML_LOOKUP_MAX_CONCURRENT", "8")

        release_bulk = asyncio.Event()

        async def _occupy_bulk_permit():
            async with bc.acquire_bulk_global_permit():
                await release_bulk.wait()

        occupier = asyncio.create_task(_occupy_bulk_permit())
        await _wait_until(lambda: bc._get_bulk_global_semaphore().locked())

        captured_tags: dict[str, str] = {}
        original_set_tag = sentry_sdk.set_tag

        def capture_tag(key, value):
            captured_tags[key] = value
            return original_set_tag(key, value)

        async def _disconnect_after_parked(_request):
            await _wait_until(_bulk_global_has_waiters, message="request to park on bulk permit")

        with (
            patch(
                "lookup.router.perform_lookup",
                new_callable=AsyncMock,
                return_value=_empty_response(),
            ),
            patch.object(bc, "watch_disconnect", _disconnect_after_parked),
            patch("lookup.router.sentry_sdk.set_tag", side_effect=capture_tag),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app_client), base_url="http://test"
            ) as ac:
                resp = await asyncio.wait_for(
                    ac.post(
                        "/api/v1/lookup",
                        json=LOOKUP_BODY,
                        headers={"X-Caller-Class": "5"},
                    ),
                    timeout=3.0,
                )

        assert resp.status_code == 499
        assert captured_tags.get("lml.client_aborted") == "true"

        release_bulk.set()
        await asyncio.wait_for(occupier, timeout=3.0)

    @pytest.mark.asyncio
    async def test_reaped_bulk_stage_disconnect_does_not_leak_a_permit(
        self, app_client, monkeypatch
    ):
        """A reaped class-5 request must return its never-taken bulk permit."""
        monkeypatch.setenv("LML_BULK_GLOBAL_MAX_CONCURRENT", "1")
        monkeypatch.setenv("LML_LOOKUP_MAX_CONCURRENT", "8")

        release_bulk = asyncio.Event()

        async def _occupy_bulk_permit():
            async with bc.acquire_bulk_global_permit():
                await release_bulk.wait()

        occupier = asyncio.create_task(_occupy_bulk_permit())
        await _wait_until(lambda: bc._get_bulk_global_semaphore().locked())

        async def _disconnect_after_parked(_request):
            await _wait_until(_bulk_global_has_waiters, message="request to park on bulk permit")

        with (
            patch(
                "lookup.router.perform_lookup",
                new_callable=AsyncMock,
                return_value=_empty_response(),
            ),
            patch.object(bc, "watch_disconnect", _disconnect_after_parked),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app_client), base_url="http://test"
            ) as ac:
                reaped = await asyncio.wait_for(
                    ac.post(
                        "/api/v1/lookup",
                        json=LOOKUP_BODY,
                        headers={"X-Caller-Class": "5"},
                    ),
                    timeout=3.0,
                )

        assert reaped.status_code == 499

        release_bulk.set()
        await asyncio.wait_for(occupier, timeout=3.0)

        # The permit is back in the pool: a fresh class-5 request acquires it
        # uncontended, proving the reap never stranded a slot.
        with patch(
            "lookup.router.perform_lookup",
            new_callable=AsyncMock,
            return_value=_empty_response(),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app_client), base_url="http://test"
            ) as ac:
                third = await asyncio.wait_for(
                    ac.post(
                        "/api/v1/lookup",
                        json=LOOKUP_BODY,
                        headers={"X-Caller-Class": "5"},
                    ),
                    timeout=3.0,
                )

        assert third.status_code == 200


class TestClassFiveBudgetAndServerTiming:
    """LML#953 sub-effect 3: the bulk-global queue wait must be debited from
    ``X-Caller-Budget-Ms`` and reflected in the ``queue_wait`` Server-Timing
    leg, the same way the interactive-semaphore wait already is."""

    @pytest.mark.asyncio
    async def test_bulk_stage_wait_is_debited_from_caller_budget(self, app_client, monkeypatch):
        monkeypatch.setenv("LML_BULK_GLOBAL_MAX_CONCURRENT", "1")
        monkeypatch.setenv("LML_LOOKUP_MAX_CONCURRENT", "8")

        release_bulk = asyncio.Event()

        async def _occupy_bulk_permit():
            async with bc.acquire_bulk_global_permit():
                await release_bulk.wait()

        occupier = asyncio.create_task(_occupy_bulk_permit())
        await _wait_until(lambda: bc._get_bulk_global_semaphore().locked())

        received_budgets: list[int | None] = []

        async def recording_lookup(request, **kwargs):
            received_budgets.append(kwargs.get("caller_budget_ms"))
            return _empty_response()

        with patch(
            "lookup.router.perform_lookup", new_callable=AsyncMock, side_effect=recording_lookup
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app_client), base_url="http://test"
            ) as ac:
                five = asyncio.create_task(
                    ac.post(
                        "/api/v1/lookup",
                        json=LOOKUP_BODY,
                        headers={"X-Caller-Class": "5", "X-Caller-Budget-Ms": "10000"},
                    )
                )
                await _wait_until(
                    _bulk_global_has_waiters, message="class-5 request to park on bulk permit"
                )
                await asyncio.sleep(0.05)
                release_bulk.set()
                resp = await asyncio.wait_for(five, timeout=3.0)
                await asyncio.wait_for(occupier, timeout=3.0)

        assert resp.status_code == 200
        assert len(received_budgets) == 1
        deducted = received_budgets[0]
        assert deducted is not None
        assert 1 <= deducted < 10000
        assert deducted <= 10000 - 40

    @pytest.mark.asyncio
    async def test_bulk_stage_wait_reflected_in_queue_wait_server_timing_leg(
        self, app_client, monkeypatch
    ):
        monkeypatch.setenv("LML_BULK_GLOBAL_MAX_CONCURRENT", "1")
        monkeypatch.setenv("LML_LOOKUP_MAX_CONCURRENT", "8")

        release_bulk = asyncio.Event()

        async def _occupy_bulk_permit():
            async with bc.acquire_bulk_global_permit():
                await release_bulk.wait()

        occupier = asyncio.create_task(_occupy_bulk_permit())
        await _wait_until(lambda: bc._get_bulk_global_semaphore().locked())

        with patch(
            "lookup.router.perform_lookup",
            new_callable=AsyncMock,
            return_value=_empty_response(),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app_client), base_url="http://test"
            ) as ac:
                five = asyncio.create_task(
                    ac.post(
                        "/api/v1/lookup",
                        json=LOOKUP_BODY,
                        headers={"X-Caller-Class": "5"},
                    )
                )
                await _wait_until(
                    _bulk_global_has_waiters, message="class-5 request to park on bulk permit"
                )
                await asyncio.sleep(0.05)
                release_bulk.set()
                resp = await asyncio.wait_for(five, timeout=3.0)
                await asyncio.wait_for(occupier, timeout=3.0)

        assert resp.status_code == 200
        parsed = _parse_server_timing(resp.headers["Server-Timing"])
        assert parsed.get("queue_wait") is not None
        assert parsed["queue_wait"] >= 40

    @pytest.mark.asyncio
    async def test_uncontended_class_five_has_zero_queue_wait(self, app_client, monkeypatch):
        """A class-5 request that finds the bulk-global budget free must
        report a zero queue wait, exactly like an uncontended interactive
        request today."""
        monkeypatch.setenv("LML_BULK_GLOBAL_MAX_CONCURRENT", "4")
        monkeypatch.setenv("LML_LOOKUP_MAX_CONCURRENT", "8")

        with patch(
            "lookup.router.perform_lookup",
            new_callable=AsyncMock,
            return_value=_empty_response(),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app_client), base_url="http://test"
            ) as ac:
                resp = await ac.post(
                    "/api/v1/lookup",
                    json=LOOKUP_BODY,
                    headers={"X-Caller-Class": "5"},
                )

        assert resp.status_code == 200
        parsed = _parse_server_timing(resp.headers["Server-Timing"])
        assert parsed.get("queue_wait") == 0


class TestNoDeadlock:
    """LML#953's ordering must not deadlock: many concurrent class-5 AND
    class-1 requests, both gates saturated, must all eventually complete."""

    @pytest.mark.asyncio
    async def test_mixed_class_traffic_under_double_saturation_completes(
        self, app_client, monkeypatch
    ):
        monkeypatch.setenv("LML_BULK_GLOBAL_MAX_CONCURRENT", "2")
        monkeypatch.setenv("LML_LOOKUP_MAX_CONCURRENT", "2")

        async def fake_lookup(request, **kwargs):
            await asyncio.sleep(0.01)
            return _empty_response()

        with patch("lookup.router.perform_lookup", new_callable=AsyncMock, side_effect=fake_lookup):
            async with AsyncClient(
                transport=ASGITransport(app=app_client), base_url="http://test"
            ) as ac:
                requests = [
                    ac.post(
                        "/api/v1/lookup",
                        json=LOOKUP_BODY,
                        headers={"X-Caller-Class": "5"} if i % 2 == 0 else {},
                    )
                    for i in range(12)
                ]
                responses = await asyncio.wait_for(asyncio.gather(*requests), timeout=5.0)

        assert [r.status_code for r in responses] == [200] * 12
