"""Unit tests for identity/router.py REST endpoints."""

from unittest.mock import AsyncMock

import asyncpg
import pytest
from httpx import ASGITransport, AsyncClient

from tests.unit.conftest import override_deps


@pytest.fixture
def mock_entity_store():
    """Mock EntityStore with async methods."""
    store = AsyncMock()
    return store


@pytest.fixture
def app_client(mock_settings, mock_entity_store):
    from config.settings import get_settings
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
            get_entity_store: mock_entity_store,
        },
    ):
        yield app


def _make_identity_record(
    library_name: str,
    *,
    id: int = 1,
    discogs_artist_id: int | None = 2154,
    wikidata_qid: str | None = "Q484464",
    musicbrainz_artist_id: str | None = "d4133898-91ea-48ea-8820-1b85825901fe",
    spotify_artist_id: str | None = "1p6GVMFhLhSrRE7qgy8aAS",
    apple_music_artist_id: str | None = "5765873",
    bandcamp_id: str | None = None,
    reconciliation_status: str = "reconciled",
):
    """Build a mock Identity dataclass-like object."""
    from entity.store import Identity

    return Identity(
        id=id,
        library_name=library_name,
        discogs_artist_id=discogs_artist_id,
        wikidata_qid=wikidata_qid,
        musicbrainz_artist_id=musicbrainz_artist_id,
        spotify_artist_id=spotify_artist_id,
        apple_music_artist_id=apple_music_artist_id,
        bandcamp_id=bandcamp_id,
        reconciliation_status=reconciliation_status,
    )


class TestResolveEndpoint:
    @pytest.mark.asyncio
    async def test_resolve_found(self, app_client, mock_entity_store):
        """GET /identity/resolve?name=Stereolab returns 200 with full identity."""
        mock_entity_store.get_identity.return_value = _make_identity_record(
            "Stereolab",
            discogs_artist_id=2154,
            wikidata_qid="Q484464",
        )

        async with AsyncClient(
            transport=ASGITransport(app=app_client), base_url="http://test"
        ) as ac:
            resp = await ac.get("/identity/resolve", params={"name": "Stereolab"})

        assert resp.status_code == 200
        data = resp.json()
        assert data["library_name"] == "Stereolab"
        assert data["discogs_artist_id"] == 2154
        assert data["wikidata_qid"] == "Q484464"
        assert data["reconciliation_status"] == "reconciled"
        mock_entity_store.get_identity.assert_awaited_once_with("Stereolab")

    @pytest.mark.asyncio
    async def test_resolve_not_found(self, app_client, mock_entity_store):
        """GET /identity/resolve?name=Nonexistent returns 404."""
        mock_entity_store.get_identity.return_value = None

        async with AsyncClient(
            transport=ASGITransport(app=app_client), base_url="http://test"
        ) as ac:
            resp = await ac.get("/identity/resolve", params={"name": "Nonexistent"})

        assert resp.status_code == 404
        assert "Nonexistent" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_resolve_missing_param(self, app_client):
        """GET /identity/resolve without name param returns 422."""
        async with AsyncClient(
            transport=ASGITransport(app=app_client), base_url="http://test"
        ) as ac:
            resp = await ac.get("/identity/resolve")

        assert resp.status_code == 422


class TestBulkEndpoint:
    @pytest.mark.asyncio
    async def test_bulk_resolve(self, app_client, mock_entity_store):
        """POST /identity/bulk resolves multiple names, separating found and unresolved."""
        autechre = _make_identity_record("Autechre", id=1, discogs_artist_id=12)
        stereolab = _make_identity_record("Stereolab", id=2, discogs_artist_id=2154)

        async def mock_get_identity(name: str):
            mapping = {"Autechre": autechre, "Stereolab": stereolab}
            return mapping.get(name)

        mock_entity_store.get_identity.side_effect = mock_get_identity

        async with AsyncClient(
            transport=ASGITransport(app=app_client), base_url="http://test"
        ) as ac:
            resp = await ac.post(
                "/identity/bulk",
                json={"names": ["Autechre", "Stereolab", "Unknown Artist"]},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert len(data["identities"]) == 2
        assert data["unresolved"] == ["Unknown Artist"]
        names = {i["library_name"] for i in data["identities"]}
        assert names == {"Autechre", "Stereolab"}

    @pytest.mark.asyncio
    async def test_bulk_empty_list(self, app_client, mock_entity_store):
        """POST /identity/bulk with empty names list returns empty results."""
        async with AsyncClient(
            transport=ASGITransport(app=app_client), base_url="http://test"
        ) as ac:
            resp = await ac.post("/identity/bulk", json={"names": []})

        assert resp.status_code == 200
        data = resp.json()
        assert data["identities"] == []
        assert data["unresolved"] == []

    @pytest.mark.asyncio
    async def test_bulk_large_batch(self, app_client, mock_entity_store):
        """POST /identity/bulk handles 1000 names without error."""
        mock_entity_store.get_identity.return_value = None
        names = [f"Artist_{i}" for i in range(1000)]

        async with AsyncClient(
            transport=ASGITransport(app=app_client), base_url="http://test"
        ) as ac:
            resp = await ac.post("/identity/bulk", json={"names": names})

        assert resp.status_code == 200
        data = resp.json()
        assert len(data["unresolved"]) == 1000

    @pytest.mark.asyncio
    async def test_bulk_all_found(self, app_client, mock_entity_store):
        """POST /identity/bulk where all names resolve returns no unresolved."""
        mock_entity_store.get_identity.side_effect = lambda name: _make_identity_record(name)

        async with AsyncClient(
            transport=ASGITransport(app=app_client), base_url="http://test"
        ) as ac:
            resp = await ac.post(
                "/identity/bulk",
                json={"names": ["Autechre", "Stereolab"]},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert len(data["identities"]) == 2
        assert data["unresolved"] == []


class TestEntityStoreUnavailable:
    @pytest.mark.asyncio
    async def test_resolve_returns_503_when_store_unavailable(self, mock_settings):
        """GET /identity/resolve returns 503 when entity store is not configured."""
        from config.settings import get_settings
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
                get_entity_store: None,
            },
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                resp = await ac.get("/identity/resolve", params={"name": "Stereolab"})

        assert resp.status_code == 503
        assert "entity store" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_resolve_returns_503_when_pg_raises_undefined_table(
        self, app_client, mock_entity_store
    ):
        """A mid-request asyncpg.UndefinedTableError maps to 503, not 500."""
        mock_entity_store.get_identity.side_effect = asyncpg.UndefinedTableError(
            'relation "entity.identity" does not exist'
        )

        async with AsyncClient(
            transport=ASGITransport(app=app_client), base_url="http://test"
        ) as ac:
            resp = await ac.get("/identity/resolve", params={"name": "Stereolab"})

        assert resp.status_code == 503
        assert "entity store" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_resolve_returns_503_when_pg_unreachable(self, app_client, mock_entity_store):
        """A mid-request OSError (PG host unreachable) maps to 503, not 500."""
        mock_entity_store.get_identity.side_effect = OSError("connection refused")

        async with AsyncClient(
            transport=ASGITransport(app=app_client), base_url="http://test"
        ) as ac:
            resp = await ac.get("/identity/resolve", params={"name": "Stereolab"})

        assert resp.status_code == 503

    @pytest.mark.asyncio
    async def test_bulk_returns_503_when_pg_fails_midway(self, app_client, mock_entity_store):
        """Bulk endpoint returns 503 (not partial 200) when PG dies mid-request.

        Distinguishing 'this name had no identity' (correctly in unresolved[]) from
        'this name was never tried because PG died' is impossible from a partial
        response — fail closed instead.
        """
        good = _make_identity_record("Autechre", id=1, discogs_artist_id=12)

        async def flaky_get_identity(name: str):
            if name == "Autechre":
                return good
            raise asyncpg.PostgresConnectionError("server closed the connection unexpectedly")

        mock_entity_store.get_identity.side_effect = flaky_get_identity

        async with AsyncClient(
            transport=ASGITransport(app=app_client), base_url="http://test"
        ) as ac:
            resp = await ac.post(
                "/identity/bulk",
                json={"names": ["Autechre", "Stereolab"]},
            )

        assert resp.status_code == 503
        assert "entity store" in resp.json()["detail"].lower()
