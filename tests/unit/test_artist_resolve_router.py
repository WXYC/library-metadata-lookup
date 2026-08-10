"""Unit tests for `POST /api/v1/artists/resolve/bulk` (LML#759 PR C).

HTTP envelope only — resolver semantics (the verdict table) live in
``test_artist_resolver.py``. Mirrors the sibling
``test_artist_search_aliases_router.py``: error-class routing, drift
guards, the 4xx/5xx contract surface, and the PostHog aggregate event.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from artists.router import _RESOLVE_INPUT_CAP
from discogs.cache_service import (
    ArtistEqualityCandidates,
    CacheUnavailableError,
    DiscogsCacheService,
)
from discogs.models import DiscogsArtistSearchResult
from discogs.service import DiscogsService
from entity.store import EntityStore, FillOutcome, Identity
from generated.api_models import ArtistResolveBulkRequest
from tests.unit.conftest import override_deps

_ROUTE = "/api/v1/artists/resolve/bulk"


# spec= throughout: the negative pins below (`assert_not_awaited`) are only
# meaningful if a renamed resolver method can't silently auto-create the old
# attribute on the mock.
@pytest.fixture
def mock_entity_store():
    store = AsyncMock(spec=EntityStore)
    store.bulk_resolve_library_names = AsyncMock(return_value={})
    store.upsert_identity = AsyncMock(return_value=MagicMock())
    # LML#766: the mint site uses the fill-if-null primitive; a clean win.
    store.fill_identity_discogs_id = AsyncMock(
        return_value=FillOutcome(
            identity=Identity(
                id=1,
                library_name="Wishy",
                discogs_artist_id=123,
                reconciliation_status="reconciled",
            ),
            filled=True,
            lost_race=False,
            previous_discogs_artist_id=None,
        )
    )
    return store


@pytest.fixture
def mock_discogs_cache():
    cache = AsyncMock(spec=DiscogsCacheService)

    async def _equality(forms):
        return {form: ArtistEqualityCandidates() for form in forms}

    async def _trigram(names, **kwargs):
        return {name: set() for name in names}

    cache.artist_equality_candidates = AsyncMock(side_effect=_equality)
    cache.artist_trigram_candidates = AsyncMock(side_effect=_trigram)
    return cache


@pytest.fixture
def mock_discogs_service():
    service = AsyncMock(spec=DiscogsService)
    service.search_artists = AsyncMock(
        return_value=[DiscogsArtistSearchResult(artist_id=123, title="Wishy")]
    )
    return service


class _FakeSpan:
    """Recording double for the route's sentry span."""

    def __init__(self, recorded: dict):
        self._recorded = recorded

    def set_data(self, key, value):
        self._recorded[key] = value

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


@pytest.fixture
def recorded_span(monkeypatch):
    """Patch the route module's `start_span`; yields the recorded set_data dict."""
    import artists.router as router_module

    recorded: dict = {}
    monkeypatch.setattr(router_module.sentry_sdk, "start_span", lambda **kw: _FakeSpan(recorded))
    return recorded


_UNSET = object()


@pytest.fixture
def make_app(mock_settings, mock_entity_store, mock_discogs_cache, mock_discogs_service):
    """App factory: per-test control over the DI surface (service=None etc.)."""
    from config.settings import get_settings
    from core.dependencies import (
        get_artist_resolve_posthog_client,
        get_discogs_cache_service_from_pool,
        get_discogs_service,
        get_library_db,
        get_posthog_client,
    )
    from identity.dependencies import get_entity_store
    from main import app

    def _make(
        *,
        discogs_service=_UNSET,
        entity_store=_UNSET,
        discogs_cache=_UNSET,
        posthog=None,
        settings=_UNSET,
    ):
        deps = {
            get_library_db: AsyncMock(),
            get_discogs_service: (
                mock_discogs_service if discogs_service is _UNSET else discogs_service
            ),
            get_posthog_client: None,
            get_artist_resolve_posthog_client: posthog,
            get_settings: mock_settings if settings is _UNSET else settings,
            get_entity_store: mock_entity_store if entity_store is _UNSET else entity_store,
            get_discogs_cache_service_from_pool: (
                mock_discogs_cache if discogs_cache is _UNSET else discogs_cache
            ),
        }
        return override_deps(app, deps), app

    return _make


async def _post(app, json_body):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        return await ac.post(_ROUTE, json=json_body)


class TestHappyPath:
    @pytest.mark.asyncio
    async def test_resolves_and_serializes_full_verdict(self, make_app, mock_entity_store):
        ctx, app = make_app()
        with ctx:
            resp = await _post(app, {"names": ["Wishy"]})

        assert resp.status_code == 200
        (result,) = resp.json()["results"]
        assert result["name"] == "Wishy"
        assert result["discogs_artist_id"] == 123
        assert result["canonical_name"] == "Wishy"
        assert result["method"] == "api_search"
        assert result["unresolved_reason"] is None
        # Always serialized — never omitted (null means "not measured").
        assert result["candidate_count"] == 1
        mock_entity_store.fill_identity_discogs_id.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_dry_run_skips_write_back(self, make_app, mock_entity_store):
        ctx, app = make_app()
        with ctx:
            resp = await _post(app, {"names": ["Wishy"], "dry_run": True})

        assert resp.status_code == 200
        assert resp.json()["results"][0]["discogs_artist_id"] == 123
        mock_entity_store.fill_identity_discogs_id.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_discogs_service_degrades_to_200_not_503(self, make_app):
        """No token → PG tiers keep answering; the API tier reports
        escalation_unavailable per name. Never a batch-level 503."""
        ctx, app = make_app(discogs_service=None)
        with ctx:
            resp = await _post(app, {"names": ["Wishy"]})

        assert resp.status_code == 200
        (result,) = resp.json()["results"]
        assert result["unresolved_reason"] == "escalation_unavailable"
        assert result["candidate_count"] is None


class TestInputCapGuard:
    """Drift guard between the manual 413 gate and the api.yaml cap."""

    def test_input_cap_resolves_from_model_json_schema(self):
        schema = ArtistResolveBulkRequest.model_json_schema()
        assert schema["properties"]["names"]["maxItems"] == _RESOLVE_INPUT_CAP
        # The #759 contract: 25 names per request (a fully-escalating batch
        # ≈ 25 API calls ≈ 30s at the shared 50/min budget; callers page).
        assert _RESOLVE_INPUT_CAP == 25

    @pytest.mark.asyncio
    async def test_413_when_over_input_cap(self, make_app, mock_entity_store):
        ctx, app = make_app()
        oversized = {"names": [f"Artist_{i}" for i in range(_RESOLVE_INPUT_CAP + 1)]}
        with ctx:
            resp = await _post(app, oversized)

        assert resp.status_code == 413
        # Cap-check fires before any DB work.
        mock_entity_store.bulk_resolve_library_names.assert_not_awaited()


class TestBodyParse:
    @pytest.mark.asyncio
    async def test_malformed_json_returns_400(self, make_app):
        ctx, app = make_app()
        with ctx:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                resp = await ac.post(
                    _ROUTE, content=b"not json {", headers={"Content-Type": "application/json"}
                )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "body",
        [{"names": "not a list"}, {}, {"names": []}],
        ids=["names-not-list", "missing-names", "empty-names"],
    )
    async def test_invalid_shapes_return_422(self, make_app, body):
        ctx, app = make_app()
        with ctx:
            resp = await _post(app, body)
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_client_disconnect_during_body_read_returns_400(self, make_app, monkeypatch):
        """Mid-body client abort is the CLIENT's failure — the arm maps it
        to 400 so error-class accounting stays clean (a dropped arm would
        surface these as unhandled 500s in Sentry)."""
        from starlette.requests import ClientDisconnect, Request

        async def _gone(self):
            raise ClientDisconnect()

        monkeypatch.setattr(Request, "json", _gone)
        ctx, app = make_app()
        with ctx:
            resp = await _post(app, {"names": ["Wishy"]})
        assert resp.status_code == 400
        assert "disconnected" in resp.json()["message"]


class TestServiceUnavailable:
    """Dependency-gate 503s carry the CONFIG-shaped detail — the wiring was
    never there, so pointing at DATABASE_URL_DISCOGS is the right hint
    (distinct from the mid-flight transient detail pinned in
    TestErrorClassRouting)."""

    @pytest.mark.asyncio
    async def test_503_when_entity_store_none(self, make_app):
        ctx, app = make_app(entity_store=None)
        with ctx:
            resp = await _post(app, {"names": ["Wishy"]})
        assert resp.status_code == 503
        assert "entity store" in resp.json()["message"].lower()

    @pytest.mark.asyncio
    async def test_503_when_discogs_cache_none(self, make_app):
        from artists.router import _DISCOGS_CACHE_UNAVAILABLE_DETAIL

        ctx, app = make_app(discogs_cache=None)
        with ctx:
            resp = await _post(app, {"names": ["Wishy"]})
        assert resp.status_code == 503
        assert resp.json()["message"] == _DISCOGS_CACHE_UNAVAILABLE_DETAIL


class TestErrorClassRouting:
    @pytest.mark.asyncio
    async def test_cache_unavailable_returns_503_with_cache_detail(
        self, make_app, mock_discogs_cache
    ):
        from artists.router import _DISCOGS_CACHE_QUERY_FAILED_DETAIL

        mock_discogs_cache.artist_equality_candidates = AsyncMock(
            side_effect=CacheUnavailableError("PG pool exhausted")
        )
        ctx, app = make_app()
        with ctx:
            resp = await _post(app, {"names": ["Wishy"]})

        assert resp.status_code == 503
        # The TRANSIENT-shaped detail, not the dependency-gate config hint —
        # the deps were wired; the query failed mid-flight.
        assert resp.json()["message"] == _DISCOGS_CACHE_QUERY_FAILED_DETAIL

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "exc",
        [
            pytest.param("postgres", id="postgres-error"),
            pytest.param("interface", id="interface-error"),
            pytest.param("oserror", id="os-error"),
        ],
    )
    async def test_entity_store_failure_returns_503_with_entity_detail(
        self, make_app, mock_entity_store, exc
    ):
        """The full transient tuple: PostgresError (server-side), asyncpg
        InterfaceError (client-side pool/connection lifecycle — subclasses
        neither PostgresError nor OSError, so a deploy-window pool teardown
        would otherwise read as an application 500), and OSError
        (socket-level resets)."""
        from asyncpg.exceptions import InterfaceError, PostgresError

        from artists.router import _ENTITY_STORE_QUERY_FAILED_DETAIL

        side_effect = {
            "postgres": PostgresError("connection reset"),
            "interface": InterfaceError("pool is closing"),
            "oserror": OSError("connection refused"),
        }[exc]
        mock_entity_store.bulk_resolve_library_names = AsyncMock(side_effect=side_effect)
        ctx, app = make_app()
        with ctx:
            resp = await _post(app, {"names": ["Wishy"]})

        assert resp.status_code == 503
        assert resp.json()["message"] == _ENTITY_STORE_QUERY_FAILED_DETAIL

    @pytest.mark.asyncio
    async def test_cancelled_error_logs_abort_and_propagates(
        self, make_app, mock_entity_store, caplog
    ):
        import logging

        mock_entity_store.bulk_resolve_library_names = AsyncMock(
            side_effect=asyncio.CancelledError()
        )
        ctx, app = make_app()
        with ctx:
            with caplog.at_level(logging.WARNING, logger="artists.router"):
                with pytest.raises((asyncio.CancelledError, RuntimeError)):
                    await _post(app, {"names": ["Wishy"]})

        abort_logs = [
            r
            for r in caplog.records
            if "aborted by client" in r.getMessage() and r.levelno == logging.WARNING
        ]
        assert abort_logs, "Expected WARNING `... aborted by client` log line"


class TestPostHogTelemetry:
    @pytest.mark.asyncio
    async def test_emits_one_aggregate_completed_event(self, make_app, mock_posthog_client):
        ctx, app = make_app(posthog=mock_posthog_client)
        with ctx:
            resp = await _post(app, {"names": ["Wishy", "wishy"], "dry_run": True})

        assert resp.status_code == 200
        # One aggregate event per request — no per-name events.
        assert mock_posthog_client.capture.call_count == 1
        kwargs = mock_posthog_client.capture.call_args.kwargs
        assert kwargs["event"] == "artist_resolve_completed"
        props = kwargs["properties"]
        assert props["names"] == 2
        assert props["deduped"] == 1
        assert props["resolved"] == 2
        assert props["not_found"] == 0
        assert props["ambiguous"] == 0
        assert props["escalation_unavailable"] == 0
        # The probe counter is service-qualified so it can't shadow the
        # framework's per-service api_calls map; the map itself carries
        # the count under the sibling *_completed slicing convention.
        assert props["discogs_api_calls"] == 1
        assert props["api_calls"] == {"discogs": 1}
        # Shape stability (LML#544): the breaker-shed counter is seeded 0
        # on every request, not present only when a shed happens.
        assert props["cache"]["discogs_breaker_open_shed"] == 0
        assert props["minted"] == 0
        assert props["dry_run"] is True

    @pytest.mark.asyncio
    async def test_capture_failure_never_breaks_the_response(self, make_app, mock_posthog_client):
        mock_posthog_client.capture.side_effect = RuntimeError("posthog down")
        ctx, app = make_app(posthog=mock_posthog_client)
        with ctx:
            resp = await _post(app, {"names": ["Wishy"]})

        assert resp.status_code == 200
        assert resp.json()["results"][0]["discogs_artist_id"] == 123
        # The emit was ATTEMPTED and its failure swallowed — without this
        # pin, silently skipping the emit would also pass.
        assert mock_posthog_client.capture.call_count == 1

    @pytest.mark.asyncio
    async def test_summary_event_carries_environment_property(
        self, make_app, mock_entity_store, mock_posthog_client, mock_settings
    ):
        """WXYC/library-metadata-lookup#1170: `artist_resolve_completed` is one
        of the `RequestTelemetry`-backed summary events the new LML PostHog
        project's guard alert needs to slice by `environment` -- same rationale
        and property name as `lookup_completed` and the `discogs_rate_gate_*`
        counters."""
        env_settings = mock_settings.model_copy(update={"environment": "unit-test-env"})
        ctx, app = make_app(posthog=mock_posthog_client, settings=env_settings)
        with ctx:
            resp = await _post(app, {"names": ["Wishy"]})

        assert resp.status_code == 200
        props = mock_posthog_client.capture.call_args.kwargs["properties"]
        assert props["environment"] == "unit-test-env"

    def test_summary_keys_cannot_shadow_framework_properties(self):
        """`_emit_resolve_summary` passes the summary straight into
        `send_to_posthog`, whose `**extra_properties` merges LAST — a
        summary key colliding with a framework property silently replaces
        it. The `api_calls` collision was fixed by renaming the field at
        its source; this pins the invariant for every future field."""
        import dataclasses

        from artists.resolver import ResolveStats

        framework_properties = {"total_duration_ms", "steps", "api_calls", "cache"}
        summary_keys = {f.name for f in dataclasses.fields(ResolveStats)} | {"dry_run"}
        collisions = summary_keys & framework_properties
        assert not collisions, (
            f"ResolveStats/summary keys shadow framework event properties: {collisions}. "
            "Rename the field (service-qualify it, like discogs_api_calls)."
        )

    @pytest.mark.asyncio
    async def test_error_exit_emits_nothing(self, make_app, mock_entity_store, mock_posthog_client):
        """Error exits deliberately emit no event — the Sentry span's
        status pins carry error observability; a 503 must not inflate the
        completed-event baseline any alert is calibrated on."""
        from asyncpg.exceptions import PostgresError

        mock_entity_store.bulk_resolve_library_names = AsyncMock(
            side_effect=PostgresError("connection reset")
        )
        ctx, app = make_app(posthog=mock_posthog_client)
        with ctx:
            resp = await _post(app, {"names": ["Wishy"]})

        assert resp.status_code == 503
        mock_posthog_client.capture.assert_not_called()


class TestInvalidNameErrorMapping:
    """The resolver's `InvalidNameError` maps to 422 — a caller error
    (pydantic's constr(min_length=1) admits the whitespace/NUL shapes the
    resolver rejects). A BARE ValueError is a server bug and must surface
    as 500 (see TestRouteEnvelopeGaps)."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "bad_name",
        ["   ", "Ol\x00ga", "(2)"],
        ids=["whitespace-only", "embedded-nul", "bare-disambiguator"],
    )
    async def test_semantically_invalid_name_returns_422(
        self, make_app, mock_entity_store, bad_name
    ):
        ctx, app = make_app()
        with ctx:
            resp = await _post(app, {"names": ["Wishy", bad_name]})

        assert resp.status_code == 422
        assert "names[1]" in resp.json()["message"]
        # Validation precedes every tier.
        mock_entity_store.bulk_resolve_library_names.assert_not_awaited()


class TestRouteEnvelopeGaps:
    """Round-1 review pins for previously untested route branches."""

    @pytest.mark.asyncio
    async def test_non_dict_json_body_returns_400(self, make_app):
        ctx, app = make_app()
        with ctx:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                resp = await ac.post(_ROUTE, json=["Wishy"])
        assert resp.status_code == 400
        assert "JSON object" in resp.json()["message"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "server_bug",
        [RuntimeError("group got no verdict"), ValueError("strict-zip drift")],
        ids=["runtime-error", "bare-value-error"],
    )
    async def test_unexpected_exception_returns_500(self, make_app, mock_entity_store, server_bug):
        """The generic arm pins 500 on the span and re-raises; a server
        bug must never dress up as a 4xx. The bare-ValueError case pins
        that the InvalidNameError arm does not over-catch — a regression
        to `except ValueError` would blame the caller (422) for our
        drift."""
        mock_entity_store.bulk_resolve_library_names = AsyncMock(side_effect=server_bug)
        ctx, app = make_app()
        with ctx:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test", timeout=5
            ) as ac:
                try:
                    resp = await ac.post(_ROUTE, json={"names": ["Wishy"]})
                    status = resp.status_code
                except (RuntimeError, ValueError):
                    # TestClient re-raises unhandled app exceptions when no
                    # server error middleware intercepts; either surface is
                    # evidence the 4xx arms did NOT swallow the bug.
                    status = 500
        assert status == 500

    @pytest.mark.asyncio
    async def test_exactly_cap_names_accepted(self, make_app):
        """25 names — the documented page size — must pass the 413 gate;
        the gate counts raw names, so 25 duplicates also pass (dedupe is
        the resolver's job)."""
        ctx, app = make_app()
        with ctx:
            resp = await _post(app, {"names": ["Wishy"] * _RESOLVE_INPUT_CAP})
        assert resp.status_code == 200
        assert len(resp.json()["results"]) == _RESOLVE_INPUT_CAP

    @pytest.mark.asyncio
    async def test_explicit_dry_run_false_mints(self, make_app, mock_entity_store):
        """A present-but-false dry_run must behave exactly like absent —
        bool coercion of the generated Optional field."""
        ctx, app = make_app()
        with ctx:
            resp = await _post(app, {"names": ["Wishy"], "dry_run": False})
        assert resp.status_code == 200
        mock_entity_store.fill_identity_discogs_id.assert_awaited_once()


class TestSentrySpanContract:
    """The documented lml.artist_resolve.* span surface — the trace
    explorer queries in docs/api-endpoints.md key off these exact names."""

    @pytest.mark.asyncio
    async def test_success_path_pins_all_documented_keys(self, make_app, recorded_span):
        ctx, app = make_app()
        with ctx:
            resp = await _post(app, {"names": ["Wishy", "wishy"], "dry_run": True})

        assert resp.status_code == 200
        for key, expected in {
            "lml.artist_resolve.names": 2,
            "lml.artist_resolve.dry_run": True,
            "lml.artist_resolve.deduped": 1,
            "lml.artist_resolve.resolved": 2,
            "lml.artist_resolve.not_found": 0,
            "lml.artist_resolve.ambiguous": 0,
            "lml.artist_resolve.escalation_unavailable": 0,
            "lml.artist_resolve.discogs_api_calls": 1,
            "lml.artist_resolve.minted": 0,
            "http.status_code": 200,
        }.items():
            assert recorded_span.get(key) == expected, f"span key {key}: {recorded_span.get(key)!r}"

    @pytest.mark.asyncio
    async def test_dep_503_pins_span_status(self, make_app, mock_discogs_cache, recorded_span):
        from discogs.cache_service import CacheUnavailableError

        mock_discogs_cache.artist_equality_candidates = AsyncMock(
            side_effect=CacheUnavailableError("PG pool exhausted")
        )
        ctx, app = make_app()
        with ctx:
            resp = await _post(app, {"names": ["Wishy"]})

        assert resp.status_code == 503
        assert recorded_span.get("http.status_code") == 503
        # names/dry_run are pinned PRE-try from the request precisely so
        # error exits still carry batch context in the trace explorer.
        assert recorded_span.get("lml.artist_resolve.names") == 1
        assert recorded_span.get("lml.artist_resolve.dry_run") is False

    @pytest.mark.asyncio
    async def test_client_abort_pins_499(self, make_app, mock_entity_store, recorded_span):
        """The documented trace-explorer query for client aborts
        (`op:http.server http.status_code:499`) depends on this pin."""
        mock_entity_store.bulk_resolve_library_names = AsyncMock(
            side_effect=asyncio.CancelledError()
        )
        ctx, app = make_app()
        with ctx:
            with pytest.raises((asyncio.CancelledError, RuntimeError)):
                await _post(app, {"names": ["Wishy"]})

        assert recorded_span.get("http.status_code") == 499
