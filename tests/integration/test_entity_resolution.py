"""Integration tests for the entity resolution pipeline.

These tests run against a test PostgreSQL (Docker on port 5433) with the
entity schema applied and Discogs fixture data loaded.

Run with: pytest -m pg -v
Requires: DATABASE_URL_TEST env var or Docker postgres on port 5433.
"""

from __future__ import annotations

from typing import ClassVar
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from entity.sources import PgSource
from entity.store import EntityStore
from scripts.entity_resolution.__main__ import (
    OrphanDrainAbortError,
    prune_orphan_identities,
    run_discogs_stage,
    seed_identities,
)
from scripts.entity_resolution.dedup import EntityDeduplicator
from scripts.entity_resolution.discogs import DiscogsReconciler, ReconciliationMatch

# ``pg_pool`` (max_size=3) lives in conftest; ``DATABASE_URL`` is imported from
# there for the ``source._dsn`` wiring and ``monkeypatch.setenv`` calls below.
from tests.integration.conftest import DATABASE_URL


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
            assert match.method in (
                "exact_match",
                "member_group",
                "alias_match",
                "name_variation",
                "name_preprocessing",
                "trigram_fallback",
            )


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
class TestTrigramFallbackSQLIntegration:
    """Exercise the Stage 6 ``_TRIGRAM_FALLBACK_SQL`` against real pg_trgm.

    The full ``reconcile_batch`` cascade needs ``wxyc_identity_match_artist``
    (alembic 0004, absent on CI's plain ``postgres:16-alpine``), so this class
    drives the Stage 6 method ``_trigram_match`` directly — it only depends on
    ``pg_trgm`` + ``f_unaccent``, both provisionable from contrib on the CI
    service container. See WXYC/library-metadata-lookup#215 (parent #211).

    Data safety: ``release_artist`` is a real, multi-million-row table on a live
    discogs-cache. This fixture **skips** if that table already exists rather
    than stubbing over it; it only creates (and on teardown drops) the table
    when absent, so it can never clobber real cache data. Same posture as
    ``TestDiscogsReconciliationSQLSmoke``'s ``created_tables`` guard.
    """

    @pytest_asyncio.fixture(autouse=True)
    async def set_up_trigram_fixture(self, pg_pool):
        async with pg_pool.acquire() as conn:
            try:
                await conn.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
                await conn.execute("CREATE EXTENSION IF NOT EXISTS unaccent")
            except Exception as e:  # locked-down Postgres without contrib
                pytest.skip(f"pg_trgm/unaccent extensions unavailable: {e}")

            release_artist_exists = await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = current_schema() AND table_name = 'release_artist')"
            )
            if release_artist_exists:
                pytest.skip(
                    "release_artist already present -- refusing to stub over real "
                    "discogs-cache data (run the cascade test against a live cache instead)"
                )

            # IMMUTABLE f_unaccent wrapper mirroring discogs-cache, so the
            # trigram operator can ride a functional GIN index. Idempotent.
            await conn.execute(
                "CREATE OR REPLACE FUNCTION f_unaccent(text) RETURNS text "
                "AS $$ SELECT unaccent('unaccent', $1) $$ "
                "LANGUAGE sql IMMUTABLE PARALLEL SAFE"
            )
            await conn.execute(
                "CREATE TABLE release_artist ("
                "release_id INTEGER, artist_id INTEGER, artist_name TEXT, extra INTEGER DEFAULT 0)"
            )
            await conn.executemany(
                "INSERT INTO release_artist (release_id, artist_id, artist_name, extra) "
                "VALUES ($1, $2, $3, $4)",
                [
                    (1, 4242, "Stereolab", 0),
                    (2, 5499521, "Nilufer Yanya", 0),  # diacritic-free cache form
                    (3, 7777, "Hot 8 Brass Band", 0),
                    # ``extra = 1`` credit that must be invisible to the fallback,
                    # mirroring the Stage 1 ``extra = 0`` filter.
                    (4, 9999, "Stereolab", 1),
                ],
            )
        try:
            yield
        finally:
            async with pg_pool.acquire() as conn:
                await conn.execute("DROP TABLE IF EXISTS release_artist")

    @pytest.mark.asyncio
    async def test_diacritic_variant_clears_default_threshold(self, pg_source):
        """``Nilüfer Yanya`` (umlaut) matches the diacritic-free cache row at 1.0.

        ``f_unaccent`` collapses both sides to ``nilufer yanya`` → similarity 1.0,
        well over the 0.85 floor; proves the operator + f_unaccent path end to end.
        """
        reconciler = DiscogsReconciler(pg_source)
        hit = await reconciler._trigram_match("Nilüfer Yanya")
        assert hit is not None
        artist_id, score = hit
        assert artist_id == 5499521
        assert score >= 0.85

    @pytest.mark.asyncio
    async def test_sub_unit_fuzzy_match_resolves_when_threshold_lowered(self, pg_source):
        """A genuine sub-1.0 fuzzy hit (typo ``Stereolabs``) resolves once the
        threshold is set below its pg_trgm score — proving the gate is the
        threshold, not an exact-string coincidence."""
        reconciler = DiscogsReconciler(pg_source, trigram_threshold=0.4)
        hit = await reconciler._trigram_match("Stereolabs")  # trailing 's'
        assert hit is not None
        artist_id, score = hit
        assert artist_id == 4242
        assert 0.4 <= score < 1.0

    @pytest.mark.asyncio
    async def test_substring_near_miss_rejected_at_default_threshold(self, pg_source):
        """``Hot 8`` must NOT match ``Hot 8 Brass Band`` — incidental substring
        overlap scores well under the 0.85 floor (the issue's motivating case)."""
        reconciler = DiscogsReconciler(pg_source)
        hit = await reconciler._trigram_match("Hot 8")
        assert hit is None

    @pytest.mark.asyncio
    async def test_extra_credit_excluded_from_fallback(self, pg_source):
        """The ``extra = 1`` Stereolab row (artist_id 9999) is filtered out, so the
        only Stereolab candidate is the primary ``extra = 0`` row (4242)."""
        reconciler = DiscogsReconciler(pg_source)
        hit = await reconciler._trigram_match("Stereolab")
        assert hit is not None
        artist_id, _score = hit
        assert artist_id == 4242

    @pytest.mark.asyncio
    async def test_raw_sql_score_in_unit_interval(self, pg_source):
        """``_TRIGRAM_FALLBACK_SQL`` parses, executes, and returns a float
        similarity in [0, 1] for a fuzzy query."""
        from scripts.entity_resolution.discogs import _TRIGRAM_FALLBACK_SQL

        rows = await pg_source.fetchall(_TRIGRAM_FALLBACK_SQL, "Stereolab")
        assert rows, "expected at least the Stereolab primary-credit candidate"
        score = float(rows[0]["score"])
        assert 0.0 <= score <= 1.0


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
        import core.dependencies as core_deps
        import identity.dependencies as deps
        from config.settings import get_settings

        monkeypatch.setenv("DATABASE_URL_DISCOGS", DATABASE_URL)
        get_settings.cache_clear()
        deps._entity_store = None
        deps._entity_probe_failed = False
        # Post-WXYC#395 the entity store reuses ``core.dependencies.get_discogs_pool``.
        # Reset the shared pool so each test sees a fresh init cycle.
        await core_deps.close_discogs_pool()

        from main import app

        yield app

        deps._entity_store = None
        deps._entity_probe_failed = False
        await core_deps.close_discogs_pool()
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
    """Acceptance tests for LML#377: the orphan pass after seed_identities.

    The Beyonce → Beyoncé scenario is the motivating case in the issue body
    — these tests pin the contract: reconciliation_log provenance is either
    *preserved* (merge path) or *explicitly removed* (delete path), never
    silently abandoned under the new orphan row.

    Production ordering: ``main()`` runs ``seed_identities(snapshot)`` first
    (upserting a new empty row for every current artist name), then
    ``prune_orphan_identities(snapshot)`` to merge or delete the names no
    longer in the snapshot. These tests mirror that ordering so the merge
    path's target row exists at the time of the merge call.
    """

    @pytest.mark.asyncio
    async def test_rename_preserves_provenance_via_merge(self, pg_source):
        """Beyonce → Beyoncé: prior reconciliation_log preserved on the new row.

        Mirrors the full production sequence: librarian had ``"Beyonce"``
        reconciled; renames to ``"Beyoncé"`` in library.db. The next
        reconciliation run calls ``seed_identities(["Beyoncé"])`` (adds an
        empty new row) and then ``prune_orphan_identities({"Beyoncé"})``
        which merges the orphan's provenance into the new row.
        """

        store = EntityStore(pg_source)

        # Pre-existing state: librarian had "Beyonce" with a reconciled Discogs ID.
        old = await store.upsert_identity(library_name="Beyonce", discogs_artist_id=1419)
        assert old is not None
        await store.log_reconciliation(
            identity_id=old.id,
            source="discogs",
            external_id="1419",
            method="exact_match",
            confidence=1.0,
        )

        # Production flow: seed first (adds empty new row), then prune.
        current_snapshot = ["Beyoncé"]
        await seed_identities(store, current_snapshot)
        merged, deleted, orphans = await prune_orphan_identities(store, set(current_snapshot))

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
    async def test_removal_deletes_identity_and_logs(self, pg_source):
        """Truly-removed artist: identity AND its log rows both go (no FK leak).

        Librarian retires an artist from the library entirely (not a rename).
        After seed_identities, the orphan has no canonical-form sibling in
        the snapshot and falls through to hard delete.
        """

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

        # Production flow: seed the new snapshot, then prune the orphan.
        current_snapshot = ["DifferentBand"]
        await seed_identities(store, current_snapshot)
        merged, deleted, orphans = await prune_orphan_identities(store, set(current_snapshot))

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
        # "DifferentBand" (the surviving new entry) is still there.
        survivors = await pg_source.fetchall("SELECT library_name FROM entity.identity")
        assert [r["library_name"] for r in survivors] == ["DifferentBand"]

    @pytest.mark.asyncio
    async def test_threshold_aborts_without_mutation(self, pg_source):
        """Past the drain threshold, the prune aborts and touches nothing.

        A corrupted ``library.db`` (zero rows, or with most names missing)
        would otherwise be interpreted as a giant rename and silently wipe
        the accumulated provenance. The threshold makes that loud.
        """

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
        """Mirror the rename-merge case using WXYC canonical fixture artist.

        ``Nilüfer Yanya`` is the canonical diacritic-bearing fixture from
        ``wxycCanonicalArtistNames`` (see WXYC org-level CLAUDE.md, "Example
        Music Data"). Locks the orphan-pass behavior on a name we already
        use as the diacritic test artist throughout this codebase.
        """

        store = EntityStore(pg_source)

        old = await store.upsert_identity(library_name="Nilufer Yanya", discogs_artist_id=5499521)
        assert old is not None
        await store.log_reconciliation(
            identity_id=old.id, source="discogs", external_id="5499521", method="exact_match"
        )

        current_snapshot = ["Nilüfer Yanya"]
        await seed_identities(store, current_snapshot)
        merged, deleted, _ = await prune_orphan_identities(store, set(current_snapshot))

        assert (merged, deleted) == (1, 0)
        rows = await pg_source.fetchall("SELECT library_name FROM entity.identity")
        assert [r["library_name"] for r in rows] == ["Nilüfer Yanya"]
        log_rows = await pg_source.fetchall("SELECT external_id FROM entity.reconciliation_log")
        assert [r["external_id"] for r in log_rows] == ["5499521"]

    @pytest.mark.asyncio
    async def test_merge_carries_wikidata_qid_and_reconciled_status(self, pg_source):
        """Orphan's Wikidata QID + reconciled status survive the rename merge.

        Regression pin for the bug surfaced by /review-loop: a librarian's
        rename ("Beyonce" → "Beyoncé") on a row that the Wikidata bridge had
        already populated would silently drop the QID and leave the merged
        row's reconciliation_status at the freshly-seeded default of
        'unreconciled'. Both losses defeat the orphan-pass's whole point
        (provenance preservation) and trigger wasted re-fetches on the next
        reconciliation pass.
        """
        store = EntityStore(pg_source)

        # Pre-existing fully-reconciled orphan: Discogs ID, Wikidata QID,
        # status='reconciled', and a log row.
        old = await store.upsert_identity(
            library_name="Beyonce",
            discogs_artist_id=1419,
            wikidata_qid="Q36153",
        )
        assert old is not None
        await store.update_status(old.id, "reconciled")
        await store.log_reconciliation(
            identity_id=old.id,
            source="wikidata",
            external_id="Q36153",
            method="discogs_bridge",
        )

        await seed_identities(store, ["Beyoncé"])
        merged, deleted, _ = await prune_orphan_identities(store, {"Beyoncé"})
        assert (merged, deleted) == (1, 0)

        # Surviving row carries the QID + reconciled status.
        rows = await pg_source.fetchall(
            "SELECT library_name, discogs_artist_id, wikidata_qid, reconciliation_status "
            "FROM entity.identity"
        )
        assert len(rows) == 1
        assert rows[0]["library_name"] == "Beyoncé"
        assert rows[0]["discogs_artist_id"] == 1419
        assert rows[0]["wikidata_qid"] == "Q36153"
        assert rows[0]["reconciliation_status"] == "reconciled"

    @pytest.mark.asyncio
    async def test_idempotent_on_re_run(self, pg_source):
        """Second seed+prune over the same snapshot is a no-op.

        After a successful rename merge, the next reconciliation run sees
        ``stored_names == current_names`` and the orphan diff is empty — no
        further merges, no further deletes, no reconciliation_log shuffling.
        """

        store = EntityStore(pg_source)

        old = await store.upsert_identity(library_name="Beyonce", discogs_artist_id=1419)
        assert old is not None
        await store.log_reconciliation(
            identity_id=old.id, source="discogs", external_id="1419", method="exact_match"
        )

        snapshot = ["Beyoncé"]
        # Run 1: the rename merge.
        await seed_identities(store, snapshot)
        m1, d1, _ = await prune_orphan_identities(store, set(snapshot))
        assert (m1, d1) == (1, 0)

        # Run 2 over the same snapshot: nothing to do.
        await seed_identities(store, snapshot)
        m2, d2, orphans = await prune_orphan_identities(store, set(snapshot))
        assert (m2, d2, orphans) == (0, 0, [])

        rows = await pg_source.fetchall("SELECT library_name FROM entity.identity")
        assert [r["library_name"] for r in rows] == ["Beyoncé"]
        log_rows = await pg_source.fetchall("SELECT count(*) AS n FROM entity.reconciliation_log")
        assert log_rows[0]["n"] == 1


@pytest.mark.pg
class TestSeedIdentitiesCompilationGuard:
    """Acceptance tests for LML#385: V/A filings never enter entity.identity.

    The audit on LML#379 found 67 polluted rows in prod ``entity.identity``
    seeded by prior runs of this function. The guard adds an
    ``is_compilation_artist`` check inside the upsert loop; these PG-backed
    tests pin the contract end-to-end.
    """

    @pytest.mark.asyncio
    async def test_seed_skips_v_a_rows_in_pg(self, pg_source):
        """V/A bucket entries from the librarian's shelf-navigation filings stay out."""
        store = EntityStore(pg_source)
        snapshot = [
            "Various Artists - Soundtracks",
            "Soundtracks - A",
            "V/A - Rock - C",
            "Juana Molina",
        ]

        await seed_identities(store, snapshot)

        rows = await pg_source.fetchall(
            "SELECT library_name FROM entity.identity ORDER BY library_name"
        )
        assert [r["library_name"] for r in rows] == ["Juana Molina"]

    @pytest.mark.asyncio
    async def test_seed_preserves_real_artists_with_v_a_lookalikes(self, pg_source):
        """The wxyc-etl 0.5.0 keep-set: anchored matcher must not trip on real artists.

        ``Epic Soundtracks`` (Swell Maps drummer) and ``The 27 Various``
        (Minneapolis indie band) end in / contain the V/A token but are not
        V/A filings. They must seed successfully.
        """
        store = EntityStore(pg_source)
        snapshot = ["Epic Soundtracks", "The 27 Various", "Jessica Pratt"]

        await seed_identities(store, snapshot)

        rows = await pg_source.fetchall(
            "SELECT library_name FROM entity.identity ORDER BY library_name"
        )
        assert [r["library_name"] for r in rows] == [
            "Epic Soundtracks",
            "Jessica Pratt",
            "The 27 Various",
        ]


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


# §3.4.1 locked bands (inclusive) keyed by method, mirroring Backend's §3.2.2
# write-contract sanity-check ranges. A row whose (method, confidence) lands
# outside its band would be rejected by Backend's writer.
_METHOD_BANDS_341: dict[str, tuple[float, float]] = {
    "exact_match": (1.00, 1.00),
    "name_variation": (0.90, 0.99),
    "member_group": (0.80, 0.89),
    "alias_match": (0.75, 0.84),
}


@pytest.mark.pg
class TestDiscogsStageConfidencePersisted:
    """LML#233: `run_discogs_stage` persists per-method confidence to PG.

    Runs the real `EntityStore` write path against PostgreSQL with a stubbed
    reconciler so the assertion covers the REAL-column round-trip (confidence
    is stored as a 4-byte `REAL`, so we assert bands, not float equality).
    """

    @pytest.mark.parametrize("method", list(_METHOD_BANDS_341))
    @pytest.mark.asyncio
    async def test_logged_confidence_within_341_band(self, pg_source, method):
        store = EntityStore(pg_source)
        await store.upsert_identity(library_name="Stereolab")

        reconciler = AsyncMock()
        reconciler.reconcile_batch = AsyncMock(
            return_value={"Stereolab": ReconciliationMatch(discogs_artist_id=2154, method=method)}
        )

        await run_discogs_stage(store, reconciler, batch_size=1000)

        rows = await pg_source.fetchall(
            "SELECT method, confidence FROM entity.reconciliation_log WHERE source = 'discogs'"
        )
        assert len(rows) == 1
        assert rows[0]["method"] == method
        lo, hi = _METHOD_BANDS_341[method]
        # Pad the band by the float32 storage epsilon so e.g. 0.85 → 0.8500000238
        # doesn't fall foul of an exact band edge.
        assert lo - 1e-6 <= rows[0]["confidence"] <= hi + 1e-6


@pytest.mark.pg
class TestLatestProvenanceTiebreak:
    """LML#233: identical `created_at` rows resolve deterministically by id."""

    @pytest.mark.asyncio
    async def test_identical_created_at_breaks_by_id_desc(self, pg_source):
        store = EntityStore(pg_source)
        identity = await store.upsert_identity(library_name="Stereolab")

        # Two discogs rows for the same identity at the SAME created_at (a SQL
        # literal so both rows land on the identical microsecond); the
        # second-inserted (higher id) row must win the DISTINCT ON pick.
        await pg_source.execute(
            "INSERT INTO entity.reconciliation_log "
            "(identity_id, source, external_id, confidence, method, created_at) "
            "VALUES ($1, 'discogs', '111', 0.85, 'member_group', "
            "TIMESTAMPTZ '2026-01-01 00:00:00+00')",
            identity.id,
        )
        await pg_source.execute(
            "INSERT INTO entity.reconciliation_log "
            "(identity_id, source, external_id, confidence, method, created_at) "
            "VALUES ($1, 'discogs', '222', 1.0, 'exact_match', "
            "TIMESTAMPTZ '2026-01-01 00:00:00+00')",
            identity.id,
        )

        # Repeat the read to prove stability — without the id tiebreak PG could
        # return either row on each call.
        for _ in range(5):
            prov = await store.get_latest_provenance_by_source(identity.id)
            assert prov["discogs"].external_id == "222"
            assert prov["discogs"].method == "exact_match"
