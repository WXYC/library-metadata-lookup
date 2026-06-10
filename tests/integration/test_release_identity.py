"""Integration tests for the release-identity layer added by LML#526.

Two surfaces:

- ``TestReleaseIdentityStore`` exercises the new ``EntityStore`` methods
  (``mint_or_get_release_identity`` / ``get_release_identity_by_source`` /
  ``log_release_reconciliation``) against the real ``entity.release_identity``
  and ``entity.release_reconciliation_log`` tables.

- ``TestReleaseIdentityResolveEndpoint`` drives ``POST /api/v1/identity/resolve``
  end-to-end through the FastAPI app.

The ``set_up_entity_schema`` fixture creates BOTH the artist-side tables
(``entity.identity``, ``entity.reconciliation_log``) and the new release-side
tables. The artist tables are required so the entity store's probe
(``SELECT 1 FROM entity.identity LIMIT 0``) passes — without it the
endpoint returns 503 before the new code path runs.

Run with: pytest -m pg -v tests/integration/test_release_identity.py
"""

from __future__ import annotations

import asyncio
import os

import asyncpg
import pytest
import pytest_asyncio

from entity.sources import PgSource
from entity.store import EntityStore

DATABASE_URL = os.getenv(
    "DATABASE_URL_TEST",
    "postgresql://discogs:discogs@localhost:5433/discogs",
)


@pytest_asyncio.fixture
async def pg_pool():
    try:
        pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=4)
    except Exception as e:
        pytest.skip(f"Cannot connect to test PostgreSQL: {e}")
        return
    yield pool
    await pool.close()


@pytest_asyncio.fixture
async def pg_source(pg_pool):
    source = PgSource.__new__(PgSource)
    source._dsn = DATABASE_URL
    source._pool = pg_pool
    return source


@pytest_asyncio.fixture(autouse=True)
async def set_up_entity_schema(pg_pool):
    """Drop + recreate the entity schema, including the new release tables."""
    async with pg_pool.acquire() as conn:
        await conn.execute("DROP SCHEMA IF EXISTS entity CASCADE")
        await conn.execute("CREATE SCHEMA entity")
        # Artist-side tables — required so the entity store probe passes.
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
        # Release-side tables — added by LML#526.
        await conn.execute("""
            CREATE TABLE entity.release_identity (
                id SERIAL PRIMARY KEY,
                discogs_release_id INTEGER UNIQUE,
                discogs_master_id INTEGER UNIQUE,
                musicbrainz_release_id TEXT UNIQUE,
                spotify_album_id TEXT UNIQUE,
                apple_music_album_id TEXT UNIQUE,
                bandcamp_album_url TEXT UNIQUE,
                reconciliation_status TEXT NOT NULL DEFAULT 'unreconciled',
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        await conn.execute("""
            CREATE TABLE entity.release_reconciliation_log (
                id SERIAL PRIMARY KEY,
                identity_id INTEGER NOT NULL REFERENCES entity.release_identity(id),
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
class TestReleaseIdentityStore:
    """Store-level CRUD for the release-identity table."""

    @pytest.mark.asyncio
    async def test_mint_creates_row_and_logs_reconciliation(self, pg_source):
        store = EntityStore(pg_source)
        identity_id, minted = await store.mint_or_get_release_identity(
            source="discogs_release", external_id="12345"
        )
        assert minted is True
        assert identity_id > 0
        # Row exists with the right column populated.
        row = await pg_source.fetchone(
            "SELECT discogs_release_id, discogs_master_id, bandcamp_album_url "
            "FROM entity.release_identity WHERE id = $1",
            identity_id,
        )
        assert row["discogs_release_id"] == 12345
        assert row["discogs_master_id"] is None
        assert row["bandcamp_album_url"] is None
        # And the reconciliation log carries the originating pair.
        logs = await pg_source.fetchall(
            "SELECT source, external_id, method, confidence "
            "FROM entity.release_reconciliation_log WHERE identity_id = $1",
            identity_id,
        )
        assert len(logs) == 1
        assert logs[0]["source"] == "discogs_release"
        assert logs[0]["external_id"] == "12345"
        assert logs[0]["method"] == "exact_match"
        assert logs[0]["confidence"] == pytest.approx(1.0)

    @pytest.mark.asyncio
    async def test_idempotent_re_resolve_writes_no_log(self, pg_source):
        store = EntityStore(pg_source)
        first_id, first_minted = await store.mint_or_get_release_identity(
            source="discogs_release", external_id="12345"
        )
        second_id, second_minted = await store.mint_or_get_release_identity(
            source="discogs_release", external_id="12345"
        )
        assert first_minted is True
        assert second_minted is False
        assert first_id == second_id
        # Critical: re-resolve writes no reconciliation row — the act of
        # looking up carries no new evidence (per LML#526).
        logs = await pg_source.fetchall(
            "SELECT id FROM entity.release_reconciliation_log WHERE identity_id = $1",
            first_id,
        )
        assert len(logs) == 1

    @pytest.mark.asyncio
    async def test_mints_distinct_identities_for_distinct_external_ids(self, pg_source):
        store = EntityStore(pg_source)
        id_a, _ = await store.mint_or_get_release_identity(
            source="discogs_release", external_id="111"
        )
        id_b, _ = await store.mint_or_get_release_identity(
            source="discogs_release", external_id="222"
        )
        assert id_a != id_b

    @pytest.mark.asyncio
    async def test_mint_bandcamp_persists_canonical_url(self, pg_source):
        store = EntityStore(pg_source)
        url = "https://autechre.bandcamp.com/album/confield"
        identity_id, minted = await store.mint_or_get_release_identity(
            source="bandcamp", external_id=url
        )
        assert minted is True
        row = await pg_source.fetchone(
            "SELECT bandcamp_album_url FROM entity.release_identity WHERE id = $1",
            identity_id,
        )
        assert row["bandcamp_album_url"] == url

    @pytest.mark.asyncio
    async def test_get_release_identity_by_source_hits_existing_row(self, pg_source):
        store = EntityStore(pg_source)
        minted_id, _ = await store.mint_or_get_release_identity(
            source="discogs_master", external_id="789"
        )
        found = await store.get_release_identity_by_source(
            source="discogs_master", external_id="789"
        )
        assert found == minted_id

    @pytest.mark.asyncio
    async def test_get_release_identity_by_source_misses_returns_none(self, pg_source):
        store = EntityStore(pg_source)
        assert (
            await store.get_release_identity_by_source(
                source="discogs_release", external_id="999999"
            )
            is None
        )

    @pytest.mark.asyncio
    async def test_concurrent_mint_converges_on_one_identity(self, pg_source):
        """Two concurrent mint calls with the same input must converge.

        Without the unique constraint + ON CONFLICT, the second concurrent
        INSERT would either succeed (two rows) or block on the unique
        index. With the documented mint protocol, both callers end up with
        the same identity_id, one log row total, and one row in
        entity.release_identity.
        """
        store = EntityStore(pg_source)
        results = await asyncio.gather(
            store.mint_or_get_release_identity("discogs_release", "55555"),
            store.mint_or_get_release_identity("discogs_release", "55555"),
        )
        ids = {r[0] for r in results}
        assert len(ids) == 1, f"expected one identity_id, got {ids}"
        # Exactly one caller minted; the other got the existing row.
        minted_count = sum(1 for _, minted in results if minted)
        assert minted_count == 1
        # One row in the identity table.
        identity_rows = await pg_source.fetchall(
            "SELECT id FROM entity.release_identity WHERE discogs_release_id = 55555"
        )
        assert len(identity_rows) == 1
        # And exactly one reconciliation log row — the minter's.
        log_rows = await pg_source.fetchall(
            "SELECT id FROM entity.release_reconciliation_log WHERE identity_id = $1",
            identity_rows[0]["id"],
        )
        assert len(log_rows) == 1


@pytest_asyncio.fixture
async def app_client(monkeypatch):
    """ASGI client with the LML app pointed at the test PG.

    Mirrors the fixture in ``tests/integration/test_bulk_resolve_libraries.py`` —
    monkeypatch the DSN, clear the entity-store singleton, then build a fresh
    app. ``LML_REQUIRE_AUTH`` stays off by default (matching the prod rollout
    posture); the auth-required case is covered by its own test below.
    """
    from httpx import ASGITransport, AsyncClient

    import core.dependencies as core_deps
    import identity.dependencies as deps
    from config.settings import get_settings

    monkeypatch.setenv("DATABASE_URL_DISCOGS", DATABASE_URL)
    get_settings.cache_clear()
    deps._entity_store = None
    deps._entity_probe_failed = False
    await core_deps.close_discogs_pool()

    from main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac

    deps._entity_store = None
    deps._entity_probe_failed = False
    await core_deps.close_discogs_pool()
    get_settings.cache_clear()


@pytest.mark.pg
class TestReleaseIdentityResolveEndpoint:
    """End-to-end POST /api/v1/identity/resolve."""

    @pytest.mark.asyncio
    async def test_first_call_mints(self, app_client):
        resp = await app_client.post(
            "/api/v1/identity/resolve",
            json={
                "kind": "release",
                "source": "discogs_release",
                "external_id": "12345",
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["kind"] == "release"
        assert body["minted"] is True
        assert isinstance(body["identity_id"], int)
        assert body["identity_id"] > 0

    @pytest.mark.asyncio
    async def test_second_call_is_idempotent(self, app_client):
        first = await app_client.post(
            "/api/v1/identity/resolve",
            json={
                "kind": "release",
                "source": "discogs_release",
                "external_id": "12345",
            },
        )
        second = await app_client.post(
            "/api/v1/identity/resolve",
            json={
                "kind": "release",
                "source": "discogs_release",
                "external_id": "12345",
            },
        )
        assert first.status_code == 200
        assert second.status_code == 200
        assert second.json()["minted"] is False
        assert second.json()["identity_id"] == first.json()["identity_id"]

    @pytest.mark.asyncio
    async def test_bandcamp_round_trip(self, app_client):
        url = "https://autechre.bandcamp.com/album/confield"
        resp = await app_client.post(
            "/api/v1/identity/resolve",
            json={"kind": "release", "source": "bandcamp", "external_id": url},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["minted"] is True
        assert body["kind"] == "release"

    @pytest.mark.asyncio
    async def test_bandcamp_url_canonicalisation_collapses_trailing_slash(self, app_client):
        # Trailing-slash variant must hit the same identity row as the
        # bare form — the validator canonicalises before mint.
        bare = await app_client.post(
            "/api/v1/identity/resolve",
            json={
                "kind": "release",
                "source": "bandcamp",
                "external_id": "https://autechre.bandcamp.com/album/confield",
            },
        )
        trailing = await app_client.post(
            "/api/v1/identity/resolve",
            json={
                "kind": "release",
                "source": "bandcamp",
                "external_id": "https://autechre.bandcamp.com/album/confield/",
            },
        )
        assert bare.status_code == 200 and trailing.status_code == 200
        assert trailing.json()["minted"] is False
        assert trailing.json()["identity_id"] == bare.json()["identity_id"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("source", "external_id"),
        [
            ("discogs_release", "0"),
            ("discogs_release", "-1"),
            ("discogs_release", "abc"),
            ("discogs_master", "0"),
            ("bandcamp", "not a url"),
            ("bandcamp", "https://autechre.bandcamp.com/track/foo"),
        ],
    )
    async def test_invalid_external_id_is_rejected_before_mint(
        self, app_client, source, external_id, pg_source
    ):
        resp = await app_client.post(
            "/api/v1/identity/resolve",
            json={"kind": "release", "source": source, "external_id": external_id},
        )
        assert resp.status_code == 422, resp.text
        # No identity row should have been written.
        row = await pg_source.fetchone("SELECT count(*)::int AS n FROM entity.release_identity")
        assert row["n"] == 0

    @pytest.mark.asyncio
    async def test_unknown_source_rejected_by_pydantic(self, app_client):
        resp = await app_client.post(
            "/api/v1/identity/resolve",
            json={
                "kind": "release",
                "source": "musicbrainz_release",
                "external_id": "abc",
            },
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_unknown_kind_rejected_by_pydantic(self, app_client):
        resp = await app_client.post(
            "/api/v1/identity/resolve",
            json={
                "kind": "artist",
                "source": "discogs_release",
                "external_id": "12345",
            },
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_requires_auth_when_enabled(self, monkeypatch, app_client):
        """With LML_REQUIRE_AUTH=true and no header, the endpoint returns 401."""
        from config.settings import get_settings

        monkeypatch.setenv("LML_REQUIRE_AUTH", "true")
        monkeypatch.setenv("LML_API_KEY", "sekret")
        # get_settings is @lru_cache'd — clear before the test sees the new
        # env, and again after, so a later test that re-imports the cache
        # doesn't pick up the LML_REQUIRE_AUTH=true settings from this one's
        # warm cache. monkeypatch restores the env var on teardown, but the
        # cache entry it primed survives unless we clear it here.
        get_settings.cache_clear()
        try:
            resp = await app_client.post(
                "/api/v1/identity/resolve",
                json={
                    "kind": "release",
                    "source": "discogs_release",
                    "external_id": "12345",
                },
            )
            assert resp.status_code == 401
        finally:
            get_settings.cache_clear()
