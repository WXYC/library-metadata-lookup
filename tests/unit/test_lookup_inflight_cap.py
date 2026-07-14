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
lazily on the first request. Laziness is NOT a loop constraint (Python 3.10+
semaphores bind a loop only at the first contended acquire) — it exists so
the env is read at request time (the no-redeploy Railway lever) and so tests
can reset the global between event loops (the suite-wide autouse fixture in
``tests/conftest.py``). Excess requests QUEUE on the semaphore — no 429/503
load-shedding; callers see latency, not errors. A request that arrives to a
saturated cap deducts its measured queue wait from ``X-Caller-Budget-Ms``
(floored at 1 — a non-positive header value falls back to the env default,
which would ironically UNBOUND the budget) and projects a filterable Sentry
tag plus a wait-duration measurement.

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


async def _wait_until(predicate, *, timeout_s: float = 5.0, message: str = "condition"):
    """Poll ``predicate`` on the loop until true, failing loudly at the deadline.

    The repo has no pytest-timeout and CI no ``timeout-minutes``, so an
    unbounded ``while not started`` spin would convert a regression (e.g. the
    handler erroring before ``perform_lookup``) into an indefinite CI hang
    instead of a red assertion.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while not predicate():
        if loop.time() > deadline:
            pytest.fail(f"timed out after {timeout_s}s waiting for: {message}")
        await asyncio.sleep(0.005)


def _lookup_semaphore_has_waiters() -> bool:
    """True once a request is parked on the in-flight cap.

    Peeks the private ``_waiters`` deque — same territory as this file's
    ``sem._value`` peeks (repo precedent in the warm-semaphore tests) and the
    only loop-external observable for "a request is queued": the Sentry tag
    and the budget deduction both happen only AFTER the acquire completes, so
    they can't serve as the readiness signal.
    """
    sem = router_mod._lookup_semaphore
    return bool(sem is not None and getattr(sem, "_waiters", None))


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
        cap queues, it never sheds. (No lock around the counters: the
        increment/decrement sections contain no await, so they are atomic on
        the single event loop.)
        """
        monkeypatch.setenv("LML_LOOKUP_MAX_CONCURRENT", "2")

        in_flight = 0
        peak = 0

        async def fake_lookup(request, **kwargs):
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.01)
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

        The first lookup blocks on an Event while the second request arrives;
        both return 200 once the gate opens and the lookup count is exactly 2.
        The mid-flight negative check (second not started while the permit is
        held) is a bounded-window best-effort observation, not the load-bearing
        assertion — the deterministic contract is queue-don't-shed: both
        complete, neither errors.
        """
        monkeypatch.setenv("LML_LOOKUP_MAX_CONCURRENT", "1")

        gate = asyncio.Event()
        started = 0

        async def gated_lookup(request, **kwargs):
            nonlocal started
            started += 1
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
                await _wait_until(lambda: started >= 1, message="first lookup to start")
                # Best-effort window: with the only permit held, the second
                # lookup must not have started.
                await asyncio.sleep(0.02)
                assert started == 1, f"cap=1 admitted {started} lookups concurrently"

                gate.set()
                responses = await asyncio.gather(first, second)

        assert [r.status_code for r in responses] == [200, 200]
        assert started == 2


class TestInFlightCapConfiguration:
    def test_env_var_name_and_ceiling(self):
        assert router_mod._LOOKUP_MAX_CONCURRENT_ENV_VAR == "LML_LOOKUP_MAX_CONCURRENT"
        # The ceiling is the historical default; the *effective* default is now
        # this clamped to the PG pools (see TestInFlightCapPoolClamp).
        assert router_mod._LOOKUP_MAX_CONCURRENT_CEILING == 8

    def test_semaphore_uses_env_override(self, monkeypatch):
        monkeypatch.setenv("LML_LOOKUP_MAX_CONCURRENT", "3")
        sem = router_mod._get_lookup_semaphore()
        assert sem._value == 3

    def test_semaphore_falls_back_to_pool_clamped_default_when_unset(self, monkeypatch):
        # With the cap unset and the discogs-cache pool at its default of 5, the
        # effective default is min(ceiling=8, discogs=5) = 5 — the cap can never
        # silently exceed the pool it contends for (LML#706).
        monkeypatch.delenv("LML_LOOKUP_MAX_CONCURRENT", raising=False)
        monkeypatch.delenv("LML_DISCOGS_POOL_MAX_SIZE", raising=False)
        sem = router_mod._get_lookup_semaphore()
        assert sem._value == router_mod._lookup_default_max_concurrent()
        assert sem._value == 5

    @pytest.mark.parametrize("bad", ["0", "-4", "banana"])
    def test_invalid_env_falls_back_to_pool_clamped_default(self, monkeypatch, bad):
        # resolve_positive_int_env semantics: operator typos must not change
        # the cap to something surprising — unparseable/zero/negative all WARN
        # and fall back (a 0 cap would deadlock every request forever). The
        # fallback is now the pool-clamped default, not a bare 8.
        monkeypatch.setenv("LML_LOOKUP_MAX_CONCURRENT", bad)
        monkeypatch.delenv("LML_DISCOGS_POOL_MAX_SIZE", raising=False)
        sem = router_mod._get_lookup_semaphore()
        assert sem._value == router_mod._lookup_default_max_concurrent()
        assert sem._value == 5

    def test_semaphore_is_process_global_across_calls(self, monkeypatch):
        # One cap per worker process, spanning requests — NOT per-request like
        # the bulk handler's batch-internal semaphore.
        monkeypatch.setenv("LML_LOOKUP_MAX_CONCURRENT", "2")
        assert router_mod._get_lookup_semaphore() is router_mod._get_lookup_semaphore()


class TestInFlightCapPoolClamp:
    """The default cap must never exceed the discogs-cache pool (LML#706).

    Every `/lookup` borrows the discogs-cache asyncpg pool for its cache reads,
    trigram match, and identity mint (WXYC#395 — the entity store reuses that
    one pool; `LML_PG_POOL_MAX_SIZE` sizes only the off-hot-path musicbrainz
    pool). A lookup semaphore wider than that pool doesn't add throughput — it
    just relocates the queue from the semaphore (where waits are measured and
    bounded) onto ``pool.acquire()``, where the wait attaches to whatever PG
    span is open. That is the mechanism behind the reopen's "1.4 ms
    entity.identity SELECT that took 5,070 ms". So the *default* cap is
    ``min(ceiling, discogs_pool)``; an explicit env override may still exceed
    it, but emits a guard warning.
    """

    def test_default_is_ceiling_when_pool_is_large(self, monkeypatch):
        monkeypatch.delenv("LML_LOOKUP_MAX_CONCURRENT", raising=False)
        monkeypatch.setenv("LML_DISCOGS_POOL_MAX_SIZE", "20")
        sem = router_mod._get_lookup_semaphore()
        assert sem._value == router_mod._LOOKUP_MAX_CONCURRENT_CEILING == 8

    def test_default_clamps_to_pool_below_ceiling(self, monkeypatch):
        monkeypatch.delenv("LML_LOOKUP_MAX_CONCURRENT", raising=False)
        monkeypatch.setenv("LML_DISCOGS_POOL_MAX_SIZE", "3")
        sem = router_mod._get_lookup_semaphore()
        assert sem._value == 3

    def test_raising_pool_lifts_the_default_up_to_the_ceiling(self, monkeypatch):
        monkeypatch.delenv("LML_LOOKUP_MAX_CONCURRENT", raising=False)
        monkeypatch.setenv("LML_DISCOGS_POOL_MAX_SIZE", "7")
        sem = router_mod._get_lookup_semaphore()
        assert sem._value == 7

    def test_musicbrainz_pool_does_not_couple_the_cap(self, monkeypatch):
        # LML_PG_POOL_MAX_SIZE sizes the owned musicbrainz pool, which a
        # `/lookup` does not borrow on the hot path — it must NOT drag the cap
        # down. Discogs pool at 5, musicbrainz at 1 => cap stays 5.
        monkeypatch.delenv("LML_LOOKUP_MAX_CONCURRENT", raising=False)
        monkeypatch.setenv("LML_DISCOGS_POOL_MAX_SIZE", "5")
        monkeypatch.setenv("LML_PG_POOL_MAX_SIZE", "1")
        sem = router_mod._get_lookup_semaphore()
        assert sem._value == 5

    def test_explicit_override_above_pool_still_wins_but_warns(self, monkeypatch, caplog):
        # Operators keep the no-redeploy Railway lever: an explicit cap over the
        # pool is honoured (they may be about to raise the pool too), but the
        # guard makes the hazardous mismatch loud instead of silent — AC#4.
        monkeypatch.setenv("LML_LOOKUP_MAX_CONCURRENT", "8")
        monkeypatch.setenv("LML_DISCOGS_POOL_MAX_SIZE", "5")
        with caplog.at_level("WARNING", logger=router_mod.logger.name):
            sem = router_mod._get_lookup_semaphore()
        assert sem._value == 8
        assert any(
            "LML_LOOKUP_MAX_CONCURRENT" in r.message and "pool" in r.message.lower()
            for r in caplog.records
        ), "expected a guard warning when the cap exceeds the discogs-cache pool"

    def test_default_within_pool_does_not_warn(self, monkeypatch, caplog):
        monkeypatch.delenv("LML_LOOKUP_MAX_CONCURRENT", raising=False)
        monkeypatch.setenv("LML_DISCOGS_POOL_MAX_SIZE", "5")
        with caplog.at_level("WARNING", logger=router_mod.logger.name):
            sem = router_mod._get_lookup_semaphore()
        assert sem._value == 5
        assert not any(
            "LML_LOOKUP_MAX_CONCURRENT" in r.message and "exceeds" in r.message.lower()
            for r in caplog.records
        )


class TestCallerBudgetDeduction:
    """Queue wait must be debited from the X-Caller-Budget-Ms contract.

    The budget clock (min(header − transport overhead, env)) historically
    started inside ``perform_lookup`` — after the permit was acquired — so a
    request queued K seconds behind the cap still granted itself a full
    budget and returned up to K seconds past the caller's deadline, exactly
    on the congested path the header exists for (A8 / LML#345: "LML returns
    slightly before the caller times out").
    """

    @pytest.mark.asyncio
    async def test_capped_request_deducts_queue_wait_from_caller_budget(
        self, app_client, monkeypatch
    ):
        monkeypatch.setenv("LML_LOOKUP_MAX_CONCURRENT", "1")

        gate = asyncio.Event()
        started = 0
        received_budgets: list[int | None] = []

        async def gated_lookup(request, **kwargs):
            nonlocal started
            started += 1
            received_budgets.append(kwargs.get("caller_budget_ms"))
            if started == 1:
                await gate.wait()
            return _empty_response()

        with patch(
            "lookup.router.perform_lookup", new_callable=AsyncMock, side_effect=gated_lookup
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app_client), base_url="http://test"
            ) as ac:
                first = asyncio.create_task(ac.post("/api/v1/lookup", json=LOOKUP_BODY))
                await _wait_until(lambda: started >= 1, message="first lookup to start")
                second = asyncio.create_task(
                    ac.post(
                        "/api/v1/lookup",
                        json=LOOKUP_BODY,
                        headers={"X-Caller-Budget-Ms": "10000"},
                    )
                )
                # Deterministic: wait until the second request is actually
                # parked on the cap (a fixed sleep can lose the race on a
                # loaded runner — the second would then arrive to a free
                # permit and no deduction would happen), THEN accrue ≥50ms of
                # measured queue wait before releasing.
                await _wait_until(
                    _lookup_semaphore_has_waiters, message="second request to park on the cap"
                )
                await asyncio.sleep(0.05)
                gate.set()
                await asyncio.gather(first, second)

        assert len(received_budgets) == 2
        deducted = received_budgets[1]
        assert deducted is not None
        # Strictly less than sent (≥50ms of measured queue wait) but never
        # zero/negative (which the budget resolver treats as "fall back to the
        # env default" — i.e. an UNBOUNDED budget for the most-delayed caller).
        assert 1 <= deducted < 10000
        assert deducted <= 10000 - 40

    @pytest.mark.asyncio
    async def test_uncontended_request_passes_budget_through_unchanged(
        self, app_client, monkeypatch
    ):
        monkeypatch.setenv("LML_LOOKUP_MAX_CONCURRENT", "8")
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
                resp = await ac.post(
                    "/api/v1/lookup",
                    json=LOOKUP_BODY,
                    headers={"X-Caller-Budget-Ms": "5000"},
                )

        assert resp.status_code == 200
        assert received_budgets == [5000]

    @pytest.mark.asyncio
    async def test_deduction_floors_at_one_never_nonpositive(self, app_client, monkeypatch):
        # A tiny header + a long queue wait must clamp to 1ms, not go ≤0:
        # the pipeline treats non-positive as "unset" and falls back to the
        # env default, which would hand the MOST delayed caller the LARGEST
        # budget.
        monkeypatch.setenv("LML_LOOKUP_MAX_CONCURRENT", "1")

        gate = asyncio.Event()
        started = 0
        received_budgets: list[int | None] = []

        async def gated_lookup(request, **kwargs):
            nonlocal started
            started += 1
            received_budgets.append(kwargs.get("caller_budget_ms"))
            if started == 1:
                await gate.wait()
            return _empty_response()

        with patch(
            "lookup.router.perform_lookup", new_callable=AsyncMock, side_effect=gated_lookup
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app_client), base_url="http://test"
            ) as ac:
                first = asyncio.create_task(ac.post("/api/v1/lookup", json=LOOKUP_BODY))
                await _wait_until(lambda: started >= 1, message="first lookup to start")
                second = asyncio.create_task(
                    ac.post(
                        "/api/v1/lookup",
                        json=LOOKUP_BODY,
                        headers={"X-Caller-Budget-Ms": "10"},
                    )
                )
                await _wait_until(
                    _lookup_semaphore_has_waiters, message="second request to park on the cap"
                )
                await asyncio.sleep(0.05)
                gate.set()
                await asyncio.gather(first, second)

        assert received_budgets[1] == 1


class TestInFlightCapObservability:
    @pytest.mark.asyncio
    async def test_capped_wait_sets_filterable_tag_and_wait_measurement(
        self, app_client, monkeypatch
    ):
        """A queued request tags + measures; the tag is the post-deploy signal.

        ``sentry_sdk.set_tag`` (not ``set_data``) is load-bearing: per the
        LML#683 lesson documented on ``_project_cache_stats_to_transaction``,
        ``set_data`` span attributes read back as "Unknown attribute" in the
        spans dataset — unqueryable. The tag mirrors ``lml.client_aborted``;
        the wait-ms measurement is the quantitative series that decides
        whether the default cap of 8 is tuned right.
        """
        monkeypatch.setenv("LML_LOOKUP_MAX_CONCURRENT", "1")

        gate = asyncio.Event()
        started = 0

        async def gated_lookup(request, **kwargs):
            nonlocal started
            started += 1
            if started == 1:
                await gate.wait()
            return _empty_response()

        transaction = Mock()
        scope = Mock()
        scope.transaction = transaction
        set_tag = Mock()

        with (
            patch("lookup.router.perform_lookup", new_callable=AsyncMock, side_effect=gated_lookup),
            patch.object(router_mod.sentry_sdk, "get_current_scope", return_value=scope),
            patch.object(router_mod.sentry_sdk, "set_tag", set_tag),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app_client), base_url="http://test"
            ) as ac:
                first = asyncio.create_task(ac.post("/api/v1/lookup", json=LOOKUP_BODY))
                await _wait_until(lambda: started >= 1, message="first lookup to start")
                # First request held the permit uncontended: no tag yet.
                assert not any(
                    c.args[0] == "lml.lookup.inflight_capped" for c in set_tag.call_args_list
                )

                second = asyncio.create_task(ac.post("/api/v1/lookup", json=LOOKUP_BODY))
                # Deterministic: the second request must be parked before the
                # release, or it could arrive to a freed permit and never tag.
                await _wait_until(
                    _lookup_semaphore_has_waiters, message="second request to park on the cap"
                )
                gate.set()
                responses = await asyncio.gather(first, second)

        assert [r.status_code for r in responses] == [200, 200]
        capped_tags = [
            c for c in set_tag.call_args_list if c.args == ("lml.lookup.inflight_capped", "true")
        ]
        assert len(capped_tags) == 1
        wait_measurements = [
            c
            for c in transaction.set_measurement.call_args_list
            if c.args[0] == "lml.lookup.inflight_wait_ms"
        ]
        assert len(wait_measurements) == 1
        assert wait_measurements[0].args[1] > 0

    @pytest.mark.asyncio
    async def test_uncontended_request_sets_no_tag_or_measurement(self, app_client, monkeypatch):
        monkeypatch.setenv("LML_LOOKUP_MAX_CONCURRENT", "8")

        transaction = Mock()
        scope = Mock()
        scope.transaction = transaction
        set_tag = Mock()

        with (
            patch(
                "lookup.router.perform_lookup",
                new_callable=AsyncMock,
                return_value=_empty_response(),
            ),
            patch.object(router_mod.sentry_sdk, "get_current_scope", return_value=scope),
            patch.object(router_mod.sentry_sdk, "set_tag", set_tag),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app_client), base_url="http://test"
            ) as ac:
                resp = await ac.post("/api/v1/lookup", json=LOOKUP_BODY)

        assert resp.status_code == 200
        assert not any(c.args[0] == "lml.lookup.inflight_capped" for c in set_tag.call_args_list)
        assert not any(
            c.args[0] == "lml.lookup.inflight_wait_ms"
            for c in transaction.set_measurement.call_args_list
        )
