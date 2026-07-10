"""Behavior pins for the LML#716 process-global bulk-item permit.

``core.bulk_concurrency.acquire_bulk_global_permit`` is the cross-request
budget shared by every bulk-family dispatcher (``/lookup/bulk``, identity
bulk-resolve, cache refresh). These tests exercise the public context
manager directly — the per-endpoint wiring is pinned in each endpoint's own
test module.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from tests.unit.conftest import override_deps


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


class TestGlobalPermitTelemetry:
    """Capped acquires must be queryable in Sentry (the LML#683 lesson).

    Mirrors the #714 `lml.lookup.inflight_capped` / `inflight_wait_ms` pair:
    a filterable tag for "this request queued on the global budget" plus a
    quantitative wait measurement for tuning the knob. ``set_data`` alone is
    unqueryable in the spans dataset.
    """

    @staticmethod
    def _sentry_mocks():
        from unittest.mock import Mock

        transaction = Mock()
        scope = Mock()
        scope.transaction = transaction
        return transaction, scope, Mock()

    @pytest.mark.asyncio
    async def test_capped_acquire_sets_tag_and_wait_measurement(self, monkeypatch):
        monkeypatch.setenv("LML_BULK_GLOBAL_MAX_CONCURRENT", "1")

        from core import bulk_concurrency as bc

        transaction, scope, set_tag = self._sentry_mocks()

        async def _second_holder(release: asyncio.Event):
            async with bc.acquire_bulk_global_permit():
                release.set()

        with (
            patch.object(bc.sentry_sdk, "get_current_scope", return_value=scope),
            patch.object(bc.sentry_sdk, "set_tag", set_tag),
        ):
            release = asyncio.Event()
            first_release = asyncio.Event()

            async def _first_holder():
                async with bc.acquire_bulk_global_permit():
                    await first_release.wait()

            first = asyncio.create_task(_first_holder())
            await asyncio.sleep(0)
            # First holder took the permit uncontended: no tag.
            assert not any(
                c.args[0] == "lml.bulk.global_capped" for c in set_tag.call_args_list
            )

            second = asyncio.create_task(_second_holder(release))
            # Park the second holder deterministically before releasing.
            semaphore = bc._get_bulk_global_semaphore()
            for _ in range(100):
                if semaphore._waiters:
                    break
                await asyncio.sleep(0.001)
            else:
                pytest.fail("second holder never parked on the global permit")

            first_release.set()
            await asyncio.gather(first, second)

        capped_tags = [
            c for c in set_tag.call_args_list if c.args == ("lml.bulk.global_capped", "true")
        ]
        assert len(capped_tags) == 1
        wait_measurements = [
            c
            for c in transaction.set_measurement.call_args_list
            if c.args[0] == "lml.bulk.global_wait_ms"
        ]
        assert len(wait_measurements) == 1
        assert wait_measurements[0].args[1] > 0

    @pytest.mark.asyncio
    async def test_uncontended_acquire_sets_no_tag_or_measurement(self, monkeypatch):
        monkeypatch.setenv("LML_BULK_GLOBAL_MAX_CONCURRENT", "4")

        from core import bulk_concurrency as bc

        transaction, scope, set_tag = self._sentry_mocks()

        with (
            patch.object(bc.sentry_sdk, "get_current_scope", return_value=scope),
            patch.object(bc.sentry_sdk, "set_tag", set_tag),
        ):
            async with bc.acquire_bulk_global_permit():
                pass

        assert not any(c.args[0] == "lml.bulk.global_capped" for c in set_tag.call_args_list)
        assert not any(
            c.args[0] == "lml.bulk.global_wait_ms"
            for c in transaction.set_measurement.call_args_list
        )


class TestCrossEndpointBudgetSharing:
    @pytest.mark.asyncio
    async def test_bulk_lookup_and_identity_resolve_share_one_budget(
        self, mock_settings, monkeypatch
    ):
        """A /lookup/bulk batch and a bulk-resolve request draw from ONE budget.

        Guards the LML#716 core property against a future per-endpoint split:
        with the global knob at 2 and per-request knobs wide, combined
        in-flight items across BOTH endpoints never exceed 2.
        """
        monkeypatch.setenv("LML_BULK_MAX_CONCURRENT", "10")
        monkeypatch.setenv("LML_BULK_GLOBAL_MAX_CONCURRENT", "2")

        from config.settings import get_settings
        from core.dependencies import get_discogs_service, get_library_db, get_posthog_client
        from identity.dependencies import get_entity_store
        from main import app
        from tests.unit.test_bulk_lookup_endpoint import _no_match_response

        in_flight = 0
        peak = 0
        lock = asyncio.Lock()

        async def _track():
            nonlocal in_flight, peak
            async with lock:
                in_flight += 1
                peak = max(peak, in_flight)
            await asyncio.sleep(0.01)
            async with lock:
                in_flight -= 1

        async def fake_lookup(request, **kwargs):
            await _track()
            return _no_match_response()

        async def fake_resolve(artist_name: str):
            await _track()
            return None

        mock_entity_store = AsyncMock()
        mock_entity_store.resolve_library_name.side_effect = fake_resolve

        with override_deps(
            app,
            {
                get_library_db: AsyncMock(),
                get_discogs_service: None,
                get_posthog_client: None,
                get_settings: mock_settings,
                get_entity_store: mock_entity_store,
            },
        ):
            with patch(
                "lookup.router.perform_lookup", new_callable=AsyncMock, side_effect=fake_lookup
            ):
                async with AsyncClient(
                    transport=ASGITransport(app=app), base_url="http://test"
                ) as ac:
                    lookup_body = {"items": [{"artist": f"a{i}", "album": "x"} for i in range(4)]}
                    resolve_body = {
                        "inputs": [
                            {"library_id": i, "artist_name": f"b{i}", "album_title": "y"}
                            for i in range(4)
                        ]
                    }
                    resp_lookup, resp_resolve = await asyncio.gather(
                        ac.post("/api/v1/lookup/bulk", json=lookup_body),
                        ac.post("/api/v1/identity/bulk-resolve-libraries", json=resolve_body),
                    )

        assert resp_lookup.status_code == 200
        assert resp_resolve.status_code == 200
        assert peak <= 2, f"Combined cross-endpoint peak was {peak}; budgets are not shared"


class TestBudgetFairness:
    @pytest.mark.asyncio
    async def test_small_batch_completes_while_drain_holds_the_budget(
        self, mock_settings, monkeypatch
    ):
        """A 3-item batch finishes while a 30-item drain is still in flight.

        The motivating LML#716 scenario is a 35k-album operator drain running
        alongside interactive callers. ``asyncio.Semaphore`` waiters are FIFO,
        so the small batch's items interleave with the drain's instead of
        parking behind all of them — this pins that operator-relevant
        property rather than assuming it.
        """
        monkeypatch.setenv("LML_BULK_MAX_CONCURRENT", "10")
        monkeypatch.setenv("LML_BULK_GLOBAL_MAX_CONCURRENT", "2")

        import time

        from config.settings import get_settings
        from core.dependencies import get_discogs_service, get_library_db, get_posthog_client
        from main import app
        from tests.unit.test_bulk_lookup_endpoint import _no_match_response

        async def fake_lookup(request, **kwargs):
            await asyncio.sleep(0.01)
            return _no_match_response()

        with override_deps(
            app,
            {
                get_library_db: AsyncMock(),
                get_discogs_service: None,
                get_posthog_client: None,
                get_settings: mock_settings,
            },
        ):
            with patch(
                "lookup.router.perform_lookup", new_callable=AsyncMock, side_effect=fake_lookup
            ):
                async with AsyncClient(
                    transport=ASGITransport(app=app), base_url="http://test"
                ) as ac:

                    async def _timed_post(items: int) -> float:
                        body = {"items": [{"artist": f"a{i}", "album": "x"} for i in range(items)]}
                        resp = await ac.post("/api/v1/lookup/bulk", json=body)
                        assert resp.status_code == 200
                        return time.monotonic()

                    async def _small_after_drain_starts() -> float:
                        # Let the drain occupy the budget before the small
                        # batch arrives — the adversarial ordering.
                        await asyncio.sleep(0.02)
                        return await _timed_post(3)

                    drain_done, small_done = await asyncio.gather(
                        _timed_post(30),
                        _small_after_drain_starts(),
                    )

        assert small_done < drain_done, (
            "small batch finished after the drain — FIFO interleaving is broken and "
            "interactive callers would park behind entire operator drains"
        )
