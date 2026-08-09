"""Unit tests for `POST /api/v1/lookup/bulk` (LML#368).

Bulk variant of `/api/v1/lookup` that amortizes per-row cold-cache cost across
N items in a single request. The handler runs `perform_lookup` per item under
a bounded semaphore; results are returned in input order and per-item failures
are isolated so one item cannot poison the batch.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
import sentry_sdk
from httpx import ASGITransport, AsyncClient
from wxyc_fastapi.testing import CountingPosthog, as_posthog, capture_budget

from config.settings import get_settings
from core.dependencies import (
    get_discogs_cache_pg,
    get_discogs_cache_service,
    get_discogs_service,
    get_library_db,
    get_musicbrainz_pg,
    get_posthog_client,
)
from discogs.models import DiscogsSearchResponse
from discogs.service import DiscogsService
from identity.dependencies import get_entity_store
from library.db import LibraryDB
from lookup.models import LookupResponse, LookupResultItem
from main import app
from streaming.dependencies import get_apple_music_client, get_spotify_client
from tests.factories import make_catalog_item, make_discogs_result, make_match_result
from tests.unit.conftest import override_deps


@pytest.fixture
def mock_db():
    return AsyncMock(spec=LibraryDB)


@pytest.fixture
def mock_discogs():
    return AsyncMock(spec=DiscogsService)


@pytest.fixture
def app_client(mock_db, mock_discogs, mock_settings):
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


def _live_orchestrator_overrides(mock_library_db, mock_discogs_service, mock_settings, *, pg=None):
    """The one shared override map for live-``perform_lookup`` harnesses.

    Both live-orchestrator fixtures (the step-3a gate class and the LML#1026
    location-union guard class) consume this so the dependency graph they
    exercise cannot drift apart; ``pg`` is the single knob that differs
    (``None`` = no discogs-cache source; a mock = the union-task gate's
    PG precondition is satisfied).
    """
    return {
        get_library_db: mock_library_db,
        get_discogs_service: mock_discogs_service,
        get_discogs_cache_service: None,
        get_musicbrainz_pg: None,
        get_entity_store: None,
        get_discogs_cache_pg: pg,
        get_posthog_client: None,
        get_apple_music_client: None,
        get_spotify_client: None,
        get_settings: mock_settings,
    }


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
    async def test_bulk_excludes_release_resolution_fallback(self, app_client):
        """Every per-item perform_lookup call from the bulk drain must pass
        allow_release_resolution_fallback=False so the 35k-album backfill never
        triggers the LML#604 lazy Discogs fan-out (cascade-shape guard)."""
        with patch(
            "lookup.router.perform_lookup",
            new_callable=AsyncMock,
            return_value=_no_match_response(),
        ) as mock_lookup:
            async with AsyncClient(
                transport=ASGITransport(app=app_client), base_url="http://test"
            ) as ac:
                resp = await ac.post(
                    "/api/v1/lookup/bulk",
                    json={
                        "items": [
                            {
                                "artist": "A Guy Called Gerald",
                                "album": "When There Is No Sun",
                                "song": "Message to Black Youth",
                            }
                        ]
                    },
                )

        assert resp.status_code == 200
        assert mock_lookup.await_count == 1
        assert mock_lookup.await_args.kwargs.get("allow_release_resolution_fallback") is False

    @pytest.mark.asyncio
    async def test_bulk_release_resolution_fallback_query_flag_forwards_true(self, app_client):
        """LML#920: ``?allow_release_resolution_fallback=true`` must thread
        through to every per-item ``perform_lookup`` call. Mirrors the
        ``skip_cache`` query flag on this same endpoint — the kill switch
        default stays ``False`` (pinned above), but a caller (the live
        enrichment worker, BS#1815) can opt in per-request."""
        with patch(
            "lookup.router.perform_lookup",
            new_callable=AsyncMock,
            return_value=_no_match_response(),
        ) as mock_lookup:
            async with AsyncClient(
                transport=ASGITransport(app=app_client), base_url="http://test"
            ) as ac:
                resp = await ac.post(
                    "/api/v1/lookup/bulk?allow_release_resolution_fallback=true",
                    json={
                        "items": [
                            {
                                "artist": "A Guy Called Gerald",
                                "album": "When There Is No Sun",
                                "song": "Message to Black Youth",
                            }
                        ]
                    },
                )

        assert resp.status_code == 200
        assert mock_lookup.await_count == 1
        assert mock_lookup.await_args.kwargs.get("allow_release_resolution_fallback") is True

    @pytest.mark.asyncio
    async def test_bulk_emits_total_only_server_timing(self, app_client):
        """BS#881 (Epic G) observability: /lookup/bulk carries a Server-Timing header
        with a single batch-level ``total`` and no per-item step timings or the
        derived ``discogs`` leg — per-item stages aren't meaningful in one
        per-HTTP-request header. Degrade-safe: presence, never a crash.
        """
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
                    json={
                        "items": [{"artist": "Jessica Pratt", "album": "On Your Own Love Again"}]
                    },
                )

        assert resp.status_code == 200
        assert "Server-Timing" in resp.headers
        header = resp.headers["Server-Timing"]
        names = {entry.split(";")[0].strip() for entry in header.split(",")}
        assert names == {"total"}

    @pytest.mark.asyncio
    async def test_bulk_hard_pins_warm_cache_off(self, app_client, caplog):
        """A bulk item that sets ``warm_cache: true`` reaches perform_lookup with
        ``request.warm_cache`` falsy (LML#742). ``warm_cache`` gates the
        fire-and-forget ``_warm_bio_cache`` deep parse (per-bio-ref live Discogs
        calls), so on a 35k-item drain it is exactly the fan-out the bulk
        posture forbids — same rationale as the ``bandcamp`` /
        ``allow_release_resolution_fallback`` pins at the same call site. The
        pin must be surgical: sibling per-item flags (``extended``) ride
        through untouched. And it must be observable: unlike the sibling pins
        (server-side kwargs), warm_cache is a caller-supplied field being
        discarded, and post-pin the Sentry ``lml.lookup.warm_cache`` tag records
        the pinned value — so a warning at the pin site is the only signal that
        distinguishes a misconfigured caller from a compliant one."""
        with patch(
            "lookup.router.perform_lookup",
            new_callable=AsyncMock,
            return_value=_no_match_response(),
        ) as mock_lookup:
            async with AsyncClient(
                transport=ASGITransport(app=app_client), base_url="http://test"
            ) as ac:
                with caplog.at_level(logging.WARNING, logger="lookup.router"):
                    resp = await ac.post(
                        "/api/v1/lookup/bulk",
                        json={
                            "items": [
                                {
                                    "artist": "Chuquimamani-Condori",
                                    "album": "Edits",
                                    "extended": True,
                                    "warm_cache": True,
                                }
                            ]
                        },
                    )

        assert resp.status_code == 200
        assert mock_lookup.await_count == 1
        forwarded = mock_lookup.await_args.kwargs["request"]
        assert not forwarded.warm_cache
        assert forwarded.extended is True
        assert forwarded.artist == "Chuquimamani-Condori"
        pin_warnings = [
            r for r in caplog.records if "warm_cache" in r.message and "pinned" in r.message
        ]
        assert len(pin_warnings) == 1, (
            f"expected one warm_cache-pin warning, got: {[r.message for r in caplog.records]}"
        )

    @pytest.mark.asyncio
    async def test_bulk_warm_cache_pin_is_silent_for_compliant_callers(self, app_client, caplog):
        """The pin warning fires only when a caller actually sets
        ``warm_cache: true`` — a compliant item (flag unset) must not log,
        so the warning stays a high-signal misconfiguration marker instead
        of per-item batch noise."""
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
                        json={
                            "items": [
                                {"artist": "Jessica Pratt", "album": "On Your Own Love Again"}
                            ]
                        },
                    )

        assert resp.status_code == 200
        assert not [r for r in caplog.records if "warm_cache" in r.message]

    @pytest.mark.asyncio
    async def test_bulk_forwards_per_item_extended_true(self, app_client):
        """A bulk item that sets ``extended: true`` reaches perform_lookup with
        ``request.extended is True`` — the model path (BulkLookupRequest →
        LookupRequest items) must not strip it (LML#685).

        This is the contract BS#1442's 35k-row album_metadata backfill relies
        on: the bulk drain is the only Discogs-ceiling-safe way to request the
        extended DiscogsMatchResult fields at that scale. ``perform_lookup``
        reads ``request.extended`` identically to the single ``/lookup`` route,
        so forwarding the flag intact is the whole contract.
        """
        with patch(
            "lookup.router.perform_lookup",
            new_callable=AsyncMock,
            return_value=_no_match_response(),
        ) as mock_lookup:
            async with AsyncClient(
                transport=ASGITransport(app=app_client), base_url="http://test"
            ) as ac:
                resp = await ac.post(
                    "/api/v1/lookup/bulk",
                    json={
                        "items": [
                            {"artist": "Stereolab", "album": "Aluminum Tunes", "extended": True}
                        ]
                    },
                )

        assert resp.status_code == 200
        assert mock_lookup.await_count == 1
        assert mock_lookup.await_args.kwargs["request"].extended is True

    @pytest.mark.asyncio
    async def test_bulk_omitted_extended_defaults_off(self, app_client):
        """Omitting ``extended`` preserves today's bulk contract for non-iOS
        callers (request-o-matic): the extended payload stays off (LML#685 — no
        behavior change for non-extended bulk callers).

        The bulk route forwards the item untouched, so ``perform_lookup`` sees
        the ``LookupRequest.extended`` wire default of ``None`` — no accidental
        opt-in. (In production the spine coerces ``None`` to off once at the
        ``LookupServices`` bundle build — ``extended=bool(request.extended)``
        in ``lookup/orchestrator.py``; that coercion is exercised by the
        orchestrator-level tests, not here — ``perform_lookup`` is mocked on
        this route test.)
        """
        with patch(
            "lookup.router.perform_lookup",
            new_callable=AsyncMock,
            return_value=_no_match_response(),
        ) as mock_lookup:
            async with AsyncClient(
                transport=ASGITransport(app=app_client), base_url="http://test"
            ) as ac:
                resp = await ac.post(
                    "/api/v1/lookup/bulk",
                    json={"items": [{"artist": "Juana Molina", "album": "DOGA"}]},
                )

        assert resp.status_code == 200
        assert mock_lookup.await_count == 1
        assert mock_lookup.await_args.kwargs["request"].extended is None

    @pytest.mark.asyncio
    async def test_bulk_omits_bandcamp_client_when_flag_off(self, app_client):
        """With ``lml_bulk_bandcamp_streaming_warm`` off (the default), the bulk
        drain must NOT forward the Bandcamp client: a per-item live probe would
        serialize a 35k-album drain against Bandcamp's 1 req/s limit and starve
        the interactive path's warms. The handler passes bandcamp=None so the
        post-process skips the Bandcamp leg on bulk (the search-URL fallback
        still applies). LML#573 PR-3; flag-gate extends #1052."""
        with patch(
            "lookup.router.perform_lookup",
            new_callable=AsyncMock,
            return_value=_no_match_response(),
        ) as mock_lookup:
            async with AsyncClient(
                transport=ASGITransport(app=app_client), base_url="http://test"
            ) as ac:
                resp = await ac.post(
                    "/api/v1/lookup/bulk",
                    json={"items": [{"artist": "Juana Molina", "album": "DOGA"}]},
                )

        assert resp.status_code == 200
        assert mock_lookup.await_count == 1
        assert mock_lookup.await_args.kwargs.get("bandcamp") is None

    @pytest.mark.asyncio
    async def test_bulk_forwards_bandcamp_client_when_flag_on(
        self, mock_db, mock_discogs, mock_settings
    ):
        """With ``lml_bulk_bandcamp_streaming_warm`` on, the Bandcamp client
        dependency flows into every per-item ``perform_lookup`` so the
        streaming-URL post-process can warm the direct Bandcamp URL on the bulk
        enrichment path (extends #1052's bulk warm from Spotify to Bandcamp)."""
        from streaming.dependencies import get_bandcamp_client

        sentinel = object()
        flag_on_settings = mock_settings.model_copy(
            update={"lml_bulk_bandcamp_streaming_warm": True}
        )
        with (
            override_deps(
                app,
                {
                    get_library_db: mock_db,
                    get_discogs_service: mock_discogs,
                    get_posthog_client: None,
                    get_settings: flag_on_settings,
                    get_bandcamp_client: sentinel,
                },
            ),
            patch(
                "lookup.router.perform_lookup",
                new_callable=AsyncMock,
                return_value=_no_match_response(),
            ) as mock_lookup,
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                resp = await ac.post(
                    "/api/v1/lookup/bulk",
                    json={"items": [{"artist": "Juana Molina", "album": "DOGA"}]},
                )

        assert resp.status_code == 200
        assert mock_lookup.await_count == 1
        assert mock_lookup.await_args.kwargs.get("bandcamp") is sentinel

    @pytest.mark.asyncio
    async def test_bulk_omits_bandcamp_client_when_live_probe_flag_off(self, app_client):
        """Mirrors ``test_bulk_omits_bandcamp_client_when_flag_off`` for the
        OTHER disjunct in the handler's ``bandcamp=(... if (lml_bulk_bandcamp_
        streaming_warm or lml_bandcamp_live_probe) else None)`` gate
        (LML#1106 review round 2, FIX D): with both flags off (the default),
        the bulk drain must not forward the Bandcamp client."""
        with patch(
            "lookup.router.perform_lookup",
            new_callable=AsyncMock,
            return_value=_no_match_response(),
        ) as mock_lookup:
            async with AsyncClient(
                transport=ASGITransport(app=app_client), base_url="http://test"
            ) as ac:
                resp = await ac.post(
                    "/api/v1/lookup/bulk",
                    json={"items": [{"artist": "Juana Molina", "album": "DOGA"}]},
                )

        assert resp.status_code == 200
        assert mock_lookup.await_count == 1
        assert mock_lookup.await_args.kwargs.get("bandcamp") is None

    @pytest.mark.asyncio
    async def test_bulk_forwards_bandcamp_client_when_live_probe_flag_on(
        self, mock_db, mock_discogs, mock_settings
    ):
        """LML#1106 review round 2, FIX D: with ``lml_bandcamp_live_probe`` on
        (and ``lml_bulk_bandcamp_streaming_warm`` left at its default off, to
        isolate this disjunct), the Bandcamp client must still flow into
        ``perform_lookup`` -- LML#1098's inline live probe (``lookup/
        enrichment/bandcamp_probe.py``) needs ``ctx.bandcamp`` wired to
        resolve a DIRECT album URL on the enrichment path. Without this
        disjunct, enabling ``LML_BANDCAMP_LIVE_PROBE`` in Railway would leave
        ``ctx.bandcamp`` ``None``, failing the probe's own precondition
        silently -- the feature would be a no-op in production with nothing
        catching it (the whole suite stays green without this disjunct)."""
        from streaming.dependencies import get_bandcamp_client

        sentinel = object()
        flag_on_settings = mock_settings.model_copy(update={"lml_bandcamp_live_probe": True})
        with (
            override_deps(
                app,
                {
                    get_library_db: mock_db,
                    get_discogs_service: mock_discogs,
                    get_posthog_client: None,
                    get_settings: flag_on_settings,
                    get_bandcamp_client: sentinel,
                },
            ),
            patch(
                "lookup.router.perform_lookup",
                new_callable=AsyncMock,
                return_value=_no_match_response(),
            ) as mock_lookup,
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                resp = await ac.post(
                    "/api/v1/lookup/bulk",
                    json={"items": [{"artist": "Juana Molina", "album": "DOGA"}]},
                )

        assert resp.status_code == 200
        assert mock_lookup.await_count == 1
        assert mock_lookup.await_args.kwargs.get("bandcamp") is sentinel

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
    async def test_concurrent_batches_share_the_global_bound(self, app_client, monkeypatch):
        """Two concurrent batches never exceed LML_BULK_GLOBAL_MAX_CONCURRENT (LML#716).

        The per-batch semaphore multiplies across requests (N batches admit
        N x LML_BULK_MAX_CONCURRENT items). The process-global permit is the
        cross-request bound: with the per-batch knob wide (10) and the global
        knob at 3, two 6-item batches must peak at <= 3 in-flight items.
        """
        monkeypatch.setenv("LML_BULK_MAX_CONCURRENT", "10")
        monkeypatch.setenv("LML_BULK_GLOBAL_MAX_CONCURRENT", "3")

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
                batch = {"items": [{"artist": f"a{i}", "album": "x"} for i in range(6)]}
                resp_a, resp_b = await asyncio.gather(
                    ac.post("/api/v1/lookup/bulk", json=batch),
                    ac.post("/api/v1/lookup/bulk", json=batch),
                )

        assert resp_a.status_code == 200
        assert resp_b.status_code == 200
        # Queue-don't-shed: every item in both batches still completes.
        assert all(r["status"] == "match" for r in resp_a.json()["results"])
        assert all(r["status"] == "match" for r in resp_b.json()["results"])
        assert peak <= 3, (
            f"Peak cross-request concurrency was {peak}; global permit did not bound it"
        )

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
        assert "malformed json" in resp.json()["message"].lower()
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

        Pins the contract documented at `max_concurrency_from_env`: a misconfigured
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


class TestBulkLookupReleaseResolutionFallbackFlag:
    """LML#920: ``allow_release_resolution_fallback`` is a per-caller query flag
    on ``/lookup/bulk``, mirroring ``skip_cache``, instead of the LML#671
    hardcoded ``False``. Default stays ``False`` — the 35k-album offline drain
    is unchanged — but a caller (the live enrichment worker, BS#1815) can pass
    ``?allow_release_resolution_fallback=true`` to restore the LML#583
    library-miss probe that ``/lookup`` gets by default.

    Unlike the rest of this file (which mocks ``perform_lookup`` entirely),
    these tests let the REAL orchestrator run so the step-3a gate genuinely
    fires or doesn't — only the library DB and Discogs service are mocked,
    mirroring ``tests/unit/test_library_miss_discogs.py``'s orchestrator-level
    coverage of the same gate (``test_step_3a_fires_on_non_bulk_with_typed_artist_album``
    / ``test_step_3a_suppressed_on_bulk_kill_switch``), but driven through the
    actual HTTP endpoint instead of a direct ``perform_lookup`` call.
    """

    @pytest.fixture
    def app_client_live_orchestrator(self, mock_library_db, mock_discogs_service, mock_settings):
        """Real ``perform_lookup``. Only the library DB and Discogs service are
        mocked; every other optional dependency is pinned to ``None`` so the
        request exercises the step-3a gate without needing a live discogs-cache
        pool, MusicBrainz source, entity store, or streaming clients."""
        with override_deps(
            app,
            _live_orchestrator_overrides(mock_library_db, mock_discogs_service, mock_settings),
        ):
            yield app

    @pytest.mark.asyncio
    async def test_flag_true_resolves_nonlibrary_album_via_step_3a(
        self, app_client_live_orchestrator, mock_discogs_service
    ):
        """``?allow_release_resolution_fallback=true`` plus a library-miss
        (artist, album) pair resolves via the #583
        ``_library_miss_discogs_search`` path: an exact-title Discogs
        candidate clears the 80/80 floor and the item surfaces as a
        ``match`` — the same outcome ``/lookup`` gives by default."""
        mock_discogs_service.search.return_value = DiscogsSearchResponse(
            results=[
                make_discogs_result(
                    release_id=5150001,
                    artist="Sessa",
                    album="Pequena Vertigem de Amor",
                    artwork_url="https://img.discogs.com/sessa-cover.jpg",
                )
            ]
        )

        async with AsyncClient(
            transport=ASGITransport(app=app_client_live_orchestrator), base_url="http://test"
        ) as ac:
            resp = await ac.post(
                "/api/v1/lookup/bulk?allow_release_resolution_fallback=true",
                json={"items": [{"artist": "Sessa", "album": "Pequena Vertigem de Amor"}]},
            )

        assert resp.status_code == 200
        result = resp.json()["results"][0]
        assert result["status"] == "match", (
            f"expected step-3a to resolve the library-miss album; got {result!r}"
        )
        assert result["lookup"]["results"][0]["library_item"]["artist"] == "Sessa"

    @pytest.mark.asyncio
    async def test_flag_default_false_keeps_kill_switch_on_same_item(
        self, app_client_live_orchestrator, mock_discogs_service
    ):
        """The identical library-miss item WITHOUT the query flag must stay
        ``no_match`` — the LML#671 kill switch still holds for callers that
        omit the flag (the 35k-album offline drain), even though Discogs
        would confidently resolve it if asked."""
        mock_discogs_service.search.return_value = DiscogsSearchResponse(
            results=[
                make_discogs_result(
                    release_id=5150001,
                    artist="Sessa",
                    album="Pequena Vertigem de Amor",
                    artwork_url="https://img.discogs.com/sessa-cover.jpg",
                )
            ]
        )

        async with AsyncClient(
            transport=ASGITransport(app=app_client_live_orchestrator), base_url="http://test"
        ) as ac:
            resp = await ac.post(
                "/api/v1/lookup/bulk",
                json={"items": [{"artist": "Sessa", "album": "Pequena Vertigem de Amor"}]},
            )

        assert resp.status_code == 200
        result = resp.json()["results"][0]
        assert result["status"] == "no_match", (
            f"expected the default kill switch to suppress step-3a; got {result!r}"
        )


class TestBulkLookupObservability:
    """Entry/exit instrumentation pins (LML#371).

    The bulk endpoint was silent in production — successful requests emitted no
    uvicorn access lines AND no Sentry ``http.server`` spans (issue #371). The
    underlying cause was that both signals fire on response completion, so a
    handler that hung past the client's AbortController timeout left zero trace
    on the server side even though Discogs work was visibly running downstream.

    These tests pin the defensive instrumentation that closes the gap:
    1. An ``INFO`` log at handler entry carrying the request shape — fires
       BEFORE any awaits, so even a totally-hung handler produces this signal.
    2. An ``INFO`` log at handler exit carrying status counts — confirms
       response completion and gives operators a count breakdown without
       digging into Sentry.
    3. A Sentry ``http.server`` span tied to the bulk route — emitted via an
       explicit ``start_span(op="http.server", ...)`` wrap so the signal does
       not depend on the FastApiIntegration patch landing for this specific
       endpoint.

    The matching production change is in ``lookup/router.py:handle_bulk_lookup``.
    """

    @pytest.mark.asyncio
    async def test_entry_log_includes_request_shape(self, app_client, caplog):
        """An INFO log fires at handler entry with size + max_concurrent.

        This is the load-bearing observability signal — it fires synchronously
        before any awaits, so even if the handler later hangs and the response
        never completes, operators see the request shape in Railway logs.
        """
        import logging

        with patch(
            "lookup.router.perform_lookup",
            new_callable=AsyncMock,
            return_value=_no_match_response(),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app_client), base_url="http://test"
            ) as ac:
                with caplog.at_level(logging.INFO, logger="lookup.router"):
                    resp = await ac.post(
                        "/api/v1/lookup/bulk",
                        json={
                            "items": [
                                {"artist": "Juana Molina", "album": "DOGA"},
                                {"artist": "Cat Power", "album": "Moon Pix"},
                                {"artist": "Stereolab", "album": "Aluminum Tunes"},
                            ]
                        },
                    )

        assert resp.status_code == 200
        entry_records = [
            r for r in caplog.records if "bulk lookup" in r.message and "start" in r.message
        ]
        assert entry_records, (
            "Expected an INFO log at bulk handler entry; got logs: "
            f"{[r.message for r in caplog.records]}"
        )
        entry_msg = entry_records[0].message
        # Pin the shape: size of the batch must be in the log line so operators
        # can correlate Railway log entries with caller-side batch sizes. Match
        # the `size=N` prefix (not just the bare digit) so a future format change
        # that drops the key — or a timestamp containing the digit — can't
        # silently pass this assertion.
        assert "size=3" in entry_msg, f"Entry log missing batch size: {entry_msg!r}"

    @pytest.mark.asyncio
    async def test_exit_log_includes_status_counts(self, app_client, caplog):
        """An INFO log fires at handler exit carrying per-status counts.

        Pairs with the entry log: the exit log confirms the response completed
        and gives operators a count breakdown (match/no_match/error) without
        having to query Sentry or correlate by trace id.
        """
        import logging

        async def fake_lookup(request, **kwargs):
            if request.artist == "miss":
                return _no_match_response()
            if request.artist == "boom":
                raise RuntimeError("boom")
            return _match_response(request.artist or "?", request.album or "?")

        with patch("lookup.router.perform_lookup", new_callable=AsyncMock, side_effect=fake_lookup):
            async with AsyncClient(
                transport=ASGITransport(app=app_client), base_url="http://test"
            ) as ac:
                with caplog.at_level(logging.INFO, logger="lookup.router"):
                    # Distinct count distribution (match=2, no_match=1, error=1) so
                    # a regression that swaps the order of `counts["match"]` and
                    # `counts["error"]` in the format args produces a *different*
                    # log line; an all-1s fixture would mask that swap entirely.
                    resp = await ac.post(
                        "/api/v1/lookup/bulk",
                        json={
                            "items": [
                                {"artist": "Juana Molina", "album": "DOGA"},
                                {"artist": "Cat Power", "album": "Moon Pix"},
                                {"artist": "miss", "album": "X"},
                                {"artist": "boom", "album": "Y"},
                            ]
                        },
                    )

        assert resp.status_code == 200
        exit_records = [
            r for r in caplog.records if "bulk lookup" in r.message and "complete" in r.message
        ]
        assert exit_records, (
            "Expected an INFO log at bulk handler exit; got logs: "
            f"{[r.message for r in caplog.records]}"
        )
        exit_msg = exit_records[0].message
        # Pin each count by its key=value pair (not bare digit) so a regression
        # that drops a key, garbles the format string, or swaps two count
        # positions all fail loudly. The distinct distribution above is what
        # makes the swap detectable.
        assert "size=4" in exit_msg, f"Exit log missing batch size: {exit_msg!r}"
        assert "match=2" in exit_msg, f"Exit log missing match count: {exit_msg!r}"
        assert "no_match=1" in exit_msg, f"Exit log missing no_match count: {exit_msg!r}"
        assert "error=1" in exit_msg, f"Exit log missing error count: {exit_msg!r}"

    @pytest.mark.asyncio
    async def test_http_server_span_emitted_for_bulk(self, app_client):
        """An ``http.server`` Sentry span is started with the bulk route name.

        Pins the explicit ``sentry_sdk.start_span(op="http.server", ...)`` in
        the handler. Without this, observability of the bulk endpoint depended
        entirely on the FastApiIntegration's automatic instrumentation — which
        the production logs at 22:21-22:34 on 2026-05-24 showed was not firing
        for hung handlers (the album-level-backfill case in #371).
        """
        # Replace `start_span` with a factory that hands back a fresh MagicMock
        # per call — including the inner `lml.bulk.batch` / `lml.bulk.item`
        # spans the handler also opens. Each mock self-enters as its own
        # context manager and records every `set_data(...)` call so the
        # assertions below can pin both the span name and the `http.target`
        # data field. (The previous version returned the real span, which made
        # the data calls untestable.)
        captured_spans: list[dict] = []

        def capture_start_span(*args, **kwargs):
            span_mock = MagicMock()
            span_mock.__enter__ = MagicMock(return_value=span_mock)
            span_mock.__exit__ = MagicMock(return_value=False)
            captured_spans.append({"args": args, "kwargs": kwargs, "span": span_mock})
            return span_mock

        with (
            patch(
                "lookup.router.perform_lookup",
                new_callable=AsyncMock,
                return_value=_no_match_response(),
            ),
            patch("lookup.router.sentry_sdk.start_span", side_effect=capture_start_span),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app_client), base_url="http://test"
            ) as ac:
                resp = await ac.post(
                    "/api/v1/lookup/bulk",
                    json={"items": [{"artist": "Juana Molina", "album": "DOGA"}]},
                )

        assert resp.status_code == 200
        http_server_spans = [s for s in captured_spans if s["kwargs"].get("op") == "http.server"]
        assert http_server_spans, (
            "Expected at least one Sentry span with op='http.server'; got: "
            f"{[s['kwargs'].get('op') for s in captured_spans]}"
        )
        # The span name must be exactly the canonical route string so operators
        # can filter for `span.description:"POST /api/v1/lookup/bulk"` in the
        # trace explorer. A substring match would not catch a drift to e.g.
        # `/api/v2/lookup/bulk` if the constant ever gets out of sync with the
        # route registration.
        span_name = http_server_spans[0]["kwargs"].get("name", "")
        assert span_name == "POST /api/v1/lookup/bulk", (
            f"Expected http.server span name 'POST /api/v1/lookup/bulk'; got {span_name!r}"
        )
        # Pin the `http.target` data field — the second canonical-route usage
        # in the handler. Together with the span-name pin above, this proves
        # both call sites stay in sync (e.g. after the constant is hoisted).
        set_data_calls = http_server_spans[0]["span"].set_data.call_args_list
        http_target_args = [c.args for c in set_data_calls if c.args and c.args[0] == "http.target"]
        assert http_target_args == [("http.target", "/api/v1/lookup/bulk")], (
            f"Expected http.target set to '/api/v1/lookup/bulk' exactly once; got {http_target_args!r}"
        )


class TestBulkLookupClientAbort:
    """Cancel-aware gather under client disconnect (LML#372).

    Under load, callers' AbortControllers fire at their per-call budget while
    LML's gather kept draining queued items — semaphore permits stayed held,
    queue depth grew monotonically across batches. These tests pin the fix:
    on disconnect, the handler cancels in-flight items, releases permits, and
    short-circuits with HTTP 499.

    Tests patch ``watch_disconnect`` directly; the receive-channel mechanism
    is exercised in production by uvicorn.
    """

    @staticmethod
    def _disconnect_after(delay_s: float = 0.05):
        """Replacement sentinel that 'detects' disconnect after `delay_s`."""

        async def fake_sentinel(_request):
            await asyncio.sleep(delay_s)
            return

        return fake_sentinel

    @pytest.mark.asyncio
    async def test_client_disconnect_cancels_in_flight_items(
        self, mock_db, mock_discogs, mock_settings
    ):
        """Mid-batch disconnect aborts gather; not all items execute; no PostHog event.

        Pins three behaviors at once:
        1. Handler returns 499 (Nginx-style "client closed request") so the
           abort branch is filterable in logs/Sentry.
        2. `perform_lookup`'s await_count is strictly less than the batch size:
           items queued behind the bulk-route semaphore never started.
        3. The PostHog batch event is NOT emitted on abort — partial counts
           would skew the existing batch-completion analytics.
        """
        mock_posthog = Mock()
        mock_posthog.capture = Mock()

        async def slow_lookup(request, **kwargs):
            # Outlast the sentinel's disconnect signal.
            await asyncio.sleep(5)
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
            with (
                patch(
                    "lookup.router.perform_lookup",
                    new_callable=AsyncMock,
                    side_effect=slow_lookup,
                ) as mock_lookup,
                patch("lookup.router.watch_disconnect", self._disconnect_after()),
            ):
                async with AsyncClient(
                    transport=ASGITransport(app=app), base_url="http://test"
                ) as ac:
                    resp = await asyncio.wait_for(
                        ac.post(
                            "/api/v1/lookup/bulk",
                            json={"items": [{"artist": f"a{i}", "album": "x"} for i in range(20)]},
                        ),
                        timeout=3.0,
                    )

        assert resp.status_code == 499, f"Expected 499 on client disconnect, got {resp.status_code}"
        assert mock_lookup.await_count < 20, (
            f"All {mock_lookup.await_count}/20 items completed — abort did not cancel "
            "in-flight items"
        )
        mock_posthog.capture.assert_not_called()

    @pytest.mark.asyncio
    async def test_client_disconnect_cancels_per_item_tasks_cleanly(self, app_client, monkeypatch):
        """Cancellation reaches per-item tasks AND each unwinds via its `finally`.

        Semaphore release is a downstream consequence: the 5-permit Discogs
        semaphore in `discogs/service.py` releases via `try/finally`, and the
        bulk route's own bounded semaphore (set via LML_BULK_MAX_CONCURRENT)
        releases via `async with semaphore:` __aexit__. Both unwinds run iff
        cancellation reaches the per-item coroutine.

        Proxy assertion: count `started` (each item that entered slow_lookup)
        and `cleaned_up` (each that exited via `finally`). On clean cancellation
        propagation, started == cleaned_up. If the cancel doesn't reach the
        per-item tasks, started > cleaned_up — and that's the bug LML#372 fixes.
        """
        monkeypatch.setenv("LML_BULK_MAX_CONCURRENT", "2")

        started = 0
        cleaned_up = 0

        async def slow_lookup(request, **kwargs):
            nonlocal started, cleaned_up
            started += 1
            try:
                await asyncio.sleep(5)
                return _match_response(request.artist or "?", request.album or "?")
            finally:
                cleaned_up += 1

        with (
            patch(
                "lookup.router.perform_lookup",
                new_callable=AsyncMock,
                side_effect=slow_lookup,
            ),
            patch("lookup.router.watch_disconnect", self._disconnect_after()),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app_client), base_url="http://test"
            ) as ac:
                resp = await asyncio.wait_for(
                    ac.post(
                        "/api/v1/lookup/bulk",
                        json={"items": [{"artist": f"a{i}", "album": "x"} for i in range(6)]},
                    ),
                    timeout=3.0,
                )

        assert resp.status_code == 499
        assert started > 0, "no items started — test mis-configured"
        assert started == cleaned_up, (
            f"{started - cleaned_up} item(s) started but never cleaned up — "
            "cancellation did not propagate into per-item tasks"
        )

    @pytest.mark.asyncio
    async def test_client_disconnect_releases_global_permits(self, app_client, monkeypatch):
        """Every LML#716 global permit is back after a mid-batch abort.

        The global permit is held INSIDE the per-item `async with`, so
        cancellation unwinding the item must release it — otherwise one
        aborted drain permanently shrinks the process-wide budget for every
        bulk-family endpoint. Pinned by re-acquiring the full budget after
        the 499: if any permit leaked, the acquire times out.
        """
        monkeypatch.setenv("LML_BULK_MAX_CONCURRENT", "10")
        monkeypatch.setenv("LML_BULK_GLOBAL_MAX_CONCURRENT", "3")

        async def slow_lookup(request, **kwargs):
            await asyncio.sleep(5)
            return _match_response(request.artist or "?", request.album or "?")

        with (
            patch(
                "lookup.router.perform_lookup",
                new_callable=AsyncMock,
                side_effect=slow_lookup,
            ),
            patch("lookup.router.watch_disconnect", self._disconnect_after()),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app_client), base_url="http://test"
            ) as ac:
                resp = await asyncio.wait_for(
                    ac.post(
                        "/api/v1/lookup/bulk",
                        json={"items": [{"artist": f"a{i}", "album": "x"} for i in range(6)]},
                    ),
                    timeout=3.0,
                )

        assert resp.status_code == 499

        from core.bulk_concurrency import acquire_bulk_global_permit

        async def _acquire_full_budget():
            async with (
                acquire_bulk_global_permit(),
                acquire_bulk_global_permit(),
                acquire_bulk_global_permit(),
            ):
                pass

        # All 3 permits must be immediately re-acquirable; a leak deadlocks.
        await asyncio.wait_for(_acquire_full_budget(), timeout=1.0)

    @pytest.mark.asyncio
    async def test_client_disconnect_sets_sentry_tag(self, app_client):
        """`lml.client_aborted=true` lands on the active Sentry scope on abort.

        Filterable in trace explorer: `lml.client_aborted:true` should return
        the aborted-batch transactions for triage.
        """
        captured_tags: dict[str, str] = {}
        original_set_tag = sentry_sdk.set_tag

        def capture_tag(key, value):
            captured_tags[key] = value
            return original_set_tag(key, value)

        async def slow_lookup(request, **kwargs):
            await asyncio.sleep(5)
            return _match_response(request.artist or "?", request.album or "?")

        with (
            patch(
                "lookup.router.perform_lookup",
                new_callable=AsyncMock,
                side_effect=slow_lookup,
            ),
            patch("lookup.router.watch_disconnect", self._disconnect_after()),
            patch("lookup.router.sentry_sdk.set_tag", side_effect=capture_tag),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app_client), base_url="http://test"
            ) as ac:
                resp = await asyncio.wait_for(
                    ac.post(
                        "/api/v1/lookup/bulk",
                        json={"items": [{"artist": f"a{i}", "album": "x"} for i in range(4)]},
                    ),
                    timeout=3.0,
                )

        assert resp.status_code == 499
        assert captured_tags.get("lml.client_aborted") == "true", (
            f"Expected lml.client_aborted=true tag; got {captured_tags!r}"
        )


class TestBulkLookupFamilyAlignment:
    """LML#767: the bulk-lookup route joins the shared envelope. It has no
    batch-level transient-PG arm (per-item failures are isolated in
    `_run_one`), so only the ClientDisconnect and structured-422 arms apply.
    The 400 over-cap contract (LML#368) is preserved."""

    @pytest.mark.asyncio
    async def test_client_disconnect_during_body_read_returns_400(self, app_client, monkeypatch):
        """A mid-body client abort maps to 400, not an unhandled 500."""
        from starlette.requests import ClientDisconnect, Request

        async def _gone(self):
            raise ClientDisconnect()

        monkeypatch.setattr(Request, "json", _gone)

        with patch("lookup.router.perform_lookup", new_callable=AsyncMock) as mock_lookup:
            async with AsyncClient(
                transport=ASGITransport(app=app_client), base_url="http://test"
            ) as ac:
                resp = await ac.post(
                    "/api/v1/lookup/bulk", json={"items": [{"artist": "x", "album": "y"}]}
                )

        assert resp.status_code == 400
        assert "disconnected" in resp.json()["message"].lower()
        mock_lookup.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_absent_items_field_returns_structured_422(self, app_client):
        """A missing `items` field falls through to model_validate, so the
        detail is Pydantic's structured errors() list, not a bare
        "`items` must be a JSON array" string."""
        with patch("lookup.router.perform_lookup", new_callable=AsyncMock) as mock_lookup:
            async with AsyncClient(
                transport=ASGITransport(app=app_client), base_url="http://test"
            ) as ac:
                resp = await ac.post("/api/v1/lookup/bulk", json={})

        assert resp.status_code == 422
        detail = resp.json()["details"]["detail"]
        assert isinstance(detail, list)
        assert detail[0]["loc"] == ["items"]
        assert detail[0]["type"] == "missing"
        mock_lookup.assert_not_awaited()


class TestCallerClassAntiUpRankInvariant:
    """LML#928 anti-up-rank guard: no ``X-Caller-Class`` value can let a
    `/lookup/bulk` caller escape the low-priority lane.

    Every item already runs under ``acquire_bulk_global_permit`` (see
    ``_run_one`` in the router) regardless of caller class -- `/lookup/bulk`
    is unconditionally low-priority today. This pins that as a REGRESSION
    GUARD: the header is accepted (so it doesn't 422 and so a future
    emission/observability seam has it in scope) but must never be read as a
    signal to skip the LML#716/#924 budget. Down-rank only, never up-rank --
    a caller cannot self-declare a "higher" class to get preferential
    treatment on this endpoint.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "headers",
        [
            {},
            {"X-Caller-Class": "1"},
            {"X-Caller-Class": "4"},
            {"X-Caller-Class": "5"},
            {"X-Caller-Class": "not-a-class"},
        ],
        ids=["absent", "class-1", "class-4", "class-5", "non-numeric"],
    )
    async def test_every_item_still_acquires_the_global_permit(self, app_client, headers):
        calls = 0

        @contextlib.asynccontextmanager
        async def _counting_permit():
            nonlocal calls
            calls += 1
            yield

        with (
            patch(
                "lookup.router.perform_lookup",
                new_callable=AsyncMock,
                return_value=_no_match_response(),
            ),
            patch("lookup.router.acquire_bulk_global_permit", _counting_permit),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app_client), base_url="http://test"
            ) as ac:
                resp = await ac.post(
                    "/api/v1/lookup/bulk",
                    json={"items": [{"artist": f"a{i}", "album": "x"} for i in range(3)]},
                    headers=headers,
                )

        assert resp.status_code == 200
        assert calls == 3, (
            "every item must acquire the low-priority global permit regardless of "
            "X-Caller-Class -- the header must never up-rank a bulk caller out of "
            "the low-priority lane"
        )


class TestBulkNeverRunsLocationUnion:
    """LML#1026 nit 2 (bulk amplification guard): a `/lookup/bulk` caller must
    never fan out one recall-index PG probe + one sqlite shelf-join per item.

    Post-fold this is structural rather than a per-item flag pin: the route
    sets ``set_discogs_low_priority(True)`` unconditionally for the whole
    batch, and the orchestrator's union-task gate checks
    ``not is_discogs_low_priority()`` -- so no task, no probe, no join. This
    test drives the REAL ``perform_lookup`` per item with a discogs-cache PG
    source wired (so the only thing standing between a bulk item and the
    probe is the low-priority contextvar) and pins that the probe is never
    awaited for song-bearing items.
    """

    @pytest.fixture
    def app_client_live_orchestrator_with_pg(
        self, mock_library_db, mock_discogs_service, mock_settings
    ):
        with override_deps(
            app,
            _live_orchestrator_overrides(
                mock_library_db, mock_discogs_service, mock_settings, pg=AsyncMock()
            ),
        ):
            yield app

    @pytest.mark.asyncio
    async def test_bulk_items_never_launch_the_recall_index_probe(
        self, app_client_live_orchestrator_with_pg, mock_discogs_service
    ):
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])

        with patch(
            "lookup.orchestrator.resolve_track_shelf_locations", new_callable=AsyncMock
        ) as probe:
            async with AsyncClient(
                transport=ASGITransport(app=app_client_live_orchestrator_with_pg),
                base_url="http://test",
            ) as ac:
                resp = await ac.post(
                    "/api/v1/lookup/bulk",
                    json={
                        "items": [
                            {"artist": "Juana Molina", "song": "la paradoja"},
                            {"artist": "Jessica Pratt", "song": "Back, Baby"},
                        ]
                    },
                )

        assert resp.status_code == 200
        probe.assert_not_awaited()


class TestBulkLookupCaptureBudget:
    """WXYC/library-metadata-lookup#1169: the bulk path emits O(1) PostHog
    events per batch, not O(N) per item.

    Unlike ``TestBulkLookupEndpoint.test_posthog_batch_event_emitted`` (which
    mocks ``perform_lookup`` entirely), this drives the REAL orchestrator per
    item — each item's own ``lookup.bulk``-prefixed ``RequestTelemetry``
    genuinely tracks ~9 steps internally (``lookup/orchestrator.py``). Only
    two things keep those from reaching PostHog: no call site ever invokes
    ``.send_to_posthog()`` on a per-item telemetry instance, and
    ``emit_step_events=False`` is now pinned explicitly on both bulk
    construction sites (the per-item instance and the batch-level
    ``batch_telemetry``) as defense-in-depth. ``wxyc_fastapi.testing.
    capture_budget`` fails loudly if a future change (e.g. wiring
    ``send_to_posthog`` onto the per-item instance) reintroduces a per-item
    fan-out — the exact shape of the 2026-08-04 quota incident, just on the
    bulk path instead of ``/lookup``.
    """

    @pytest.fixture
    def app_client_live_orchestrator_with_posthog(
        self, mock_library_db, mock_discogs_service, mock_settings
    ):
        """Real ``perform_lookup``, with a `CountingPosthog` wired through the
        same DI seam ``_live_orchestrator_overrides`` uses for every other
        dependency."""
        counting_client = CountingPosthog()
        overrides = _live_orchestrator_overrides(
            mock_library_db, mock_discogs_service, mock_settings
        )
        overrides[get_posthog_client] = as_posthog(counting_client)
        with override_deps(app, overrides):
            yield app, counting_client

    @pytest.mark.asyncio
    async def test_multi_item_batch_emits_exactly_one_posthog_event(
        self, app_client_live_orchestrator_with_posthog, mock_discogs_service
    ):
        """A 3-item batch — each running the full real pipeline — still emits
        exactly one ``lookup.bulk_completed`` summary, never one per item.

        Scope note: the ``emit_step_events=False`` pins on the batch and
        per-item construction sites are defense-in-depth no-ops today (the
        batch telemetry tracks no steps; nothing sends the per-item one), so
        no test can enforce the kwargs themselves — this test pins the
        surrounding invariant instead, and fails on the refactor that would
        actually cost money: wiring per-item ``send_to_posthog`` back up.
        """
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])
        app_client, counting_client = app_client_live_orchestrator_with_posthog

        with capture_budget(1, client=counting_client):
            async with AsyncClient(
                transport=ASGITransport(app=app_client), base_url="http://test"
            ) as ac:
                resp = await ac.post(
                    "/api/v1/lookup/bulk",
                    json={
                        "items": [
                            {"artist": "Juana Molina", "album": "DOGA"},
                            {"artist": "Jessica Pratt", "album": "On Your Own Love Again"},
                            {"artist": "Cat Power", "album": "Moon Pix"},
                        ]
                    },
                )

        assert resp.status_code == 200
        assert counting_client.count() == 1
        assert counting_client.events == ["lookup.bulk_completed"]
