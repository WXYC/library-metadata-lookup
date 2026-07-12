"""pg integration tests for the LML#759 candidate-set queries.

Exercises ``DiscogsCacheService.artist_equality_candidates`` and
``artist_trigram_candidates`` against real PostgreSQL. These are the
reconciler cascade legs rewritten to return candidate SETS — the load-bearing
behavior under test is anti-collapse: an overloaded form must surface every
candidate id, where the reconciler's first-match-wins collapse (correct for
its library-name inputs) would silently pick one.

Two classes with different infrastructure requirements, mirroring the split
in ``test_entity_resolution.py``:

- ``TestArtistEqualityCandidatesPG`` needs ``wxyc_identity_match_artist``
  (alembic 0004 from WXYC/discogs-etl) and self-skips without it — CI's
  plain postgres-16 service container doesn't have it.
- ``TestArtistTrigramCandidatesPG`` only needs ``pg_trgm`` + ``unaccent``
  contrib, both provisionable on CI.

Data safety: both fixtures **skip** when the tables they seed already exist
(a live discogs-cache) rather than writing into real cache data — the same
posture as ``TestTrigramFallbackSQLIntegration``.
"""

import pytest
import pytest_asyncio
from wxyc_etl.text import to_identity_match_form

from discogs.cache_service import DiscogsCacheService
from tests.integration.conftest import (
    F_UNACCENT_WRAPPER_SQL,
    RECONCILER_TABLE_DDL,
    skip_unless_wxyc_identity_match_artist,
)


@pytest.mark.pg
class TestArtistEqualityCandidatesPG:
    """Equality legs end to end: Python ``to_identity_match_form`` on the
    input side, ``wxyc_identity_match_artist`` on the column side.

    Because inputs are normalized in Python and columns in SQL, every
    passing assertion here is also a normalization-parity check for the
    axis it seeds (case, leading article, paren suffix, diacritics) —
    parity itself is locked upstream in wxyc-etl, this makes a break
    surface in LML's own suite.
    """

    @pytest_asyncio.fixture(autouse=True)
    async def seed_equality_tables(self, pg_pool):
        async with pg_pool.acquire() as conn:
            await skip_unless_wxyc_identity_match_artist(conn)
            for table_name in RECONCILER_TABLE_DDL:
                pre_existing = await conn.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema = current_schema() AND table_name = $1)",
                    table_name,
                )
                if pre_existing:
                    pytest.skip(
                        f"{table_name} already present -- refusing to seed into a real "
                        "discogs-cache (data-safety posture)"
                    )
        # Creation and seeding happen inside the try: a mid-seed failure must
        # still drop whatever was created, or the leaked tables trip the
        # data-safety skip above and silently disable this class on every
        # subsequent run against a persistent test PG.
        created: list[str] = []
        try:
            async with pg_pool.acquire() as conn:
                for table_name, columns in RECONCILER_TABLE_DDL.items():
                    await conn.execute(f"CREATE TABLE {table_name} ({columns})")
                    created.append(table_name)
                await conn.executemany(
                    "INSERT INTO release_artist (release_id, artist_id, artist_name, extra) "
                    "VALUES ($1, $2, $3, $4)",
                    [
                        # Overload family: the "(N)" suffix collapses under the
                        # identity-match form, so both must land in ONE form's set.
                        (1, 111, "Popsicle", 0),
                        (2, 222, "Popsicle (2)", 0),
                        # Leading-article collapse.
                        (3, 333, "The Tubs", 0),
                        (4, 4242, "Stereolab", 0),
                        # ``extra = 1`` credit: invisible to the exact leg.
                        (5, 9999, "Stereolab", 1),
                    ],
                )
                await conn.execute(
                    "INSERT INTO artist_member (artist_id, member_id, member_name) "
                    "VALUES (4242, 200, 'Laetitia Sadier')"
                )
                await conn.execute(
                    "INSERT INTO artist_alias (artist_id, alias_name) "
                    "VALUES (555, 'Chuquimamani-Condori')"
                )
                # Diacritic-free cache form; queried below with the umlaut input.
                await conn.execute(
                    "INSERT INTO artist_name_variation (artist_id, name) "
                    "VALUES (666, 'Nilufer Yanya')"
                )
            yield
        finally:
            async with pg_pool.acquire() as conn:
                for table_name in created:
                    await conn.execute(f"DROP TABLE IF EXISTS {table_name}")

    @pytest.mark.asyncio
    async def test_overload_family_returns_full_candidate_set(self, pg_pool):
        """ "Popsicle" and "Popsicle (2)" share a form — the exact leg must
        return BOTH ids. This is the anti-collapse property the resolver's
        "ambiguous names must not mint" rule depends on."""
        svc = DiscogsCacheService(pg_pool)
        form = to_identity_match_form("Popsicle")
        result = await svc.artist_equality_candidates([form])
        assert result[form].exact == {111, 222}

    @pytest.mark.asyncio
    async def test_leading_article_collapse(self, pg_pool):
        svc = DiscogsCacheService(pg_pool)
        form = to_identity_match_form("The Tubs")
        result = await svc.artist_equality_candidates([form])
        assert result[form].exact == {333}

    @pytest.mark.asyncio
    async def test_extra_credit_excluded_from_exact_leg(self, pg_pool):
        svc = DiscogsCacheService(pg_pool)
        form = to_identity_match_form("Stereolab")
        result = await svc.artist_equality_candidates([form])
        assert result[form].exact == {4242}

    @pytest.mark.asyncio
    async def test_member_alias_and_variation_legs(self, pg_pool):
        svc = DiscogsCacheService(pg_pool)
        member_form = to_identity_match_form("Laetitia Sadier")
        alias_form = to_identity_match_form("Chuquimamani-Condori")
        # Umlaut input against the diacritic-free cache row: the Python and
        # SQL normalizers must collapse the axis identically.
        variation_form = to_identity_match_form("Nilüfer Yanya")
        result = await svc.artist_equality_candidates([member_form, alias_form, variation_form])
        assert result[member_form].member == {200}
        assert result[member_form].exact == set()
        assert result[alias_form].alias == {555}
        assert result[variation_form].name_variation == {666}

    @pytest.mark.asyncio
    async def test_unmatched_form_is_measured_zero(self, pg_pool):
        """A form with no candidates anywhere still gets a key with four
        empty sets — "no candidates" must be distinguishable from "not
        queried"."""
        svc = DiscogsCacheService(pg_pool)
        form = to_identity_match_form("Csillagrablók")
        result = await svc.artist_equality_candidates([form])
        assert form in result
        assert result[form].exact == set()
        assert result[form].member == set()
        assert result[form].alias == set()
        assert result[form].name_variation == set()

    @pytest.mark.asyncio
    async def test_whole_batch_in_one_call(self, pg_pool):
        svc = DiscogsCacheService(pg_pool)
        forms = [
            to_identity_match_form("Popsicle"),
            to_identity_match_form("Stereolab"),
            to_identity_match_form("Wishy"),
        ]
        result = await svc.artist_equality_candidates(forms)
        assert set(result.keys()) == set(forms)
        assert result[to_identity_match_form("Popsicle")].exact == {111, 222}
        assert result[to_identity_match_form("Stereolab")].exact == {4242}
        assert result[to_identity_match_form("Wishy")].exact == set()


@pytest.mark.pg
class TestArtistTrigramCandidatesPG:
    """Trigram evidence leg against real pg_trgm + ``f_unaccent``."""

    @pytest_asyncio.fixture(autouse=True)
    async def seed_trigram_fixture(self, pg_pool):
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
                    "release_artist already present -- refusing to seed into a real "
                    "discogs-cache (data-safety posture)"
                )

        # Creation and seeding inside the try — see seed_equality_tables for
        # why a mid-seed failure must still drop the table.
        try:
            async with pg_pool.acquire() as conn:
                await conn.execute(F_UNACCENT_WRAPPER_SQL)
                await conn.execute(
                    f"CREATE TABLE release_artist ({RECONCILER_TABLE_DDL['release_artist']})"
                )
                await conn.executemany(
                    "INSERT INTO release_artist (release_id, artist_id, artist_name, extra) "
                    "VALUES ($1, $2, $3, $4)",
                    [
                        (1, 4242, "Stereolab", 0),
                        # Two distinct artists with the identical name: the
                        # candidate set must carry both (the reconciler's top-1
                        # would pick one arbitrarily).
                        (2, 5001, "Popsicle", 0),
                        (3, 5002, "Popsicle", 0),
                        (4, 5499521, "Nilufer Yanya", 0),  # diacritic-free cache form
                        (5, 7777, "Hot 8 Brass Band", 0),
                        (6, 9999, "Stereolab", 1),  # extra credit: excluded
                    ],
                )
            yield
        finally:
            async with pg_pool.acquire() as conn:
                await conn.execute("DROP TABLE IF EXISTS release_artist")

    @pytest.mark.asyncio
    async def test_batched_call_covers_hit_near_miss_and_overload(self, pg_pool):
        """One round-trip, three names, three distinct verdict shapes:
        diacritic-collapsed hit, substring near-miss rejected at the 0.85
        floor (the LML#215 motivating case), and an identical-name pair
        returned as a full set."""
        svc = DiscogsCacheService(pg_pool)
        result = await svc.artist_trigram_candidates(["Nilüfer Yanya", "Hot 8", "Popsicle"])
        assert result["Nilüfer Yanya"] == {5499521}
        assert result["Hot 8"] == set()
        assert result["Popsicle"] == {5001, 5002}

    @pytest.mark.asyncio
    async def test_threshold_applied_in_sql(self, pg_pool):
        """The typo "Stereolabs" scores under the 0.85 default (empty set)
        but clears a lowered floor — proving the gate is the bound
        threshold, not an exact-string coincidence."""
        svc = DiscogsCacheService(pg_pool)
        at_default = await svc.artist_trigram_candidates(["Stereolabs"])
        assert at_default["Stereolabs"] == set()
        lowered = await svc.artist_trigram_candidates(["Stereolabs"], threshold=0.4)
        assert lowered["Stereolabs"] == {4242}  # extra=1 row 9999 stays excluded
