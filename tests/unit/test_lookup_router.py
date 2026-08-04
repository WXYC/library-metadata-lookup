"""Unit tests for lookup/router.py."""

import contextlib
from unittest.mock import AsyncMock, Mock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from discogs.service import DiscogsService
from library.db import LibraryDB
from lookup.models import LookupResponse, LookupResultItem
from tests.factories import LOOKUP_BODY, make_catalog_item, make_library_item, make_match_result
from tests.unit.conftest import override_deps


def _full_cache_stats() -> dict:
    """A fully-populated cache_stats dict matching the typed CacheStats schema.

    `init_cache_stats()` populates every field in production; tests that mock
    `get_cache_stats` need to do the same so the typed `CacheStats(**stats)`
    conversion in the router doesn't raise on missing required fields.
    """
    return {
        "memory_hits": 0,
        "pg_hits": 0,
        "pg_misses": 0,
        "api_calls": 1,
        "pg_time_ms": 0.0,
        "api_time_ms": 0.0,
    }


def _parse_server_timing(value: str) -> dict[str, float | None]:
    """Parse a ``Server-Timing`` header value into ``{name: dur_ms}``.

    Mirrors the ``name;dur=<ms>`` grammar ``RequestTelemetry.as_server_timing``
    emits (comma-joined). Tolerant of a missing ``dur`` (maps to None) so a test
    asserting on presence doesn't trip over a malformed entry.
    """
    parsed: dict[str, float | None] = {}
    for entry in value.split(","):
        entry = entry.strip()
        if not entry:
            continue
        name, _, params = entry.partition(";")
        dur: float | None = None
        for param in params.split(";"):
            param = param.strip()
            if param.startswith("dur="):
                dur = float(param[len("dur=") :])
        parsed[name.strip()] = dur
    return parsed


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


class TestHandleLookup:
    @pytest.mark.asyncio
    async def test_successful_lookup(self, app_client):
        response = LookupResponse(results=[], search_type="direct")

        with patch("lookup.router.perform_lookup", new_callable=AsyncMock) as mock_lookup:
            mock_lookup.return_value = response
            async with AsyncClient(
                transport=ASGITransport(app=app_client), base_url="http://test"
            ) as client:
                resp = await client.post("/api/v1/lookup", json=LOOKUP_BODY)

        assert resp.status_code == 200
        body = resp.json()
        assert body["search_type"] == "direct"

    @pytest.mark.asyncio
    async def test_telemetry_sent_when_posthog_configured(
        self, mock_db, mock_discogs, mock_settings
    ):
        from config.settings import get_settings
        from core.dependencies import get_discogs_service, get_library_db, get_posthog_client
        from main import app

        mock_posthog = Mock()
        mock_posthog.capture = Mock()
        mock_posthog.flush = Mock()

        response = LookupResponse(results=[], search_type="direct")

        with override_deps(
            app,
            {
                get_library_db: mock_db,
                get_discogs_service: mock_discogs,
                get_posthog_client: mock_posthog,
                get_settings: mock_settings,
            },
        ):
            with patch("lookup.router.perform_lookup", new_callable=AsyncMock) as mock_lookup:
                mock_lookup.return_value = response
                async with AsyncClient(
                    transport=ASGITransport(app=app), base_url="http://test"
                ) as client:
                    resp = await client.post("/api/v1/lookup", json=LOOKUP_BODY)

            assert resp.status_code == 200
            # Telemetry sends capture calls via send_to_posthog
            assert mock_posthog.capture.call_count >= 1

    @pytest.mark.asyncio
    async def test_lookup_suppresses_per_step_posthog_events(
        self, mock_db, mock_discogs, mock_settings
    ):
        # /lookup constructs telemetry with emit_step_events=False, so even
        # when the pipeline records steps, only the `lookup_completed` summary
        # reaches PostHog — not the per-step events (`lookup_library_search`,
        # …) that are read by no insight or alert and that a Backend-Service
        # backfill multiplied into a ~7x ingestion spike (2026-08-01).
        from config.settings import get_settings
        from core.dependencies import get_discogs_service, get_library_db, get_posthog_client
        from main import app

        mock_posthog = Mock()
        mock_posthog.capture = Mock()
        mock_posthog.flush = Mock()

        response = LookupResponse(results=[], search_type="direct")

        async def _fake_lookup(*args, **kwargs):
            # Record a step so that, absent suppression, a per-step
            # `lookup_library_search` event would fire alongside the summary.
            with kwargs["telemetry"].track_step("library_search"):
                pass
            return response

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
                "lookup.router.perform_lookup",
                new=AsyncMock(side_effect=_fake_lookup),
            ):
                async with AsyncClient(
                    transport=ASGITransport(app=app), base_url="http://test"
                ) as client:
                    resp = await client.post("/api/v1/lookup", json=LOOKUP_BODY)

        assert resp.status_code == 200
        events = [c.kwargs["event"] for c in mock_posthog.capture.call_args_list]
        assert events == ["lookup_completed"]

    @pytest.mark.asyncio
    async def test_error_returns_500(self, app_client):
        with patch(
            "lookup.router.perform_lookup",
            new_callable=AsyncMock,
            side_effect=Exception("boom"),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app_client), base_url="http://test"
            ) as client:
                resp = await client.post("/api/v1/lookup", json=LOOKUP_BODY)

        assert resp.status_code == 500

    @pytest.mark.asyncio
    async def test_structured_body_without_raw_message_returns_200(
        self, mock_library_db, mock_settings
    ):
        """Regression (WXYC/library-metadata-lookup#783): a structured-only POST
        (artist/album/song, no ``raw_message``) must return 200, not 500.

        ``LookupRequest.raw_message`` is ``str | None`` and defaults to None; the
        internal ``ParsedRequest.raw_message`` is a non-optional ``str``. Copying
        None across in ``_step_prepare`` raised a Pydantic ValidationError that
        ``handle_lookup`` wrapped as HTTP 500. This drives the REAL orchestrator
        (``perform_lookup`` is not patched) so the router→pipeline seam is covered
        end-to-end. Discogs is None so the pipeline resolves purely from the
        library hit, isolating the raw_message coercion under test.
        """
        from config.settings import get_settings
        from core.dependencies import (
            get_discogs_service,
            get_library_db,
            get_posthog_client,
        )
        from main import app

        mock_library_db.search.return_value = [
            make_library_item(
                id=42,
                artist="Stereolab",
                title="Aluminum Tunes",
                call_letters="RO",
                genre="Rock",
            )
        ]

        body = {"artist": "Stereolab", "album": "Aluminum Tunes", "song": "Pop Quiz"}
        assert "raw_message" not in body

        with override_deps(
            app,
            {
                get_library_db: mock_library_db,
                get_discogs_service: None,
                get_posthog_client: None,
                get_settings: mock_settings,
            },
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post("/api/v1/lookup", json=body)

        assert resp.status_code == 200
        results = resp.json()["results"]
        assert len(results) == 1
        assert results[0]["library_item"]["artist"] == "Stereolab"

    @pytest.mark.asyncio
    async def test_http_exception_passthrough(self, app_client):
        from fastapi import HTTPException

        with patch(
            "lookup.router.perform_lookup",
            new_callable=AsyncMock,
            side_effect=HTTPException(status_code=400, detail="Bad request"),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app_client), base_url="http://test"
            ) as client:
                resp = await client.post("/api/v1/lookup", json=LOOKUP_BODY)

        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_caller_budget_header_forwarded_to_perform_lookup(self, app_client):
        """X-Caller-Budget-Ms (A8 / LML#345) flows from the HTTP header into
        perform_lookup's caller_budget_ms kwarg. The router is a thin layer;
        if the header is dropped here the search pipeline's effective-budget
        computation can't see it and BS#345's caller-supplied budgets become
        no-ops. Pins both the header→arg path and the kwarg name.
        """
        response = LookupResponse(results=[], search_type="direct")

        with patch("lookup.router.perform_lookup", new_callable=AsyncMock) as mock_lookup:
            mock_lookup.return_value = response
            async with AsyncClient(
                transport=ASGITransport(app=app_client), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/api/v1/lookup",
                    json=LOOKUP_BODY,
                    headers={"X-Caller-Budget-Ms": "3000"},
                )

        assert resp.status_code == 200
        assert mock_lookup.await_args.kwargs.get("caller_budget_ms") == 3000

    @pytest.mark.asyncio
    async def test_caller_budget_header_absent_forwards_none(self, app_client):
        """When the caller doesn't send the header, perform_lookup receives
        caller_budget_ms=None so the orchestrator can distinguish "no opinion"
        from a numeric value and fall through to the env-default contract.
        """
        response = LookupResponse(results=[], search_type="direct")

        with patch("lookup.router.perform_lookup", new_callable=AsyncMock) as mock_lookup:
            mock_lookup.return_value = response
            async with AsyncClient(
                transport=ASGITransport(app=app_client), base_url="http://test"
            ) as client:
                resp = await client.post("/api/v1/lookup", json=LOOKUP_BODY)

        assert resp.status_code == 200
        assert mock_lookup.await_args.kwargs.get("caller_budget_ms") is None

    @pytest.mark.asyncio
    async def test_bandcamp_client_forwarded_to_perform_lookup(
        self, mock_db, mock_discogs, mock_settings
    ):
        """LML#573 PR-3: the Bandcamp client dependency must flow into
        perform_lookup so the streaming-URL cache post-process can run the
        Bandcamp leg. Without this the leg is dead even with the flag on.
        """
        from config.settings import get_settings
        from core.dependencies import get_discogs_service, get_library_db, get_posthog_client
        from main import app
        from streaming.dependencies import get_bandcamp_client

        sentinel = object()
        response = LookupResponse(results=[], search_type="direct")

        with (
            override_deps(
                app,
                {
                    get_library_db: mock_db,
                    get_discogs_service: mock_discogs,
                    get_posthog_client: None,
                    get_settings: mock_settings,
                    get_bandcamp_client: sentinel,
                },
            ),
            patch("lookup.router.perform_lookup", new_callable=AsyncMock) as mock_lookup,
        ):
            mock_lookup.return_value = response
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post("/api/v1/lookup", json=LOOKUP_BODY)

        assert resp.status_code == 200
        assert mock_lookup.await_args.kwargs.get("bandcamp") is sentinel

    @pytest.mark.asyncio
    async def test_extended_flag_forwarded_to_perform_lookup(self, app_client):
        """When the request body sets extended=true, the LookupRequest carried
        into perform_lookup must reflect that. The router is a thin layer;
        if the flag is dropped here the orchestrator's extended-path branch
        is unreachable and the BS single-call optimization silently degrades.
        """
        response = LookupResponse(results=[], search_type="direct")

        with patch("lookup.router.perform_lookup", new_callable=AsyncMock) as mock_lookup:
            mock_lookup.return_value = response
            async with AsyncClient(
                transport=ASGITransport(app=app_client), base_url="http://test"
            ) as client:
                resp = await client.post("/api/v1/lookup", json={**LOOKUP_BODY, "extended": True})

        assert resp.status_code == 200
        # First positional/keyword arg is the LookupRequest.
        forwarded = mock_lookup.await_args.kwargs.get("request") or mock_lookup.await_args.args[0]
        assert forwarded.extended is True

    @pytest.mark.asyncio
    async def test_warm_cache_flag_forwarded_to_perform_lookup(self, app_client):
        """When the request body sets warm_cache=true, the LookupRequest must
        carry it into perform_lookup so the orchestrator can schedule the
        fire-and-forget bio warm.
        """
        response = LookupResponse(results=[], search_type="direct")

        with patch("lookup.router.perform_lookup", new_callable=AsyncMock) as mock_lookup:
            mock_lookup.return_value = response
            async with AsyncClient(
                transport=ASGITransport(app=app_client), base_url="http://test"
            ) as client:
                resp = await client.post("/api/v1/lookup", json={**LOOKUP_BODY, "warm_cache": True})

        assert resp.status_code == 200
        forwarded = mock_lookup.await_args.kwargs.get("request") or mock_lookup.await_args.args[0]
        assert forwarded.warm_cache is True

    @pytest.mark.asyncio
    async def test_extended_and_warm_cache_default_false(self, app_client):
        """Requests that omit the new flags must keep the legacy behavior:
        extended=False, warm_cache=False on the orchestrator side. Existing
        callers (request-o-matic, dj-site proxy) leave the flags off.
        """
        response = LookupResponse(results=[], search_type="direct")

        with patch("lookup.router.perform_lookup", new_callable=AsyncMock) as mock_lookup:
            mock_lookup.return_value = response
            async with AsyncClient(
                transport=ASGITransport(app=app_client), base_url="http://test"
            ) as client:
                resp = await client.post("/api/v1/lookup", json=LOOKUP_BODY)

        assert resp.status_code == 200
        forwarded = mock_lookup.await_args.kwargs.get("request") or mock_lookup.await_args.args[0]
        # The generated model uses Field(False, ...) so the default surfaces
        # as either False or None depending on whether the client omits the
        # field. Treat both as "off".
        assert not forwarded.extended
        assert not forwarded.warm_cache

    @pytest.mark.asyncio
    async def test_skip_cache_flag(self, app_client):
        response = LookupResponse(results=[], search_type="direct")

        with (
            patch("lookup.router.perform_lookup", new_callable=AsyncMock) as mock_lookup,
            patch("lookup.router.set_skip_cache") as mock_set_skip,
        ):
            mock_lookup.return_value = response
            async with AsyncClient(
                transport=ASGITransport(app=app_client), base_url="http://test"
            ) as client:
                resp = await client.post("/api/v1/lookup?skip_cache=true", json=LOOKUP_BODY)

        assert resp.status_code == 200
        mock_set_skip.assert_called_once_with(True)

    @pytest.mark.asyncio
    async def test_cache_stats_initialized(self, app_client):
        response = LookupResponse(results=[], search_type="direct")

        with (
            patch("lookup.router.perform_lookup", new_callable=AsyncMock) as mock_lookup,
            patch("lookup.router.init_cache_stats") as mock_init,
        ):
            mock_lookup.return_value = response
            async with AsyncClient(
                transport=ASGITransport(app=app_client), base_url="http://test"
            ) as client:
                resp = await client.post("/api/v1/lookup", json=LOOKUP_BODY)

        assert resp.status_code == 200
        mock_init.assert_called_once()

    @pytest.mark.asyncio
    async def test_cache_stats_projected_onto_sentry_transaction(self, app_client):
        """The numeric cache_stats fields are attached to the active Sentry transaction
        as `lml.cache.*` data, so they appear in trace explorer alongside latency.
        """
        response = LookupResponse(results=[], search_type="direct")
        stats = {
            "memory_hits": 2,
            "pg_hits": 5,
            "pg_misses": 1,
            "api_calls": 3,
            "pg_time_ms": 12.5,
            "api_time_ms": 480.7,
        }
        mock_transaction = Mock()
        mock_transaction.set_data = Mock()
        mock_scope = Mock()
        mock_scope.transaction = mock_transaction

        with (
            patch("lookup.router.perform_lookup", new_callable=AsyncMock) as mock_lookup,
            patch("lookup.router.get_cache_stats", return_value=stats),
            patch("lookup.router.sentry_sdk.get_current_scope", return_value=mock_scope),
        ):
            mock_lookup.return_value = response
            async with AsyncClient(
                transport=ASGITransport(app=app_client), base_url="http://test"
            ) as client:
                resp = await client.post("/api/v1/lookup", json=LOOKUP_BODY)

        assert resp.status_code == 200
        # Six numeric fields → six set_data calls
        actual_calls = {c.args[0]: c.args[1] for c in mock_transaction.set_data.call_args_list}
        assert actual_calls == {
            "lml.cache.memory_hits": 2,
            "lml.cache.pg_hits": 5,
            "lml.cache.pg_misses": 1,
            "lml.cache.api_calls": 3,
            "lml.cache.pg_time_ms": 12.5,
            "lml.cache.api_time_ms": 480.7,
        }

    @pytest.mark.asyncio
    async def test_cache_stats_projection_no_op_without_active_transaction(self, app_client):
        """When there is no active Sentry transaction, projection is a no-op (no crash)."""
        response = LookupResponse(results=[], search_type="direct")
        mock_scope = Mock()
        mock_scope.transaction = None  # no active transaction

        with (
            patch("lookup.router.perform_lookup", new_callable=AsyncMock) as mock_lookup,
            patch("lookup.router.get_cache_stats", return_value=_full_cache_stats()),
            patch("lookup.router.sentry_sdk.get_current_scope", return_value=mock_scope),
        ):
            mock_lookup.return_value = response
            async with AsyncClient(
                transport=ASGITransport(app=app_client), base_url="http://test"
            ) as client:
                resp = await client.post("/api/v1/lookup", json=LOOKUP_BODY)

        assert resp.status_code == 200

    def test_projection_skips_non_numeric_values(self):
        """Direct unit test of `_project_cache_stats_to_transaction`'s defensive
        behavior. The router-level typed CacheStats now guarantees all-numeric
        fields, but the projection helper keeps the isinstance check as belt-
        and-suspenders for any future caller that doesn't go through the router.
        """
        from lookup.router import _project_cache_stats_to_transaction

        stats = {"api_calls": 2, "weird_string": "nope", "weird_none": None}
        mock_transaction = Mock()
        mock_scope = Mock()
        mock_scope.transaction = mock_transaction

        with patch("lookup.router.sentry_sdk.get_current_scope", return_value=mock_scope):
            _project_cache_stats_to_transaction(stats)

        actual_calls = {c.args[0]: c.args[1] for c in mock_transaction.set_data.call_args_list}
        assert actual_calls == {"lml.cache.api_calls": 2}

    def test_projection_emits_measurements_for_alerting(self):
        """Each numeric cache_stats field is also recorded as a Sentry transaction
        *measurement* (`lml.cache.<key>`), not only span `data` (LML#683).

        `set_data` attaches the value as opaque span data — visible inside a single
        trace, but not aggregatable in the spans/metrics datasets ("Unknown
        attribute"), so it cannot back a metric alert. `set_measurement` promotes
        the same value to a transaction measurement that the alert engine can
        average/threshold (the row-less-flag degradation alerts in #683). Non-numeric
        values are skipped here too, mirroring the `set_data` projection.
        """
        from lookup.router import _project_cache_stats_to_transaction

        stats = {"api_calls": 2, "pg_time_ms": 12.5, "weird_string": "nope", "weird_none": None}
        mock_transaction = Mock()
        mock_scope = Mock()
        mock_scope.transaction = mock_transaction

        with patch("lookup.router.sentry_sdk.get_current_scope", return_value=mock_scope):
            _project_cache_stats_to_transaction(stats)

        measured = {c.args[0]: c.args[1] for c in mock_transaction.set_measurement.call_args_list}
        assert measured == {"lml.cache.api_calls": 2, "lml.cache.pg_time_ms": 12.5}

    @pytest.mark.asyncio
    async def test_cache_stats_projection_called_with_raw_get_cache_stats_return(self, app_client):
        """The projection helper must be invoked with the exact dict returned by
        get_cache_stats(), not with response.cache_stats. response.cache_stats is
        now a typed CacheStats Pydantic model (wxyc-shared#86), whose `.items()`
        method does not exist; the projection iterates via `.items()` so passing
        the model would AttributeError. Reading from the raw return value of
        get_cache_stats() keeps the projection working regardless.
        """
        stats = _full_cache_stats()
        response = LookupResponse(results=[], search_type="direct")

        with (
            patch("lookup.router.perform_lookup", new_callable=AsyncMock) as mock_lookup,
            patch("lookup.router.get_cache_stats", return_value=stats),
            patch("lookup.router._project_cache_stats_to_transaction") as mock_project,
        ):
            mock_lookup.return_value = response
            async with AsyncClient(
                transport=ASGITransport(app=app_client), base_url="http://test"
            ) as client:
                resp = await client.post("/api/v1/lookup", json=LOOKUP_BODY)

        assert resp.status_code == 200
        # Identity check: must be the exact dict from get_cache_stats(), not a
        # copy that might have been routed through response.cache_stats.
        mock_project.assert_called_once()
        passed_arg = mock_project.call_args.args[0]
        assert passed_arg is stats, (
            "_project_cache_stats_to_transaction must be called with the raw dict "
            "returned by get_cache_stats(), not with response.cache_stats (which is "
            "now a typed CacheStats Pydantic model)."
        )

    @pytest.mark.asyncio
    async def test_cache_stats_projection_failure_does_not_break_request(self, app_client):
        """If the Sentry projection raises, the request must still succeed.
        Observability mustn't break the request path.
        """
        response = LookupResponse(results=[], search_type="direct")
        mock_transaction = Mock()
        mock_transaction.set_data = Mock(side_effect=RuntimeError("boom"))
        mock_scope = Mock()
        mock_scope.transaction = mock_transaction

        with (
            patch("lookup.router.perform_lookup", new_callable=AsyncMock) as mock_lookup,
            patch("lookup.router.get_cache_stats", return_value=_full_cache_stats()),
            patch("lookup.router.sentry_sdk.get_current_scope", return_value=mock_scope),
        ):
            mock_lookup.return_value = response
            async with AsyncClient(
                transport=ASGITransport(app=app_client), base_url="http://test"
            ) as client:
                resp = await client.post("/api/v1/lookup", json=LOOKUP_BODY)

        assert resp.status_code == 200

    def test_extra_keys_seeded_and_projected_onto_transaction(self):
        """Regression (LML#567): the LML-specific cache_stats extra keys must land
        as `lml.cache.*` data on the Sentry transaction even when never recorded
        (value 0).

        Exercises the REAL seeding path — `init_cache_stats(extra_keys=...)`
        followed by `get_cache_stats()` — rather than a hand-built dict, so the
        assertion fails if either LML drops one of these keys from
        `_LML_CACHE_STATS_EXTRA_KEYS` or the upstream `wxyc_fastapi` seeding
        contract regresses. The original bug was the upstream half: the extras
        were not seeded with 0, so they appeared only in payloads from the first
        request that recorded them, and a representative (non-follower) request
        showed no `lml.cache.memory_cache_inflight_join` at all.
        """
        from wxyc_fastapi.observability import get_cache_stats, init_cache_stats

        from lookup.router import (
            _LML_CACHE_STATS_EXTRA_KEYS,
            _project_cache_stats_to_transaction,
        )

        init_cache_stats(extra_keys=_LML_CACHE_STATS_EXTRA_KEYS)
        stats = get_cache_stats()

        mock_transaction = Mock()
        mock_scope = Mock()
        mock_scope.transaction = mock_transaction
        with patch("lookup.router.sentry_sdk.get_current_scope", return_value=mock_scope):
            _project_cache_stats_to_transaction(stats)

        projected = {c.args[0]: c.args[1] for c in mock_transaction.set_data.call_args_list}
        for key in (
            "memory_cache_inflight_join",
            "memory_cache_inflight_retry_after_cancel",
            "memory_cache_write_failed",
        ):
            assert f"lml.cache.{key}" in projected, (
                f"{key!r} must be seeded by init_cache_stats(extra_keys=...) and "
                "projected onto the transaction even at zero (LML#567)."
            )
            assert projected[f"lml.cache.{key}"] == 0

    def test_recorded_extra_key_value_flows_to_transaction(self):
        """A recorded extra-key count must flow through the real recorder →
        get_cache_stats → projection path (LML#567).

        Pins the "even when the dedup fires" half of the report: once a
        follower-join is actually recorded, its count has to surface as
        `lml.cache.memory_cache_inflight_join`, not just the seeded zero.
        """
        from wxyc_fastapi.observability import (
            get_cache_stats,
            get_cache_stats_recorder,
            init_cache_stats,
        )

        from lookup.router import (
            _LML_CACHE_STATS_EXTRA_KEYS,
            _project_cache_stats_to_transaction,
        )

        init_cache_stats(extra_keys=_LML_CACHE_STATS_EXTRA_KEYS)
        recorder = get_cache_stats_recorder()
        recorder.record("memory_cache_inflight_join")
        recorder.record("memory_cache_inflight_join")
        stats = get_cache_stats()

        mock_transaction = Mock()
        mock_scope = Mock()
        mock_scope.transaction = mock_transaction
        with patch("lookup.router.sentry_sdk.get_current_scope", return_value=mock_scope):
            _project_cache_stats_to_transaction(stats)

        projected = {c.args[0]: c.args[1] for c in mock_transaction.set_data.call_args_list}
        assert projected["lml.cache.memory_cache_inflight_join"] == 2

    def test_streaming_warm_depth_shed_key_is_seeded(self):
        """LML#1108 review finding 4: ``streaming_warm_depth_shed`` (LML#1108's
        warm-queue-depth shed counter) must be seeded here like every other
        ``*_STAT_KEY`` in this tuple, or the shed rate has a numerator with no
        denominator (a nonzero count is indistinguishable from "never
        sampled") and the payload shape is unstable across requests. Exercises
        the REAL seeding path (mirrors
        ``test_extra_keys_seeded_and_projected_onto_transaction`` above) so a
        future drop of this key from ``_LML_CACHE_STATS_EXTRA_KEYS`` fails
        here, not just via an assertion against a mock.
        """
        from wxyc_fastapi.observability import get_cache_stats, init_cache_stats

        from lookup.router import _LML_CACHE_STATS_EXTRA_KEYS
        from lookup.streaming_warm_admission import DEPTH_SHED_STAT_KEY

        assert DEPTH_SHED_STAT_KEY in _LML_CACHE_STATS_EXTRA_KEYS

        init_cache_stats(extra_keys=_LML_CACHE_STATS_EXTRA_KEYS)
        stats = get_cache_stats()
        assert stats[DEPTH_SHED_STAT_KEY] == 0

    @pytest.mark.asyncio
    async def test_server_timing_header_present_by_default(self, app_client):
        """BS#881 (Epic G) observability: /lookup surfaces the RequestTelemetry stage
        timings as a Server-Timing response header. Default-on, so an existing
        caller sees it without opting in. The derived ``discogs`` leg and the
        live ``total`` are always present even when perform_lookup is mocked
        (no tracked steps), so a caller can always read at least those two.
        """
        response = LookupResponse(results=[], search_type="direct")

        with patch("lookup.router.perform_lookup", new_callable=AsyncMock) as mock_lookup:
            mock_lookup.return_value = response
            async with AsyncClient(
                transport=ASGITransport(app=app_client), base_url="http://test"
            ) as client:
                resp = await client.post("/api/v1/lookup", json=LOOKUP_BODY)

        assert resp.status_code == 200
        assert "Server-Timing" in resp.headers
        parsed = _parse_server_timing(resp.headers["Server-Timing"])
        assert "total" in parsed
        assert "discogs" in parsed
        # Every emitted dur must parse as a float.
        assert all(v is not None for v in parsed.values())

    @pytest.mark.asyncio
    async def test_server_timing_header_absent_when_flag_off(self, app_client, mock_settings):
        """With LML_EMIT_SERVER_TIMING off, no header is emitted — the kill
        switch is honored and the response is otherwise unchanged. Both
        Server-Timing writers read the flag independently (the router's own
        legs AND the ``lml_wall`` middleware leg — see
        ``core/server_timing_middleware.py``), so both must be pinned off here.
        """
        response = LookupResponse(results=[], search_type="direct")
        flag_off = mock_settings.model_copy(update={"lml_emit_server_timing": False})

        with (
            patch("lookup.router.perform_lookup", new_callable=AsyncMock) as mock_lookup,
            patch("lookup.router.get_settings", return_value=flag_off),
            patch("core.server_timing_middleware.get_settings", return_value=flag_off),
        ):
            mock_lookup.return_value = response
            async with AsyncClient(
                transport=ASGITransport(app=app_client), base_url="http://test"
            ) as client:
                resp = await client.post("/api/v1/lookup", json=LOOKUP_BODY)

        assert resp.status_code == 200
        assert "Server-Timing" not in resp.headers

    @pytest.mark.asyncio
    async def test_server_timing_discogs_is_pg_plus_api(self, app_client):
        """The derived ``discogs`` leg equals ``pg_time_ms + api_time_ms`` from
        cache_stats, so the header never disagrees with the cache_stats JSON.
        """
        response = LookupResponse(results=[], search_type="direct")
        stats = {**_full_cache_stats(), "pg_time_ms": 12.5, "api_time_ms": 480.7}

        with (
            patch("lookup.router.perform_lookup", new_callable=AsyncMock) as mock_lookup,
            patch("lookup.router.get_cache_stats", return_value=stats),
        ):
            mock_lookup.return_value = response
            async with AsyncClient(
                transport=ASGITransport(app=app_client), base_url="http://test"
            ) as client:
                resp = await client.post("/api/v1/lookup", json=LOOKUP_BODY)

        assert resp.status_code == 200
        parsed = _parse_server_timing(resp.headers["Server-Timing"])
        assert parsed["discogs"] == pytest.approx(493.2)

    @pytest.mark.asyncio
    async def test_server_timing_survives_none_cache_stats(self, app_client):
        """``get_cache_stats()`` returns None on an uninitialized context; the
        derived discogs leg must degrade to 0, not TypeError → 500.
        """
        response = LookupResponse(results=[], search_type="direct")

        with (
            patch("lookup.router.perform_lookup", new_callable=AsyncMock) as mock_lookup,
            patch("lookup.router.get_cache_stats", return_value=None),
        ):
            mock_lookup.return_value = response
            async with AsyncClient(
                transport=ASGITransport(app=app_client), base_url="http://test"
            ) as client:
                resp = await client.post("/api/v1/lookup", json=LOOKUP_BODY)

        assert resp.status_code == 200
        parsed = _parse_server_timing(resp.headers["Server-Timing"])
        assert parsed["discogs"] == 0
        assert "total" in parsed

    @pytest.mark.asyncio
    async def test_server_timing_does_not_alter_response_body(self, app_client, mock_settings):
        """The header is out-of-band instrumentation: the JSON body (cache_stats
        included) must be byte-identical whether the header is emitted or not.
        Runs the same request flag-on then flag-off and compares the raw bodies.
        """
        response = LookupResponse(results=[], search_type="direct")
        stats = _full_cache_stats()
        flag_off = mock_settings.model_copy(update={"lml_emit_server_timing": False})

        async def _body(get_settings_return):
            with (
                patch("lookup.router.perform_lookup", new_callable=AsyncMock) as mock_lookup,
                patch("lookup.router.get_cache_stats", return_value=stats),
                patch("lookup.router.get_settings", return_value=get_settings_return),
                patch(
                    "core.server_timing_middleware.get_settings",
                    return_value=get_settings_return,
                ),
            ):
                mock_lookup.return_value = response
                async with AsyncClient(
                    transport=ASGITransport(app=app_client), base_url="http://test"
                ) as client:
                    resp = await client.post("/api/v1/lookup", json=LOOKUP_BODY)
            return resp

        on = await _body(mock_settings)
        off = await _body(flag_off)

        assert on.status_code == off.status_code == 200
        assert "Server-Timing" in on.headers
        assert "Server-Timing" not in off.headers
        # The header must not perturb the body at all.
        assert on.content == off.content

    @pytest.mark.asyncio
    async def test_server_timing_surfaces_real_orchestrator_steps(
        self, mock_library_db, mock_settings
    ):
        """Drives the REAL orchestrator (perform_lookup not patched) so the header
        carries an actual tracked step, not just the derived legs. ``library_search``
        (orchestrator.py) runs on every lookup, so it must appear alongside the
        derived ``discogs`` and the live ``total`` — proving the header reflects
        genuine ``track_step`` timings, not a hardcoded shape. Discogs is None so
        the pipeline resolves purely from the library hit.
        """
        from core.dependencies import get_discogs_service, get_library_db, get_posthog_client
        from main import app

        mock_library_db.search.return_value = [
            make_library_item(id=42, artist="Stereolab", title="Aluminum Tunes", genre="Rock")
        ]

        # Pin the flag on the module global the helper actually reads —
        # dependency_overrides never intercepts a direct get_settings() call, so
        # this asserts on the emitted header rather than on the ambient default.
        with (
            override_deps(
                app,
                {
                    get_library_db: mock_library_db,
                    get_discogs_service: None,
                    get_posthog_client: None,
                },
            ),
            patch("lookup.router.get_settings", return_value=mock_settings),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post("/api/v1/lookup", json=LOOKUP_BODY)

        assert resp.status_code == 200
        parsed = _parse_server_timing(resp.headers["Server-Timing"])
        assert "library_search" in parsed
        assert "discogs" in parsed
        assert "total" in parsed
        assert all(v is not None for v in parsed.values())

    @pytest.mark.asyncio
    async def test_server_timing_failure_does_not_break_request(self, app_client):
        """Observability must not break the request path: if the router's own
        header build raises, the request still returns 200 and the router
        contributes no legs. The ``lml_wall`` middleware leg is a SEPARATE,
        independent measurement (it never touches ``RequestTelemetry``), so it
        still renders — proving the router-side failure was swallowed rather
        than corrupting the header outright.
        """
        response = LookupResponse(results=[], search_type="direct")

        with (
            patch("lookup.router.perform_lookup", new_callable=AsyncMock) as mock_lookup,
            patch(
                "lookup.router.RequestTelemetry.as_server_timing",
                side_effect=RuntimeError("boom"),
            ),
        ):
            mock_lookup.return_value = response
            async with AsyncClient(
                transport=ASGITransport(app=app_client), base_url="http://test"
            ) as client:
                resp = await client.post("/api/v1/lookup", json=LOOKUP_BODY)

        assert resp.status_code == 200
        parsed = _parse_server_timing(resp.headers["Server-Timing"])
        assert set(parsed) == {"lml_wall"}

    @pytest.mark.asyncio
    async def test_server_timing_wire_format_is_strict_parser_safe(
        self, mock_library_db, mock_settings
    ):
        """The emitted header must be consumable by a strict ``Server-Timing`` parser
        (request-o-matic's ``lookup`` CLI, the next PR in the trace). Drives the REAL
        orchestrator and asserts LML's own wiring end-to-end: every entry is
        ``name;dur=<plain-decimal>`` joined by ``", "`` (never scientific notation),
        ``total`` appears exactly once, and the derived ``discogs`` leg sits among
        the entries. Parsing into an ordered LIST (not a dict/set) is deliberate so
        a double-``total`` regression — e.g. an accidental ``extra={"total": …}`` —
        is visible; that LML-side wiring the upstream ``as_server_timing`` unit tests
        can't see. The flag is pinned on explicitly for both Server-Timing writers
        (``lookup.router.get_settings`` for the router's own legs,
        ``core.server_timing_middleware.get_settings`` for the appended ``lml_wall``
        leg) so the assertion never rides the ambient default.

        ``lml_wall`` (the middleware-appended leg, see
        ``core/server_timing_middleware.py``) is necessarily the LAST entry — it is
        appended after the router has already finished building its own header — so
        the router's own canonical ``total`` is second-to-last, not last.
        """
        import re

        from core.dependencies import get_discogs_service, get_library_db, get_posthog_client
        from main import app

        mock_library_db.search.return_value = [
            make_library_item(id=42, artist="Stereolab", title="Aluminum Tunes", genre="Rock")
        ]

        with (
            override_deps(
                app,
                {
                    get_library_db: mock_library_db,
                    get_discogs_service: None,
                    get_posthog_client: None,
                },
            ),
            patch("lookup.router.get_settings", return_value=mock_settings),
            patch("core.server_timing_middleware.get_settings", return_value=mock_settings),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post("/api/v1/lookup", json=LOOKUP_BODY)

        assert resp.status_code == 200
        header = resp.headers["Server-Timing"]

        # Splitting on ", " (the as_server_timing join) and matching each entry
        # against the grammar verifies BOTH the separator and the entry shape: a
        # bare-comma join or a mangled dur would leave a non-conforming token.
        entries = header.split(", ")
        grammar = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*;dur=\d+(?:\.\d+)?$")
        for entry in entries:
            assert grammar.match(entry), f"non-conforming Server-Timing entry: {entry!r}"

        names = [entry.split(";")[0] for entry in entries]
        assert names.count("total") == 1  # exactly one canonical total
        assert names.count("lml_wall") == 1  # exactly one middleware-appended leg
        assert names[-1] == "lml_wall"  # the middleware leg is appended last
        assert names[-2] == "total"  # ...right after the router's own canonical total
        assert "discogs" in names  # the derived pg+api leg
        assert "queue_wait" in names  # always-present LML#907 follow-up leg
        assert "library_search" in names  # a real track_step stage

    @pytest.mark.asyncio
    async def test_server_timing_queue_wait_present_and_zero_when_uncontended(self, app_client):
        """``queue_wait`` (LML#907 fine-grained-timing follow-up) is always
        present, not only when the in-flight cap was engaged. An uncontended
        request (the default single-request `app_client` case) never queued,
        so the leg must read exactly 0 — never absent.
        """
        response = LookupResponse(results=[], search_type="direct")

        with patch("lookup.router.perform_lookup", new_callable=AsyncMock) as mock_lookup:
            mock_lookup.return_value = response
            async with AsyncClient(
                transport=ASGITransport(app=app_client), base_url="http://test"
            ) as client:
                resp = await client.post("/api/v1/lookup", json=LOOKUP_BODY)

        assert resp.status_code == 200
        parsed = _parse_server_timing(resp.headers["Server-Timing"])
        assert "queue_wait" in parsed
        assert parsed["queue_wait"] == 0

    @pytest.mark.asyncio
    async def test_server_timing_event_loop_lag_present_when_gauge_yields_a_value(
        self, app_client, mock_settings
    ):
        """When the LML#907 gauge has sampled a value, it rides the header as
        ``event_loop_lag`` alongside the existing cache_stats projection. The
        leg builder lives in ``lookup.server_timing_legs`` (split out to stay
        under the router's module-budget ceiling), which imports its own
        ``get_settings`` — pinned here alongside the router's, mirroring the
        ``core.server_timing_middleware`` pattern above.
        """
        # Value comes from the process-global gauge (the same source the
        # recorder reads), NOT the cache_stats dict — seed the stats key with a
        # decoy 0.0 to prove the leg reflects the gauge, not the seeded value.
        from lookup.router import EVENT_LOOP_LAG_STAT_KEY

        response = LookupResponse(results=[], search_type="direct")
        stats = {**_full_cache_stats(), EVENT_LOOP_LAG_STAT_KEY: 0.0}

        with (
            patch("lookup.router.perform_lookup", new_callable=AsyncMock) as mock_lookup,
            patch("lookup.router.get_cache_stats", return_value=stats),
            patch("lookup.router.get_settings", return_value=mock_settings),
            patch("lookup.server_timing_legs.get_settings", return_value=mock_settings),
            patch("lookup.server_timing_legs.get_event_loop_lag_ms", return_value=42.5),
        ):
            mock_lookup.return_value = response
            async with AsyncClient(
                transport=ASGITransport(app=app_client), base_url="http://test"
            ) as client:
                resp = await client.post("/api/v1/lookup", json=LOOKUP_BODY)

        assert resp.status_code == 200
        parsed = _parse_server_timing(resp.headers["Server-Timing"])
        assert parsed.get("event_loop_lag") == pytest.approx(42.5)

    @pytest.mark.asyncio
    async def test_server_timing_event_loop_lag_omitted_when_gauge_disabled(
        self, app_client, mock_settings
    ):
        """With ``lml_event_loop_lag_gauge`` off, the leg is OMITTED, never a
        misleading zero — the sampled ``cache_stats`` value (present here to
        prove the omission isn't just "value absent") must not leak through.
        Both Server-Timing writers that read settings independently
        (``lookup.router`` and ``lookup.server_timing_legs``) are pinned off.
        """
        from lookup.router import EVENT_LOOP_LAG_STAT_KEY

        response = LookupResponse(results=[], search_type="direct")
        stats = {**_full_cache_stats(), EVENT_LOOP_LAG_STAT_KEY: 42.5}
        gauge_off = mock_settings.model_copy(update={"lml_event_loop_lag_gauge": False})

        with (
            patch("lookup.router.perform_lookup", new_callable=AsyncMock) as mock_lookup,
            patch("lookup.router.get_cache_stats", return_value=stats),
            patch("lookup.router.get_settings", return_value=gauge_off),
            patch("lookup.server_timing_legs.get_settings", return_value=gauge_off),
            patch("lookup.server_timing_legs.get_event_loop_lag_ms", return_value=42.5),
        ):
            mock_lookup.return_value = response
            async with AsyncClient(
                transport=ASGITransport(app=app_client), base_url="http://test"
            ) as client:
                resp = await client.post("/api/v1/lookup", json=LOOKUP_BODY)

        assert resp.status_code == 200
        parsed = _parse_server_timing(resp.headers["Server-Timing"])
        assert "event_loop_lag" not in parsed
        # The rest of the header is unaffected by the gate.
        assert "discogs" in parsed
        assert "total" in parsed

    @pytest.mark.asyncio
    async def test_server_timing_event_loop_lag_omitted_when_gauge_unsampled(
        self, app_client, mock_settings
    ):
        """Regression (review B#1): with the gauge ENABLED but unsampled (the
        first ~interval of process life, right after a redeploy),
        ``get_event_loop_lag_ms()`` returns ``None`` while ``init_cache_stats``
        has already seeded ``event_loop_lag_ms`` to 0. The leg must be OMITTED,
        not emitted as a misleading ``event_loop_lag;dur=0`` — the builder reads
        the gauge, not the seeded stats value.
        """
        from lookup.router import EVENT_LOOP_LAG_STAT_KEY

        response = LookupResponse(results=[], search_type="direct")
        # Production shape: init_cache_stats seeds the key to 0 on every request.
        stats = {**_full_cache_stats(), EVENT_LOOP_LAG_STAT_KEY: 0.0}

        with (
            patch("lookup.router.perform_lookup", new_callable=AsyncMock) as mock_lookup,
            patch("lookup.router.get_cache_stats", return_value=stats),
            patch("lookup.router.get_settings", return_value=mock_settings),
            patch("lookup.server_timing_legs.get_settings", return_value=mock_settings),
            patch("lookup.server_timing_legs.get_event_loop_lag_ms", return_value=None),
        ):
            mock_lookup.return_value = response
            async with AsyncClient(
                transport=ASGITransport(app=app_client), base_url="http://test"
            ) as client:
                resp = await client.post("/api/v1/lookup", json=LOOKUP_BODY)

        assert resp.status_code == 200
        parsed = _parse_server_timing(resp.headers["Server-Timing"])
        assert "event_loop_lag" not in parsed

    @pytest.mark.asyncio
    async def test_response_includes_call_number(self, app_client):
        """Regression: call_number must appear in the JSON response."""
        result_item = LookupResultItem(
            library_item=make_catalog_item(
                id=10,
                artist="Stereolab",
                title="Aluminum Tunes",
                call_number="Rock CD S 1/1",
            ),
            artwork=make_match_result(
                release_id=12345,
                artwork_url="https://example.com/cover.jpg",
            ),
        )
        response = LookupResponse(results=[result_item], search_type="direct")

        with patch("lookup.router.perform_lookup", new_callable=AsyncMock) as mock_lookup:
            mock_lookup.return_value = response
            async with AsyncClient(
                transport=ASGITransport(app=app_client), base_url="http://test"
            ) as client:
                resp = await client.post("/api/v1/lookup", json=LOOKUP_BODY)

        assert resp.status_code == 200
        library_item = resp.json()["results"][0]["library_item"]
        assert library_item["call_number"] == "Rock CD S 1/1"
        assert library_item["library_url"] == "https://dj.wxyc.org/dashboard/album/legacy/10"


class TestInitLookupObservability:
    """LML#1036: `init_lookup_observability` replaces the identical six-call
    preamble block `handle_lookup` and `handle_bulk_lookup` used to repeat
    inline (`init_cache_stats` -> `_record_lml_flag_tags` ->
    `_record_event_loop_lag` -> `record_endpoint_family_tag` ->
    `record_caller_reason_tag` -> conditional `set_skip_cache`). These tests
    exercise the helper directly and assert against the recorded cache-stats
    / tag state it produces, not implementation internals -- the same
    externally-observable contract the two endpoints' own end-to-end tests
    (test_rowless_flag_observability.py, this file's
    `test_extra_keys_seeded_and_projected_onto_transaction`) already pin.
    """

    def test_seeds_the_passed_extra_keys_at_zero(self):
        """Step 1: `init_cache_stats(extra_keys=...)` seeds every passed key
        at 0 -- the LML#544 round 2 shape-stability contract this helper must
        not regress."""
        from wxyc_fastapi.observability import get_cache_stats

        from lookup.router import ENDPOINT_FAMILY_LOOKUP, init_lookup_observability

        init_lookup_observability(
            ENDPOINT_FAMILY_LOOKUP,
            None,
            extra_keys=("lml1036_probe_key_a", "lml1036_probe_key_b"),
        )

        stats = get_cache_stats()
        assert stats["lml1036_probe_key_a"] == 0
        assert stats["lml1036_probe_key_b"] == 0

    @pytest.mark.parametrize("nonlibrary, expected", [(True, 1), (False, 0)])
    def test_records_the_lml681_flag_tag_once(self, monkeypatch, nonlibrary, expected):
        """Step 2: `_record_lml_flag_tags` (LML#681) -- the flag state lands
        as a clean 0/1 cache_stats key. Calling the helper once must record
        it exactly once, preserving the #681 "once per cache_stats context,
        additive-only recorder" contract -- a regression here would sum
        across repeated calls instead of reflecting the flag cleanly."""
        from wxyc_fastapi.observability import get_cache_stats

        from config.settings import get_settings
        from lookup.router import (
            _LML_CACHE_STATS_EXTRA_KEYS,
            ENDPOINT_FAMILY_LOOKUP,
            LML_RESOLVE_NONLIBRARY_RELEASE_STAT_KEY,
            init_lookup_observability,
        )

        monkeypatch.setenv("LML_RESOLVE_NONLIBRARY_RELEASE", "true" if nonlibrary else "false")
        get_settings.cache_clear()
        try:
            init_lookup_observability(
                ENDPOINT_FAMILY_LOOKUP, None, extra_keys=_LML_CACHE_STATS_EXTRA_KEYS
            )
            stats = get_cache_stats()
            assert stats[LML_RESOLVE_NONLIBRARY_RELEASE_STAT_KEY] == expected
        finally:
            get_settings.cache_clear()

    def test_records_endpoint_family_and_caller_reason_sentry_tags(self):
        """Steps 4-5: both string-valued Sentry tags fire exactly once, with
        the family/reason the caller passed in -- matching
        `record_endpoint_family_tag` / `record_caller_reason_tag`'s own
        per-call contract. Both helpers reach `sentry_sdk.set_tag` through the
        SAME shared module object regardless of which package imports it, so
        this patches the one canonical target rather than two import paths
        (patching both would silently patch-then-repatch the same attribute)."""
        from lookup.router import ENDPOINT_FAMILY_LOOKUP_BULK, init_lookup_observability

        with patch("sentry_sdk.set_tag") as mock_set_tag:
            init_lookup_observability(
                ENDPOINT_FAMILY_LOOKUP_BULK, "proxy-library-search", extra_keys=()
            )

        mock_set_tag.assert_any_call("lml.endpoint_family", ENDPOINT_FAMILY_LOOKUP_BULK)
        mock_set_tag.assert_any_call("lml.caller_reason", "proxy-library-search")
        assert mock_set_tag.call_count == 2

    def test_absent_caller_reason_sets_no_sentry_tag(self):
        """`caller_reason=None` is `record_caller_reason_tag`'s documented
        no-op contract -- no tag call at all, not an empty-string tag. The
        endpoint-family tag still fires, so this asserts on the specific
        `lml.caller_reason` call rather than total call count."""
        from lookup.router import ENDPOINT_FAMILY_LOOKUP, init_lookup_observability

        with patch("sentry_sdk.set_tag") as mock_set_tag:
            init_lookup_observability(ENDPOINT_FAMILY_LOOKUP, None, extra_keys=())

        called_keys = [c.args[0] for c in mock_set_tag.call_args_list]
        assert "lml.caller_reason" not in called_keys

    def test_skip_cache_true_sets_the_context_var(self):
        """Step 6: a truthy `skip_cache` honors the query flag by flipping the
        ContextVar the memory-cache layer reads."""
        from discogs.memory_cache import should_skip_cache
        from lookup.router import ENDPOINT_FAMILY_LOOKUP, init_lookup_observability

        init_lookup_observability(ENDPOINT_FAMILY_LOOKUP, None, skip_cache=True, extra_keys=())
        assert should_skip_cache() is True

    def test_skip_cache_defaults_to_false_and_is_a_no_op(self):
        """`skip_cache` defaults to False; the ContextVar is left alone (the
        session-wide `reset_caches` autouse fixture already primes it False,
        so this pins the helper's own default doesn't flip it)."""
        from discogs.memory_cache import should_skip_cache
        from lookup.router import ENDPOINT_FAMILY_LOOKUP, init_lookup_observability

        init_lookup_observability(ENDPOINT_FAMILY_LOOKUP, None, extra_keys=())
        assert should_skip_cache() is False


class TestCallerClassLowPriorityLane:
    """LML#928/#953: ``X-Caller-Class`` routes a single ``/lookup`` request
    onto the low-priority lane (the LML#716/#924 process-global bulk permit)
    when -- and only when -- the caller declares class 5 (batch/cron/backfill).

    Patches ``lookup.router.maybe_acquire_bulk_global_permit_or_reap`` (the
    exact name ``handle_lookup`` calls) with a spy that records the boolean
    condition it was entered with, so this test pins the router's
    class→condition wiring in isolation from the semaphore itself -- which
    ``test_caller_class.py`` pins directly. Together the two prove the full
    invariant: class 5 -> low-priority lane; classes 1-4, absent, and invalid
    values -> today's interactive-only behavior, unchanged. LML#953 replaced
    the underlying primitive (now disconnect-aware and acquired BEFORE the
    interactive semaphore, not just around ``perform_lookup``) but this
    class→condition invariant is unchanged, so the fake still just records
    the condition and yields immediately.
    """

    @staticmethod
    def _fake_maybe_acquire(calls: list[bool]):
        @contextlib.asynccontextmanager
        async def _fake(condition: bool, request):
            calls.append(condition)
            yield 0.0

        return _fake

    @pytest.mark.asyncio
    async def test_class_five_enters_the_low_priority_lane(self, app_client):
        response = LookupResponse(results=[], search_type="direct")
        calls: list[bool] = []

        with (
            patch("lookup.router.perform_lookup", new_callable=AsyncMock, return_value=response),
            patch(
                "lookup.router.maybe_acquire_bulk_global_permit_or_reap",
                self._fake_maybe_acquire(calls),
            ),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app_client), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/api/v1/lookup",
                    json=LOOKUP_BODY,
                    headers={"X-Caller-Class": "5"},
                )

        assert resp.status_code == 200
        assert calls == [True]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "headers",
        [
            {},
            {"X-Caller-Class": "1"},
            {"X-Caller-Class": "2"},
            {"X-Caller-Class": "3"},
            {"X-Caller-Class": "4"},
            {"X-Caller-Class": "0"},
            {"X-Caller-Class": "6"},
            {"X-Caller-Class": "not-a-class"},
        ],
        ids=[
            "absent",
            "class-1",
            "class-2",
            "class-3",
            "class-4",
            "out-of-range-low",
            "out-of-range-high",
            "non-numeric",
        ],
    )
    async def test_non_class_five_stays_on_interactive_lane(self, app_client, headers):
        """Absent header, classes 1-4, and malformed/out-of-range values must
        all behave EXACTLY as origin/main does today: no low-priority-lane
        entry. This is also the anti-up-rank guard -- an untrusted or
        misconfigured caller cannot use the header to escape today's
        placement in either direction.
        """
        response = LookupResponse(results=[], search_type="direct")
        calls: list[bool] = []

        with (
            patch("lookup.router.perform_lookup", new_callable=AsyncMock, return_value=response),
            patch(
                "lookup.router.maybe_acquire_bulk_global_permit_or_reap",
                self._fake_maybe_acquire(calls),
            ),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app_client), base_url="http://test"
            ) as client:
                resp = await client.post("/api/v1/lookup", json=LOOKUP_BODY, headers=headers)

        assert resp.status_code == 200
        assert calls == [False]
