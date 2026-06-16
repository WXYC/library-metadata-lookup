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
        # Mirrors the production DDL in entity/release_identity.sql so the
        # fixture has the same query-plan shape as prod.
        await conn.execute(
            "CREATE INDEX idx_release_reconciliation_log_identity_id "
            "ON entity.release_reconciliation_log(identity_id)"
        )
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
        # The Bandcamp mint path must also write a reconciliation log row
        # with the canonical URL — symmetric with the Discogs assertions in
        # test_mint_creates_row_and_logs_reconciliation, but exercising the
        # TEXT-bound bind path that the Discogs case does not.
        logs = await pg_source.fetchall(
            "SELECT source, external_id, method, confidence "
            "FROM entity.release_reconciliation_log WHERE identity_id = $1",
            identity_id,
        )
        assert len(logs) == 1
        assert logs[0]["source"] == "bandcamp"
        assert logs[0]["external_id"] == url
        assert logs[0]["method"] == "exact_match"
        assert logs[0]["confidence"] == pytest.approx(1.0)

    @pytest.mark.asyncio
    async def test_mint_apple_music_album_persists_album_id(self, pg_source):
        store = EntityStore(pg_source)
        album_id = "1234567890"
        identity_id, minted = await store.mint_or_get_release_identity(
            source="apple_music_album", external_id=album_id
        )
        assert minted is True
        row = await pg_source.fetchone(
            "SELECT apple_music_album_id FROM entity.release_identity WHERE id = $1",
            identity_id,
        )
        assert row["apple_music_album_id"] == album_id
        # The apple_music_album mint path must also write a reconciliation
        # log row with the canonical album_id — symmetric with Bandcamp
        # (the other TEXT-bound source) and Discogs (the integer-bound
        # sources). Exercises the TEXT bind path for the
        # apple_music_album_id column.
        logs = await pg_source.fetchall(
            "SELECT source, external_id, method, confidence "
            "FROM entity.release_reconciliation_log WHERE identity_id = $1",
            identity_id,
        )
        assert len(logs) == 1
        assert logs[0]["source"] == "apple_music_album"
        assert logs[0]["external_id"] == album_id
        assert logs[0]["method"] == "exact_match"
        assert logs[0]["confidence"] == pytest.approx(1.0)

    @pytest.mark.asyncio
    async def test_mint_apple_music_album_idempotent(self, pg_source):
        # Second resolve must return the same identity_id and write no new
        # reconciliation row — re-resolves carry no new evidence.
        store = EntityStore(pg_source)
        album_id = "1234567890"
        first_id, first_minted = await store.mint_or_get_release_identity(
            source="apple_music_album", external_id=album_id
        )
        second_id, second_minted = await store.mint_or_get_release_identity(
            source="apple_music_album", external_id=album_id
        )
        assert first_minted is True
        assert second_minted is False
        assert first_id == second_id
        logs = await pg_source.fetchall(
            "SELECT id FROM entity.release_reconciliation_log WHERE identity_id = $1",
            first_id,
        )
        assert len(logs) == 1

    @pytest.mark.asyncio
    async def test_get_release_identity_by_source_round_trips_apple_music_album(self, pg_source):
        store = EntityStore(pg_source)
        album_id = "9876543210"
        minted_id, _ = await store.mint_or_get_release_identity(
            source="apple_music_album", external_id=album_id
        )
        found = await store.get_release_identity_by_source(
            source="apple_music_album", external_id=album_id
        )
        assert found == minted_id

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


@pytest.mark.pg
class TestGetReleaseIdentityProvenanceBulk:
    """LML#525: bulk read that powers the cache-refresh dispatcher.

    The dispatcher takes a batch of ``identity_id``s and needs to know which
    ``(source, external_id)`` pairs each one carries, so it can fan out to the
    per-source release-cache refresh paths. The store method reads each row's
    per-source columns and returns them as a dict keyed by ``identity_id``.

    Missing identity_ids are **absent** from the dict (not None-valued) — the
    router renders absence as ``status = "not_found"``. External IDs are
    stringified at the boundary so the router doesn't have to branch on
    int-vs-str when serializing per-source outcomes.
    """

    @pytest.mark.asyncio
    async def test_empty_input_returns_empty_dict(self, pg_source):
        store = EntityStore(pg_source)
        assert await store.get_release_identity_provenance_bulk([]) == {}

    @pytest.mark.asyncio
    async def test_single_discogs_release_returns_one_pair(self, pg_source):
        store = EntityStore(pg_source)
        identity_id, _ = await store.mint_or_get_release_identity(
            source="discogs_release", external_id="12345"
        )
        result = await store.get_release_identity_provenance_bulk([identity_id])
        assert result == {identity_id: [("discogs_release", "12345")]}

    @pytest.mark.asyncio
    async def test_row_with_both_discogs_release_and_master_returns_both_pairs(self, pg_source):
        """A single row can carry multiple per-source IDs (cross-source join, LML#207)."""
        store = EntityStore(pg_source)
        # Mint via discogs_release first, then directly UPDATE the master column
        # — emulates the LML#207 joiner setting both columns on one row.
        identity_id, _ = await store.mint_or_get_release_identity(
            source="discogs_release", external_id="12345"
        )
        await pg_source.execute(
            "UPDATE entity.release_identity SET discogs_master_id = $1 WHERE id = $2",
            789,
            identity_id,
        )
        result = await store.get_release_identity_provenance_bulk([identity_id])
        assert set(result[identity_id]) == {
            ("discogs_release", "12345"),
            ("discogs_master", "789"),
        }

    @pytest.mark.asyncio
    async def test_row_with_text_sources_returns_string_external_ids(self, pg_source):
        """TEXT-typed columns (Bandcamp URL, MBID, Spotify) round-trip as strings."""
        store = EntityStore(pg_source)
        url = "https://autechre.bandcamp.com/album/confield"
        identity_id, _ = await store.mint_or_get_release_identity(
            source="bandcamp", external_id=url
        )
        # Hand-populate the TEXT-shaped sources that have no mint helper today
        # (musicbrainz_release / spotify_album / apple_music_album are
        # LML#217-era follow-ups, but the store method should already be able
        # to read them since the columns exist on the table).
        await pg_source.execute(
            "UPDATE entity.release_identity "
            "SET musicbrainz_release_id = $1, spotify_album_id = $2, apple_music_album_id = $3 "
            "WHERE id = $4",
            "mbid-abc",
            "spotify-xyz",
            "apple-456",
            identity_id,
        )
        result = await store.get_release_identity_provenance_bulk([identity_id])
        assert set(result[identity_id]) == {
            ("bandcamp", url),
            ("musicbrainz_release", "mbid-abc"),
            ("spotify_album", "spotify-xyz"),
            ("apple_music_album", "apple-456"),
        }

    @pytest.mark.asyncio
    async def test_missing_identity_ids_are_absent_not_none(self, pg_source):
        store = EntityStore(pg_source)
        identity_id, _ = await store.mint_or_get_release_identity(
            source="discogs_release", external_id="111"
        )
        result = await store.get_release_identity_provenance_bulk([identity_id, 99_999])
        assert identity_id in result
        assert 99_999 not in result

    @pytest.mark.asyncio
    async def test_multi_id_input_returns_one_entry_per_existing_row(self, pg_source):
        store = EntityStore(pg_source)
        id_a, _ = await store.mint_or_get_release_identity("discogs_release", "111")
        id_b, _ = await store.mint_or_get_release_identity("discogs_release", "222")
        id_c, _ = await store.mint_or_get_release_identity(
            "bandcamp", "https://x.bandcamp.com/album/y"
        )
        result = await store.get_release_identity_provenance_bulk([id_a, id_b, id_c])
        assert result[id_a] == [("discogs_release", "111")]
        assert result[id_b] == [("discogs_release", "222")]
        assert result[id_c] == [("bandcamp", "https://x.bandcamp.com/album/y")]


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
    async def test_apple_music_album_round_trip(self, app_client):
        album_id = "1234567890"
        resp = await app_client.post(
            "/api/v1/identity/resolve",
            json={"kind": "release", "source": "apple_music_album", "external_id": album_id},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["minted"] is True
        assert body["kind"] == "release"

    @pytest.mark.asyncio
    async def test_apple_music_album_round_trip_idempotent(self, app_client):
        # Second resolve of the same album_id returns the same identity_id
        # with minted=False — same posture as the Bandcamp idempotency test.
        album_id = "9876543210"
        first = await app_client.post(
            "/api/v1/identity/resolve",
            json={"kind": "release", "source": "apple_music_album", "external_id": album_id},
        )
        second = await app_client.post(
            "/api/v1/identity/resolve",
            json={"kind": "release", "source": "apple_music_album", "external_id": album_id},
        )
        assert first.status_code == 200 and second.status_code == 200
        assert second.json()["minted"] is False
        assert second.json()["identity_id"] == first.json()["identity_id"]

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
            ("apple_music_album", "0"),
            ("apple_music_album", "abc"),
            ("apple_music_album", "0012345"),
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
