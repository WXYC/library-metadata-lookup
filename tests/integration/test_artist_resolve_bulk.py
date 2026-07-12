"""Integration tests for `POST /api/v1/artists/resolve/bulk` (LML#759 PR C).

Real PostgreSQL via ``DATABASE_URL_TEST`` for tiers 1-2 and the
write-back; the Discogs API tier is a dependency-override mock serving
canned single-page observations (live response-shape assumptions are
pinned separately in the ``external_api``-marked smoke). This is the
full-chain tier the issue's test plan calls for: the two-route AC flow,
the dry_run inverse, the seeded overload family, the cache-conflict rule,
COALESCE non-clobber, auth, and the normalization-parity canary.

Requires ``wxyc_identity_match_artist`` (alembic 0004 from
WXYC/discogs-etl) for the equality-leg SQL — self-skips without it, same
posture as ``test_artist_candidate_sets.py``. Run with: ``pytest -m pg -v``
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from discogs.models import DiscogsArtistSearchResult
from tests.integration.conftest import (
    DATABASE_URL,
    F_UNACCENT_WRAPPER_SQL,
    RECONCILER_TABLE_DDL,
    skip_unless_wxyc_identity_match_artist,
)

# Canned single-page observations per probe string (the resolver probes
# with the group's lexicographically-least sanitized spelling). Raw
# Discogs titles, "(N)" intact.
_CANNED_PAGES: dict[str, list[DiscogsArtistSearchResult]] = {
    # Exact-form unique, nothing cached: the plain mint path.
    "Wishy": [DiscogsArtistSearchResult(artist_id=123, title="Wishy")],
    # Exact-form unique; used by the dry_run inverse.
    "REZN": [DiscogsArtistSearchResult(artist_id=555, title="REZN")],
    # Overload family: two exact-form ids (also seeded in the cache).
    "Popsicle": [
        DiscogsArtistSearchResult(artist_id=111, title="Popsicle"),
        DiscogsArtistSearchResult(artist_id=222, title="Popsicle (2)"),
    ],
    # API-unique but the seeded cache credits a DIFFERENT id (333).
    "The Tubs": [DiscogsArtistSearchResult(artist_id=999, title="The Tubs")],
    # Pre-existing identity row holds spotify only: the non-clobber fill.
    "Cat Power": [DiscogsArtistSearchResult(artist_id=3081, title="Cat Power")],
    # Parity canary: umlaut input vs diacritic-free cache row — both
    # collapse to one identity-match form through the two normalizers.
    "Nilüfer Yanya": [DiscogsArtistSearchResult(artist_id=666, title="Nilüfer Yanya")],
    # Winner-qualifier mismatch: a "(2)"-titled singleton answering a bare
    # input is family evidence, not a resolution.
    "Wand": [DiscogsArtistSearchResult(artist_id=77, title="Wand (2)")],
    # Qualified input: own group, resolves via the qualifier-matched
    # singleton, never mints, skips the cache-evidence tier.
    "Popsicle (2)": [DiscogsArtistSearchResult(artist_id=222, title="Popsicle (2)")],
}


@pytest_asyncio.fixture(autouse=True)
async def set_up_schemas(pg_pool):
    """Drop + create the entity schema and the reconciler cache tables.

    Entity side mirrors ``test_search_aliases_bulk.py``; the discogs-cache
    side seeds the four reconciler tables the #759 candidate-set queries
    read (``RECONCILER_TABLE_DDL``), not the composer's ``artist`` family.

    SAFETY: refuses to run when any about-to-drop table already has rows —
    the default ``DATABASE_URL_TEST`` may point at a real discogs-cache
    whose ``entity.identity`` took hours to reconcile.
    """

    async def _table_row_count(conn, schema: str, table: str) -> int | None:
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
        await skip_unless_wxyc_identity_match_artist(conn)
        try:
            await conn.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
            await conn.execute("CREATE EXTENSION IF NOT EXISTS unaccent")
        except Exception as e:  # locked-down Postgres without contrib
            pytest.skip(f"pg_trgm/unaccent extensions unavailable: {e}")

        targets: list[tuple[str, str]] = [
            ("entity", "identity"),
            ("entity", "reconciliation_log"),
            *(("public", t) for t in RECONCILER_TABLE_DDL),
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

    # Creation and seeding inside the try: a mid-seed failure must still
    # drop whatever was created (see test_artist_candidate_sets.py).
    created: list[str] = []
    try:
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
            await conn.execute(F_UNACCENT_WRAPPER_SQL)
            for table_name, columns in RECONCILER_TABLE_DDL.items():
                await conn.execute(f"DROP TABLE IF EXISTS {table_name} CASCADE")
                await conn.execute(f"CREATE TABLE {table_name} ({columns})")
                created.append(table_name)

            # Tier-1 seeds: an identity-store short-circuit row and the
            # non-clobber target (spotify only, no Discogs id).
            await conn.execute(
                """
                INSERT INTO entity.identity
                    (library_name, discogs_artist_id, spotify_artist_id)
                VALUES
                    ('Juana Molina', 187553, NULL),
                    ('Cat Power', NULL, 'spotify-cat-power')
                """
            )
            # Tier-2 seeds: the overload family (both exact-leg ids), the
            # conflict row (The Tubs -> 333 while the API says 999), and
            # the parity canary's diacritic-free form.
            await conn.executemany(
                "INSERT INTO release_artist (release_id, artist_id, artist_name, extra) "
                "VALUES ($1, $2, $3, $4)",
                [
                    (1, 111, "Popsicle", 0),
                    (2, 222, "Popsicle (2)", 0),
                    (3, 333, "The Tubs", 0),
                    (4, 666, "Nilufer Yanya", 0),
                ],
            )
        yield
    finally:
        async with pg_pool.acquire() as conn:
            await conn.execute("DROP SCHEMA IF EXISTS entity CASCADE")
            for table_name in created:
                await conn.execute(f"DROP TABLE IF EXISTS {table_name} CASCADE")


@pytest.fixture
def mock_discogs_service():
    """Canned-page Discogs API stand-in; unknown names measure zero."""

    async def _search(name: str):
        return _CANNED_PAGES.get(name, [])

    service = AsyncMock()
    service.search_artists = AsyncMock(side_effect=_search)
    return service


@pytest_asyncio.fixture
async def app_client(monkeypatch, mock_discogs_service):
    """ASGI client on the test PG, auth off, API tier mocked."""
    from httpx import ASGITransport, AsyncClient

    import core.dependencies as core_deps
    import identity.dependencies as deps
    from config.settings import get_settings
    from core.dependencies import get_discogs_service
    from main import app

    monkeypatch.setenv("DATABASE_URL_DISCOGS", DATABASE_URL)
    # Happy-path posture mirrors the sibling; auth enforcement gets its own
    # fixture + tests below.
    monkeypatch.setenv("LML_REQUIRE_AUTH", "false")
    get_settings.cache_clear()
    deps._entity_store = None
    deps._entity_probe_failed = False
    await core_deps.close_discogs_pool()
    await core_deps.close_discogs_service()

    app.dependency_overrides[get_discogs_service] = lambda: mock_discogs_service
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            yield ac
    finally:
        app.dependency_overrides.pop(get_discogs_service, None)
        deps._entity_store = None
        deps._entity_probe_failed = False
        await core_deps.close_discogs_pool()
        await core_deps.close_discogs_service()
        get_settings.cache_clear()


@pytest.mark.pg
class TestArtistResolveBulkEndpoint:
    @pytest.mark.asyncio
    async def test_tier1_short_circuit_makes_zero_api_calls(self, app_client, mock_discogs_service):
        """A pre-existing identity row decides without touching the API."""
        resp = await app_client.post(
            "/api/v1/artists/resolve/bulk", json={"names": ["Juana Molina"]}
        )
        assert resp.status_code == 200
        (result,) = resp.json()["results"]
        assert result["discogs_artist_id"] == 187553
        assert result["method"] == "identity_store"
        assert result["cache_corroboration"] == []
        assert result["candidate_count"] is None
        mock_discogs_service.search_artists.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_resolve_then_identity_read_returns_minted_row(self, app_client):
        """The AC-as-test two-route flow: a mint is visible to the
        pre-existing `GET /identity/resolve` read surface."""
        resp = await app_client.post("/api/v1/artists/resolve/bulk", json={"names": ["Wishy"]})
        assert resp.status_code == 200
        (result,) = resp.json()["results"]
        assert result["discogs_artist_id"] == 123
        assert result["method"] == "api_search"
        assert result["canonical_name"] == "Wishy"

        read = await app_client.get("/identity/resolve", params={"name": "Wishy"})
        assert read.status_code == 200
        assert read.json()["discogs_artist_id"] == 123

    @pytest.mark.asyncio
    async def test_dry_run_inverse_subsequent_read_404s(self, app_client):
        """dry_run returns the full verdict but persists nothing."""
        resp = await app_client.post(
            "/api/v1/artists/resolve/bulk",
            json={"names": ["REZN"], "dry_run": True},
        )
        assert resp.status_code == 200
        (result,) = resp.json()["results"]
        assert result["discogs_artist_id"] == 555
        assert result["method"] == "api_search"

        read = await app_client.get("/identity/resolve", params={"name": "REZN"})
        assert read.status_code == 404

    @pytest.mark.asyncio
    async def test_overload_family_is_ambiguous_and_mints_nothing(self, app_client):
        """Two exact-form ids on the page → ambiguous with the measured
        count; the seeded cache family corroborates via the exact leg."""
        resp = await app_client.post("/api/v1/artists/resolve/bulk", json={"names": ["Popsicle"]})
        assert resp.status_code == 200
        (result,) = resp.json()["results"]
        assert result["unresolved_reason"] == "ambiguous"
        assert result["candidate_count"] == 2
        assert result["discogs_artist_id"] is None
        assert "cache_exact" in result["cache_corroboration"]

        read = await app_client.get("/identity/resolve", params={"name": "Popsicle"})
        assert read.status_code == 404

    @pytest.mark.asyncio
    async def test_cache_conflict_is_ambiguous(self, app_client):
        """API-unique candidate (999) vs seeded exact-leg id (333):
        conflict means doubt, doubt means NULL — nothing mints."""
        resp = await app_client.post("/api/v1/artists/resolve/bulk", json={"names": ["The Tubs"]})
        assert resp.status_code == 200
        (result,) = resp.json()["results"]
        assert result["unresolved_reason"] == "ambiguous"
        assert result["candidate_count"] == 1
        # The identical seeded name also clears the 0.85 trigram floor —
        # both legs report, only the equality leg vetoes.
        assert result["cache_corroboration"] == ["cache_exact", "cache_trigram"]

        read = await app_client.get("/identity/resolve", params={"name": "The Tubs"})
        assert read.status_code == 404

    @pytest.mark.asyncio
    async def test_mint_fills_existing_row_without_clobbering(self, app_client, pg_pool):
        """COALESCE non-clobber on the real upsert: the pre-existing
        spotify_artist_id survives, the Discogs id fills in, and no
        second row appears."""
        resp = await app_client.post("/api/v1/artists/resolve/bulk", json={"names": ["Cat Power"]})
        assert resp.status_code == 200
        (result,) = resp.json()["results"]
        assert result["discogs_artist_id"] == 3081
        assert result["method"] == "api_search"

        async with pg_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT library_name, discogs_artist_id, spotify_artist_id "
                "FROM entity.identity WHERE LOWER(library_name) = LOWER($1)",
                "Cat Power",
            )
        assert len(rows) == 1
        assert rows[0]["discogs_artist_id"] == 3081
        assert rows[0]["spotify_artist_id"] == "spotify-cat-power"

    @pytest.mark.asyncio
    async def test_normalization_parity_canary(self, app_client):
        """Diacritic + "(N)"-suffix axis through both normalizers at once:
        the umlaut input matches the diacritic-free PG row (SQL side) and
        the suffixed raw API title (Python side). Parity itself is locked
        upstream in wxyc-etl — this just makes a break surface here."""
        resp = await app_client.post(
            "/api/v1/artists/resolve/bulk", json={"names": ["Nilüfer Yanya"]}
        )
        assert resp.status_code == 200
        (result,) = resp.json()["results"]
        assert result["discogs_artist_id"] == 666
        assert result["canonical_name"] == "Nilüfer Yanya"
        # cache_trigram rides along: f_unaccent collapses the umlaut, so
        # the seeded row scores 1.0 on the trigram leg too.
        assert result["cache_corroboration"] == ["cache_exact", "cache_trigram"]

    @pytest.mark.asyncio
    async def test_413_for_oversized_request(self, app_client):
        """26 names → 413 before any DB work (25-name cap)."""
        resp = await app_client.post(
            "/api/v1/artists/resolve/bulk",
            json={"names": [f"Artist_{i}" for i in range(26)]},
        )
        assert resp.status_code == 413


@pytest_asyncio.fixture
async def app_client_authed(monkeypatch, mock_discogs_service):
    """Same wiring as ``app_client`` but with bearer enforcement ON."""
    from httpx import ASGITransport, AsyncClient

    import core.dependencies as core_deps
    import identity.dependencies as deps
    from config.settings import get_settings
    from core.dependencies import get_discogs_service
    from main import app

    monkeypatch.setenv("DATABASE_URL_DISCOGS", DATABASE_URL)
    monkeypatch.setenv("LML_REQUIRE_AUTH", "true")
    monkeypatch.setenv("LML_API_KEY", "test-artist-resolve-key")
    get_settings.cache_clear()
    deps._entity_store = None
    deps._entity_probe_failed = False
    await core_deps.close_discogs_pool()
    await core_deps.close_discogs_service()

    app.dependency_overrides[get_discogs_service] = lambda: mock_discogs_service
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            yield ac
    finally:
        app.dependency_overrides.pop(get_discogs_service, None)
        deps._entity_store = None
        deps._entity_probe_failed = False
        await core_deps.close_discogs_pool()
        await core_deps.close_discogs_service()
        get_settings.cache_clear()


@pytest.mark.pg
class TestArtistResolveBulkAuth:
    @pytest.mark.asyncio
    async def test_401_without_bearer(self, app_client_authed):
        resp = await app_client_authed.post(
            "/api/v1/artists/resolve/bulk", json={"names": ["Juana Molina"]}
        )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_200_with_bearer(self, app_client_authed):
        resp = await app_client_authed.post(
            "/api/v1/artists/resolve/bulk",
            json={"names": ["Juana Molina"]},
            headers={"Authorization": "Bearer test-artist-resolve-key"},
        )
        assert resp.status_code == 200
        assert resp.json()["results"][0]["discogs_artist_id"] == 187553


@pytest.mark.pg
class TestArtistResolveQualifierSemantics:
    """Endpoint-level pins for the qualifier rules the resolver's unit
    suite covers in depth: winner-title mismatch, qualified-never-mint,
    and the 422 input contract."""

    @pytest.mark.asyncio
    async def test_qualifier_titled_singleton_is_ambiguous_for_bare_input(self, app_client):
        resp = await app_client.post("/api/v1/artists/resolve/bulk", json={"names": ["Wand"]})
        assert resp.status_code == 200
        (result,) = resp.json()["results"]
        assert result["unresolved_reason"] == "ambiguous"
        assert result["candidate_count"] == 1
        assert result["discogs_artist_id"] is None

        read = await app_client.get("/identity/resolve", params={"name": "Wand"})
        assert read.status_code == 404

    @pytest.mark.asyncio
    async def test_qualified_input_resolves_without_minting(self, app_client):
        resp = await app_client.post(
            "/api/v1/artists/resolve/bulk", json={"names": ["Popsicle (2)"]}
        )
        assert resp.status_code == 200
        (result,) = resp.json()["results"]
        assert result["discogs_artist_id"] == 222
        assert result["canonical_name"] == "Popsicle (2)"
        assert result["method"] == "api_search"
        # Qualified groups skip the cache-evidence tier entirely.
        assert result["cache_corroboration"] == []

        read = await app_client.get("/identity/resolve", params={"name": "Popsicle (2)"})
        assert read.status_code == 404

    @pytest.mark.asyncio
    async def test_semantically_invalid_name_returns_422(self, app_client, pg_pool):
        """One garbage name 422s the batch before any tier runs — no
        mints, no in-band verdict a consumer could negative-cache."""
        resp = await app_client.post(
            "/api/v1/artists/resolve/bulk", json={"names": ["Wishy", "   "]}
        )
        assert resp.status_code == 422
        assert "names[1]" in resp.json()["detail"]

        read = await app_client.get("/identity/resolve", params={"name": "Wishy"})
        assert read.status_code == 404
