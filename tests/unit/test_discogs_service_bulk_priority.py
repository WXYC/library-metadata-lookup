"""Unit tests for LML#927: class-aware priority at the Discogs semaphore itself.

``DiscogsService._request_with_retry`` is the single chokepoint that acquires
the shared 5-permit Discogs semaphore (``discogs/ratelimit.get_semaphore``).
This pins the acquire-ordering discipline the fix adds:

- Low-priority calls (``discogs.ratelimit.is_discogs_low_priority()`` true)
  acquire the NEW per-event-loop bulk sub-semaphore (``LML_DISCOGS_BULK_MAX_CONCURRENT``,
  default 2) BEFORE (outer) the shared semaphore (inner), and release
  inner -> outer in the ``finally`` -- the same ordered, deadlock-free
  discipline LML#953 established for the entry-level bulk-global permit.
- Interactive calls (the contextvar unset/false) touch ONLY the shared
  semaphore, exactly as before this change.
- Both semaphores return to their pre-call baseline after an exception --
  no permit leak on the request-error path.
- The new telemetry (the ``lml.discogs.priority`` tag on the existing
  ``lml.discogs.semaphore`` span, the ``lml.discogs.bulk_semaphore`` wait span
  + measurement, the ``lml.discogs.bulk_semaphore_queue_depth`` measurement,
  and the ``discogs_bulk_reserved_capped`` cache-stats counter) fires on the
  low-priority path per the issue's monitoring comment.
"""

from __future__ import annotations

import asyncio
from typing import Literal
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from discogs.ratelimit import set_discogs_low_priority
from discogs.service import DiscogsService


class CountingSemaphore(asyncio.Semaphore):
    """A real semaphore that records acquire/release order onto a shared log.

    Mirrors ``test_discogs_ratelimit.py``'s ``CountingSemaphore`` -- lets a
    test assert both permit accounting (net acquire == net release) and,
    with the shared ``log`` list, the relative ORDER two distinct semaphores
    were touched in.
    """

    def __init__(self, value: int, name: str, log: list[str]) -> None:
        super().__init__(value)
        self.name = name
        self.log = log
        self.acquire_count = 0
        self.release_count = 0

    async def acquire(self) -> Literal[True]:
        self.acquire_count += 1
        result = await super().acquire()
        self.log.append(f"{self.name}_acquire")
        return result

    def release(self) -> None:
        self.release_count += 1
        self.log.append(f"{self.name}_release")
        super().release()


def _make_service(client: MagicMock) -> DiscogsService:
    service = DiscogsService(token="test-token")
    service._client = client
    return service


def _response(status_code: int, headers: dict[str, str] | None = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.headers = headers or {}
    return resp


@pytest.fixture
def fake_limiter() -> MagicMock:
    limiter = MagicMock()
    limiter.acquire = AsyncMock()
    return limiter


@pytest.fixture(autouse=True)
def _reset_low_priority():
    set_discogs_low_priority(False)
    yield
    set_discogs_low_priority(False)


class TestAcquireOrdering:
    @pytest.mark.asyncio
    async def test_low_priority_acquires_bulk_outer_main_inner(self, fake_limiter):
        """Outer -> inner acquire order, inner -> outer release order."""
        log: list[str] = []
        main_sem = CountingSemaphore(5, "main", log)
        bulk_sem = CountingSemaphore(2, "bulk", log)

        client = MagicMock()
        client.request = AsyncMock(return_value=_response(200))
        service = _make_service(client)
        set_discogs_low_priority(True)

        with (
            patch("discogs.service.get_semaphore", return_value=main_sem),
            patch("discogs.service.get_bulk_discogs_semaphore", return_value=bulk_sem),
            patch("discogs.service.get_discogs_rate_gate", return_value=fake_limiter),
        ):
            result = await service._request_with_retry("GET", "/releases/1")

        assert result.status_code == 200
        assert log == ["bulk_acquire", "main_acquire", "main_release", "bulk_release"]

    @pytest.mark.asyncio
    async def test_interactive_never_touches_bulk_semaphore(self, fake_limiter):
        log: list[str] = []
        main_sem = CountingSemaphore(5, "main", log)
        bulk_sem = CountingSemaphore(2, "bulk", log)

        client = MagicMock()
        client.request = AsyncMock(return_value=_response(200))
        service = _make_service(client)
        set_discogs_low_priority(False)

        with (
            patch("discogs.service.get_semaphore", return_value=main_sem),
            patch("discogs.service.get_bulk_discogs_semaphore", return_value=bulk_sem),
            patch("discogs.service.get_discogs_rate_gate", return_value=fake_limiter),
        ):
            result = await service._request_with_retry("GET", "/releases/1")

        assert result.status_code == 200
        assert log == ["main_acquire", "main_release"]
        assert bulk_sem.acquire_count == 0


class TestPermitAccounting:
    @pytest.mark.asyncio
    async def test_both_semaphores_return_to_baseline_after_request_error(self, fake_limiter):
        """A network-layer failure must not leak either permit."""
        log: list[str] = []
        main_sem = CountingSemaphore(5, "main", log)
        bulk_sem = CountingSemaphore(2, "bulk", log)

        import httpx

        client = MagicMock()
        client.request = AsyncMock(side_effect=httpx.ConnectError("boom"))
        service = _make_service(client)
        set_discogs_low_priority(True)

        with (
            patch("discogs.service.get_semaphore", return_value=main_sem),
            patch("discogs.service.get_bulk_discogs_semaphore", return_value=bulk_sem),
            patch("discogs.service.get_discogs_rate_gate", return_value=fake_limiter),
        ):
            result = await service._request_with_retry("GET", "/releases/1")

        assert result is None
        assert main_sem._value == 5
        assert bulk_sem._value == 2
        assert main_sem.acquire_count == main_sem.release_count == 1
        assert bulk_sem.acquire_count == bulk_sem.release_count == 1

    @pytest.mark.asyncio
    async def test_both_semaphores_return_to_baseline_after_cancellation(self, fake_limiter):
        """Cancellation mid-request must not leak either permit."""
        main_sem = CountingSemaphore(5, "main", [])
        bulk_sem = CountingSemaphore(2, "bulk", [])

        hang = asyncio.Event()

        async def _hang_forever(*_args, **_kwargs):
            await hang.wait()

        client = MagicMock()
        client.request = AsyncMock(side_effect=_hang_forever)
        service = _make_service(client)
        set_discogs_low_priority(True)

        with (
            patch("discogs.service.get_semaphore", return_value=main_sem),
            patch("discogs.service.get_bulk_discogs_semaphore", return_value=bulk_sem),
            patch("discogs.service.get_discogs_rate_gate", return_value=fake_limiter),
        ):
            task = asyncio.create_task(service._request_with_retry("GET", "/releases/1"))
            await asyncio.sleep(0.01)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert main_sem._value == 5
        assert bulk_sem._value == 2


class TestTelemetry:
    @staticmethod
    def _sentry_mocks():
        transaction = Mock()
        scope = Mock()
        scope.transaction = transaction
        return transaction, scope

    @pytest.mark.asyncio
    async def test_priority_tag_set_to_bulk_when_low_priority(self, fake_limiter):
        main_sem = asyncio.Semaphore(5)
        bulk_sem = asyncio.Semaphore(2)

        client = MagicMock()
        client.request = AsyncMock(return_value=_response(200))
        service = _make_service(client)
        set_discogs_low_priority(True)

        with (
            patch("discogs.service.get_semaphore", return_value=main_sem),
            patch("discogs.service.get_bulk_discogs_semaphore", return_value=bulk_sem),
            patch("discogs.service.get_discogs_rate_gate", return_value=fake_limiter),
            patch("discogs.service.sentry_sdk.set_tag") as set_tag,
        ):
            await service._request_with_retry("GET", "/releases/1")

        priority_tags = [c for c in set_tag.call_args_list if c.args[0] == "lml.discogs.priority"]
        assert len(priority_tags) == 1
        assert priority_tags[0].args[1] == "bulk"

    @pytest.mark.asyncio
    async def test_priority_tag_set_to_interactive_when_not_low_priority(self, fake_limiter):
        main_sem = asyncio.Semaphore(5)
        bulk_sem = asyncio.Semaphore(2)

        client = MagicMock()
        client.request = AsyncMock(return_value=_response(200))
        service = _make_service(client)
        set_discogs_low_priority(False)

        with (
            patch("discogs.service.get_semaphore", return_value=main_sem),
            patch("discogs.service.get_bulk_discogs_semaphore", return_value=bulk_sem),
            patch("discogs.service.get_discogs_rate_gate", return_value=fake_limiter),
            patch("discogs.service.sentry_sdk.set_tag") as set_tag,
        ):
            await service._request_with_retry("GET", "/releases/1")

        priority_tags = [c for c in set_tag.call_args_list if c.args[0] == "lml.discogs.priority"]
        assert len(priority_tags) == 1
        assert priority_tags[0].args[1] == "interactive"

    @pytest.mark.asyncio
    async def test_capped_bulk_acquire_emits_wait_measurement_and_cache_stat(self, fake_limiter):
        """A bulk call that arrives to a saturated sub-semaphore emits the
        ``lml.discogs.bulk_semaphore_wait_ms`` measurement and increments the
        ``discogs_bulk_reserved_capped`` cache-stats counter (mirrors
        ``lml.bulk.global_capped`` at the entry gate)."""
        from wxyc_fastapi.observability import get_cache_stats_recorder, init_cache_stats

        init_cache_stats()
        main_sem = asyncio.Semaphore(5)
        bulk_sem = asyncio.Semaphore(1)

        client = MagicMock()
        client.request = AsyncMock(return_value=_response(200))
        service = _make_service(client)
        set_discogs_low_priority(True)

        transaction, scope = self._sentry_mocks()

        async def _occupy_bulk():
            async with bulk_sem:
                await asyncio.sleep(0.05)

        occupier = asyncio.create_task(_occupy_bulk())
        await asyncio.sleep(0)  # let the occupier take the only permit

        with (
            patch("discogs.service.get_semaphore", return_value=main_sem),
            patch("discogs.service.get_bulk_discogs_semaphore", return_value=bulk_sem),
            patch("discogs.service.get_discogs_rate_gate", return_value=fake_limiter),
            patch("discogs.service.sentry_sdk.get_current_scope", return_value=scope),
        ):
            await service._request_with_retry("GET", "/releases/1")

        await occupier

        wait_measurements = [
            c
            for c in transaction.set_measurement.call_args_list
            if c.args[0] == "lml.discogs.bulk_semaphore_wait_ms"
        ]
        assert len(wait_measurements) == 1
        assert wait_measurements[0].args[1] > 0

        stats = get_cache_stats_recorder()
        assert stats is not None
        from wxyc_fastapi.observability.cache_stats import _cache_stats_var

        assert _cache_stats_var.get()["discogs_bulk_reserved_capped"] == 1

    @pytest.mark.asyncio
    async def test_uncontended_bulk_acquire_emits_no_wait_measurement(self, fake_limiter):
        main_sem = asyncio.Semaphore(5)
        bulk_sem = asyncio.Semaphore(2)

        client = MagicMock()
        client.request = AsyncMock(return_value=_response(200))
        service = _make_service(client)
        set_discogs_low_priority(True)

        transaction, scope = self._sentry_mocks()

        with (
            patch("discogs.service.get_semaphore", return_value=main_sem),
            patch("discogs.service.get_bulk_discogs_semaphore", return_value=bulk_sem),
            patch("discogs.service.get_discogs_rate_gate", return_value=fake_limiter),
            patch("discogs.service.sentry_sdk.get_current_scope", return_value=scope),
        ):
            await service._request_with_retry("GET", "/releases/1")

        wait_measurements = [
            c
            for c in transaction.set_measurement.call_args_list
            if c.args[0] == "lml.discogs.bulk_semaphore_wait_ms"
        ]
        assert len(wait_measurements) == 0

    @pytest.mark.asyncio
    async def test_bulk_queue_depth_measurement_emitted_when_low_priority(self, fake_limiter):
        main_sem = asyncio.Semaphore(5)
        bulk_sem = asyncio.Semaphore(2)

        client = MagicMock()
        client.request = AsyncMock(return_value=_response(200))
        service = _make_service(client)
        set_discogs_low_priority(True)

        transaction, scope = self._sentry_mocks()

        with (
            patch("discogs.service.get_semaphore", return_value=main_sem),
            patch("discogs.service.get_bulk_discogs_semaphore", return_value=bulk_sem),
            patch("discogs.service.get_discogs_rate_gate", return_value=fake_limiter),
            patch("discogs.service.sentry_sdk.get_current_scope", return_value=scope),
        ):
            await service._request_with_retry("GET", "/releases/1")

        queue_depth_measurements = [
            c
            for c in transaction.set_measurement.call_args_list
            if c.args[0] == "lml.discogs.bulk_semaphore_queue_depth"
        ]
        assert len(queue_depth_measurements) == 1
        assert queue_depth_measurements[0].args[1] == 0

    @pytest.mark.asyncio
    async def test_no_bulk_telemetry_emitted_when_interactive(self, fake_limiter):
        main_sem = asyncio.Semaphore(5)
        bulk_sem = asyncio.Semaphore(2)

        client = MagicMock()
        client.request = AsyncMock(return_value=_response(200))
        service = _make_service(client)
        set_discogs_low_priority(False)

        transaction, scope = self._sentry_mocks()

        with (
            patch("discogs.service.get_semaphore", return_value=main_sem),
            patch("discogs.service.get_bulk_discogs_semaphore", return_value=bulk_sem),
            patch("discogs.service.get_discogs_rate_gate", return_value=fake_limiter),
            patch("discogs.service.sentry_sdk.get_current_scope", return_value=scope),
        ):
            await service._request_with_retry("GET", "/releases/1")

        assert not any(
            c.args[0].startswith("lml.discogs.bulk_semaphore")
            for c in transaction.set_measurement.call_args_list
        )
