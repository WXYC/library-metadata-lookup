"""Unit tests for `POST /api/v1/identity/bulk-resolve-libraries`.

Exercises the FastAPI endpoint with mocked dependencies. Composition
internals are covered separately in `test_bulk_resolve_composer.py`.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from scripts.entity_resolution.store import Identity
from tests.unit.conftest import override_deps


@pytest.fixture
def mock_entity_store():
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


def _identity(
    library_name: str,
    *,
    id: int = 1,
    discogs_artist_id: int | None = None,
    wikidata_qid: str | None = None,
) -> Identity:
    return Identity(
        id=id,
        library_name=library_name,
        discogs_artist_id=discogs_artist_id,
        wikidata_qid=wikidata_qid,
        musicbrainz_artist_id=None,
        spotify_artist_id=None,
        apple_music_artist_id=None,
        bandcamp_id=None,
        reconciliation_status="reconciled",
    )


class TestBulkResolveLibrariesEndpoint:
    @pytest.mark.asyncio
    async def test_single_artist_returns_main_and_provenance(self, app_client, mock_entity_store):
        """A populated identity → kind=single_artist with ReconciledIdentity main."""
        mock_entity_store.get_identity_canonical.return_value = _identity(
            "Stereolab", id=1, discogs_artist_id=2154, wikidata_qid="Q484464"
        )
        mock_entity_store.get_latest_provenance_by_source.return_value = {}

        async with AsyncClient(
            transport=ASGITransport(app=app_client), base_url="http://test"
        ) as ac:
            resp = await ac.post(
                "/api/v1/identity/bulk-resolve-libraries",
                json={
                    "inputs": [
                        {
                            "library_id": 1234,
                            "artist_name": "Stereolab",
                            "album_title": "Aluminum Tunes",
                        }
                    ]
                },
            )

        assert resp.status_code == 200
        data = resp.json()
        assert len(data["results"]) == 1
        result = data["results"][0]
        assert result["kind"] == "single_artist"
        assert result["library_id"] == 1234
        assert result["main"]["discogs_artist_id"] == 2154
        assert result["main"]["wikidata_qid"] == "Q484464"
        assert len(result["provenance"]) == 2

    @pytest.mark.asyncio
    async def test_compilation_kind_for_va_artist_name(self, app_client, mock_entity_store):
        """`Various Artists` artist_name → kind=compilation, no entity lookup."""
        async with AsyncClient(
            transport=ASGITransport(app=app_client), base_url="http://test"
        ) as ac:
            resp = await ac.post(
                "/api/v1/identity/bulk-resolve-libraries",
                json={
                    "inputs": [
                        {
                            "library_id": 5678,
                            "artist_name": "Various Artists",
                            "album_title": "Edits",
                        }
                    ]
                },
            )

        assert resp.status_code == 200
        result = resp.json()["results"][0]
        assert result["kind"] == "compilation"
        assert result["library_id"] == 5678
        assert result["main"] is None
        assert result["provenance"] == []
        assert result["tracks"] == []
        # V/A short-circuits the entity lookup — should never call get_identity.
        mock_entity_store.get_identity_canonical.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unresolved_when_identity_missing(self, app_client, mock_entity_store):
        """No entity row for the artist → kind=unresolved."""
        mock_entity_store.get_identity_canonical.return_value = None

        async with AsyncClient(
            transport=ASGITransport(app=app_client), base_url="http://test"
        ) as ac:
            resp = await ac.post(
                "/api/v1/identity/bulk-resolve-libraries",
                json={
                    "inputs": [
                        {
                            "library_id": 9999,
                            "artist_name": "Some Obscure Artist",
                            "album_title": "An Album",
                        }
                    ]
                },
            )

        assert resp.status_code == 200
        result = resp.json()["results"][0]
        assert result["kind"] == "unresolved"
        assert result["library_id"] == 9999
        assert result["main"] is None
        assert result["method"] is None
        assert result["confidence"] is None
        assert result["provenance"] == []

    @pytest.mark.asyncio
    async def test_response_order_matches_request_order(self, app_client, mock_entity_store):
        """Mixed inputs → results array preserves input order."""

        async def get_identity(name: str):
            mapping = {"Stereolab": _identity("Stereolab", id=1, discogs_artist_id=1)}
            return mapping.get(name)

        mock_entity_store.get_identity_canonical.side_effect = get_identity
        mock_entity_store.get_latest_provenance_by_source.return_value = {}

        async with AsyncClient(
            transport=ASGITransport(app=app_client), base_url="http://test"
        ) as ac:
            resp = await ac.post(
                "/api/v1/identity/bulk-resolve-libraries",
                json={
                    "inputs": [
                        {"library_id": 1, "artist_name": "Various Artists", "album_title": "VA"},
                        {"library_id": 2, "artist_name": "Stereolab", "album_title": "AT"},
                        {"library_id": 3, "artist_name": "Nobody", "album_title": "x"},
                    ]
                },
            )

        assert resp.status_code == 200
        data = resp.json()
        kinds = [r["kind"] for r in data["results"]]
        ids = [r["library_id"] for r in data["results"]]
        assert kinds == ["compilation", "single_artist", "unresolved"]
        assert ids == [1, 2, 3]

    @pytest.mark.asyncio
    async def test_413_when_over_input_cap(self, app_client, mock_entity_store):
        """1001+ inputs → 413, not 422 (per api.yaml)."""
        oversized = [
            {"library_id": i, "artist_name": f"Artist_{i}", "album_title": "x"} for i in range(1001)
        ]

        async with AsyncClient(
            transport=ASGITransport(app=app_client), base_url="http://test"
        ) as ac:
            resp = await ac.post(
                "/api/v1/identity/bulk-resolve-libraries",
                json={"inputs": oversized},
            )

        assert resp.status_code == 413
        # Cap-check fires before any DB work.
        mock_entity_store.get_identity_canonical.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_503_when_entity_store_unavailable(self, mock_settings):
        """Endpoint mirrors `/identity/*` 503 posture when the store isn't ready."""
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
                resp = await ac.post(
                    "/api/v1/identity/bulk-resolve-libraries",
                    json={
                        "inputs": [
                            {
                                "library_id": 1,
                                "artist_name": "Stereolab",
                                "album_title": "x",
                            }
                        ]
                    },
                )

        assert resp.status_code == 503

    @pytest.mark.asyncio
    async def test_canonicalizes_artist_name_before_entity_lookup(
        self, app_client, mock_entity_store
    ):
        """Per #274: diacritic / smart-quote / `&` divergence must not miss.

        The handler must call ``get_identity_canonical`` (which pre-normalizes
        via ``identity.normalize.canonicalize_for_identity_lookup``), NOT the
        exact-match ``get_identity``. Backend posts ``library.artist_name``
        verbatim from its denormalized column; any drift from
        ``entity.identity.library_name``'s canonical form would otherwise
        surface as a silent unresolved verdict.
        """
        mock_entity_store.get_identity_canonical.return_value = _identity(
            "nilufer yanya", id=1, discogs_artist_id=5499521, wikidata_qid="Q21470020"
        )
        mock_entity_store.get_latest_provenance_by_source.return_value = {}

        async with AsyncClient(
            transport=ASGITransport(app=app_client), base_url="http://test"
        ) as ac:
            resp = await ac.post(
                "/api/v1/identity/bulk-resolve-libraries",
                json={
                    "inputs": [
                        {
                            "library_id": 42,
                            "artist_name": "Nilüfer Yanya",
                            "album_title": "Painless",
                        }
                    ]
                },
            )

        assert resp.status_code == 200
        result = resp.json()["results"][0]
        assert result["kind"] == "single_artist"
        assert result["main"]["discogs_artist_id"] == 5499521
        mock_entity_store.get_identity_canonical.assert_awaited_once_with("Nilüfer Yanya")
        # The legacy exact-match path must not be exercised — divergence
        # vectors would otherwise leak past as silent unresolved verdicts.
        mock_entity_store.get_identity.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_422_for_validation_error(self, app_client, mock_entity_store):
        """Missing required field per-input → 422 (Pydantic validation)."""
        async with AsyncClient(
            transport=ASGITransport(app=app_client), base_url="http://test"
        ) as ac:
            resp = await ac.post(
                "/api/v1/identity/bulk-resolve-libraries",
                json={"inputs": [{"library_id": 1, "artist_name": "Stereolab"}]},
            )

        assert resp.status_code == 422
        mock_entity_store.get_identity_canonical.assert_not_awaited()
