"""Unit tests for lookup/router.py."""

from unittest.mock import AsyncMock, Mock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from discogs.service import DiscogsService
from library.db import LibraryDB
from lookup.models import LookupResponse, LookupResultItem
from tests.factories import LOOKUP_BODY, make_catalog_item, make_match_result
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
            patch("lookup.router.get_cache_stats", return_value={"api_calls": 1}),
            patch("lookup.router.sentry_sdk.get_current_scope", return_value=mock_scope),
        ):
            mock_lookup.return_value = response
            async with AsyncClient(
                transport=ASGITransport(app=app_client), base_url="http://test"
            ) as client:
                resp = await client.post("/api/v1/lookup", json=LOOKUP_BODY)

        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_cache_stats_projection_skips_non_numeric(self, app_client):
        """Non-numeric values in cache_stats are skipped (defensive — current shape is all numeric)."""
        response = LookupResponse(results=[], search_type="direct")
        stats = {"api_calls": 2, "weird_string": "nope", "weird_none": None}
        mock_transaction = Mock()
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
                await client.post("/api/v1/lookup", json=LOOKUP_BODY)

        actual_calls = {c.args[0]: c.args[1] for c in mock_transaction.set_data.call_args_list}
        assert actual_calls == {"lml.cache.api_calls": 2}

    @pytest.mark.asyncio
    async def test_cache_stats_projection_called_with_raw_get_cache_stats_return(self, app_client):
        """The projection helper must be invoked with the exact object returned by
        get_cache_stats(), not with response.cache_stats. Once wxyc-shared#86 ships
        a typed CacheStats Pydantic model, assigning a dict to response.cache_stats
        will auto-convert it into a model instance (whose `.items()` method does not
        exist), and the projection would AttributeError. Reading from the raw return
        value of get_cache_stats() insulates the projection from that future shape change.
        """
        stats = {"api_calls": 4, "pg_hits": 2}
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
            "returned by get_cache_stats(), not with response.cache_stats (which may "
            "be coerced into a typed Pydantic model in the future)."
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
            patch("lookup.router.get_cache_stats", return_value={"api_calls": 1}),
            patch("lookup.router.sentry_sdk.get_current_scope", return_value=mock_scope),
        ):
            mock_lookup.return_value = response
            async with AsyncClient(
                transport=ASGITransport(app=app_client), base_url="http://test"
            ) as client:
                resp = await client.post("/api/v1/lookup", json=LOOKUP_BODY)

        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_cache_stats_projection_handles_non_dict_object_gracefully(self, app_client):
        """End-to-end forward-compat: when get_cache_stats() returns a model-like
        object (no `.items()`), the projection must not break the request. This is
        the live-fire version of the future-bug from wxyc-shared#86: the projection's
        try/except must catch the AttributeError so the request still 200s.
        """
        from dataclasses import dataclass

        @dataclass
        class FakeCacheStatsModel:
            # Mimics a Pydantic v2 BaseModel: attribute access works, no `.items()`.
            api_calls: int = 7
            pg_hits: int = 3

        fake_stats = FakeCacheStatsModel()
        response = LookupResponse(results=[], search_type="direct")
        mock_transaction = Mock()
        mock_scope = Mock()
        mock_scope.transaction = mock_transaction

        with (
            patch("lookup.router.perform_lookup", new_callable=AsyncMock) as mock_lookup,
            patch("lookup.router.get_cache_stats", return_value=fake_stats),
            patch("lookup.router.sentry_sdk.get_current_scope", return_value=mock_scope),
        ):
            mock_lookup.return_value = response
            async with AsyncClient(
                transport=ASGITransport(app=app_client), base_url="http://test"
            ) as client:
                resp = await client.post("/api/v1/lookup", json=LOOKUP_BODY)

        assert resp.status_code == 200

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
        assert library_item["library_url"] == "http://www.wxyc.info/wxycdb/libraryRelease?id=10"
