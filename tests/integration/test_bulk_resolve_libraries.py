"""Integration tests for `POST /api/v1/identity/bulk-resolve-libraries`.

Exercise the endpoint end-to-end against a fresh PostgreSQL with seeded
`entity.identity` rows. Composition rules are unit-tested separately;
this tier confirms the FastAPI wiring + PG round-trip is intact and
input order is preserved across mixed-kind batches.

Run with: pytest -m pg -v
"""

from __future__ import annotations

import os

import asyncpg
import pytest
import pytest_asyncio

DATABASE_URL = os.getenv(
    "DATABASE_URL_TEST",
    "postgresql://discogs:discogs@localhost:5433/discogs",
)


@pytest_asyncio.fixture
async def pg_pool():
    try:
        pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=3)
    except Exception as e:
        pytest.skip(f"Cannot connect to test PostgreSQL: {e}")
        return
    yield pool
    await pool.close()


@pytest_asyncio.fixture(autouse=True)
async def set_up_entity_schema(pg_pool):
    """Create + seed a fresh `entity` schema for each test."""
    async with pg_pool.acquire() as conn:
        await conn.execute("DROP SCHEMA IF EXISTS entity CASCADE")
        await conn.execute("CREATE SCHEMA entity")
        await conn.execute("""
            CREATE TABLE entity.identity (
                id SERIAL PRIMARY KEY,
                library_name TEXT NOT NULL UNIQUE,
                discogs_artist_id INTEGER,
                wikidata_qid TEXT,
                musicbrainz_artist_id TEXT,
                spotify_artist_id TEXT,
                apple_music_artist_id TEXT,
                bandcamp_id TEXT,
                reconciliation_status TEXT NOT NULL DEFAULT 'unreconciled',
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        await conn.execute("""
            CREATE TABLE entity.reconciliation_log (
                id SERIAL PRIMARY KEY,
                identity_id INTEGER NOT NULL REFERENCES entity.identity(id),
                source TEXT NOT NULL,
                external_id TEXT NOT NULL,
                confidence REAL,
                method TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        # Seed a couple of identities representative of WXYC's catalog.
        await conn.execute(
            """
            INSERT INTO entity.identity (library_name, discogs_artist_id, wikidata_qid)
            VALUES
                ('Stereolab', 2154, 'Q484464'),
                ('Juana Molina', 305253, 'Q272615')
            """
        )
    yield
    async with pg_pool.acquire() as conn:
        await conn.execute("DROP SCHEMA IF EXISTS entity CASCADE")


@pytest_asyncio.fixture
async def app_client(monkeypatch):
    """ASGI client with the LML app pointed at the test PG."""
    from httpx import ASGITransport, AsyncClient

    import identity.dependencies as deps
    from config.settings import get_settings

    monkeypatch.setenv("DATABASE_URL_DISCOGS", DATABASE_URL)
    get_settings.cache_clear()
    deps._entity_store = None
    deps._entity_pg = None
    deps._entity_probe_failed = False

    from main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac

    deps._entity_store = None
    deps._entity_pg = None
    deps._entity_probe_failed = False
    get_settings.cache_clear()


@pytest.mark.pg
class TestBulkResolveLibrariesEndpoint:
    @pytest.mark.asyncio
    async def test_round_trip_single_artist_against_real_pg(self, app_client):
        """End-to-end: seeded identity → composed single_artist verdict."""
        resp = await app_client.post(
            "/api/v1/identity/bulk-resolve-libraries",
            json={
                "inputs": [
                    {"library_id": 1234, "artist_name": "Stereolab", "album_title": "x"},
                ]
            },
        )

        assert resp.status_code == 200
        data = resp.json()
        result = data["results"][0]
        assert result["kind"] == "single_artist"
        assert result["library_id"] == 1234
        assert result["main"]["discogs_artist_id"] == 2154
        assert result["main"]["wikidata_qid"] == "Q484464"
        # Log-less identity: both legs default to exact_match 1.00 but are
        # internally marked is_inherited=True so Rule 3 excludes them from
        # the cross-source-agreement detector. Composed method is just the
        # weakest leg's mapped method (exact_match), confidence is MIN
        # without the boost.
        assert result["method"] == "exact_match"
        assert result["confidence"] == pytest.approx(1.0)
        assert len(result["provenance"]) == 2

    @pytest.mark.asyncio
    async def test_input_order_preserved_across_mixed_kinds(self, app_client):
        """Response[i] corresponds to inputs[i] for compilation / hit / miss."""
        resp = await app_client.post(
            "/api/v1/identity/bulk-resolve-libraries",
            json={
                "inputs": [
                    {"library_id": 100, "artist_name": "Various Artists", "album_title": "VA"},
                    {"library_id": 200, "artist_name": "Stereolab", "album_title": "AT"},
                    {"library_id": 300, "artist_name": "Nobody Artist", "album_title": "x"},
                    {"library_id": 400, "artist_name": "Juana Molina", "album_title": "DOGA"},
                ]
            },
        )

        assert resp.status_code == 200
        results = resp.json()["results"]
        assert [r["library_id"] for r in results] == [100, 200, 300, 400]
        assert [r["kind"] for r in results] == [
            "compilation",
            "single_artist",
            "unresolved",
            "single_artist",
        ]

    @pytest.mark.asyncio
    async def test_413_for_oversized_request(self, app_client):
        """1001 inputs → 413."""
        oversized = [
            {"library_id": i, "artist_name": f"Artist_{i}", "album_title": "x"} for i in range(1001)
        ]
        resp = await app_client.post(
            "/api/v1/identity/bulk-resolve-libraries",
            json={"inputs": oversized},
        )
        assert resp.status_code == 413
