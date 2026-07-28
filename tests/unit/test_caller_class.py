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

import pytest

from core.bulk_concurrency import (
    LOW_PRIORITY_CALLER_CLASS,
    is_low_priority_caller_class,
    maybe_acquire_bulk_global_permit,
    resolve_caller_class,
)


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


class TestMaybeAcquireBulkGlobalPermit:
    """``maybe_acquire_bulk_global_permit`` must gate the REAL LML#716/#924
    global bulk semaphore -- the identical one ``/lookup/bulk`` items use --
    not a lookalike, so the class-5 low-priority lane genuinely shares the
    same budget as bulk drains."""

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
                async with maybe_acquire_bulk_global_permit(False):
                    pass

    @pytest.mark.asyncio
    async def test_true_condition_holds_the_real_global_permit(self, monkeypatch):
        """Condition True must route through the SAME semaphore bulk items use.

        Budget of 1: a concurrent direct `acquire_bulk_global_permit()` call
        must queue behind the `maybe_acquire_bulk_global_permit(True)` holder,
        proving it's the identical process-global semaphore, not a lookalike.
        """
        monkeypatch.setenv("LML_BULK_GLOBAL_MAX_CONCURRENT", "1")
        from core.bulk_concurrency import acquire_bulk_global_permit

        entered_second = asyncio.Event()

        async def _second_holder():
            async with acquire_bulk_global_permit():
                entered_second.set()

        async with maybe_acquire_bulk_global_permit(True):
            second = asyncio.create_task(_second_holder())
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert not entered_second.is_set(), "second holder should queue behind the first"

        await asyncio.wait_for(second, timeout=1.0)
        assert entered_second.is_set()
