"""Unit tests for the app-level error-payload normalization (LML#771).

Every LML route used to emit FastAPI's default `{"detail": ...}` error body,
while wxyc-shared's `api.yaml` `ApiErrorResponse` schema promises
`{"message": ..., "code": ..., "details": ...}`. `main.py` registers a
`StarletteHTTPException` handler and a `RequestValidationError` handler that
reshape every route's error body to the `ApiErrorResponse` contract without
per-route edits (DECISION LOCKED on #771: Option A, no api.yaml change).

This file pins the shape for one representative route per status class
(400 / 413 / 422 / 503) plus the 499 client-disconnect non-regression guard.
Route-specific status/detail-string assertions stay in each route's own test
file; this file only asserts the *envelope* the handlers produce.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from artists.router import _GENRES_INPUT_CAP
from config.settings import get_settings
from core.dependencies import get_discogs_cache_service_from_pool, get_discogs_service
from discogs.cache_service import DiscogsCacheService
from tests.unit.conftest import override_deps

_GENRES_ROUTE = "/api/v1/artists/genres/bulk"


@pytest.fixture
def mock_cache():
    cache = AsyncMock(spec=DiscogsCacheService)
    cache.aggregate_artist_genre_style = AsyncMock(return_value=({}, {}))
    cache.get_artist_details_bulk = AsyncMock(return_value={})
    return cache


@pytest.fixture
def app_client(mock_settings, mock_cache):
    from main import app

    with override_deps(
        app,
        {
            get_settings: mock_settings,
            get_discogs_cache_service_from_pool: mock_cache,
            get_discogs_service: None,
        },
    ):
        yield app


async def _post(app, path, *, json_body=None, content=None):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        if content is not None:
            return await ac.post(
                path, content=content, headers={"Content-Type": "application/json"}
            )
        return await ac.post(path, json=json_body)


def _assert_envelope(body: dict) -> None:
    """Every normalized error body carries `message` and never a top-level `detail`."""
    assert "detail" not in body
    assert isinstance(body["message"], str) and body["message"]


class TestHttpExceptionEnvelope:
    """Plain-string `HTTPException(detail=...)` -> `{"message": <that string>}`."""

    @pytest.mark.asyncio
    async def test_400_malformed_json_body(self, app_client):
        resp = await _post(app_client, _GENRES_ROUTE, content=b"not json {")

        assert resp.status_code == 400
        body = resp.json()
        _assert_envelope(body)
        assert "Malformed JSON body" in body["message"]

    @pytest.mark.asyncio
    async def test_413_batch_over_cap(self, app_client):
        artists = [{"artist_name": f"Artist {i}"} for i in range(_GENRES_INPUT_CAP + 1)]
        resp = await _post(app_client, _GENRES_ROUTE, json_body={"artists": artists})

        assert resp.status_code == 413
        body = resp.json()
        _assert_envelope(body)
        assert "cap" in body["message"].lower()

    @pytest.mark.asyncio
    async def test_503_cache_unavailable(self, mock_settings):
        # Force the dependency-gate 503: discogs_cache is None.
        from main import app

        with override_deps(
            app,
            {
                get_settings: mock_settings,
                get_discogs_cache_service_from_pool: None,
                get_discogs_service: None,
            },
        ):
            resp = await _post(
                app, _GENRES_ROUTE, json_body={"artists": [{"artist_name": "Stereolab"}]}
            )

        assert resp.status_code == 503
        body = resp.json()
        _assert_envelope(body)

    @pytest.mark.asyncio
    async def test_422_structured_bulk_validation_error_preserved_under_details(self, app_client):
        """A bulk route's hand-raised `HTTPException(detail=e.errors())` (a
        Pydantic `errors()` list, from `core/bulk_body.py::parse_bulk_body`)
        must not be flattened to an opaque string (#767 regression guard) —
        it stays a machine-readable list under `details.detail`.
        """
        resp = await _post(app_client, _GENRES_ROUTE, json_body={"artists": []})

        assert resp.status_code == 422
        body = resp.json()
        _assert_envelope(body)
        assert body["message"] == "Request failed"
        errors = body["details"]["detail"]
        assert isinstance(errors, list) and errors
        assert all("type" in e and "loc" in e for e in errors)

    @pytest.mark.asyncio
    async def test_499_client_disconnect_status_unchanged(self, app_client):
        """A client-disconnect 499 (raised as a plain `HTTPException`) keeps
        its status code — only the body shape changes, matching every other
        route. Genuine mid-flight cancellation (`asyncio.CancelledError`) is a
        `BaseException`, so it is never caught by either handler and still
        propagates unconverted.
        """

        async def _always_disconnected(self):
            return True

        # is_disconnected is read off the raw Starlette Request, not a DI seam,
        # so patch it directly rather than through dependency_overrides.
        from starlette.requests import Request as StarletteRequest

        original = StarletteRequest.is_disconnected
        StarletteRequest.is_disconnected = _always_disconnected
        try:
            resp = await _post(
                app_client, _GENRES_ROUTE, json_body={"artists": [{"artist_name": "Stereolab"}]}
            )
        finally:
            StarletteRequest.is_disconnected = original

        assert resp.status_code == 499


class TestRequestValidationErrorEnvelope:
    """Framework-level (`Query`/typed-`Body`) validation -> the 422 handler."""

    @pytest.mark.asyncio
    async def test_422_framework_validation_error(self, mock_settings):
        from core.dependencies import get_discogs_service, get_library_db, get_posthog_client
        from identity.dependencies import get_entity_store
        from main import app

        with override_deps(
            app,
            {
                get_library_db: AsyncMock(),
                get_discogs_service: None,
                get_posthog_client: None,
                get_settings: mock_settings,
                get_entity_store: AsyncMock(),
            },
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                resp = await ac.get("/identity/resolve")  # missing required `name` query param

        assert resp.status_code == 422
        body = resp.json()
        _assert_envelope(body)
        assert body["message"] == "Request validation failed"
        errors = body["details"]["errors"]
        assert isinstance(errors, list) and errors
        assert errors[0]["loc"] == ["query", "name"]
