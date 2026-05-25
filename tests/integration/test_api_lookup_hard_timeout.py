"""End-to-end test: LML#370 hard cap surfaces as ``LookupResponse.timeout``.

When ``execute_search_pipeline``'s hard cap fires (loop gate or per-strategy
``asyncio.wait_for``), ``perform_lookup`` must project ``state.timed_out``
into the response shape so callers can distinguish "no match" (empty
``results``, ``timeout: False``) from "ran out of time" (``timeout: True``).

This is a pure TestClient + mocked-strategy test — no `pg` or `external_api`
marker, runs in the default suite.
"""

import asyncio
import time
from unittest.mock import patch

import pytest


class TestApiLookupHardTimeout:
    @pytest.mark.asyncio
    async def test_hard_cap_surfaces_as_timeout_true(self, app_client, monkeypatch):
        """Slow first strategy + tight hard cap → response has ``timeout: true``.

        Patches the orchestrator's ``search_library_with_fallback`` so it
        hangs for longer than the configured hard cap. With the cap at
        150 ms, the per-strategy ``wait_for`` raises ``TimeoutError``, the
        loop records ``state.timed_out=True``, and the response should
        carry ``timeout: true`` (default ``false``) with empty ``results``.
        """
        monkeypatch.setenv("LML_SEARCH_HARD_TIMEOUT_MS", "150")
        monkeypatch.setenv("LML_SEARCH_BUDGET_MS", "60000")

        async def slow_empty(*_args, **_kwargs):
            await asyncio.sleep(10)
            return ([], False)

        with patch(
            "lookup.orchestrator.search_library_with_fallback",
            side_effect=slow_empty,
        ):
            # 5s outer wait_for: if wait_for-propagation regresses, fail
            # fast instead of stalling CI for 10s waiting on the mock.
            start = time.monotonic()
            resp = await asyncio.wait_for(
                app_client.post(
                    "/api/v1/lookup",
                    json={
                        "artist": "Untraceable Artist",
                        "song": "Untraceable Song",
                        "raw_message": "Untraceable Artist - Untraceable Song",
                    },
                ),
                timeout=5.0,
            )
            elapsed = time.monotonic() - start

        assert resp.status_code == 200
        body = resp.json()
        # The whole point: response carries the hard-cap signal.
        assert body.get("timeout") is True, body
        # Pipeline abandoned mid-execution; no library results promoted.
        assert body.get("results") == []
        # Wall time bounded by hard cap (plus pipeline overhead).
        assert elapsed < 1.5, f"hard cap should bound wall time, took {elapsed:.2f}s"

    @pytest.mark.asyncio
    async def test_normal_lookup_has_timeout_false(self, app_client):
        """A successful lookup carries ``timeout: false`` (default).

        Guards against accidentally always-setting the field — the default
        path must remain byte-identical for callers that ignore it.
        """
        resp = await app_client.post(
            "/api/v1/lookup",
            json={
                "artist": "Stereolab",
                "album": "Dots and Loops",
                "raw_message": "Stereolab - Dots and Loops",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        # Field is present and false (or omitted, which the consumer reads as false).
        assert body.get("timeout") is False or "timeout" not in body
