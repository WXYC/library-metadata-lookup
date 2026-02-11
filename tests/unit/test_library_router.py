"""Unit tests for library/router.py."""

from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from library.db import LibraryDB
from library.models import LibraryItem


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

    app.dependency_overrides[get_library_db] = lambda: mock_db
    app.dependency_overrides[get_discogs_service] = lambda: None
    app.dependency_overrides[get_posthog_client] = lambda: None
    app.dependency_overrides[get_settings] = lambda: mock_settings
    yield app
    app.dependency_overrides.clear()


class TestSearchLibrary:
    @pytest.mark.asyncio
    async def test_query_search(self, app_client, mock_db):
        item = LibraryItem(
            id=1, title="The Game", artist="Queen",
            call_letters="Q", artist_call_number=1, release_call_number=1,
            genre="Rock", format="CD",
        )
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
        item = LibraryItem(
            id=2, title="Album", artist="Radiohead",
            call_letters="R", artist_call_number=1, release_call_number=1,
            genre="Rock", format="CD",
        )
        mock_db.search = AsyncMock(return_value=[item])

        async with AsyncClient(
            transport=ASGITransport(app=app_client), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/library/search", params={"artist": "Radiohead"})

        assert resp.status_code == 200
        assert resp.json()["total"] == 1

    @pytest.mark.asyncio
    async def test_title_filter(self, app_client, mock_db):
        item = LibraryItem(
            id=3, title="OK Computer", artist="Radiohead",
            call_letters="R", artist_call_number=1, release_call_number=1,
            genre="Rock", format="CD",
        )
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
