"""Integration tests for `POST /api/v1/identity/bulk-resolve-libraries`.

Exercise the endpoint end-to-end against a fresh PostgreSQL with seeded
`entity.identity` rows. Composition rules are unit-tested separately;
this tier confirms the FastAPI wiring + PG round-trip is intact and
input order is preserved across mixed-kind batches.

Run with: pytest -m pg -v
"""

from __future__ import annotations

import pytest
import pytest_asyncio

# ``pg_pool`` (max_size=3) and ``pg_app_client`` (the reconciled Family-C PG
# client, LML#613) both live in conftest.
from tests.integration.conftest import ENTITY_IDENTITY_DDL, skip_if_drop_targets_populated


@pytest_asyncio.fixture(autouse=True)
async def set_up_entity_schema(pg_pool):
    """Create + seed a fresh `entity` schema for each test.

    SAFETY: refuses to run when anything this fixture would drop already
    holds rows — the default ``DATABASE_URL_TEST`` may point at a real
    discogs-cache whose ``entity.*`` took hours of rate-limited
    reconciliation to build. The guard sweeps the entity schema dynamically
    from ``pg_class`` (see ``skip_if_drop_targets_populated``); this fixture
    drops no public tables, so its public-table list is empty.
    """
    async with pg_pool.acquire() as conn:
        await skip_if_drop_targets_populated(conn, ())

    # Creation and seeding inside the try: a mid-seed failure must still
    # drop whatever was created instead of stranding rows that veto the
    # next run (same posture as the artists-route siblings).
    try:
        async with pg_pool.acquire() as conn:
            await conn.execute("DROP SCHEMA IF EXISTS entity CASCADE")
            await conn.execute("CREATE SCHEMA entity")
            await conn.execute(ENTITY_IDENTITY_DDL)
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
    finally:
        async with pg_pool.acquire() as conn:
            await conn.execute("DROP SCHEMA IF EXISTS entity CASCADE")


@pytest.mark.pg
class TestBulkResolveLibrariesEndpoint:
    @pytest.mark.asyncio
    async def test_round_trip_single_artist_against_real_pg(self, pg_app_client):
        """End-to-end: seeded identity → composed single_artist verdict."""
        resp = await pg_app_client.post(
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
    async def test_input_order_preserved_across_mixed_kinds(self, pg_app_client):
        """Response[i] corresponds to inputs[i] for compilation / hit / miss."""
        resp = await pg_app_client.post(
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
    async def test_include_tracks_pair_and_marker_end_to_end(self, pg_app_client):
        """1.31.0 wire against real PG: flag on -> resolved kinds carry
        (false, []); flag omitted -> null pair everywhere (the un-upgraded
        caller's byte-compatible view, minus the retired V/A empty array).

        `tracks_contract_version` stays None on BOTH paths: api.yaml 1.32.0
        (wxyc-shared#314) requires both producer arms (`kind: compilation` --
        LML#1021, done -- and `kind: single_artist` -- LML#1138, not yet) to
        emit real `tracks_attempted` before the marker may be `1`. Setting it
        on `include_tracks` alone (the pre-1.32.0 behavior) would tell a
        consumer to trust `tracks_attempted` on `single_artist` rows where
        it's still meaningless.
        """
        inputs = [
            {"library_id": 100, "artist_name": "Various Artists", "album_title": "VA"},
            {"library_id": 200, "artist_name": "Stereolab", "album_title": "AT"},
            {"library_id": 300, "artist_name": "Nobody Artist", "album_title": "x"},
        ]

        flag_on = await pg_app_client.post(
            "/api/v1/identity/bulk-resolve-libraries",
            json={"include_tracks": True, "inputs": inputs},
        )
        assert flag_on.status_code == 200
        data = flag_on.json()
        assert data["tracks_contract_version"] is None
        by_id = {r["library_id"]: r for r in data["results"]}
        assert (by_id[100]["tracks_attempted"], by_id[100]["tracks"]) == (False, [])
        assert (by_id[200]["tracks_attempted"], by_id[200]["tracks"]) == (False, [])
        assert (by_id[300]["tracks_attempted"], by_id[300]["tracks"]) == (None, None)

        flag_off = await pg_app_client.post(
            "/api/v1/identity/bulk-resolve-libraries",
            json={"inputs": inputs},
        )
        assert flag_off.status_code == 200
        data = flag_off.json()
        assert data["tracks_contract_version"] is None
        for r in data["results"]:
            assert (r["tracks_attempted"], r["tracks"]) == (None, None)

    @pytest.mark.asyncio
    async def test_case_drift_resolves_via_lower_fall_through(self, pg_app_client, pg_pool):
        """Per #276: case drift between Backend input and verbatim-cased storage hits.

        Seeds verbatim mixed-case rows (the production shape per the #276
        audit — 99.8% of `entity.identity.library_name` is mixed-case
        verbatim). Posts the *lowercase* variant and asserts the LOWER fall-
        through leg resolves both rows. This is the regression test for the
        #275 ship: that PR canonicalized the input to lowercase and would
        have missed the verbatim-stored row entirely.
        """
        resp = await pg_app_client.post(
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
    async def test_canonical_lookup_collapses_divergence_vectors(self, pg_app_client, pg_pool):
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

        resp = await pg_app_client.post(
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
    async def test_all_three_legs_resolve_in_one_batch(self, pg_app_client, pg_pool):
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

        resp = await pg_app_client.post(
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
    async def test_leg_2_picks_lowest_id_when_two_rows_lower_match(self, pg_app_client, pg_pool):
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

        resp = await pg_app_client.post(
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
    async def test_413_for_oversized_request(self, pg_app_client):
        """1001 inputs → 413."""
        oversized = [
            {"library_id": i, "artist_name": f"Artist_{i}", "album_title": "x"} for i in range(1001)
        ]
        resp = await pg_app_client.post(
            "/api/v1/identity/bulk-resolve-libraries",
            json={"inputs": oversized},
        )
        assert resp.status_code == 413


@pytest_asyncio.fixture
async def set_up_track_identity_schema(pg_pool, pg_source):
    """Fresh `lml_cache.compilation_track_identity` for the LML#1021 end-to-end suite.

    Independent of `set_up_entity_schema` above (`entity.*` vs `lml_cache.*` --
    different schemas, both live on the same `pg_app_client` pool). Same
    populated-table safety guard as every other schema-dropping fixture in
    this suite.
    """
    from entity.compilation_track_identity import set_up_compilation_track_identity_schema
    from tests.integration.conftest import skip_if_named_tables_populated

    async with pg_pool.acquire() as conn:
        await skip_if_named_tables_populated(conn, (("lml_cache", "compilation_track_identity"),))
        await conn.execute("DROP TABLE IF EXISTS lml_cache.compilation_track_identity")
    await set_up_compilation_track_identity_schema(pg_source)
    yield
    async with pg_pool.acquire() as conn:
        await conn.execute("DROP TABLE IF EXISTS lml_cache.compilation_track_identity")


@pytest.mark.pg
class TestIncludeTracksCompilationStoreEndToEnd:
    """LML#1021 end-to-end: `tracks[]` composed from real
    `lml_cache.compilation_track_identity` rows via the router's batched
    read, keyed on `legacy_release_id` (deliberately a DIFFERENT number from
    `library_id` in every test here, to prove the id-space bridge is what's
    doing the join — see wxyc-shared#315 and LML#1021's F2 finding).
    """

    @pytest.mark.asyncio
    async def test_visited_release_composes_a_hit_and_a_miss(
        self, pg_app_client, pg_source, set_up_track_identity_schema
    ):
        from entity.compilation_track_identity import write_compilation_track_identity_verdict

        await write_compilation_track_identity_verdict(
            pg_source,
            library_id=555000,
            track_artist_raw="Juana Molina",
            track_title_raw="La Paradoja",
            source="discogs",
            external_id="305253",
            confidence=1.0,
            method="exact_match",
            resolved_artist_name="Juana Molina",
        )
        # A miss: the matcher visited this credit but neither leg produced
        # a candidate. Attempt rows exist for misses (D2) -- the whole point
        # of the LML#1021 amendments' three-state tracks[] convention.
        await write_compilation_track_identity_verdict(
            pg_source,
            library_id=555000,
            track_artist_raw="Unrecognized Artist",
            track_title_raw="Unrecognized Track",
            source="discogs",
            external_id=None,
            confidence=None,
            method=None,
            resolved_artist_name=None,
        )

        resp = await pg_app_client.post(
            "/api/v1/identity/bulk-resolve-libraries",
            json={
                "include_tracks": True,
                "inputs": [
                    {
                        "library_id": 42,
                        "legacy_release_id": 555000,
                        "artist_name": "Various Artists",
                        "album_title": "Edits",
                    },
                ],
            },
        )

        assert resp.status_code == 200
        result = resp.json()["results"][0]
        assert result["kind"] == "compilation"
        assert result["library_id"] == 42
        assert result["tracks_attempted"] is True
        assert len(result["tracks"]) == 2

        by_artist = {t["artist_name"]: t for t in result["tracks"]}

        hit = by_artist["Juana Molina"]
        assert hit["track_title"] == "La Paradoja"
        assert hit["resolved_artist_name"] == "Juana Molina"
        assert hit["confidence"] == pytest.approx(1.0)
        assert hit["method"] == "exact_match"
        assert len(hit["sources"]) == 1
        assert hit["sources"][0]["source"] == "discogs"
        assert hit["sources"][0]["external_id"] == "305253"

        miss = by_artist["Unrecognized Artist"]
        assert miss["track_title"] == "Unrecognized Track"
        assert miss["resolved_artist_name"] is None
        assert miss["confidence"] is None
        assert miss["method"] is None
        # The miss leg cannot be echoed into sources[] on the current wire
        # contract -- BulkResolveProvenanceEntry.method is required and
        # non-nullable, but D1's CHECK constraint makes a miss row's method
        # NULL. See identity/bulk_resolve.py's _store_row_to_composed.
        assert miss["sources"] == []

    @pytest.mark.asyncio
    async def test_unvisited_release_stays_false_and_empty(
        self, pg_app_client, set_up_track_identity_schema
    ):
        resp = await pg_app_client.post(
            "/api/v1/identity/bulk-resolve-libraries",
            json={
                "include_tracks": True,
                "inputs": [
                    {
                        "library_id": 42,
                        "legacy_release_id": 999999,
                        "artist_name": "Various Artists",
                        "album_title": "Never Visited",
                    },
                ],
            },
        )

        assert resp.status_code == 200
        result = resp.json()["results"][0]
        assert result["tracks_attempted"] is False
        assert result["tracks"] == []

    @pytest.mark.asyncio
    async def test_missing_legacy_release_id_degrades_to_unvisited(
        self, pg_app_client, pg_source, set_up_track_identity_schema
    ):
        """A compilation input with NO legacy_release_id (un-upgraded
        caller) degrades to the unvisited state even when the store DOES
        hold rows LML simply has no bridge to find."""
        from entity.compilation_track_identity import write_compilation_track_identity_verdict

        await write_compilation_track_identity_verdict(
            pg_source,
            library_id=555000,
            track_artist_raw="Juana Molina",
            track_title_raw="La Paradoja",
            source="discogs",
            external_id="305253",
            confidence=1.0,
            method="exact_match",
            resolved_artist_name="Juana Molina",
        )

        resp = await pg_app_client.post(
            "/api/v1/identity/bulk-resolve-libraries",
            json={
                "include_tracks": True,
                "inputs": [
                    {
                        "library_id": 42,
                        "artist_name": "Various Artists",
                        "album_title": "No Bridge",
                    },
                ],
            },
        )

        assert resp.status_code == 200
        result = resp.json()["results"][0]
        assert result["tracks_attempted"] is False
        assert result["tracks"] == []

    @pytest.mark.asyncio
    async def test_identity_store_shaped_row_composes_a_real_name_not_null(
        self, pg_app_client, pg_source, set_up_track_identity_schema
    ):
        """Review finding: a tier-1 `identity_store` Discogs hit persists a
        real `external_id` alongside a NULL `resolved_artist_name` (the
        store never learned a canonical name for an exact-match cache hit --
        see `tests/integration/test_compilation_track_identity_store.py`'s
        `TestMatcherVerdictMappingInsertsCleanly`, which proves this exact
        shape writes cleanly against the real CHECK constraint but never
        routed it through composition). End-to-end through the real HTTP
        endpoint + real PG, this credit's `resolved_artist_name` on the wire
        must not be null alongside a non-null confidence/method -- with no
        other leg to supply a name, it falls back to the raw credit."""
        from entity.compilation_track_identity import write_compilation_track_identity_verdict

        await write_compilation_track_identity_verdict(
            pg_source,
            library_id=555000,
            track_artist_raw="Sessa",
            track_title_raw="Pequena Vertigem de Amor",
            source="discogs",
            external_id="7654321",
            confidence=1.0,
            method="exact_match",
            resolved_artist_name=None,
        )

        resp = await pg_app_client.post(
            "/api/v1/identity/bulk-resolve-libraries",
            json={
                "include_tracks": True,
                "inputs": [
                    {
                        "library_id": 42,
                        "legacy_release_id": 555000,
                        "artist_name": "Various Artists",
                        "album_title": "Edits",
                    },
                ],
            },
        )

        assert resp.status_code == 200
        result = resp.json()["results"][0]
        assert result["tracks_attempted"] is True
        track = result["tracks"][0]
        assert track["artist_name"] == "Sessa"
        assert track["resolved_artist_name"] == "Sessa"  # raw-credit fallback, never null
        assert track["confidence"] == pytest.approx(1.0)
        assert track["method"] == "exact_match"
