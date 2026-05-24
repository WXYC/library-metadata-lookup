"""Unit tests for `POST /api/v1/lookup/bulk` (LML#368).

Bulk variant of `/api/v1/lookup` that amortizes per-row cold-cache cost across
N items in a single request. The handler runs `perform_lookup` per item under
a bounded semaphore; results are returned in input order and per-item failures
are isolated so one item cannot poison the batch.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from discogs.service import DiscogsService
from library.db import LibraryDB
from lookup.models import LookupResponse, LookupResultItem
from tests.factories import make_catalog_item, make_match_result
from tests.unit.conftest import override_deps


@pytest.fixture
def mock_db():
    return AsyncMock(spec=LibraryDB)


@pytest.fixture
def mock_discogs():
    return AsyncMock(spec=DiscogsService)


@pytest.fixture
def app_client(mock_db, mock_discogs, mock_settings):
    from config.settings import get_settings
    from core.dependencies import get_discogs_service, get_library_db, get_posthog_client
    from main import app

    with override_deps(
        app,
        {
            get_library_db: mock_db,
            get_discogs_service: mock_discogs,
            get_posthog_client: None,
            get_settings: mock_settings,
        },
    ):
        yield app


def _match_response(artist: str, album: str) -> LookupResponse:
    """LookupResponse carrying one match — what Backend sees on a happy lookup."""
    return LookupResponse(
        results=[
            LookupResultItem(
                library_item=make_catalog_item(artist=artist, title=album),
                artwork=make_match_result(album=album, artist=artist),
            )
        ],
        search_type="direct",
    )


def _no_match_response() -> LookupResponse:
    return LookupResponse(results=[], search_type="none")


class TestBulkLookupEndpoint:
    @pytest.mark.asyncio
    async def test_happy_path_two_items(self, app_client):
        """Two matching items → 200 with `match` status on each, in order."""
        side_effect = [
            _match_response("Jessica Pratt", "On Your Own Love Again"),
            _match_response("Juana Molina", "DOGA"),
        ]

        with patch(
            "lookup.router.perform_lookup", new_callable=AsyncMock, side_effect=side_effect
        ) as mock_lookup:
            async with AsyncClient(
                transport=ASGITransport(app=app_client), base_url="http://test"
            ) as ac:
                resp = await ac.post(
                    "/api/v1/lookup/bulk",
                    json={
                        "items": [
                            {"artist": "Jessica Pratt", "album": "On Your Own Love Again"},
                            {"artist": "Juana Molina", "album": "DOGA"},
                        ]
                    },
                )

        assert resp.status_code == 200
        data = resp.json()
        assert len(data["results"]) == 2
        assert [r["index"] for r in data["results"]] == [0, 1]
        assert [r["status"] for r in data["results"]] == ["match", "match"]
        assert (
            data["results"][0]["lookup"]["results"][0]["library_item"]["artist"] == "Jessica Pratt"
        )
        assert (
            data["results"][1]["lookup"]["results"][0]["library_item"]["artist"] == "Juana Molina"
        )
        assert mock_lookup.await_count == 2

    @pytest.mark.asyncio
    async def test_results_preserve_input_order(self, app_client):
        """Even with mixed match/no_match/error, response[i] corresponds to request[i]."""

        async def fake_lookup(request, **kwargs):
            if request.artist == "boom":
                raise RuntimeError("upstream failure")
            if request.artist == "miss":
                return _no_match_response()
            return _match_response(request.artist, request.album or "")

        with patch("lookup.router.perform_lookup", new_callable=AsyncMock, side_effect=fake_lookup):
            async with AsyncClient(
                transport=ASGITransport(app=app_client), base_url="http://test"
            ) as ac:
                resp = await ac.post(
                    "/api/v1/lookup/bulk",
                    json={
                        "items": [
                            {"artist": "Stereolab", "album": "Aluminum Tunes"},
                            {"artist": "miss", "album": "X"},
                            {"artist": "boom", "album": "Y"},
                            {"artist": "Cat Power", "album": "Moon Pix"},
                        ]
                    },
                )

        assert resp.status_code == 200
        data = resp.json()
        assert [r["index"] for r in data["results"]] == [0, 1, 2, 3]
        assert [r["status"] for r in data["results"]] == ["match", "no_match", "error", "match"]

    @pytest.mark.asyncio
    async def test_per_item_error_isolated(self, app_client):
        """A raising item must not abort siblings; its result carries a sanitized message.

        The sibling row completes; the failing row reports the exception class name
        only. We deliberately do not surface `str(e)` — exception payloads can
        carry SQL fragments, file paths, or upstream error bodies. Sentry retains
        the full traceback for diagnosis via `logger.exception`.
        """
        leaky_payload = "secret_table_name in SELECT * FROM internal_table"
        side_effect = [
            _match_response("Stereolab", "Aluminum Tunes"),
            RuntimeError(leaky_payload),
        ]

        with patch("lookup.router.perform_lookup", new_callable=AsyncMock, side_effect=side_effect):
            async with AsyncClient(
                transport=ASGITransport(app=app_client), base_url="http://test"
            ) as ac:
                resp = await ac.post(
                    "/api/v1/lookup/bulk",
                    json={
                        "items": [
                            {"artist": "Stereolab", "album": "Aluminum Tunes"},
                            {"artist": "Whoever", "album": "Whatever"},
                        ]
                    },
                )

        assert resp.status_code == 200
        results = resp.json()["results"]
        assert results[0]["status"] == "match"
        assert results[1]["status"] == "error"
        assert results[1]["lookup"] is None
        # Surface the class only — payload contents must NOT appear in the response.
        assert results[1]["message"] == "RuntimeError"
        assert leaky_payload not in results[1]["message"]

    @pytest.mark.asyncio
    async def test_empty_items_rejected_with_422(self, app_client):
        """`items: []` fails Pydantic min_length validation (no DB hit)."""
        with patch("lookup.router.perform_lookup", new_callable=AsyncMock) as mock_lookup:
            async with AsyncClient(
                transport=ASGITransport(app=app_client), base_url="http://test"
            ) as ac:
                resp = await ac.post("/api/v1/lookup/bulk", json={"items": []})

        assert resp.status_code == 422
        mock_lookup.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_oversize_input_rejected_with_400(self, app_client):
        """101 items → 400 per LML#368 acceptance criteria (not 422)."""
        oversized = [{"artist": f"Artist_{i}", "album": "x"} for i in range(101)]

        with patch("lookup.router.perform_lookup", new_callable=AsyncMock) as mock_lookup:
            async with AsyncClient(
                transport=ASGITransport(app=app_client), base_url="http://test"
            ) as ac:
                resp = await ac.post("/api/v1/lookup/bulk", json={"items": oversized})

        assert resp.status_code == 400
        # Cap-check fires before any item work begins.
        mock_lookup.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_concurrency_bounded_by_semaphore(self, app_client, monkeypatch):
        """Concurrent items in flight never exceed `lml_bulk_max_concurrent`.

        Sets the cap to 2 and verifies the peak observed concurrency across a
        7-item batch stays at 2. Without a bounded semaphore the gather would
        admit all 7 immediately and the peak would be 7.
        """
        monkeypatch.setenv("LML_BULK_MAX_CONCURRENT", "2")

        import asyncio

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
            return _match_response(request.artist or "?", request.album or "?")

        with patch("lookup.router.perform_lookup", new_callable=AsyncMock, side_effect=fake_lookup):
            async with AsyncClient(
                transport=ASGITransport(app=app_client), base_url="http://test"
            ) as ac:
                resp = await ac.post(
                    "/api/v1/lookup/bulk",
                    json={"items": [{"artist": f"a{i}", "album": "x"} for i in range(7)]},
                )

        assert resp.status_code == 200
        assert peak <= 2, f"Peak concurrency was {peak}; semaphore did not bound it"

    @pytest.mark.asyncio
    async def test_no_match_status_when_results_empty(self, app_client):
        """`results: []` in the per-item LookupResponse → top-level status no_match."""
        with patch(
            "lookup.router.perform_lookup",
            new_callable=AsyncMock,
            return_value=_no_match_response(),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app_client), base_url="http://test"
            ) as ac:
                resp = await ac.post(
                    "/api/v1/lookup/bulk",
                    json={"items": [{"artist": "Nobody Knows", "album": "Anywhere"}]},
                )

        assert resp.status_code == 200
        result = resp.json()["results"][0]
        assert result["status"] == "no_match"
        # The LookupResponse is still surfaced so callers can inspect search_type
        # / external_source / cache_stats even on a no-match.
        assert result["lookup"]["search_type"] == "none"
        assert result["lookup"]["results"] == []

    @pytest.mark.asyncio
    async def test_malformed_json_body_returns_400(self, app_client):
        """A non-JSON body must hit the manual-parse 400 branch before any item work."""
        with patch("lookup.router.perform_lookup", new_callable=AsyncMock) as mock_lookup:
            async with AsyncClient(
                transport=ASGITransport(app=app_client), base_url="http://test"
            ) as ac:
                resp = await ac.post(
                    "/api/v1/lookup/bulk",
                    content=b"not json",
                    headers={"content-type": "application/json"},
                )

        assert resp.status_code == 400
        assert "malformed json" in resp.json()["detail"].lower()
        mock_lookup.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_body_must_be_object_returns_400(self, app_client):
        """A JSON array (not object) at the top level is a 400, not a 422."""
        with patch("lookup.router.perform_lookup", new_callable=AsyncMock) as mock_lookup:
            async with AsyncClient(
                transport=ASGITransport(app=app_client), base_url="http://test"
            ) as ac:
                # Send a JSON array at top level — valid JSON, wrong shape.
                resp = await ac.post("/api/v1/lookup/bulk", json=[])

        assert resp.status_code == 400
        mock_lookup.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_items_must_be_array_returns_422(self, app_client):
        """`items` present but not a list → 422 (structural-validation tier)."""
        with patch("lookup.router.perform_lookup", new_callable=AsyncMock) as mock_lookup:
            async with AsyncClient(
                transport=ASGITransport(app=app_client), base_url="http://test"
            ) as ac:
                resp = await ac.post("/api/v1/lookup/bulk", json={"items": "nope"})

        assert resp.status_code == 422
        mock_lookup.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skip_cache_query_param_sets_contextvar(self, app_client):
        """`?skip_cache=true` must flip the in-process ContextVar at batch entry.

        Mirrors `/api/v1/lookup`'s `skip_cache` flag (router.py:131-132). The
        ContextVar is task-context-inherited, so a single set at the batch top
        propagates to every concurrent per-item task without per-item resets.
        """
        from discogs.memory_cache import should_skip_cache

        observed: list[bool] = []

        async def fake_lookup(request, **kwargs):
            # Reads the ContextVar from the per-item task — must inherit the
            # batch-level set.
            observed.append(should_skip_cache())
            return _match_response(request.artist or "?", request.album or "?")

        with patch("lookup.router.perform_lookup", new_callable=AsyncMock, side_effect=fake_lookup):
            async with AsyncClient(
                transport=ASGITransport(app=app_client), base_url="http://test"
            ) as ac:
                resp = await ac.post(
                    "/api/v1/lookup/bulk?skip_cache=true",
                    json={
                        "items": [
                            {"artist": "Juana Molina", "album": "DOGA"},
                            {"artist": "Jessica Pratt", "album": "On Your Own Love Again"},
                        ]
                    },
                )

        assert resp.status_code == 200
        assert observed == [True, True], (
            "skip_cache ContextVar did not propagate to per-item perform_lookup tasks"
        )

    @pytest.mark.asyncio
    async def test_posthog_batch_event_emitted(self, mock_db, mock_discogs, mock_settings):
        """When PostHog is configured, the batch route emits exactly one event."""
        from config.settings import get_settings
        from core.dependencies import get_discogs_service, get_library_db, get_posthog_client
        from main import app

        mock_posthog = Mock()
        mock_posthog.capture = Mock()
        mock_posthog.flush = Mock()

        async def fake_lookup(request, **kwargs):
            if request.artist == "miss":
                return _no_match_response()
            if request.artist == "boom":
                raise RuntimeError("boom")
            return _match_response(request.artist or "?", request.album or "?")

        with override_deps(
            app,
            {
                get_library_db: mock_db,
                get_discogs_service: mock_discogs,
                get_posthog_client: mock_posthog,
                get_settings: mock_settings,
            },
        ):
            with patch(
                "lookup.router.perform_lookup", new_callable=AsyncMock, side_effect=fake_lookup
            ):
                async with AsyncClient(
                    transport=ASGITransport(app=app), base_url="http://test"
                ) as ac:
                    resp = await ac.post(
                        "/api/v1/lookup/bulk",
                        json={
                            "items": [
                                {"artist": "Stereolab", "album": "Aluminum Tunes"},
                                {"artist": "miss", "album": "X"},
                                {"artist": "boom", "album": "Y"},
                            ]
                        },
                    )

        assert resp.status_code == 200
        # Exactly one batch-level event, not one per item.
        assert mock_posthog.capture.call_count == 1
        # Inspect the payload — keys differ by Posthog client version, so just
        # verify the count breakdown landed somewhere in the captured payload.
        captured_payload = repr(mock_posthog.capture.call_args)
        assert "batch_size" in captured_payload
        assert "match_count" in captured_payload
        assert "no_match_count" in captured_payload
        assert "error_count" in captured_payload

    @pytest.mark.asyncio
    async def test_invalid_concurrency_env_falls_back_to_default(
        self, app_client, monkeypatch, caplog
    ):
        """Garbage in `LML_BULK_MAX_CONCURRENT` must fall back to the default + warn.

        Pins the contract documented at `_max_concurrency_from_env`: a misconfigured
        env var doesn't blow up the route, it warns and uses the default.
        """
        import logging

        monkeypatch.setenv("LML_BULK_MAX_CONCURRENT", "not-an-int")

        with patch(
            "lookup.router.perform_lookup",
            new_callable=AsyncMock,
            return_value=_no_match_response(),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app_client), base_url="http://test"
            ) as ac:
                with caplog.at_level(logging.WARNING, logger="lookup.router"):
                    resp = await ac.post(
                        "/api/v1/lookup/bulk",
                        json={"items": [{"artist": "Cat Power", "album": "Moon Pix"}]},
                    )

        assert resp.status_code == 200
        assert any(
            "LML_BULK_MAX_CONCURRENT" in rec.message and "falling back" in rec.message
            for rec in caplog.records
        ), "Expected a fallback warning for malformed LML_BULK_MAX_CONCURRENT"

    @pytest.mark.asyncio
    async def test_zero_concurrency_env_floored_to_one(self, app_client, monkeypatch):
        """`LML_BULK_MAX_CONCURRENT=0` must floor to 1, not hang the batch."""
        monkeypatch.setenv("LML_BULK_MAX_CONCURRENT", "0")

        with patch(
            "lookup.router.perform_lookup",
            new_callable=AsyncMock,
            return_value=_no_match_response(),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app_client), base_url="http://test"
            ) as ac:
                resp = await ac.post(
                    "/api/v1/lookup/bulk",
                    json={"items": [{"artist": "Juana Molina", "album": "DOGA"}]},
                )

        assert resp.status_code == 200
