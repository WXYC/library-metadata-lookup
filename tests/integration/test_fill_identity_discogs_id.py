"""Integration tests for ``EntityStore.fill_identity_discogs_id`` (LML#766).

The fill-if-null mint primitive: sets ``entity.identity.discogs_artist_id``
only when the stored value is NULL, so a concurrent writer's non-null id can
never be silently clobbered. The loser observes the loss via RETURNING and the
store logs it loudly.

Run with: pytest -m pg -v
Requires: DATABASE_URL_TEST env var or Docker postgres on port 5433.
"""

from __future__ import annotations

import asyncio
import logging

import pytest
import pytest_asyncio

from entity.sources import PgSource
from entity.store import EntityStore
from tests.integration.conftest import DATABASE_URL


def _source(pg_pool) -> PgSource:
    source = PgSource.__new__(PgSource)
    source._dsn = DATABASE_URL
    source._pool = pg_pool
    return source


@pytest_asyncio.fixture
async def pg_source(pg_pool):
    """PgSource wrapping the test pool."""
    return _source(pg_pool)


@pytest_asyncio.fixture(autouse=True)
async def set_up_entity_schema(pg_pool):
    """Create (or re-create) the entity schema for each test.

    Mirrors ``test_entity_resolution.py``'s fixture -- the discogs-cache
    alembic shape (``reconciliation_status TEXT NOT NULL DEFAULT
    'unreconciled'``, case-sensitive UNIQUE on ``library_name``).
    """
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
    yield
    async with pg_pool.acquire() as conn:
        await conn.execute("DROP SCHEMA IF EXISTS entity CASCADE")


@pytest.mark.pg
class TestFillIdentityDiscogsId:
    """Fill-if-null semantics, lost-race detection, minted status."""

    @pytest.mark.asyncio
    async def test_insert_on_absent_row_fills_and_marks_reconciled(self, pg_source):
        """First sight: the row is minted with our id and a reconciled
        status so reconciler campaigns don't reprocess it."""
        store = EntityStore(pg_source)
        outcome = await store.fill_identity_discogs_id("Wishy", 123)
        assert outcome.filled is True
        assert outcome.lost_race is False
        assert outcome.identity is not None
        assert outcome.identity.discogs_artist_id == 123
        assert outcome.identity.reconciliation_status == "reconciled"
        assert outcome.previous_discogs_artist_id is None

    @pytest.mark.asyncio
    async def test_fill_null_column_on_existing_row(self, pg_source):
        """An existing row with a NULL discogs id and other columns
        populated gets its id filled without clobbering siblings."""
        store = EntityStore(pg_source)
        await store.upsert_identity(library_name="Cat Power", spotify_artist_id="spotify-cat-power")
        outcome = await store.fill_identity_discogs_id("Cat Power", 3081)
        assert outcome.filled is True
        assert outcome.identity is not None
        assert outcome.identity.discogs_artist_id == 3081
        assert outcome.identity.spotify_artist_id == "spotify-cat-power"
        assert outcome.previous_discogs_artist_id is None

    @pytest.mark.asyncio
    async def test_no_clobber_when_id_already_present_same(self, pg_source):
        """Idempotent re-fill with the SAME id: not a lost race, the row
        already holds our value."""
        store = EntityStore(pg_source)
        await store.fill_identity_discogs_id("Stereolab", 99)
        outcome = await store.fill_identity_discogs_id("Stereolab", 99)
        assert outcome.filled is False
        assert outcome.lost_race is False
        assert outcome.identity is not None
        assert outcome.identity.discogs_artist_id == 99
        assert outcome.previous_discogs_artist_id == 99

    @pytest.mark.asyncio
    async def test_lost_race_when_id_already_present_different(self, pg_source, caplog):
        """A non-null id different from ours is NEVER overwritten; the
        caller observes the loss via RETURNING and the store logs loudly."""
        store = EntityStore(pg_source)
        # A concurrent writer already set a (higher-evidence) id.
        await store.upsert_identity(library_name="Popsicle", discogs_artist_id=111)
        with caplog.at_level(logging.WARNING, logger="entity.store"):
            outcome = await store.fill_identity_discogs_id("Popsicle", 222)
        assert outcome.filled is False
        assert outcome.lost_race is True
        assert outcome.identity is not None
        # The winner's id survives -- our 222 did NOT overwrite 111.
        assert outcome.identity.discogs_artist_id == 111
        assert outcome.previous_discogs_artist_id == 111
        # Loud log names both values so the clobbered attempt leaves a trail.
        assert any(
            "111" in rec.getMessage() and "222" in rec.getMessage() for rec in caplog.records
        ), caplog.text

    @pytest.mark.asyncio
    async def test_concurrent_fill_cannot_clobber_non_null_id(self, pg_pool):
        """Two-writer interleave: even racing, the non-null id survives and
        at most one writer reports ``filled`` -- a losing writer sees
        ``lost_race``, never a silent overwrite.

        Each writer runs on its own pool connection (its own EntityStore) so
        the two ``INSERT ... ON CONFLICT`` statements genuinely contend on the
        UNIQUE index lock rather than serialising on a shared connection.
        """
        store_a = EntityStore(_source(pg_pool))
        store_b = EntityStore(_source(pg_pool))

        # No pre-existing row: both writers race to create it with DIFFERENT
        # ids. The UNIQUE index serialises them; whoever inserts first wins,
        # and the fill-if-null guard blocks the loser's DO UPDATE.
        results = await asyncio.gather(
            store_a.fill_identity_discogs_id("Sault", 1001),
            store_b.fill_identity_discogs_id("Sault", 2002),
        )
        filled = [r for r in results if r.filled]
        lost = [r for r in results if r.lost_race]
        assert len(filled) == 1, "exactly one writer may fill"
        assert len(lost) == 1, "the other writer must observe the loss"

        # The stored id is whichever writer won; the loser's id is absent.
        winner_id = filled[0].identity.discogs_artist_id
        assert winner_id in (1001, 2002)
        async with pg_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT discogs_artist_id FROM entity.identity WHERE library_name = $1",
                "Sault",
            )
        assert len(rows) == 1
        assert rows[0]["discogs_artist_id"] == winner_id
        # The loser saw the winner's id, not its own.
        assert lost[0].previous_discogs_artist_id == winner_id

    @pytest.mark.asyncio
    async def test_upsert_new_wins_semantics_still_available(self, pg_source):
        """``upsert_identity`` keeps its new-wins-when-non-null contract for
        flows that legitimately UPDATE an id -- the fill primitive does not
        replace it."""
        store = EntityStore(pg_source)
        await store.upsert_identity(library_name="Grimes", discogs_artist_id=10)
        # upsert overwrites a non-null id with a new non-null id.
        await store.upsert_identity(library_name="Grimes", discogs_artist_id=20)
        identity = await store.get_identity("Grimes")
        assert identity is not None
        assert identity.discogs_artist_id == 20


@pytest.mark.pg
class TestFillIdentityCaseSibling:
    """The case-sibling awareness leg (#766 point 4)."""

    @pytest.mark.asyncio
    async def test_fills_id_bearing_case_sibling_instead_of_new_variant(self, pg_source):
        """An id-bearing ``WISHY`` sibling short-circuits a ``wishy`` fill:
        no new case-variant row is accreted, and the existing id is
        returned rather than a conflicting second row minted."""
        store = EntityStore(pg_source)
        # Existing id-bearing row under a different casing.
        await store.upsert_identity(library_name="WISHY", discogs_artist_id=123)
        outcome = await store.fill_identity_discogs_id("wishy", 123)
        # No new row: the case sibling already answers.
        async with pg_source.acquire() as conn:
            rows = await conn.fetch(
                "SELECT library_name FROM entity.identity WHERE LOWER(library_name) = LOWER($1)",
                "wishy",
            )
        assert len(rows) == 1, "must not accrete a case-variant row"
        assert rows[0]["library_name"] == "WISHY"
        assert outcome.filled is False
        assert outcome.identity is not None
        assert outcome.identity.discogs_artist_id == 123

    @pytest.mark.asyncio
    async def test_case_sibling_conflict_is_lost_race(self, pg_source):
        """An id-bearing case sibling holding a DIFFERENT id is a lost race,
        not a silent second-row mint."""
        store = EntityStore(pg_source)
        await store.upsert_identity(library_name="WISHY", discogs_artist_id=123)
        outcome = await store.fill_identity_discogs_id("wishy", 999)
        assert outcome.filled is False
        assert outcome.lost_race is True
        assert outcome.identity is not None
        assert outcome.identity.discogs_artist_id == 123
        async with pg_source.acquire() as conn:
            count = await conn.fetchval("SELECT count(*) FROM entity.identity")
        assert count == 1, "no case-variant row minted on conflict"
