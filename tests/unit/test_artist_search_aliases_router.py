"""Unit tests for `POST /api/v1/artists/search-aliases/bulk` router.

Exercises the FastAPI endpoint with mocked dependencies. Composer
internals are covered separately in
``test_artist_search_aliases_composer.py``; this file focuses on the
HTTP envelope: error-class routing, span instrumentation, drift
guards, and the 4xx/5xx contract surface.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from artists.router import _BULK_INPUT_CAP
from discogs.cache_service import CacheUnavailableError
from generated.api_models import ArtistSearchAliasesBulkRequest
from tests.unit.conftest import override_deps


@pytest.fixture
def mock_entity_store():
    store = AsyncMock()
    store.bulk_resolve_library_names = AsyncMock(return_value={})
    return store


@pytest.fixture
def mock_discogs_cache():
    cache = AsyncMock()
    cache.get_artist_details_bulk = AsyncMock(return_value={})
    return cache


@pytest.fixture
def app_client(mock_settings, mock_entity_store, mock_discogs_cache):
    from config.settings import get_settings
    from core.dependencies import (
        get_discogs_cache_service_from_pool,
        get_discogs_service,
        get_library_db,
        get_posthog_client,
    )
    from identity.dependencies import get_entity_store
    from main import app

    with override_deps(
        app,
        {
            get_library_db: AsyncMock(),
            get_discogs_service: None,
            get_posthog_client: None,
            get_settings: mock_settings,
            get_entity_store: mock_entity_store,
            get_discogs_cache_service_from_pool: mock_discogs_cache,
        },
    ):
        yield app


class TestErrorClassRouting:
    """Each exception class lands on the right status code + detail."""

    @pytest.mark.asyncio
    async def test_cache_unavailable_returns_503_with_cache_detail(
        self, app_client, mock_entity_store, mock_discogs_cache
    ):
        """`CacheUnavailableError` from Phase 2 → 503 with the cache-specific detail.

        Pre-fix: the route caught only (PostgresError, OSError), so a
        cache outage produced 500 instead of 503. Pin both the status
        code AND the detail string so on-call sees which subsystem
        failed.
        """
        from entity.store import Identity

        mock_entity_store.bulk_resolve_library_names = AsyncMock(
            return_value={
                "Stereolab": Identity(
                    id=1,
                    library_name="Stereolab",
                    discogs_artist_id=2154,
                    wikidata_qid=None,
                    musicbrainz_artist_id=None,
                    spotify_artist_id=None,
                    apple_music_artist_id=None,
                    bandcamp_id=None,
                    reconciliation_status="reconciled",
                ),
            }
        )
        mock_discogs_cache.get_artist_details_bulk = AsyncMock(
            side_effect=CacheUnavailableError("PG pool exhausted")
        )

        async with AsyncClient(
            transport=ASGITransport(app=app_client), base_url="http://test"
        ) as ac:
            resp = await ac.post(
                "/api/v1/artists/search-aliases/bulk", json={"names": ["Stereolab"]}
            )

        assert resp.status_code == 503
        # The TRANSIENT-shaped mid-flight detail, not the dependency-gate
        # config hint (deps were wired; the query failed).
        from artists.router import _DISCOGS_CACHE_QUERY_FAILED_DETAIL

        assert resp.json()["detail"] == _DISCOGS_CACHE_QUERY_FAILED_DETAIL

    @pytest.mark.asyncio
    async def test_entity_store_pg_failure_returns_503_with_entity_detail(
        self, app_client, mock_entity_store
    ):
        """Phase 1 raw `PostgresError` → 503 with entity-store detail."""
        from asyncpg.exceptions import PostgresError

        mock_entity_store.bulk_resolve_library_names = AsyncMock(
            side_effect=PostgresError("connection reset")
        )

        async with AsyncClient(
            transport=ASGITransport(app=app_client), base_url="http://test"
        ) as ac:
            resp = await ac.post(
                "/api/v1/artists/search-aliases/bulk", json={"names": ["Stereolab"]}
            )

        assert resp.status_code == 503
        from artists.router import _ENTITY_STORE_QUERY_FAILED_DETAIL

        assert resp.json()["detail"] == _ENTITY_STORE_QUERY_FAILED_DETAIL

    @pytest.mark.asyncio
    async def test_asyncpg_interface_error_returns_503_not_500(self, app_client, mock_entity_store):
        """asyncpg `InterfaceError` (client-side pool/connection lifecycle)
        subclasses neither PostgresError nor OSError; a deploy-window pool
        teardown must read as the transient 503, not an application 500 —
        the same pin the resolve route carries."""
        from asyncpg.exceptions import InterfaceError

        mock_entity_store.bulk_resolve_library_names = AsyncMock(
            side_effect=InterfaceError("pool is closing")
        )

        async with AsyncClient(
            transport=ASGITransport(app=app_client), base_url="http://test"
        ) as ac:
            resp = await ac.post(
                "/api/v1/artists/search-aliases/bulk", json={"names": ["Stereolab"]}
            )

        assert resp.status_code == 503
        from artists.router import _ENTITY_STORE_QUERY_FAILED_DETAIL

        assert resp.json()["detail"] == _ENTITY_STORE_QUERY_FAILED_DETAIL

    @pytest.mark.asyncio
    async def test_cancelled_error_logs_abort_and_propagates(
        self, app_client, mock_entity_store, caplog
    ):
        """`asyncio.CancelledError` produces the abort log line and propagates.

        F3 fix: when the client disconnects mid-compose, the route logs
        a WARNING (`artist-search-alias bulk aborted by client`) so the
        Sentry/Railway audit trail surfaces aborts, then re-raises the
        CancelledError (per the asyncio cancellation contract — the
        client is gone, no HTTPException to send). Starlette converts
        the propagated CancelledError into a 'No response returned'
        RuntimeError at the ASGI boundary; both forms are evidence the
        handler took the right branch.
        """
        import logging

        mock_entity_store.bulk_resolve_library_names = AsyncMock(
            side_effect=asyncio.CancelledError()
        )

        async with AsyncClient(
            transport=ASGITransport(app=app_client), base_url="http://test"
        ) as ac:
            with caplog.at_level(logging.WARNING, logger="artists.router"):
                with pytest.raises((asyncio.CancelledError, RuntimeError)):
                    await ac.post(
                        "/api/v1/artists/search-aliases/bulk",
                        json={"names": ["Stereolab"]},
                    )

        abort_logs = [
            r
            for r in caplog.records
            if "aborted by client" in r.getMessage() and r.levelno == logging.WARNING
        ]
        assert abort_logs, "Expected WARNING `... aborted by client` log line"


class TestInputCapGuard:
    """Drift guard between the manual 413 gate and the api.yaml cap."""

    def test_input_cap_resolves_from_model_json_schema(self):
        """`_resolve_input_cap()` reads `maxItems` from the model's JSON schema.

        If api.yaml's `maxItems` for `names` ever changes, codegen
        updates the Pydantic model's constraint and the route's 413
        gate must follow automatically. This pins that mechanism via
        the same JSON-schema export the route uses (Pydantic's
        documented public API).
        """
        schema = ArtistSearchAliasesBulkRequest.model_json_schema()
        max_items = schema["properties"]["names"]["maxItems"]
        # The gate uses the same source — and the literal is pinned so an
        # api.yaml copy-paste that shrinks THIS schema's cap (the resolve
        # sibling is 25) can't silently 413 the alias-consumer ETL.
        assert max_items == _BULK_INPUT_CAP == 1000

    @pytest.mark.asyncio
    async def test_413_when_over_input_cap(self, app_client, mock_entity_store):
        """Over-cap names → 413, not 422 (per api.yaml contract)."""
        oversized = {"names": [f"Artist_{i}" for i in range(_BULK_INPUT_CAP + 1)]}

        async with AsyncClient(
            transport=ASGITransport(app=app_client), base_url="http://test"
        ) as ac:
            resp = await ac.post("/api/v1/artists/search-aliases/bulk", json=oversized)

        assert resp.status_code == 413
        # Cap-check fires before any DB work.
        mock_entity_store.bulk_resolve_library_names.assert_not_awaited()


class TestBodyParse:
    """JSON body shape errors land on 400/422 rather than 500."""

    @pytest.mark.asyncio
    async def test_malformed_json_returns_400(self, app_client, mock_entity_store):
        """Non-JSON body → 400."""
        async with AsyncClient(
            transport=ASGITransport(app=app_client), base_url="http://test"
        ) as ac:
            resp = await ac.post(
                "/api/v1/artists/search-aliases/bulk",
                content=b"not json {",
                headers={"Content-Type": "application/json"},
            )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_names_not_a_list_returns_422(self, app_client, mock_entity_store):
        """`names` of wrong type → 422."""
        async with AsyncClient(
            transport=ASGITransport(app=app_client), base_url="http://test"
        ) as ac:
            resp = await ac.post(
                "/api/v1/artists/search-aliases/bulk", json={"names": "not a list"}
            )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_missing_names_returns_422(self, app_client, mock_entity_store):
        """No `names` key → 422."""
        async with AsyncClient(
            transport=ASGITransport(app=app_client), base_url="http://test"
        ) as ac:
            resp = await ac.post("/api/v1/artists/search-aliases/bulk", json={})
        assert resp.status_code == 422


class TestServiceUnavailable:
    """503 paths when DI deps resolve to None."""

    @pytest.mark.asyncio
    async def test_503_when_entity_store_none(self, mock_settings, mock_discogs_cache):
        from config.settings import get_settings
        from core.dependencies import (
            get_discogs_cache_service_from_pool,
            get_discogs_service,
            get_library_db,
            get_posthog_client,
        )
        from identity.dependencies import get_entity_store
        from main import app

        with override_deps(
            app,
            {
                get_library_db: AsyncMock(),
                get_discogs_service: None,
                get_posthog_client: None,
                get_settings: mock_settings,
                get_entity_store: None,
                get_discogs_cache_service_from_pool: mock_discogs_cache,
            },
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                resp = await ac.post(
                    "/api/v1/artists/search-aliases/bulk", json={"names": ["Stereolab"]}
                )

        assert resp.status_code == 503
        from artists.router import _ENTITY_STORE_UNAVAILABLE_DETAIL

        assert resp.json()["detail"] == _ENTITY_STORE_UNAVAILABLE_DETAIL

    @pytest.mark.asyncio
    async def test_503_when_discogs_cache_none(self, mock_settings, mock_entity_store):
        from config.settings import get_settings
        from core.dependencies import (
            get_discogs_cache_service_from_pool,
            get_discogs_service,
            get_library_db,
            get_posthog_client,
        )
        from identity.dependencies import get_entity_store
        from main import app

        with override_deps(
            app,
            {
                get_library_db: AsyncMock(),
                get_discogs_service: None,
                get_posthog_client: None,
                get_settings: mock_settings,
                get_entity_store: mock_entity_store,
                get_discogs_cache_service_from_pool: None,
            },
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                resp = await ac.post(
                    "/api/v1/artists/search-aliases/bulk", json={"names": ["Stereolab"]}
                )

        assert resp.status_code == 503
        from artists.router import _DISCOGS_CACHE_UNAVAILABLE_DETAIL

        assert resp.json()["detail"] == _DISCOGS_CACHE_UNAVAILABLE_DETAIL


class TestDuplicateInputDedup:
    """Duplicate input names produce one result entry, not fan-out."""

    @pytest.mark.asyncio
    async def test_duplicate_inputs_deduped(
        self, app_client, mock_entity_store, mock_discogs_cache
    ):
        """names=['Stereolab', 'Stereolab', 'Stereolab'] → one ArtistSearchAliasesResult."""
        from entity.store import Identity

        mock_entity_store.bulk_resolve_library_names = AsyncMock(
            return_value={
                "Stereolab": Identity(
                    id=1,
                    library_name="Stereolab",
                    discogs_artist_id=None,
                    wikidata_qid=None,
                    musicbrainz_artist_id=None,
                    spotify_artist_id=None,
                    apple_music_artist_id=None,
                    bandcamp_id=None,
                    reconciliation_status="reconciled",
                ),
            }
        )

        async with AsyncClient(
            transport=ASGITransport(app=app_client), base_url="http://test"
        ) as ac:
            resp = await ac.post(
                "/api/v1/artists/search-aliases/bulk",
                json={"names": ["Stereolab", "Stereolab", "Stereolab"]},
            )

        assert resp.status_code == 200
        data = resp.json()
        # First-occurrence dedup: one result, the unique-names bundle is what Phase 1 saw.
        assert len(data["artists"]) == 1
        assert data["artists"][0]["name"] == "Stereolab"
        # Phase 1 received the deduped list.
        passed_names = mock_entity_store.bulk_resolve_library_names.await_args[0][0]
        assert passed_names == ["Stereolab"]
