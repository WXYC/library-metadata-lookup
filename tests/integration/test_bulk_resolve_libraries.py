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
        # Seed in verbatim Backend casing — matches the production shape
        # surfaced by the #276 audit (99.8% of entity.identity rows are
        # mixed-case verbatim, not canonical). The handler's three-leg
        # fall-through (#276) handles both verbatim hits and canonical-form
        # divergence on top of this shape.
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

    import core.dependencies as core_deps
    import identity.dependencies as deps
    from config.settings import get_settings

    monkeypatch.setenv("DATABASE_URL_DISCOGS", DATABASE_URL)
    get_settings.cache_clear()
    deps._entity_store = None
    deps._entity_probe_failed = False
    # Post-WXYC#395 the entity store reuses ``core.dependencies.get_discogs_pool``.
    # Clearing the entity-store singleton alone leaves the shared pool cached;
    # the next ``get_entity_store`` would reuse it (which is fine within one
    # test) but a parallel suite that toggles the DSN env var would race.
    await core_deps.close_discogs_pool()

    from main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac

    deps._entity_store = None
    deps._entity_probe_failed = False
    await core_deps.close_discogs_pool()
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
    async def test_case_drift_resolves_via_lower_fall_through(self, app_client, pg_pool):
        """Per #276: case drift between Backend input and verbatim-cased storage hits.

        Seeds verbatim mixed-case rows (the production shape per the #276
        audit — 99.8% of `entity.identity.library_name` is mixed-case
        verbatim). Posts the *lowercase* variant and asserts the LOWER fall-
        through leg resolves both rows. This is the regression test for the
        #275 ship: that PR canonicalized the input to lowercase and would
        have missed the verbatim-stored row entirely.
        """
        resp = await app_client.post(
            "/api/v1/identity/bulk-resolve-libraries",
            json={
                "inputs": [
                    {"library_id": 1, "artist_name": "stereolab", "album_title": "x"},
                    {"library_id": 2, "artist_name": "JUANA MOLINA", "album_title": "x"},
                ]
            },
        )
        assert resp.status_code == 200
        results = resp.json()["results"]
        assert [r["kind"] for r in results] == ["single_artist", "single_artist"]
        assert results[0]["main"]["discogs_artist_id"] == 2154
        assert results[1]["main"]["discogs_artist_id"] == 305253

    @pytest.mark.asyncio
    async def test_canonical_lookup_collapses_divergence_vectors(self, app_client, pg_pool):
        """Per #274: diverged inputs resolve to the same canonical-form row.

        Seeds three identities in canonical form (lowercase, no diacritics,
        ``and`` conjunction, ASCII apostrophe) and posts non-canonical
        variants. Each should land on its canonical row via the canonical
        leg of the #276 three-leg fall-through.
        """
        async with pg_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO entity.identity (library_name, discogs_artist_id)
                VALUES
                    ('nilufer yanya', 5499521),
                    ('sleater and kinney', 99999),
                    ('don''t stop', 88888)
                """
            )

        resp = await app_client.post(
            "/api/v1/identity/bulk-resolve-libraries",
            json={
                "inputs": [
                    {"library_id": 1, "artist_name": "Nilüfer Yanya", "album_title": "x"},
                    {"library_id": 2, "artist_name": "Sleater & Kinney", "album_title": "x"},
                    {"library_id": 3, "artist_name": "Don’t Stop", "album_title": "x"},
                ]
            },
        )

        assert resp.status_code == 200
        results = resp.json()["results"]
        kinds = [r["kind"] for r in results]
        # Pre-#274 every one of these would have been `unresolved`.
        assert kinds == ["single_artist", "single_artist", "single_artist"]
        assert results[0]["main"]["discogs_artist_id"] == 5499521
        assert results[1]["main"]["discogs_artist_id"] == 99999
        assert results[2]["main"]["discogs_artist_id"] == 88888

    @pytest.mark.asyncio
    async def test_all_three_legs_resolve_in_one_batch(self, app_client, pg_pool):
        """Mixed-shape stored rows + mixed-shape inputs all resolve in one call.

        Locks the full chain end-to-end. Seeds three rows in three different
        shapes (verbatim mixed-case, canonical, canonical) and posts inputs
        that each trigger a different leg, plus one miss.

        Layout:

        | input                | matches via         | stored row             |
        |---------------------|---------------------|------------------------|
        | ``"Stereolab"``      | leg 1 (verbatim)    | ``'Stereolab'``        |
        | ``"sTeReOlAb"``      | leg 2 (LOWER)       | ``'Stereolab'``        |
        | ``"Nilüfer Yanya"``  | leg 3 (canonical)   | ``'nilufer yanya'``    |
        | ``"Sleater & Kinney"`` | leg 3 (canonical) | ``'sleater and kinney'`` |
        | ``"Nobody Knows"``   | (all legs miss)     | —                       |
        """
        async with pg_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO entity.identity (library_name, discogs_artist_id)
                VALUES
                    ('nilufer yanya', 5499521),
                    ('sleater and kinney', 99999)
                """
            )
            # Note: 'Stereolab' is already seeded with discogs_artist_id=2154
            # by the schema fixture.

        resp = await app_client.post(
            "/api/v1/identity/bulk-resolve-libraries",
            json={
                "inputs": [
                    {"library_id": 1, "artist_name": "Stereolab", "album_title": "x"},
                    {"library_id": 2, "artist_name": "sTeReOlAb", "album_title": "x"},
                    {"library_id": 3, "artist_name": "Nilüfer Yanya", "album_title": "x"},
                    {"library_id": 4, "artist_name": "Sleater & Kinney", "album_title": "x"},
                    {"library_id": 5, "artist_name": "Nobody Knows", "album_title": "x"},
                ]
            },
        )

        assert resp.status_code == 200
        results = resp.json()["results"]
        assert [r["kind"] for r in results] == [
            "single_artist",
            "single_artist",
            "single_artist",
            "single_artist",
            "unresolved",
        ]
        # Same identity reached via two different legs (1 and 2) → same row.
        assert results[0]["main"]["discogs_artist_id"] == 2154
        assert results[1]["main"]["discogs_artist_id"] == 2154
        assert results[2]["main"]["discogs_artist_id"] == 5499521
        assert results[3]["main"]["discogs_artist_id"] == 99999
        assert results[4]["main"] is None

    @pytest.mark.asyncio
    async def test_leg_2_picks_lowest_id_when_two_rows_lower_match(self, app_client, pg_pool):
        """`library_name` is case-sensitive-UNIQUE; two case-variants can co-exist.

        Seed two rows whose lower-form is identical (``'Stereolab'`` already
        in fixture; add ``'stereolab'``). Post a third case variant
        (``"sTeReOlAb"``) so leg 1 misses but leg 2's `LOWER(...)` matches
        both stored rows. The `ORDER BY id ASC` tie-break must pick the
        oldest (the fixture's ``'Stereolab'``, id 1).
        """
        async with pg_pool.acquire() as conn:
            # Insert a second case-variant. Discogs id is intentionally
            # different so we can tell which row leg 2 picked.
            await conn.execute(
                "INSERT INTO entity.identity (library_name, discogs_artist_id) "
                "VALUES ('stereolab', 99999)"
            )

        resp = await app_client.post(
            "/api/v1/identity/bulk-resolve-libraries",
            json={
                "inputs": [
                    {"library_id": 1, "artist_name": "sTeReOlAb", "album_title": "x"},
                ]
            },
        )

        assert resp.status_code == 200
        result = resp.json()["results"][0]
        assert result["kind"] == "single_artist"
        # Oldest row wins (the fixture's `'Stereolab'`, discogs_artist_id=2154),
        # not the freshly-inserted `'stereolab'` (id=99999).
        assert result["main"]["discogs_artist_id"] == 2154

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
