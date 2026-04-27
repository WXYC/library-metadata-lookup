"""Integration tests for the entity resolution pipeline.

These tests run against a test PostgreSQL (Docker on port 5433) with the
entity schema applied and Discogs fixture data loaded.

Run with: pytest -m pg -v
Requires: DATABASE_URL_TEST env var or Docker postgres on port 5433.
"""

from __future__ import annotations

import os

import asyncpg
import pytest
import pytest_asyncio

from scripts.entity_resolution.dedup import EntityDeduplicator
from scripts.entity_resolution.discogs import DiscogsReconciler
from scripts.entity_resolution.sources import PgSource
from scripts.entity_resolution.store import EntityStore

DATABASE_URL = os.getenv(
    "DATABASE_URL_TEST",
    "postgresql://discogs:discogs@localhost:5433/discogs",
)


@pytest_asyncio.fixture
async def pg_pool():
    """Create a connection pool to the test database."""
    try:
        pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=3)
    except Exception as e:
        pytest.skip(f"Cannot connect to test PostgreSQL: {e}")
        return
    yield pool
    await pool.close()


@pytest_asyncio.fixture
async def pg_source(pg_pool):
    """PgSource wrapping the test pool."""
    source = PgSource.__new__(PgSource)
    source._dsn = DATABASE_URL
    source._pool = pg_pool
    return source


@pytest_asyncio.fixture(autouse=True)
async def set_up_entity_schema(pg_pool):
    """Create (or re-create) the entity schema for each test."""
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
        await conn.execute(
            "CREATE INDEX idx_entity_identity_status ON entity.identity(reconciliation_status)"
        )
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
    yield
    async with pg_pool.acquire() as conn:
        await conn.execute("DROP SCHEMA IF EXISTS entity CASCADE")


@pytest.mark.pg
class TestEntityStoreCRUD:
    """Test entity store CRUD operations against real PostgreSQL."""

    @pytest.mark.asyncio
    async def test_upsert_and_get(self, pg_source):
        store = EntityStore(pg_source)
        identity = await store.upsert_identity(library_name="Autechre")
        assert identity is not None
        assert identity.library_name == "Autechre"
        assert identity.reconciliation_status == "unreconciled"

        fetched = await store.get_identity("Autechre")
        assert fetched is not None
        assert fetched.id == identity.id

    @pytest.mark.asyncio
    async def test_upsert_coalesce_semantics(self, pg_source):
        store = EntityStore(pg_source)
        await store.upsert_identity(library_name="Stereolab", discogs_artist_id=99)
        await store.upsert_identity(library_name="Stereolab", wikidata_qid="Q483507")
        identity = await store.get_identity("Stereolab")
        assert identity is not None
        assert identity.discogs_artist_id == 99
        assert identity.wikidata_qid == "Q483507"

    @pytest.mark.asyncio
    async def test_status_update_and_filter(self, pg_source):
        store = EntityStore(pg_source)
        id1 = await store.upsert_identity(library_name="Autechre")
        id2 = await store.upsert_identity(library_name="Stereolab")

        await store.update_status(id1.id, "reconciled")
        await store.update_status(id2.id, "no_match")

        reconciled = await store.get_identities_by_status("reconciled")
        no_match = await store.get_identities_by_status("no_match")
        assert len(reconciled) == 1
        assert reconciled[0].library_name == "Autechre"
        assert len(no_match) == 1
        assert no_match[0].library_name == "Stereolab"

    @pytest.mark.asyncio
    async def test_reconciliation_log(self, pg_source):
        store = EntityStore(pg_source)
        identity = await store.upsert_identity(library_name="Autechre")
        await store.log_reconciliation(
            identity_id=identity.id,
            source="discogs",
            external_id="12",
            method="exact_match",
            confidence=1.0,
        )
        # Verify via direct query
        rows = await pg_source.fetchall(
            "SELECT * FROM entity.reconciliation_log WHERE identity_id = $1",
            identity.id,
        )
        assert len(rows) == 1
        assert rows[0]["source"] == "discogs"
        assert rows[0]["method"] == "exact_match"


@pytest.mark.pg
class TestDiscogsReconciliationIntegration:
    """Test Discogs reconciliation against real discogs-cache data.

    These tests require the discogs-cache PostgreSQL to have the standard
    Discogs tables (release_artist, artist_member, artist_alias, etc.)
    populated with data. If the tables don't exist, the tests are skipped.
    """

    @pytest.mark.asyncio
    async def test_reconcile_known_artists(self, pg_source, pg_pool):
        """Known WXYC artists should resolve against Discogs cache data."""
        # Check if release_artist table exists and has data
        async with pg_pool.acquire() as conn:
            exists = await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                "WHERE table_name = 'release_artist')"
            )
            if not exists:
                pytest.skip("release_artist table not found in test database")
            count = await conn.fetchval("SELECT count(*) FROM release_artist")
            if count == 0:
                pytest.skip("release_artist table is empty -- no Discogs data loaded")

        reconciler = DiscogsReconciler(pg_source)
        names = ["Autechre", "Stereolab", "Jessica Pratt", "Cat Power", "Juana Molina"]
        results = await reconciler.reconcile_batch(names)

        # We expect at least some matches (>= 70% is the target)
        assert len(results) >= 1, f"Expected at least 1 match, got {len(results)}"
        for _name, match in results.items():
            assert match.discogs_artist_id > 0
            assert match.method in ("exact_match", "member_group", "alias_match", "name_variation")


@pytest.mark.pg
class TestDeduplicationIntegration:
    """Test deduplication against real PostgreSQL."""

    @pytest.mark.asyncio
    async def test_dedup_merges_shared_qid(self, pg_source):
        store = EntityStore(pg_source)
        await store.upsert_identity(
            library_name="Autechre", wikidata_qid="Q378288", discogs_artist_id=12
        )
        await store.upsert_identity(
            library_name="autechre",
            wikidata_qid="Q378288",
            musicbrainz_artist_id="mbid-1",
        )

        dedup = EntityDeduplicator(pg_source)
        groups = await dedup.find_duplicate_groups()
        assert len(groups) == 1

        await dedup.merge_group(groups[0][0], groups[0][1])

        # After merge, only one identity should remain
        remaining = await pg_source.fetchall(
            "SELECT * FROM entity.identity WHERE wikidata_qid = $1", "Q378288"
        )
        assert len(remaining) == 1
        # The merged record should have both discogs and musicbrainz IDs
        assert remaining[0]["discogs_artist_id"] == 12
        assert remaining[0]["musicbrainz_artist_id"] == "mbid-1"
