"""Unit tests for LML#927: the Discogs bulk sub-semaphore reservation.

Durable, code-level replacement for the LML#924 env-only interim
(``LML_BULK_GLOBAL_MAX_CONCURRENT``, an entry-level admission cap). This adds a
SECOND, per-event-loop ``asyncio.Semaphore(LML_DISCOGS_BULK_MAX_CONCURRENT)`` in
``discogs/ratelimit.py`` (default 2) that low-priority Discogs calls must hold
BEFORE (outer) the existing shared 5-permit semaphore (inner) -- so bulk/
enrichment traffic can occupy at most ``LML_DISCOGS_BULK_MAX_CONCURRENT`` of
the 5 permits, always reserving the rest for interactive ``/lookup``.

These tests pin the primitive itself (sizing, env-fallback, and the raw
concurrency cap) -- see ``test_discogs_semaphore_class_priority.py`` for the
router-level priority-inversion regression and the ``_request_with_retry``
acquire-ordering / telemetry pins.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from discogs.ratelimit import (
    get_bulk_discogs_semaphore,
    get_semaphore,
    reset_rate_limiting,
)

_BULK_MAX_CONCURRENT_ENV = "LML_DISCOGS_BULK_MAX_CONCURRENT"


class TestBulkSemaphoreSizing:
    @pytest.mark.asyncio
    async def test_default_is_two(self, monkeypatch):
        """Unset knob -> default of 2, reserving >= 3 of the 5 shared permits."""
        monkeypatch.delenv(_BULK_MAX_CONCURRENT_ENV, raising=False)
        reset_rate_limiting()

        sem = get_bulk_discogs_semaphore()

        assert sem._value == 2

    @pytest.mark.asyncio
    async def test_env_override_resizes_the_semaphore(self, monkeypatch):
        monkeypatch.setenv(_BULK_MAX_CONCURRENT_ENV, "3")
        reset_rate_limiting()

        sem = get_bulk_discogs_semaphore()

        assert sem._value == 3

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad_value", ["not-an-int", "0", "-1"])
    async def test_invalid_env_warns_and_falls_back_to_default(
        self, monkeypatch, caplog, bad_value
    ):
        """Garbage/zero/negative -> WARN + fall back to 2, never a 0 permit.

        A ``Semaphore(0)`` would deadlock every low-priority Discogs call
        forever -- same contract ``resolve_positive_int_env`` enforces
        everywhere else in the repo.
        """
        monkeypatch.setenv(_BULK_MAX_CONCURRENT_ENV, bad_value)
        reset_rate_limiting()

        with caplog.at_level(logging.WARNING):
            sem = get_bulk_discogs_semaphore()

        assert sem._value == 2
        assert any(_BULK_MAX_CONCURRENT_ENV in rec.message for rec in caplog.records)

    @pytest.mark.asyncio
    async def test_reset_rate_limiting_clears_the_per_loop_semaphore(self, monkeypatch):
        monkeypatch.setenv(_BULK_MAX_CONCURRENT_ENV, "1")
        reset_rate_limiting()
        first = get_bulk_discogs_semaphore()

        monkeypatch.setenv(_BULK_MAX_CONCURRENT_ENV, "4")
        reset_rate_limiting()
        second = get_bulk_discogs_semaphore()

        assert first is not second
        assert second._value == 4


class TestBulkSemaphoreConcurrencySaturation:
    @pytest.mark.asyncio
    async def test_bulk_concurrency_capped_and_interactive_not_blocked(self, monkeypatch):
        """Saturate the bulk sub-semaphore; a concurrent interactive acquire on
        the shared 5-permit semaphore must still complete promptly.

        This is the LML#927 acceptance criteria at the primitive level: bulk
        holding the reservation gate never touches the shared semaphore, so
        interactive concurrency is untouched even while bulk is fully parked.
        """
        monkeypatch.setenv(_BULK_MAX_CONCURRENT_ENV, "2")
        monkeypatch.setenv("DISCOGS_MAX_CONCURRENT", "5")
        reset_rate_limiting()

        bulk_sem = get_bulk_discogs_semaphore()
        main_sem = get_semaphore()

        max_concurrent = 0
        current = 0
        lock = asyncio.Lock()
        release = asyncio.Event()

        async def bulk_worker():
            nonlocal max_concurrent, current
            async with bulk_sem:
                async with lock:
                    current += 1
                    max_concurrent = max(max_concurrent, current)
                await release.wait()
                async with lock:
                    current -= 1

        holders = [asyncio.create_task(bulk_worker()) for _ in range(5)]
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert max_concurrent <= 2, f"expected at most 2 concurrent, observed {max_concurrent}"

        async def interactive_acquire():
            async with main_sem:
                pass

        await asyncio.wait_for(interactive_acquire(), timeout=1.0)

        release.set()
        await asyncio.gather(*holders)
        assert max_concurrent >= 2
