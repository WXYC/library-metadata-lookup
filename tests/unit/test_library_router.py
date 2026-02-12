"""Unit tests for library/router.py."""

from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from library.db import LibraryDB
from tests.factories import make_library_item
from tests.unit.conftest import override_deps


@pytest.fixture
def mock_db():
    db = AsyncMock(spec=LibraryDB)
    db.search = AsyncMock(return_value=[])
    return db


@pytest.fixture
def app_client(mock_db, mock_settings):
    from main import app
    from core.dependencies import get_library_db, get_discogs_service, get_posthog_client
    from config.settings import get_settings

    with override_deps(app, {
        get_library_db: mock_db, get_discogs_service: None,
        get_posthog_client: None, get_settings: mock_settings,
    }):
        yield app


class TestSearchLibrary:
    @pytest.mark.asyncio
    async def test_query_search(self, app_client, mock_db):
        item = make_library_item(id=1, artist="Queen", title="The Game", call_letters="Q")
        mock_db.search = AsyncMock(return_value=[item])

        async with AsyncClient(
            transport=ASGITransport(app=app_client), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/library/search", params={"q": "Queen"})

        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert body["results"][0]["artist"] == "Queen"

    @pytest.mark.asyncio
    async def test_artist_filter(self, app_client, mock_db):
        item = make_library_item(id=2, artist="Radiohead", title="Album", call_letters="R")
        mock_db.search = AsyncMock(return_value=[item])

        async with AsyncClient(
            transport=ASGITransport(app=app_client), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/library/search", params={"artist": "Radiohead"})

        assert resp.status_code == 200
        assert resp.json()["total"] == 1

    @pytest.mark.asyncio
    async def test_title_filter(self, app_client, mock_db):
        item = make_library_item(id=3, artist="Radiohead", title="OK Computer", call_letters="R")
        mock_db.search = AsyncMock(return_value=[item])

        async with AsyncClient(
            transport=ASGITransport(app=app_client), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/api/v1/library/search", params={"title": "OK Computer"}
            )

        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_no_params_returns_400(self, app_client):
        async with AsyncClient(
            transport=ASGITransport(app=app_client), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/library/search")

        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_search_error_returns_500(self, app_client, mock_db):
        mock_db.search = AsyncMock(side_effect=Exception("db error"))

        async with AsyncClient(
            transport=ASGITransport(app=app_client), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/library/search", params={"q": "test"})

        assert resp.status_code == 500
