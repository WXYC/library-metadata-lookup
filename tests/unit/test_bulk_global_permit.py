"""Behavior pins for the LML#716 process-global bulk-item permit.

``core.bulk_concurrency.acquire_bulk_global_permit`` is the cross-request
budget shared by every bulk-family dispatcher (``/lookup/bulk``, identity
bulk-resolve, cache refresh). These tests exercise the public context
manager directly — the per-endpoint wiring is pinned in each endpoint's own
test module.
"""

from __future__ import annotations

import asyncio

import pytest


async def _hold_permit(entered: list[int], release: asyncio.Event) -> None:
    """Enter the global permit, record occupancy, and hold until released."""
    from core.bulk_concurrency import acquire_bulk_global_permit

    async with acquire_bulk_global_permit():
        entered.append(1)
        await release.wait()


class TestBulkGlobalPermitSizing:
    @pytest.mark.asyncio
    async def test_admits_exactly_the_configured_bound(self, monkeypatch):
        """With LML_BULK_GLOBAL_MAX_CONCURRENT=2, a third holder queues.

        The queued holder must enter as soon as one permit releases — the
        budget queues, it never sheds.
        """
        monkeypatch.setenv("LML_BULK_GLOBAL_MAX_CONCURRENT", "2")

        entered: list[int] = []
        release = asyncio.Event()
        holders = [asyncio.create_task(_hold_permit(entered, release)) for _ in range(3)]
        await asyncio.sleep(0)  # let the first wave acquire
        await asyncio.sleep(0)

        assert len(entered) == 2, "third holder should be queued behind the bound"

        release.set()
        await asyncio.gather(*holders)
        assert len(entered) == 3, "queued holder must eventually enter"

    @pytest.mark.asyncio
    async def test_default_bound_tracks_discogs_pool_max_size(self, monkeypatch):
        """Unset knob → the budget defaults to the discogs pool's max_size.

        The budget exists to bound contention on the discogs-cache asyncpg
        pool, so its default must follow ``LML_DISCOGS_POOL_MAX_SIZE`` (the
        LML#745 lever) rather than a third hand-synced literal — the sizing
        decision recorded on LML#716 (2026-07-10).
        """
        monkeypatch.delenv("LML_BULK_GLOBAL_MAX_CONCURRENT", raising=False)
        monkeypatch.setenv("LML_DISCOGS_POOL_MAX_SIZE", "2")

        entered: list[int] = []
        release = asyncio.Event()
        holders = [asyncio.create_task(_hold_permit(entered, release)) for _ in range(3)]
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert len(entered) == 2, "default bound should equal discogs_pool_max_size()"

        release.set()
        await asyncio.gather(*holders)
        assert len(entered) == 3

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad_value", ["not-an-int", "0", "-3"])
    async def test_invalid_env_warns_and_falls_back(self, monkeypatch, caplog, bad_value):
        """Garbage/zero/negative knob → WARN + pool-sized default, never a 0 cap.

        A ``Semaphore(0)`` would deadlock every bulk item in the process
        forever — same contract as ``resolve_positive_int_env`` everywhere
        else in the repo.
        """
        import logging

        monkeypatch.setenv("LML_BULK_GLOBAL_MAX_CONCURRENT", bad_value)
        monkeypatch.setenv("LML_DISCOGS_POOL_MAX_SIZE", "2")

        entered: list[int] = []
        release = asyncio.Event()
        with caplog.at_level(logging.WARNING):
            holders = [asyncio.create_task(_hold_permit(entered, release)) for _ in range(3)]
            await asyncio.sleep(0)
            await asyncio.sleep(0)

            assert len(entered) == 2, "invalid knob must fall back to the pool-sized default"

            release.set()
            await asyncio.gather(*holders)

        assert any(
            "LML_BULK_GLOBAL_MAX_CONCURRENT" in rec.message for rec in caplog.records
        ), "expected a fallback warning naming the misconfigured env var"
