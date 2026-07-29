"""Unit tests for the ``/api/v1/streaming-check`` in-flight cap (LML#753).

Mirrors the ``/lookup`` cap (LML#706 PR3, ``lookup/router.py``), narrowed to
this endpoint's shape: no PG pool clamp (the endpoint borrows no pool
connection — that's the whole reason it gets its own knob rather than folding
into the LML#716 bulk-family budget), no caller-budget deduction and no
Server-Timing leg (this endpoint has neither). Shape: a process-global
``asyncio.Semaphore`` around the ``check_streaming_availability`` fan-out,
sized by ``LML_STREAMING_CHECK_MAX_CONCURRENT``, built lazily on the first
request so the env is read at request time and tests can reset the global
between event loops (the suite-wide autouse fixture in ``tests/conftest.py``).
Excess requests QUEUE — no 429/503 load-shedding.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, Mock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from config.settings import get_settings
from core.dependencies import get_streaming_posthog_client
from streaming import router as router_mod
from streaming.dependencies import (
    get_apple_music_client,
    get_bandcamp_client,
    get_deezer_client,
    get_spotify_client,
)
from streaming.models import StreamingCheckResponse, StreamingCheckSources
from tests.unit.conftest import override_deps

STREAMING_CHECK_BODY = {"artist": "Stereolab", "title": "Aluminum Tunes"}


async def _wait_until(predicate, *, timeout_s: float = 5.0, message: str = "condition"):
    """Poll ``predicate`` on the loop until true, failing loudly at the deadline."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while not predicate():
        if loop.time() > deadline:
            pytest.fail(f"timed out after {timeout_s}s waiting for: {message}")
        await asyncio.sleep(0.005)


def _streaming_check_semaphore_has_waiters() -> bool:
    """True once a request is parked on the in-flight cap."""
    sem = router_mod._streaming_check_semaphore
    return bool(sem is not None and getattr(sem, "_waiters", None))


def _empty_response() -> StreamingCheckResponse:
    return StreamingCheckResponse(on_streaming=False, sources=StreamingCheckSources())


@pytest.fixture
def app_client(mock_settings):
    from main import app

    with override_deps(
        app,
        {
            get_settings: mock_settings,
            get_spotify_client: None,
            get_deezer_client: AsyncMock(),
            get_apple_music_client: None,
            get_bandcamp_client: None,
            get_streaming_posthog_client: None,
        },
    ):
        yield app


class TestInFlightCapBehavior:
    @pytest.mark.asyncio
    async def test_concurrent_streaming_checks_bounded_by_semaphore(self, app_client, monkeypatch):
        """Peak concurrent ``check_streaming_availability`` executions never exceed the cap.

        Cap 2, 7 concurrent requests: without the semaphore all 7 admit
        immediately and the peak is 7. All 7 must still complete 200 — the
        cap queues, it never sheds.
        """
        monkeypatch.setenv("LML_STREAMING_CHECK_MAX_CONCURRENT", "2")

        in_flight = 0
        peak = 0

        async def fake_check(artist, title, **kwargs):
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.01)
            in_flight -= 1
            return _empty_response()

        with patch(
            "streaming.router.check_streaming_availability",
            new_callable=AsyncMock,
            side_effect=fake_check,
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app_client), base_url="http://test"
            ) as ac:
                responses = await asyncio.gather(
                    *(
                        ac.post("/api/v1/streaming-check", json=STREAMING_CHECK_BODY)
                        for _ in range(7)
                    )
                )

        assert [r.status_code for r in responses] == [200] * 7
        assert peak <= 2, f"Peak concurrency was {peak}; semaphore did not bound it"

    @pytest.mark.asyncio
    async def test_queued_request_completes_after_permit_frees(self, app_client, monkeypatch):
        """Cap 1: a second request queues behind a gated first, then completes."""
        monkeypatch.setenv("LML_STREAMING_CHECK_MAX_CONCURRENT", "1")

        gate = asyncio.Event()
        started = 0

        async def gated_check(artist, title, **kwargs):
            nonlocal started
            started += 1
            await gate.wait()
            return _empty_response()

        with patch(
            "streaming.router.check_streaming_availability",
            new_callable=AsyncMock,
            side_effect=gated_check,
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app_client), base_url="http://test"
            ) as ac:
                first = asyncio.create_task(
                    ac.post("/api/v1/streaming-check", json=STREAMING_CHECK_BODY)
                )
                second = asyncio.create_task(
                    ac.post("/api/v1/streaming-check", json=STREAMING_CHECK_BODY)
                )
                await _wait_until(lambda: started >= 1, message="first check to start")
                await _wait_until(
                    _streaming_check_semaphore_has_waiters,
                    message="second request to park on the cap",
                )
                assert started == 1, f"cap=1 admitted {started} checks concurrently"

                gate.set()
                responses = await asyncio.gather(first, second)

        assert [r.status_code for r in responses] == [200, 200]
        assert started == 2


class TestInFlightCapConfiguration:
    def test_env_var_name_and_default(self):
        assert router_mod._STREAMING_CHECK_MAX_CONCURRENT_ENV_VAR == (
            "LML_STREAMING_CHECK_MAX_CONCURRENT"
        )
        assert router_mod._STREAMING_CHECK_MAX_CONCURRENT_DEFAULT == 8

    def test_semaphore_uses_env_override(self, monkeypatch):
        monkeypatch.setenv("LML_STREAMING_CHECK_MAX_CONCURRENT", "3")
        sem = router_mod._get_streaming_check_semaphore()
        assert sem._value == 3

    def test_semaphore_falls_back_to_default_when_unset(self, monkeypatch):
        monkeypatch.delenv("LML_STREAMING_CHECK_MAX_CONCURRENT", raising=False)
        sem = router_mod._get_streaming_check_semaphore()
        assert sem._value == router_mod._STREAMING_CHECK_MAX_CONCURRENT_DEFAULT == 8

    @pytest.mark.parametrize("bad", ["0", "-4", "banana"])
    def test_invalid_env_falls_back_to_default(self, monkeypatch, bad):
        monkeypatch.setenv("LML_STREAMING_CHECK_MAX_CONCURRENT", bad)
        sem = router_mod._get_streaming_check_semaphore()
        assert sem._value == router_mod._STREAMING_CHECK_MAX_CONCURRENT_DEFAULT == 8

    def test_semaphore_is_process_global_across_calls(self, monkeypatch):
        monkeypatch.setenv("LML_STREAMING_CHECK_MAX_CONCURRENT", "2")
        assert (
            router_mod._get_streaming_check_semaphore()
            is router_mod._get_streaming_check_semaphore()
        )


class TestInFlightCapObservability:
    @pytest.mark.asyncio
    async def test_capped_wait_sets_filterable_tag_and_wait_measurement(
        self, app_client, monkeypatch
    ):
        """A queued request tags + measures; the tag is the post-deploy signal.

        ``sentry_sdk.set_tag`` (not ``set_data``) is load-bearing — the LML#683
        lesson: ``set_data`` span attributes read back as "Unknown attribute" in
        the spans dataset, so it can't back a query or an alert.
        """
        monkeypatch.setenv("LML_STREAMING_CHECK_MAX_CONCURRENT", "1")

        gate = asyncio.Event()
        started = 0

        async def gated_check(artist, title, **kwargs):
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
            patch(
                "streaming.router.check_streaming_availability",
                new_callable=AsyncMock,
                side_effect=gated_check,
            ),
            patch.object(router_mod.sentry_sdk, "get_current_scope", return_value=scope),
            patch.object(router_mod.sentry_sdk, "set_tag", set_tag),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app_client), base_url="http://test"
            ) as ac:
                first = asyncio.create_task(
                    ac.post("/api/v1/streaming-check", json=STREAMING_CHECK_BODY)
                )
                await _wait_until(lambda: started >= 1, message="first check to start")
                assert not any(
                    c.args[0] == "lml.streaming_check.inflight_capped"
                    for c in set_tag.call_args_list
                )

                second = asyncio.create_task(
                    ac.post("/api/v1/streaming-check", json=STREAMING_CHECK_BODY)
                )
                await _wait_until(
                    _streaming_check_semaphore_has_waiters,
                    message="second request to park on the cap",
                )
                gate.set()
                responses = await asyncio.gather(first, second)

        assert [r.status_code for r in responses] == [200, 200]
        capped_tags = [
            c
            for c in set_tag.call_args_list
            if c.args == ("lml.streaming_check.inflight_capped", "true")
        ]
        assert len(capped_tags) == 1
        wait_measurements = [
            c
            for c in transaction.set_measurement.call_args_list
            if c.args[0] == "lml.streaming_check.inflight_wait_ms"
        ]
        assert len(wait_measurements) == 1
        assert wait_measurements[0].args[1] > 0

    @pytest.mark.asyncio
    async def test_uncontended_request_sets_no_tag_or_measurement(self, app_client, monkeypatch):
        monkeypatch.setenv("LML_STREAMING_CHECK_MAX_CONCURRENT", "8")

        transaction = Mock()
        scope = Mock()
        scope.transaction = transaction
        set_tag = Mock()

        with (
            patch(
                "streaming.router.check_streaming_availability",
                new_callable=AsyncMock,
                return_value=_empty_response(),
            ),
            patch.object(router_mod.sentry_sdk, "get_current_scope", return_value=scope),
            patch.object(router_mod.sentry_sdk, "set_tag", set_tag),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app_client), base_url="http://test"
            ) as ac:
                resp = await ac.post("/api/v1/streaming-check", json=STREAMING_CHECK_BODY)

        assert resp.status_code == 200
        assert not any(
            c.args[0] == "lml.streaming_check.inflight_capped" for c in set_tag.call_args_list
        )
        assert not any(
            c.args[0] == "lml.streaming_check.inflight_wait_ms"
            for c in transaction.set_measurement.call_args_list
        )
