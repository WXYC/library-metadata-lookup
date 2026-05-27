"""Integration tests for the entity resolution pipeline.

These tests run against a test PostgreSQL (Docker on port 5433) with the
entity schema applied and Discogs fixture data loaded.

Run with: pytest -m pg -v
Requires: DATABASE_URL_TEST env var or Docker postgres on port 5433.
"""

from __future__ import annotations

import os
from typing import ClassVar

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
class TestDiscogsReconciliationSQLSmoke:
    """Smoke-test that each of the four reconciler SQL strings parses and
    executes against a Postgres with the wxyc_identity_match_* functions
    deployed (WXYC/discogs-etl#195, alembic 0004).

    Why this test exists: the sibling ``test_reconcile_known_artists`` (above)
    self-skips when ``release_artist`` is empty, which is the CI scenario.
    Pre-#285 that meant CI never executed the
    ``wxyc_identity_match_artist(col)`` SQL even after the flip — only prod
    saw the symmetric pair under live traffic. This test closes that gap by
    running each query with an empty ``ANY($1)`` array against stub tables,
    asserting only that the SQL parses, the function call resolves, and the
    rowset comes back empty.

    Skip behavior: when ``wxyc_identity_match_artist`` is not present in
    ``pg_proc`` (e.g. CI's vanilla ``postgres:16-alpine`` without alembic
    0004 applied), this class self-skips. The function depends on the
    ``unaccent`` extension and the ``wxyc_unaccent`` text-search dictionary
    (which requires the rules file installed at the server's
    ``$SHAREDIR/tsearch_data/``), neither of which is trivially provisioned
    on the CI service container.

    TODO(wxyc-etl): when wxyc-etl ships ``wxyc_identity_match_functions.sql``
    as ``importlib.resources``-readable package data, the fixture below can
    deploy the functions inline and this test will run on CI's plain
    postgres-16 service container. Today the SQL only lives in the wxyc-etl
    source tree and is vendored byte-for-byte into each cache repo.
    """

    _STUB_TABLES: ClassVar[dict[str, str]] = {
        "release_artist": (
            "release_id INTEGER, artist_id INTEGER, artist_name TEXT, extra INTEGER DEFAULT 0"
        ),
        "artist_member": "artist_id INTEGER, member_id INTEGER, member_name TEXT",
        "artist_alias": "artist_id INTEGER, alias_name TEXT",
        "artist_name_variation": "artist_id INTEGER, name TEXT",
    }

    @pytest_asyncio.fixture(autouse=True)
    async def ensure_reconciler_schema(self, pg_pool):
        """Skip when ``wxyc_identity_match_artist`` isn't deployed, otherwise
        ensure the four reconciler tables exist (stub them only if absent),
        and drop any stubs we created on teardown.

        Same skip pattern as ``test_reconcile_known_artists`` (which skips on
        missing ``release_artist``). On a real discogs-cache the tables exist
        with data, so ``CREATE TABLE IF NOT EXISTS`` is a no-op and the
        teardown's ``created_tables`` set is empty — real data survives. On a
        fresh PG (currently unreachable: we skip before getting here) only the
        stubs we created get dropped, so cross-test pollution can't accrete.
        """
        async with pg_pool.acquire() as conn:
            exists = await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM pg_proc WHERE proname = $1)",
                "wxyc_identity_match_artist",
            )
            if not exists:
                pytest.skip(
                    "wxyc_identity_match_artist not deployed -- needs alembic 0004 "
                    "from WXYC/discogs-etl. CI's plain postgres-16 service container "
                    "doesn't have it. See class docstring for the TODO."
                )
            created_tables: set[str] = set()
            for table_name, columns in self._STUB_TABLES.items():
                pre_existing = await conn.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema = current_schema() AND table_name = $1)",
                    table_name,
                )
                if not pre_existing:
                    await conn.execute(f"CREATE TABLE {table_name} ({columns})")
                    created_tables.add(table_name)
        try:
            yield
        finally:
            async with pg_pool.acquire() as conn:
                for table_name in created_tables:
                    await conn.execute(f"DROP TABLE IF EXISTS {table_name}")

    @pytest.mark.asyncio
    async def test_exact_match_sql_parses_and_executes(self, pg_source):
        from scripts.entity_resolution.discogs import _EXACT_MATCH_SQL

        rows = await pg_source.fetchall(_EXACT_MATCH_SQL, [])
        assert rows == []

    @pytest.mark.asyncio
    async def test_member_match_sql_parses_and_executes(self, pg_source):
        from scripts.entity_resolution.discogs import _MEMBER_MATCH_SQL

        rows = await pg_source.fetchall(_MEMBER_MATCH_SQL, [])
        assert rows == []

    @pytest.mark.asyncio
    async def test_alias_match_sql_parses_and_executes(self, pg_source):
        from scripts.entity_resolution.discogs import _ALIAS_MATCH_SQL

        rows = await pg_source.fetchall(_ALIAS_MATCH_SQL, [])
        assert rows == []

    @pytest.mark.asyncio
    async def test_name_variation_match_sql_parses_and_executes(self, pg_source):
        from scripts.entity_resolution.discogs import _NAME_VARIATION_MATCH_SQL

        rows = await pg_source.fetchall(_NAME_VARIATION_MATCH_SQL, [])
        assert rows == []

    @pytest.mark.asyncio
    async def test_wxyc_identity_match_artist_strips_leading_article(self, pg_pool):
        """Behavior-assertion: ``wxyc_identity_match_artist('The Microphones')``
        must return ``'microphones'`` — same body shape as the Rust
        ``to_identity_match_form`` (lowercase + leading-article strip).

        The other tests in this class only prove the SQL parses and the
        function symbol resolves. They'd still pass if a future alembic
        revision redeployed the function with a no-op body. This test pins
        the actual transformation behavior for one canonical case, so
        drift between the PG function and the Rust reference fails loudly
        here.
        """
        async with pg_pool.acquire() as conn:
            result = await conn.fetchval("SELECT wxyc_identity_match_artist($1)", "The Microphones")
        assert result == "microphones", (
            f"wxyc_identity_match_artist('The Microphones') returned {result!r}; "
            "expected 'microphones' (lowercase + leading-article strip). "
            "Function body has drifted from the Rust to_identity_match_form. "
            "See WXYC/wxyc-etl#112 + WXYC/discogs-etl#195."
        )

    @pytest.mark.asyncio
    async def test_all_four_match_functions_exist(self, pg_pool):
        """Confirm the full family of cross-cache-identity functions deploys
        together. Pinned because LML's reconciler only currently uses the
        ``_artist`` flavor, but the column-side flip in #285 was paired with
        a function family deploy in discogs-etl#195; a future reconciler
        change that reaches for ``_title`` / ``_with_punctuation`` /
        ``_with_disambiguator_strip`` should fail loudly here rather than
        at runtime if any sibling went missing.
        """
        expected = {
            "wxyc_identity_match_artist",
            "wxyc_identity_match_title",
            "wxyc_identity_match_with_punctuation",
            "wxyc_identity_match_with_disambiguator_strip",
        }
        async with pg_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT proname FROM pg_proc WHERE proname = ANY($1)",
                list(expected),
            )
        present = {row["proname"] for row in rows}
        missing = expected - present
        assert not missing, (
            f"Missing wxyc_identity_match_* functions: {sorted(missing)}. "
            "Re-run alembic 0004 from WXYC/discogs-etl against this cache."
        )


@pytest.mark.pg
class TestIdentityRouterEntityStoreUnavailable:
    """Identity routes return 503 — not 500 — when the entity schema is missing.

    Regression for #169: a misconfigured deploy (no DATABASE_URL_DISCOGS, or DSN
    pointing at a DB without the entity schema applied) used to leak a 500.
    """

    @pytest_asyncio.fixture(autouse=True)
    async def set_up_entity_schema(self, pg_pool):
        """Override the module-level autouse fixture: keep the schema *missing*."""
        async with pg_pool.acquire() as conn:
            await conn.execute("DROP SCHEMA IF EXISTS entity CASCADE")
        yield
        async with pg_pool.acquire() as conn:
            await conn.execute("DROP SCHEMA IF EXISTS entity CASCADE")

    @pytest_asyncio.fixture
    async def app_with_pg_dsn(self, monkeypatch):
        """Reset the dep cache and point Settings at the test DSN."""
        import identity.dependencies as deps
        from config.settings import get_settings

        monkeypatch.setenv("DATABASE_URL_DISCOGS", DATABASE_URL)
        get_settings.cache_clear()
        deps._entity_store = None
        deps._entity_pg = None
        deps._entity_probe_failed = False

        from main import app

        yield app

        deps._entity_store = None
        deps._entity_pg = None
        deps._entity_probe_failed = False
        get_settings.cache_clear()

    @pytest.mark.asyncio
    async def test_resolve_returns_503_when_entity_schema_missing(self, app_with_pg_dsn):
        from httpx import ASGITransport, AsyncClient

        async with AsyncClient(
            transport=ASGITransport(app=app_with_pg_dsn), base_url="http://test"
        ) as ac:
            resp = await ac.get("/identity/resolve", params={"name": "Stereolab"})

        assert resp.status_code == 503
        assert "entity store" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_bulk_returns_503_when_entity_schema_missing(self, app_with_pg_dsn):
        from httpx import ASGITransport, AsyncClient

        async with AsyncClient(
            transport=ASGITransport(app=app_with_pg_dsn), base_url="http://test"
        ) as ac:
            resp = await ac.post("/identity/bulk", json={"names": ["Stereolab"]})

        assert resp.status_code == 503


@pytest.mark.pg
class TestOrphanPass:
    """Acceptance tests for LML#377: the orphan pass before seed_identities.

    The Beyonce → Beyoncé scenario is the motivating case in the issue body
    — these tests pin the contract: reconciliation_log provenance is either
    *preserved* (merge path) or *explicitly removed* (delete path), never
    silently abandoned under the new orphan row.
    """

    @pytest.mark.asyncio
    async def test_orphan_with_canonical_match_merges_provenance(self, pg_source):
        """Beyonce → Beyoncé: prior reconciliation_log preserved on the new row."""
        from scripts.entity_resolution.__main__ import prune_orphan_identities

        store = EntityStore(pg_source)

        # Pre-orphan state: librarian had "Beyonce" with a reconciled Discogs ID.
        old = await store.upsert_identity(library_name="Beyonce", discogs_artist_id=1419)
        assert old is not None
        await store.log_reconciliation(
            identity_id=old.id,
            source="discogs",
            external_id="1419",
            method="exact_match",
            confidence=1.0,
        )

        # Librarian renames to "Beyoncé". Pre-pass, both rows exist (the new
        # one is empty; the orphan-pass has not yet run).
        new = await store.upsert_identity(library_name="Beyoncé")
        assert new is not None
        assert new.id != old.id

        merged, deleted, orphans = await prune_orphan_identities(store, {"Beyoncé"})

        assert merged == 1
        assert deleted == 0
        assert orphans == ["Beyonce"]

        # Post-pass: exactly one identity row, keyed on the new name.
        rows = await pg_source.fetchall("SELECT id, library_name FROM entity.identity")
        assert len(rows) == 1
        assert rows[0]["library_name"] == "Beyoncé"
        # The merge re-points the reconciliation_log to the surviving (new) row.
        surviving_id = rows[0]["id"]
        log_rows = await pg_source.fetchall(
            "SELECT identity_id, external_id, method FROM entity.reconciliation_log"
        )
        assert len(log_rows) == 1
        assert log_rows[0]["identity_id"] == surviving_id
        assert log_rows[0]["external_id"] == "1419"
        assert log_rows[0]["method"] == "exact_match"
        # The COALESCE-merge brought the old row's Discogs ID along.
        ids_row = await pg_source.fetchone(
            "SELECT discogs_artist_id FROM entity.identity WHERE id = $1", surviving_id
        )
        assert ids_row is not None
        assert ids_row["discogs_artist_id"] == 1419

    @pytest.mark.asyncio
    async def test_orphan_with_no_canonical_match_is_deleted(self, pg_source):
        """Truly-removed artist: identity AND its log rows both go (no FK leak)."""
        from scripts.entity_resolution.__main__ import prune_orphan_identities

        store = EntityStore(pg_source)

        gone = await store.upsert_identity(
            library_name="BandThatLeftRotation", discogs_artist_id=999
        )
        assert gone is not None
        await store.log_reconciliation(
            identity_id=gone.id,
            source="discogs",
            external_id="999",
            method="exact_match",
        )

        merged, deleted, orphans = await prune_orphan_identities(store, {"DifferentBand"})

        assert merged == 0
        assert deleted == 1
        assert orphans == ["BandThatLeftRotation"]

        # Identity row gone; reconciliation_log child cleaned up too — without
        # this, the FK without ON DELETE CASCADE would have blocked the delete.
        ids = await pg_source.fetchall(
            "SELECT id FROM entity.identity WHERE library_name = $1",
            "BandThatLeftRotation",
        )
        assert ids == []
        log_rows = await pg_source.fetchall(
            "SELECT identity_id FROM entity.reconciliation_log WHERE identity_id = $1",
            gone.id,
        )
        assert log_rows == []

    @pytest.mark.asyncio
    async def test_threshold_aborts_without_mutation(self, pg_source):
        """Past the drain threshold, the prune aborts and touches nothing.

        A corrupted ``library.db`` (zero rows, or with most names missing)
        would otherwise be interpreted as a giant rename and silently wipe
        the accumulated provenance. The threshold makes that loud.
        """
        from scripts.entity_resolution.__main__ import (
            OrphanDrainAbortError,
            prune_orphan_identities,
        )

        store = EntityStore(pg_source)

        # Seed 10 identities; the empty snapshot would orphan all 10.
        for i in range(10):
            await store.upsert_identity(library_name=f"orphan_{i}")

        with pytest.raises(OrphanDrainAbortError):
            await prune_orphan_identities(
                store,
                {"only_one"},
                threshold_abs=5,
                threshold_frac=0.01,
            )

        # Nothing mutated.
        rows = await pg_source.fetchall("SELECT count(*) AS n FROM entity.identity")
        assert rows[0]["n"] == 10

    @pytest.mark.asyncio
    async def test_diacritic_canonical_merge_with_canonical_fixture_artist(self, pg_source):
        """Mirror the Beyonce → Beyoncé case using WXYC canonical fixture artist.

        ``Nilüfer Yanya`` is the canonical diacritic-bearing fixture from
        ``wxycCanonicalArtistNames`` (see WXYC org-level CLAUDE.md, "Example
        Music Data"). Locks the orphan-pass behavior on a name we already
        use as the diacritic test artist throughout this codebase.
        """
        from scripts.entity_resolution.__main__ import prune_orphan_identities

        store = EntityStore(pg_source)

        old = await store.upsert_identity(library_name="Nilufer Yanya", discogs_artist_id=5499521)
        assert old is not None
        await store.log_reconciliation(
            identity_id=old.id, source="discogs", external_id="5499521", method="exact_match"
        )
        new = await store.upsert_identity(library_name="Nilüfer Yanya")
        assert new is not None

        merged, deleted, _ = await prune_orphan_identities(store, {"Nilüfer Yanya"})

        assert (merged, deleted) == (1, 0)
        rows = await pg_source.fetchall("SELECT library_name FROM entity.identity")
        assert [r["library_name"] for r in rows] == ["Nilüfer Yanya"]
        log_rows = await pg_source.fetchall("SELECT external_id FROM entity.reconciliation_log")
        assert [r["external_id"] for r in log_rows] == ["5499521"]


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
