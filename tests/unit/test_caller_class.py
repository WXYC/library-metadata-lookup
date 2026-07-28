"""Unit tests for the LML#928 caller-class low-priority-lane routing.

``core.bulk_concurrency.resolve_caller_class`` / ``is_low_priority_caller_class``
parse and classify the ``X-Caller-Class`` header (forwarded by Backend-Service
per BS#1843); these tests pin the parsing contract and the down-rank-only
routing invariant in isolation from the FastAPI wiring, which is pinned
separately at the router boundary in ``test_lookup_router.py`` and
``test_bulk_lookup_endpoint.py``.

Security invariant under test (LML#928 issue comment, carried from the BS#1843
review): the header may only DOWN-rank a caller into the low-priority lane
(class 5); it must never be read as a signal to grant a protected/interactive
lane. There is deliberately no "up-rank" branch anywhere in this module --
classes 1-4, and anything unparseable/out-of-range, all collapse to the same
"leave today's lane placement alone" outcome as an absent header.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import patch

import pytest

from core.bulk_concurrency import (
    LOW_PRIORITY_CALLER_CLASS,
    ClientDisconnectedWhileQueuedError,
    is_low_priority_caller_class,
    maybe_acquire_bulk_global_permit_or_reap,
    resolve_caller_class,
)


async def _never_disconnects(_request) -> None:
    """A ``watch_disconnect`` replacement that never observes a disconnect.

    Every test below that doesn't specifically exercise the disconnect race
    patches this in, so the fake ``request`` object it's paired with (never a
    real ASGI ``Request``) is never actually touched.
    """
    await asyncio.Event().wait()


class TestResolveCallerClass:
    def test_absent_header_resolves_to_none(self):
        assert resolve_caller_class(None) is None

    @pytest.mark.parametrize("value", ["1", "2", "3", "4", "5"])
    def test_valid_classes_parse_to_int(self, value):
        assert resolve_caller_class(value) == int(value)

    @pytest.mark.parametrize(
        "bad_value",
        ["0", "6", "-1", "not-a-class", "", "3.5", "  "],
    )
    def test_invalid_or_out_of_range_resolves_to_none(self, bad_value):
        assert resolve_caller_class(bad_value) is None

    def test_non_numeric_value_warns_naming_the_header(self, caplog):
        with caplog.at_level(logging.WARNING):
            resolve_caller_class("bogus")
        assert any("X-Caller-Class" in rec.message for rec in caplog.records)

    def test_out_of_range_value_warns_naming_the_header(self, caplog):
        with caplog.at_level(logging.WARNING):
            resolve_caller_class("99")
        assert any("X-Caller-Class" in rec.message for rec in caplog.records)

    def test_absent_header_does_not_warn(self, caplog):
        """A caller that simply doesn't send the header is not a misconfiguration."""
        with caplog.at_level(logging.WARNING):
            resolve_caller_class(None)
        assert not caplog.records


class TestIsLowPriorityCallerClass:
    """Pins the LML#928 routing invariant: class 5 only, down-rank only."""

    @pytest.mark.parametrize("caller_class", [1, 2, 3, 4])
    def test_classes_one_through_four_are_not_low_priority(self, caller_class):
        assert is_low_priority_caller_class(caller_class) is False

    def test_class_five_is_low_priority(self):
        assert is_low_priority_caller_class(5) is True

    def test_absent_class_is_not_low_priority(self):
        assert is_low_priority_caller_class(None) is False

    def test_low_priority_constant_is_five(self):
        # Pins the constant itself, not just the predicate -- a future edit
        # that changes the constant without updating call sites should fail
        # loudly here rather than silently reclassifying a class.
        assert LOW_PRIORITY_CALLER_CLASS == 5


class TestMaybeAcquireBulkGlobalPermitOrReap:
    """``maybe_acquire_bulk_global_permit_or_reap`` (LML#953) must gate the
    REAL LML#716/#924 global bulk semaphore -- the identical one
    ``/lookup/bulk`` items use -- not a lookalike, so the class-5
    low-priority lane genuinely shares the same budget as bulk drains. Unlike
    its LML#928 predecessor (``maybe_acquire_bulk_global_permit``, removed),
    it also races a saturated wait against client disconnect and reports the
    measured wait, so a single ``/lookup`` request can debit it from the
    caller budget the same way the interactive in-flight cap already does.
    """

    @pytest.mark.asyncio
    async def test_false_condition_does_not_touch_the_semaphore(self, monkeypatch):
        """With the global budget pinned to 0 concurrent holders (via a
        pre-acquired permit), a False condition must still complete instantly
        -- proof it never calls into the semaphore at all.
        """
        monkeypatch.setenv("LML_BULK_GLOBAL_MAX_CONCURRENT", "1")
        from core.bulk_concurrency import acquire_bulk_global_permit

        async with acquire_bulk_global_permit():
            # The single permit is held here; a real acquire would hang.
            async with asyncio.timeout(0.2):
                async with maybe_acquire_bulk_global_permit_or_reap(False, object()) as wait_ms:
                    assert wait_ms == 0.0

    @pytest.mark.asyncio
    async def test_false_condition_never_watches_for_disconnect(self, monkeypatch):
        """A False condition must not spawn a disconnect sentinel at all --
        the common (non-class-5) path stays exactly as cheap as before."""
        from core import bulk_concurrency as bc

        watched: list[object] = []

        async def _tracking_watch(request):
            watched.append(request)
            await asyncio.sleep(10)

        with patch.object(bc, "watch_disconnect", _tracking_watch):
            async with maybe_acquire_bulk_global_permit_or_reap(False, object()):
                pass

        assert watched == []

    @pytest.mark.asyncio
    async def test_true_condition_holds_the_real_global_permit(self, monkeypatch):
        """Condition True must route through the SAME semaphore bulk items use.

        Budget of 1: a concurrent direct `acquire_bulk_global_permit()` call
        must queue behind the `maybe_acquire_bulk_global_permit_or_reap(True, ...)`
        holder, proving it's the identical process-global semaphore, not a
        lookalike.
        """
        monkeypatch.setenv("LML_BULK_GLOBAL_MAX_CONCURRENT", "1")
        from core import bulk_concurrency as bc
        from core.bulk_concurrency import acquire_bulk_global_permit

        entered_second = asyncio.Event()

        async def _second_holder():
            async with acquire_bulk_global_permit():
                entered_second.set()

        with patch.object(bc, "watch_disconnect", _never_disconnects):
            async with maybe_acquire_bulk_global_permit_or_reap(True, object()) as wait_ms:
                assert wait_ms == 0.0, "uncontended acquire must report zero wait"
                second = asyncio.create_task(_second_holder())
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                assert not entered_second.is_set(), "second holder should queue behind the first"

            await asyncio.wait_for(second, timeout=1.0)
        assert entered_second.is_set()

    @pytest.mark.asyncio
    async def test_true_condition_reports_measured_wait_when_capped(self, monkeypatch):
        monkeypatch.setenv("LML_BULK_GLOBAL_MAX_CONCURRENT", "1")
        from core import bulk_concurrency as bc
        from core.bulk_concurrency import _get_bulk_global_semaphore, acquire_bulk_global_permit

        release = asyncio.Event()

        async def _holder():
            async with acquire_bulk_global_permit():
                await release.wait()

        holder = asyncio.create_task(_holder())
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        wait_ms_holder: list[float] = []

        async def _second():
            async with maybe_acquire_bulk_global_permit_or_reap(True, object()) as wait_ms:
                wait_ms_holder.append(wait_ms)

        with patch.object(bc, "watch_disconnect", _never_disconnects):
            second = asyncio.create_task(_second())
            for _ in range(100):
                if getattr(_get_bulk_global_semaphore(), "_waiters", None):
                    break
                await asyncio.sleep(0.001)
            else:
                pytest.fail("second holder never parked on the global permit")

            await asyncio.sleep(0.05)
            release.set()
            await asyncio.gather(holder, second)

        assert wait_ms_holder[0] >= 40

    @pytest.mark.asyncio
    async def test_disconnect_while_parked_raises_and_frees_the_permit(self, monkeypatch):
        """A client that disconnects while parked on a saturated budget must
        never take the permit -- the exception signals the caller to reap
        the request (LML#953), mirroring the #706/#715 in-flight-cap pattern.
        """
        monkeypatch.setenv("LML_BULK_GLOBAL_MAX_CONCURRENT", "1")
        from core import bulk_concurrency as bc
        from core.bulk_concurrency import _get_bulk_global_semaphore, acquire_bulk_global_permit

        release = asyncio.Event()

        async def _holder():
            async with acquire_bulk_global_permit():
                await release.wait()

        holder = asyncio.create_task(_holder())
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        async def _disconnect_after_parked(_request):
            for _ in range(100):
                if getattr(_get_bulk_global_semaphore(), "_waiters", None):
                    return
                await asyncio.sleep(0.001)
            pytest.fail("second holder never parked on the global permit")

        entered = False
        with patch.object(bc, "watch_disconnect", _disconnect_after_parked):
            with pytest.raises(ClientDisconnectedWhileQueuedError):
                async with maybe_acquire_bulk_global_permit_or_reap(True, object()):
                    entered = True

        assert not entered, "the guarded block must never run for a reaped request"

        release.set()
        await asyncio.wait_for(holder, timeout=1.0)

        # The permit was never taken by the reaped waiter: the semaphore
        # accepts a fresh acquire immediately once the original holder frees
        # it, with no stranded slot.
        async with asyncio.timeout(0.2):
            async with acquire_bulk_global_permit():
                pass

    @pytest.mark.asyncio
    async def test_uncontended_disconnect_after_grant_does_not_reap(self, monkeypatch):
        """A disconnect signalled AFTER an uncontended acquire must not reap --
        mirrors the interactive cap's "permit holder unaffected" contract.
        """
        monkeypatch.setenv("LML_BULK_GLOBAL_MAX_CONCURRENT", "4")
        from core import bulk_concurrency as bc

        async def _disconnect_soon(_request):
            await asyncio.sleep(0.05)

        entered = False
        with patch.object(bc, "watch_disconnect", _disconnect_soon):
            async with maybe_acquire_bulk_global_permit_or_reap(True, object()):
                entered = True
                await asyncio.sleep(0.1)

        assert entered
