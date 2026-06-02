"""Integration tests for `POST /api/v1/artists/search-aliases/bulk`.

Real PostgreSQL via ``DATABASE_URL_TEST``. Seeds ``entity.identity`` plus
the five discogs-cache child tables (``artist``, ``artist_alias``,
``artist_name_variation``, ``artist_member``, ``artist_url``) inside a
fresh schema, then exercises the endpoint end-to-end through the FastAPI
ASGI client. Composer internals are covered in
``tests/unit/test_artist_search_aliases_composer.py``; this tier confirms
the FastAPI wiring + PG round-trip is intact and the bearer-auth posture
matches sibling endpoints.

Run with: ``pytest -m pg -v``
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
async def set_up_schemas(pg_pool):
    """Drop + create the entity schema and the discogs cache tables.

    Mirrors ``tests/integration/test_bulk_resolve_libraries.py`` for the
    entity side, then adds the discogs-cache children. The composer
    queries ``artist``, ``artist_alias``, ``artist_name_variation``,
    ``artist_member`` in the default (public) schema — same shape as
    the production discogs-cache.

    SAFETY: this fixture DROPs both `entity` schema CASCADE and the
    public-schema `artist` + four child tables. The default
    ``DATABASE_URL_TEST`` (``postgresql://discogs:discogs@localhost:5433/discogs``)
    points at the developer's local discogs-cache, which may also host
    a populated `entity.identity` from a prior reconciliation campaign.
    Wiping either set is catastrophic (the cache rebuild + reconcile
    re-run takes hours). We refuse to run when ANY of the about-to-drop
    tables already has rows, so the operator must point
    ``DATABASE_URL_TEST`` at a clean DB.
    """

    async def _table_row_count(conn, schema: str, table: str) -> int | None:
        """Return row count, or None if the table doesn't exist."""
        exists = await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = $1 AND table_name = $2)",
            schema,
            table,
        )
        if not exists:
            return None
        return await conn.fetchval(f'SELECT COUNT(*) FROM "{schema}"."{table}"')

    async with pg_pool.acquire() as conn:
        # All tables this fixture will DROP. If any has data, bail.
        targets: list[tuple[str, str]] = [
            ("entity", "identity"),
            ("entity", "reconciliation_log"),
            ("public", "artist"),
            ("public", "artist_alias"),
            ("public", "artist_name_variation"),
            ("public", "artist_member"),
            ("public", "artist_url"),
        ]
        populated: list[tuple[str, str, int]] = []
        for schema, table in targets:
            count = await _table_row_count(conn, schema, table)
            if count is not None and count > 0:
                populated.append((schema, table, count))
        if populated:
            details = ", ".join(f"{s}.{t}={n}" for s, t, n in populated)
            pytest.skip(
                "DATABASE_URL_TEST points at a populated DB; refusing to "
                f"DROP tables that hold data ({details}). Point "
                "DATABASE_URL_TEST at a clean PG before running this suite."
            )
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
        # Discogs cache child tables. Drop first so prior runs don't poison
        # them — these live in the public schema in test as in prod.
        await conn.execute("DROP TABLE IF EXISTS artist_alias CASCADE")
        await conn.execute("DROP TABLE IF EXISTS artist_name_variation CASCADE")
        await conn.execute("DROP TABLE IF EXISTS artist_member CASCADE")
        await conn.execute("DROP TABLE IF EXISTS artist_url CASCADE")
        await conn.execute("DROP TABLE IF EXISTS artist CASCADE")
        await conn.execute("""
            CREATE TABLE artist (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                profile TEXT,
                image_url TEXT,
                fetched_at TIMESTAMPTZ
            )
        """)
        await conn.execute("""
            CREATE TABLE artist_alias (
                artist_id INTEGER NOT NULL REFERENCES artist(id),
                alias_id INTEGER,
                alias_name TEXT NOT NULL
            )
        """)
        await conn.execute("""
            CREATE TABLE artist_name_variation (
                artist_id INTEGER NOT NULL REFERENCES artist(id),
                name TEXT NOT NULL
            )
        """)
        await conn.execute("""
            CREATE TABLE artist_member (
                artist_id INTEGER NOT NULL REFERENCES artist(id),
                member_id INTEGER,
                member_name TEXT NOT NULL,
                active BOOLEAN
            )
        """)
        await conn.execute("""
            CREATE TABLE artist_url (
                artist_id INTEGER NOT NULL REFERENCES artist(id),
                url TEXT NOT NULL
            )
        """)

        # Seed entity.identity rows.
        await conn.execute(
            """
            INSERT INTO entity.identity (library_name, discogs_artist_id)
            VALUES
                ('Stereolab', 2154),
                ('Juana Molina', 305253),
                ('Cat Power', NULL)
            """
        )

        # Seed discogs cache: Stereolab gets one entry in each child table so
        # we can assert source-tagging end-to-end. Juana Molina is in the
        # ``artist`` table but has no children (the "ran the leg, found
        # nothing" case). 305253 IS in artist so cache lookup hits; we just
        # leave the child tables empty for that row.
        await conn.execute(
            """
            INSERT INTO artist (id, name, profile, image_url)
            VALUES
                (2154, 'Stereolab', NULL, NULL),
                (305253, 'Juana Molina', NULL, NULL)
            """
        )
        await conn.execute(
            "INSERT INTO artist_alias (artist_id, alias_id, alias_name) "
            "VALUES (2154, 999, 'Monade')"
        )
        await conn.execute(
            "INSERT INTO artist_name_variation (artist_id, name) VALUES (2154, 'The Stereolab')"
        )
        await conn.execute(
            "INSERT INTO artist_member (artist_id, member_id, member_name, active) "
            "VALUES (2154, 200, 'Laetitia Sadier', TRUE)"
        )

    yield

    async with pg_pool.acquire() as conn:
        await conn.execute("DROP SCHEMA IF EXISTS entity CASCADE")
        await conn.execute("DROP TABLE IF EXISTS artist_alias CASCADE")
        await conn.execute("DROP TABLE IF EXISTS artist_name_variation CASCADE")
        await conn.execute("DROP TABLE IF EXISTS artist_member CASCADE")
        await conn.execute("DROP TABLE IF EXISTS artist_url CASCADE")
        await conn.execute("DROP TABLE IF EXISTS artist CASCADE")


@pytest_asyncio.fixture
async def app_client(monkeypatch):
    """ASGI client with the LML app pointed at the test PG, auth off."""
    from httpx import ASGITransport, AsyncClient

    import core.dependencies as core_deps
    import identity.dependencies as deps
    from config.settings import get_settings

    monkeypatch.setenv("DATABASE_URL_DISCOGS", DATABASE_URL)
    # Test posture mirrors `test_bulk_resolve_libraries.py`: leave
    # `LML_REQUIRE_AUTH` off so we can assert the happy-path responses
    # without provisioning a bearer. Auth enforcement is covered in
    # `tests/unit/test_auth.py`.
    monkeypatch.setenv("LML_REQUIRE_AUTH", "false")
    get_settings.cache_clear()
    deps._entity_store = None
    deps._entity_probe_failed = False
    await core_deps.close_discogs_pool()
    await core_deps.close_discogs_service()

    from main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac

    deps._entity_store = None
    deps._entity_probe_failed = False
    await core_deps.close_discogs_pool()
    await core_deps.close_discogs_service()
    get_settings.cache_clear()


@pytest.mark.pg
class TestSearchAliasesBulkEndpoint:
    @pytest.mark.asyncio
    async def test_round_trip_emits_all_three_discogs_source_variants(self, app_client):
        """Stereolab returns one variant per source type and all three tags."""
        resp = await app_client.post(
            "/api/v1/artists/search-aliases/bulk",
            json={"names": ["Stereolab"]},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["missing"] == []
        assert len(data["artists"]) == 1
        result = data["artists"][0]
        assert result["name"] == "Stereolab"
        variants_by_source = {v["source"]: v for v in result["variants"]}
        assert set(variants_by_source.keys()) == {
            "discogs_name_variation",
            "discogs_alias",
            "discogs_member",
        }
        assert variants_by_source["discogs_name_variation"]["variant"] == "The Stereolab"
        assert variants_by_source["discogs_alias"]["variant"] == "Monade"
        assert variants_by_source["discogs_member"]["variant"] == "Laetitia Sadier"
        # Tagged sources for reconcile scoping.
        assert set(result["sources_present"]) == {
            "discogs_name_variation",
            "discogs_alias",
            "discogs_member",
        }

    @pytest.mark.asyncio
    async def test_missing_identity_routed_to_missing_array(self, app_client):
        """Names without an ``entity.identity`` row → in ``missing[]``, not ``artists[]``."""
        resp = await app_client.post(
            "/api/v1/artists/search-aliases/bulk",
            json={"names": ["Stereolab", "Nobody Knows"]},
        )
        assert resp.status_code == 200
        data = resp.json()
        names_in_artists = {a["name"] for a in data["artists"]}
        assert "Stereolab" in names_in_artists
        assert "Nobody Knows" not in names_in_artists
        assert data["missing"] == ["Nobody Knows"]

    @pytest.mark.asyncio
    async def test_identity_without_discogs_id_yields_empty_sources_present(self, app_client):
        """Cat Power has NULL ``discogs_artist_id`` → empty variants AND empty sources_present.

        Reconcile contract: consumer must NOT delete cached rows for this
        artist because the composer never ran any source leg.
        """
        resp = await app_client.post(
            "/api/v1/artists/search-aliases/bulk",
            json={"names": ["Cat Power"]},
        )
        assert resp.status_code == 200
        result = resp.json()["artists"][0]
        assert result["variants"] == []
        assert result["sources_present"] == []

    @pytest.mark.asyncio
    async def test_discogs_cache_miss_keeps_sources_present_populated(self, app_client):
        """Juana Molina has a Discogs id but no children in the cache.

        Composer ran the leg but found nothing — variants empty, but
        sources_present must list all three discogs_* tags. Consumer needs
        this to scope DELETE during reconcile.
        """
        resp = await app_client.post(
            "/api/v1/artists/search-aliases/bulk",
            json={"names": ["Juana Molina"]},
        )
        assert resp.status_code == 200
        result = resp.json()["artists"][0]
        assert result["variants"] == []
        assert set(result["sources_present"]) == {
            "discogs_name_variation",
            "discogs_alias",
            "discogs_member",
        }

    @pytest.mark.asyncio
    async def test_413_for_oversized_request(self, app_client):
        """1001 names → 413 before any DB work."""
        oversized = [f"Artist_{i}" for i in range(1001)]
        resp = await app_client.post(
            "/api/v1/artists/search-aliases/bulk",
            json={"names": oversized},
        )
        assert resp.status_code == 413

    @pytest.mark.asyncio
    async def test_case_drift_resolves_via_lower_fall_through(self, app_client):
        """Backend posts lowercase, storage is mixed-case → leg 2 resolves it.

        Mirrors the #276 fall-through contract: stored row ``'Stereolab'``,
        posted name ``'stereolab'`` — the composer's bulk variant of
        ``resolve_library_name`` finds the row via LOWER fall-through.
        """
        resp = await app_client.post(
            "/api/v1/artists/search-aliases/bulk",
            json={"names": ["stereolab"]},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["missing"] == []
        result = data["artists"][0]
        # The returned ``name`` echoes the input, not the stored shape.
        assert result["name"] == "stereolab"
        # The discogs leg still fires and tags itself, because the resolved
        # identity row carries discogs_artist_id=2154.
        assert set(result["sources_present"]) == {
            "discogs_name_variation",
            "discogs_alias",
            "discogs_member",
        }
