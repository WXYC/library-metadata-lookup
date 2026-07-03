"""Unit tests for the ``/api/v1/lookup`` in-flight cap (LML#706 PR3).

The #706 congestion collapse compounded because single ``/lookup`` had NO
in-flight bound: under cold-tail load, requests piled up (Little's law —
service time × arrival rate), each holding the single event loop through
seconds of external I/O and contending for the 5-connection asyncpg pool,
which inflated even trivial PG spans to tens of seconds. PR1 took the
streaming probe off the response path (the cure); this cap structurally
breaks the pileup feedback loop and shields the background warmers +
upstream APIs from burst.

Shape: a process-global semaphore around ``perform_lookup`` in
``handle_lookup``, sized by ``LML_LOOKUP_MAX_CONCURRENT`` (default 8), built
lazily on the first request (mirrors the LML#706 warm semaphore — a
semaphore needs a running loop, and reading the env at construction keeps
the bound a no-redeploy Railway lever). Excess requests QUEUE on the
semaphore — no 429/503 load-shedding; callers see latency, not errors.

The bulk path keeps its own per-batch semaphore (``LML_BULK_MAX_CONCURRENT``)
— different gate shape (outer per-request vs. inner per-item), so the knobs
stay separate.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, Mock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from config.settings import get_settings
from core.dependencies import get_discogs_service, get_library_db, get_posthog_client
from discogs.service import DiscogsService
from library.db import LibraryDB
from lookup import router as router_mod
from lookup.models import LookupResponse
from main import app
from tests.unit.conftest import override_deps

LOOKUP_BODY = {
    "artist": "Jessica Pratt",
    "album": "On Your Own Love Again",
    "raw_message": "Jessica Pratt - On Your Own Love Again",
}


@pytest.fixture(autouse=True)
def _reset_lookup_semaphore():
    """Isolate the process-global cap between tests.

    The semaphore is process-global by design (one cap per worker, spanning
    requests) and snapshots its env at first construction — reset it so each
    test's monkeypatched ``LML_LOOKUP_MAX_CONCURRENT`` takes effect and no
    semaphore bound to a prior event loop leaks forward.
    """
    router_mod._lookup_semaphore = None
    yield
    router_mod._lookup_semaphore = None


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


def _empty_response() -> LookupResponse:
    return LookupResponse(results=[], search_type="none")


class TestInFlightCapBehavior:
    @pytest.mark.asyncio
    async def test_concurrent_lookups_bounded_by_semaphore(self, app_client, monkeypatch):
        """Peak concurrent ``perform_lookup`` executions never exceed the cap.

        Cap 2, 7 concurrent requests: without the semaphore all 7 admit
        immediately and the peak is 7. All 7 must still complete 200 — the
        cap queues, it never sheds.
        """
        monkeypatch.setenv("LML_LOOKUP_MAX_CONCURRENT", "2")

        in_flight = 0
        peak = 0
        lock = asyncio.Lock()

        async def fake_lookup(request, **kwargs):
            nonlocal in_flight, peak
            async with lock:
                in_flight += 1
                peak = max(peak, in_flight)
            await asyncio.sleep(0.01)
            async with lock:
                in_flight -= 1
            return _empty_response()

        with patch("lookup.router.perform_lookup", new_callable=AsyncMock, side_effect=fake_lookup):
            async with AsyncClient(
                transport=ASGITransport(app=app_client), base_url="http://test"
            ) as ac:
                responses = await asyncio.gather(
                    *(ac.post("/api/v1/lookup", json=LOOKUP_BODY) for _ in range(7))
                )

        assert [r.status_code for r in responses] == [200] * 7
        assert peak <= 2, f"Peak concurrency was {peak}; semaphore did not bound it"

    @pytest.mark.asyncio
    async def test_queued_request_completes_after_permit_frees(self, app_client, monkeypatch):
        """Cap 1: a second request queues behind a gated first, then completes.

        Deterministic causality (no sleeps): the first lookup blocks on an
        Event while the second request arrives; the second's ``perform_lookup``
        must NOT start while the first holds the only permit, and both return
        200 once the gate opens. Queue-don't-shed is the load-bearing contract
        — Backend-Service sees latency, never a new error mode.
        """
        monkeypatch.setenv("LML_LOOKUP_MAX_CONCURRENT", "1")

        gate = asyncio.Event()
        started: list[int] = []

        async def gated_lookup(request, **kwargs):
            started.append(len(started))
            await gate.wait()
            return _empty_response()

        with patch(
            "lookup.router.perform_lookup", new_callable=AsyncMock, side_effect=gated_lookup
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app_client), base_url="http://test"
            ) as ac:
                first = asyncio.create_task(ac.post("/api/v1/lookup", json=LOOKUP_BODY))
                second = asyncio.create_task(ac.post("/api/v1/lookup", json=LOOKUP_BODY))
                # Let both requests reach the handler; only one may enter
                # perform_lookup while the permit is held.
                while not started:
                    await asyncio.sleep(0)
                await asyncio.sleep(0.01)
                assert started == [0], f"cap=1 admitted {len(started)} lookups concurrently"

                gate.set()
                responses = await asyncio.gather(first, second)

        assert [r.status_code for r in responses] == [200, 200]
        assert started == [0, 1]


class TestInFlightCapConfiguration:
    def test_env_var_name_and_default(self):
        assert router_mod._LOOKUP_MAX_CONCURRENT_ENV_VAR == "LML_LOOKUP_MAX_CONCURRENT"
        assert router_mod._LOOKUP_DEFAULT_MAX_CONCURRENT == 8

    @pytest.mark.asyncio
    async def test_semaphore_uses_env_override(self, monkeypatch):
        monkeypatch.setenv("LML_LOOKUP_MAX_CONCURRENT", "3")
        sem = router_mod._get_lookup_semaphore()
        assert sem._value == 3

    @pytest.mark.asyncio
    async def test_semaphore_falls_back_to_default_when_unset(self, monkeypatch):
        monkeypatch.delenv("LML_LOOKUP_MAX_CONCURRENT", raising=False)
        sem = router_mod._get_lookup_semaphore()
        assert sem._value == router_mod._LOOKUP_DEFAULT_MAX_CONCURRENT

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", ["0", "-4", "banana"])
    async def test_invalid_env_falls_back_to_default(self, monkeypatch, bad):
        # resolve_positive_int_env semantics: operator typos must not change
        # the cap to something surprising — unparseable/zero/negative all WARN
        # and fall back (a 0 cap would deadlock every request forever).
        monkeypatch.setenv("LML_LOOKUP_MAX_CONCURRENT", bad)
        sem = router_mod._get_lookup_semaphore()
        assert sem._value == router_mod._LOOKUP_DEFAULT_MAX_CONCURRENT

    @pytest.mark.asyncio
    async def test_semaphore_is_process_global_across_calls(self, monkeypatch):
        # One cap per worker process, spanning requests — NOT per-request like
        # the bulk handler's batch-internal semaphore.
        monkeypatch.setenv("LML_LOOKUP_MAX_CONCURRENT", "2")
        assert router_mod._get_lookup_semaphore() is router_mod._get_lookup_semaphore()


class TestInFlightCapObservability:
    @pytest.mark.asyncio
    async def test_capped_wait_projects_sentry_flag(self, app_client, monkeypatch):
        """A request that finds the cap saturated tags its transaction.

        ``lml.lookup.inflight_capped`` is the post-deploy signal that the cap
        actually engaged (the #706 acceptance is measured in Sentry). Only the
        queued request carries the tag — an uncontended request must not.
        """
        monkeypatch.setenv("LML_LOOKUP_MAX_CONCURRENT", "1")

        gate = asyncio.Event()
        started: list[int] = []

        async def gated_lookup(request, **kwargs):
            started.append(len(started))
            await gate.wait()
            return _empty_response()

        transaction = Mock()
        scope = Mock()
        scope.transaction = transaction

        with (
            patch("lookup.router.perform_lookup", new_callable=AsyncMock, side_effect=gated_lookup),
            patch.object(router_mod.sentry_sdk, "get_current_scope", return_value=scope),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app_client), base_url="http://test"
            ) as ac:
                first = asyncio.create_task(ac.post("/api/v1/lookup", json=LOOKUP_BODY))
                while not started:
                    await asyncio.sleep(0)
                # First request holds the permit and is NOT capped.
                capped_keys = [
                    c.args[0]
                    for c in transaction.set_data.call_args_list
                    if c.args[0] == "lml.lookup.inflight_capped"
                ]
                assert capped_keys == []

                second = asyncio.create_task(ac.post("/api/v1/lookup", json=LOOKUP_BODY))
                # Let the second request travel the ASGI transport and reach
                # the saturated semaphore — bounded poll, no fixed sleep.
                for _ in range(200):
                    await asyncio.sleep(0.005)
                    if any(
                        c.args == ("lml.lookup.inflight_capped", True)
                        for c in transaction.set_data.call_args_list
                    ):
                        break
                capped_calls = [
                    c
                    for c in transaction.set_data.call_args_list
                    if c.args == ("lml.lookup.inflight_capped", True)
                ]
                assert len(capped_calls) == 1

                gate.set()
                responses = await asyncio.gather(first, second)

        assert [r.status_code for r in responses] == [200, 200]

    @pytest.mark.asyncio
    async def test_uncontended_request_does_not_project_flag(self, app_client, monkeypatch):
        monkeypatch.setenv("LML_LOOKUP_MAX_CONCURRENT", "8")

        transaction = Mock()
        scope = Mock()
        scope.transaction = transaction

        with (
            patch(
                "lookup.router.perform_lookup",
                new_callable=AsyncMock,
                return_value=_empty_response(),
            ),
            patch.object(router_mod.sentry_sdk, "get_current_scope", return_value=scope),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app_client), base_url="http://test"
            ) as ac:
                resp = await ac.post("/api/v1/lookup", json=LOOKUP_BODY)

        assert resp.status_code == 200
        assert not any(
            c.args[0] == "lml.lookup.inflight_capped" for c in transaction.set_data.call_args_list
        )
