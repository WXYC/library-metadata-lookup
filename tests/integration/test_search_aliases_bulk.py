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

import pytest
import pytest_asyncio

# ``pg_pool`` (max_size=3) and ``pg_app_client_no_auth`` (the reconciled
# Family-C PG client, LML#613) both live in conftest.
from tests.integration.conftest import ENTITY_IDENTITY_DDL, skip_if_drop_targets_populated

# The five public-schema discogs-cache tables this fixture drops and
# recreates (the composer's read surface).
_PUBLIC_DROP_TARGETS = (
    "artist",
    "artist_alias",
    "artist_name_variation",
    "artist_member",
    "artist_url",
)


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
    re-run takes hours). We refuse to run when ANYTHING the fixture would
    drop already has rows (the entity side swept dynamically from
    ``pg_class`` — see ``skip_if_drop_targets_populated``), so the
    operator must point ``DATABASE_URL_TEST`` at a clean DB.
    """
    async with pg_pool.acquire() as conn:
        await skip_if_drop_targets_populated(conn, _PUBLIC_DROP_TARGETS)

    # Creation and seeding inside the try: a mid-seed failure must still
    # drop whatever was created (same posture as the resolve-bulk sibling)
    # instead of stranding seeded rows that veto the next run.
    try:
        async with pg_pool.acquire() as conn:
            await conn.execute("DROP SCHEMA IF EXISTS entity CASCADE")
            await conn.execute("CREATE SCHEMA entity")
            await conn.execute(ENTITY_IDENTITY_DDL)
            # Discogs cache child tables. Drop first so prior runs don't
            # poison them — these live in the public schema in test as in
            # prod.
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
                    fetched_at TIMESTAMPTZ,
                    not_found BOOLEAN NOT NULL DEFAULT FALSE
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
            #
            # ``fetched_at = now()`` matches the shape ``write_artist_details``
            # produces in production (any row LML writes has it stamped). The
            # stub-row edge case (``fetched_at IS NULL``, the monthly-rebuild
            # leftover) is exercised by NULL-ing the column per-test in
            # ``TestGetArtistDetailsBulkFetchedAt`` below -- the fixture defaults
            # to the common case so a new endpoint test added here inherits the
            # production shape, not the edge case. Writer-side stamping is
            # pinned in ``tests/integration/test_cache_service_artist_writer.py``.
            await conn.execute(
                """
                INSERT INTO artist (id, name, profile, image_url, fetched_at)
                VALUES
                    (2154, 'Stereolab', NULL, NULL, now()),
                    (305253, 'Juana Molina', NULL, NULL, now())
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
    finally:
        async with pg_pool.acquire() as conn:
            await conn.execute("DROP SCHEMA IF EXISTS entity CASCADE")
            await conn.execute("DROP TABLE IF EXISTS artist_alias CASCADE")
            await conn.execute("DROP TABLE IF EXISTS artist_name_variation CASCADE")
            await conn.execute("DROP TABLE IF EXISTS artist_member CASCADE")
            await conn.execute("DROP TABLE IF EXISTS artist_url CASCADE")
            await conn.execute("DROP TABLE IF EXISTS artist CASCADE")


@pytest.mark.pg
class TestSearchAliasesBulkEndpoint:
    @pytest.mark.asyncio
    async def test_round_trip_emits_all_three_discogs_source_variants(self, pg_app_client_no_auth):
        """Stereolab returns one variant per source type and all three tags."""
        resp = await pg_app_client_no_auth.post(
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
    async def test_missing_identity_routed_to_missing_array(self, pg_app_client_no_auth):
        """Names without an ``entity.identity`` row → in ``missing[]``, not ``artists[]``."""
        resp = await pg_app_client_no_auth.post(
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
    async def test_identity_without_discogs_id_yields_empty_sources_present(
        self, pg_app_client_no_auth
    ):
        """Cat Power has NULL ``discogs_artist_id`` → empty variants AND empty sources_present.

        Reconcile contract: consumer must NOT delete cached rows for this
        artist because the composer never ran any source leg.
        """
        resp = await pg_app_client_no_auth.post(
            "/api/v1/artists/search-aliases/bulk",
            json={"names": ["Cat Power"]},
        )
        assert resp.status_code == 200
        result = resp.json()["artists"][0]
        assert result["variants"] == []
        assert result["sources_present"] == []

    @pytest.mark.asyncio
    async def test_discogs_cache_miss_keeps_sources_present_populated(self, pg_app_client_no_auth):
        """Juana Molina has a Discogs id but no children in the cache.

        Composer ran the leg but found nothing — variants empty, but
        sources_present must list all three discogs_* tags. Consumer needs
        this to scope DELETE during reconcile.
        """
        resp = await pg_app_client_no_auth.post(
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
    async def test_413_for_oversized_request(self, pg_app_client_no_auth):
        """1001 names → 413 before any DB work."""
        oversized = [f"Artist_{i}" for i in range(1001)]
        resp = await pg_app_client_no_auth.post(
            "/api/v1/artists/search-aliases/bulk",
            json={"names": oversized},
        )
        assert resp.status_code == 413

    @pytest.mark.asyncio
    async def test_case_drift_resolves_via_lower_fall_through(self, pg_app_client_no_auth):
        """Backend posts lowercase, storage is mixed-case → leg 2 resolves it.

        Mirrors the #276 fall-through contract: stored row ``'Stereolab'``,
        posted name ``'stereolab'`` — the composer's bulk variant of
        ``resolve_library_name`` finds the row via LOWER fall-through.
        """
        resp = await pg_app_client_no_auth.post(
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

    @pytest.mark.asyncio
    async def test_case_variant_pair_both_resolve_against_real_pg(self, pg_app_client_no_auth):
        """Two case-variant inputs of the same stored row both resolve through real PG.

        End-to-end coverage for the leg-2 `DISTINCT ON (bind.name)` SQL.
        Pre-iteration-2, the SQL used `DISTINCT ON (LOWER(bind.name))`,
        which collapsed `['STEREOLAB', 'stereolab']` (both LOWER-match
        'stereolab') into one returned row — one input silently
        dropped to `missing[]`. The unit test exercises the Python-side
        mapping via mocks; this test exercises the actual PG behavior so
        a future regression of the SQL is caught here.
        """
        resp = await pg_app_client_no_auth.post(
            "/api/v1/artists/search-aliases/bulk",
            json={"names": ["STEREOLAB", "stereolab"]},
        )
        assert resp.status_code == 200
        data = resp.json()
        # Both case-variant inputs resolve — neither falls into `missing[]`.
        assert data["missing"] == []
        # One ArtistSearchAliasesResult per unique input, both resolving
        # to the same stored 'Stereolab' identity.
        names_in_result = {a["name"] for a in data["artists"]}
        assert names_in_result == {"STEREOLAB", "stereolab"}
        # Both inputs share the same resolved discogs sources.
        for entry in data["artists"]:
            assert set(entry["sources_present"]) == {
                "discogs_name_variation",
                "discogs_alias",
                "discogs_member",
            }


@pytest.mark.pg
class TestGetArtistDetailsBulkFetchedAt:
    """LML#520: the bulk cache read must surface ``fetched_at`` faithfully.

    The singular-path ``get_artist_details`` already projects ``fetched_at``
    so the LML#503 stub-vs-hydrated discriminator works. The bulk path
    did not, so every ``ArtistDetails`` came back with ``fetched_at = None``
    regardless of the row's actual state. This suite pins both halves of
    the contract through the real PG fixture.
    """

    @pytest.mark.asyncio
    async def test_stub_row_returns_null_fetched_at(self, pg_pool):
        """A row with ``fetched_at IS NULL`` (monthly rebuild stub shape)
        reads back through ``get_artist_details_bulk`` carrying the NULL.
        """
        from discogs.cache_service import DiscogsCacheService

        # Fixture seeds 2154 with ``fetched_at = now()`` (production shape).
        # Roll it back to the stub shape for this test only -- the stub is
        # the edge case, so it owns its own setup rather than relying on a
        # fixture default that would silently apply to unrelated tests.
        async with pg_pool.acquire() as conn:
            await conn.execute("UPDATE artist SET fetched_at = NULL WHERE id = 2154")

        cache = DiscogsCacheService(pg_pool)
        bundle = await cache.get_artist_details_bulk([2154])

        assert 2154 in bundle
        assert bundle[2154].fetched_at is None

    @pytest.mark.asyncio
    async def test_hydrated_row_returns_non_null_fetched_at(self, pg_pool):
        """A row with ``fetched_at = now()`` (production write shape) reads
        back through ``get_artist_details_bulk`` carrying that stamp.

        Relies on the fixture default; the production-shape case is what
        the fixture is for, so no per-test setup is needed.
        """
        from discogs.cache_service import DiscogsCacheService

        cache = DiscogsCacheService(pg_pool)
        bundle = await cache.get_artist_details_bulk([305253])

        assert 305253 in bundle
        assert bundle[305253].fetched_at is not None

    @pytest.mark.asyncio
    async def test_mixed_batch_preserves_per_row_fetched_at(self, pg_pool):
        """In a single batch, a stub and a hydrated row each reflect their
        own ``fetched_at`` state. The discriminator is per-row, not global."""
        from discogs.cache_service import DiscogsCacheService

        async with pg_pool.acquire() as conn:
            # Fixture seeds both as hydrated (production shape). Roll 2154
            # back to stub shape; 305253 stays hydrated.
            await conn.execute("UPDATE artist SET fetched_at = NULL WHERE id = 2154")

        cache = DiscogsCacheService(pg_pool)
        bundle = await cache.get_artist_details_bulk([2154, 305253])

        assert bundle[2154].fetched_at is None
        assert bundle[305253].fetched_at is not None
