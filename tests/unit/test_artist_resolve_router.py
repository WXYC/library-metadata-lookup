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
from discogs.cache_service import ArtistEqualityCandidates, CacheUnavailableError
from discogs.models import DiscogsArtistSearchResult
from generated.api_models import ArtistResolveBulkRequest
from tests.unit.conftest import override_deps

_ROUTE = "/api/v1/artists/resolve/bulk"


@pytest.fixture
def mock_entity_store():
    store = AsyncMock()
    store.bulk_resolve_library_names = AsyncMock(return_value={})
    store.upsert_identity = AsyncMock(return_value=MagicMock())
    return store


@pytest.fixture
def mock_discogs_cache():
    cache = AsyncMock()

    async def _equality(forms):
        return {form: ArtistEqualityCandidates() for form in forms}

    async def _trigram(names, **kwargs):
        return {name: set() for name in names}

    cache.artist_equality_candidates = AsyncMock(side_effect=_equality)
    cache.artist_trigram_candidates = AsyncMock(side_effect=_trigram)
    return cache


@pytest.fixture
def mock_discogs_service():
    service = AsyncMock()
    service.search_artists = AsyncMock(
        return_value=[DiscogsArtistSearchResult(artist_id=123, title="Wishy")]
    )
    return service


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

    def _make(*, discogs_service=_UNSET, entity_store=_UNSET, discogs_cache=_UNSET, posthog=None):
        deps = {
            get_library_db: AsyncMock(),
            get_discogs_service: (
                mock_discogs_service if discogs_service is _UNSET else discogs_service
            ),
            get_posthog_client: None,
            get_artist_resolve_posthog_client: posthog,
            get_settings: mock_settings,
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
        assert "candidate_count" in result
        assert result["candidate_count"] == 1
        mock_entity_store.upsert_identity.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_dry_run_skips_write_back(self, make_app, mock_entity_store):
        ctx, app = make_app()
        with ctx:
            resp = await _post(app, {"names": ["Wishy"], "dry_run": True})

        assert resp.status_code == 200
        assert resp.json()["results"][0]["discogs_artist_id"] == 123
        mock_entity_store.upsert_identity.assert_not_awaited()

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


class TestServiceUnavailable:
    @pytest.mark.asyncio
    async def test_503_when_entity_store_none(self, make_app):
        ctx, app = make_app(entity_store=None)
        with ctx:
            resp = await _post(app, {"names": ["Wishy"]})
        assert resp.status_code == 503
        assert "Entity store" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_503_when_discogs_cache_none(self, make_app):
        ctx, app = make_app(discogs_cache=None)
        with ctx:
            resp = await _post(app, {"names": ["Wishy"]})
        assert resp.status_code == 503
        assert "Discogs cache" in resp.json()["detail"]


class TestErrorClassRouting:
    @pytest.mark.asyncio
    async def test_cache_unavailable_returns_503_with_cache_detail(
        self, make_app, mock_discogs_cache
    ):
        mock_discogs_cache.artist_equality_candidates = AsyncMock(
            side_effect=CacheUnavailableError("PG pool exhausted")
        )
        ctx, app = make_app()
        with ctx:
            resp = await _post(app, {"names": ["Wishy"]})

        assert resp.status_code == 503
        assert "Discogs cache" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_entity_store_pg_failure_returns_503_with_entity_detail(
        self, make_app, mock_entity_store
    ):
        from asyncpg.exceptions import PostgresError

        mock_entity_store.bulk_resolve_library_names = AsyncMock(
            side_effect=PostgresError("connection reset")
        )
        ctx, app = make_app()
        with ctx:
            resp = await _post(app, {"names": ["Wishy"]})

        assert resp.status_code == 503
        assert "Entity store" in resp.json()["detail"]

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
    async def test_emits_one_aggregate_completed_event(self, make_app):
        posthog = MagicMock()
        ctx, app = make_app(posthog=posthog)
        with ctx:
            resp = await _post(app, {"names": ["Wishy", "wishy"], "dry_run": True})

        assert resp.status_code == 200
        # One aggregate event per request — no per-name events.
        assert posthog.capture.call_count == 1
        kwargs = posthog.capture.call_args.kwargs
        assert kwargs["event"] == "artist_resolve_completed"
        props = kwargs["properties"]
        assert props["names"] == 2
        assert props["deduped"] == 1
        assert props["resolved"] == 2
        assert props["not_found"] == 0
        assert props["ambiguous"] == 0
        assert props["escalation_unavailable"] == 0
        assert props["api_calls"] == 1
        assert props["minted"] == 0
        assert props["dry_run"] is True

    @pytest.mark.asyncio
    async def test_capture_failure_never_breaks_the_response(self, make_app):
        posthog = MagicMock()
        posthog.capture.side_effect = RuntimeError("posthog down")
        ctx, app = make_app(posthog=posthog)
        with ctx:
            resp = await _post(app, {"names": ["Wishy"]})

        assert resp.status_code == 200
        assert resp.json()["results"][0]["discogs_artist_id"] == 123


class TestResolverValueErrorMapping:
    """The resolver's input-validation ValueError maps to 422 — a caller
    error, never a 500 (pydantic's constr(min_length=1) admits the
    whitespace/NUL shapes the resolver rejects)."""

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
        assert "names[1]" in resp.json()["detail"]
        # Validation precedes every tier.
        mock_entity_store.bulk_resolve_library_names.assert_not_awaited()


class TestInterfaceErrorMapping:
    @pytest.mark.asyncio
    async def test_asyncpg_interface_error_returns_503_not_500(self, make_app, mock_entity_store):
        """asyncpg's client-side pool/connection errors subclass neither
        PostgresError nor OSError; a deploy-window pool teardown must
        read as the transient 503, not an application 500."""
        from asyncpg.exceptions import InterfaceError

        mock_entity_store.bulk_resolve_library_names = AsyncMock(
            side_effect=InterfaceError("pool is closing")
        )
        ctx, app = make_app()
        with ctx:
            resp = await _post(app, {"names": ["Wishy"]})

        assert resp.status_code == 503
        assert "Entity store" in resp.json()["detail"]
