"""Integration (``@pytest.mark.pg``) tests for the per-track identity schema (LML#1020).

All unit coverage mocks ``PgSource``; this file drives the real DDL -- the
real credit-shaped primary key, the real ``source`` and verdict-coherence
CHECKs, and the real partial retry-cohort index -- against an actual
PostgreSQL connection.

Two assertions here are the ones a mocked test cannot make, and both encode
findings from ``docs/plans/lml-1020-per-track-identity-matcher.md``:

* the coherence CHECK must ACCEPT a tier-1 ``identity_store`` verdict, which
  carries a non-NULL ``external_id`` alongside a NULL ``resolved_artist_name``
  (``entity.identity`` stores no Discogs title). Binding that column into the
  constraint would reject the common resolved shape on a warm store.
* the partial index must be usable by the planner for the ``--retry-misses``
  cohort query -- the one access path the primary key does not already serve,
  since ``library_id`` is the PK's leading column.

Run with: pytest -m pg -v tests/integration/test_compilation_track_identity_schema.py
"""

from __future__ import annotations

import asyncpg
import pytest
import pytest_asyncio

from entity.compilation_track_identity import set_up_compilation_track_identity_schema
from tests.integration.conftest import skip_if_named_tables_populated

_INSERT_SQL = """\
INSERT INTO lml_cache.compilation_track_identity
    (library_id, track_artist, track_title, source, external_id, confidence,
     method, resolved_artist_name, track_artist_raw, track_title_raw, track_position)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)\
"""


@pytest_asyncio.fixture(autouse=True)
async def set_up_identity_schema(pg_pool, pg_source):
    """Reset just this table (not the whole ``lml_cache`` schema), then apply its DDL.

    The populated-table veto runs FIRST so a mispointed ``DATABASE_URL_TEST``
    at a real discogs-cache can't drop collected data.
    """
    async with pg_pool.acquire() as conn:
        await skip_if_named_tables_populated(conn, (("lml_cache", "compilation_track_identity"),))
        await conn.execute("DROP TABLE IF EXISTS lml_cache.compilation_track_identity")
    await set_up_compilation_track_identity_schema(pg_source)
    yield
    async with pg_pool.acquire() as conn:
        await conn.execute("DROP TABLE IF EXISTS lml_cache.compilation_track_identity")


@pytest.mark.pg
class TestSchemaBootstrap:
    @pytest.mark.asyncio
    async def test_second_boot_is_a_no_op(self, pg_source, pg_pool):
        await set_up_compilation_track_identity_schema(pg_source)
        async with pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT count(*)::int AS n FROM information_schema.tables "
                "WHERE table_schema = 'lml_cache' "
                "AND table_name = 'compilation_track_identity'"
            )
        assert row["n"] == 1

    @pytest.mark.asyncio
    async def test_retry_cohort_index_is_partial(self, pg_pool):
        async with pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT indexdef FROM pg_indexes "
                "WHERE schemaname = 'lml_cache' "
                "AND indexname = 'idx_compilation_track_identity_misses'"
            )
        assert row is not None
        assert "external_id IS NULL" in row["indexdef"]

    @pytest.mark.asyncio
    async def test_source_check_rejects_unknown_source(self, pg_pool):
        with pytest.raises(asyncpg.PostgresError):
            async with pg_pool.acquire() as conn:
                await conn.execute(
                    _INSERT_SQL,
                    1,
                    "juana molina",
                    "la paradoja",
                    "spotify",
                    "123",
                    0.9,
                    "exact_match",
                    "Juana Molina",
                    "Juana Molina",
                    "la paradoja",
                    "A1",
                )


@pytest.mark.pg
class TestVerdictCoherence:
    @pytest.mark.asyncio
    async def test_accepts_tier_one_verdict_with_null_canonical_name(self, pg_pool):
        """A resolved row with no ``resolved_artist_name`` must INSERT.

        This is the common shape on a warm store: an ``identity_store`` hit
        resolves the Discogs id but carries no canonical name, because
        ``entity.identity`` stores no Discogs title.
        """
        async with pg_pool.acquire() as conn:
            await conn.execute(
                _INSERT_SQL,
                1,
                "juana molina",
                "la paradoja",
                "discogs",
                "12345",
                1.0,
                "exact_match",
                None,
                "Juana Molina",
                "la paradoja",
                "A1",
            )
            row = await conn.fetchrow(
                "SELECT external_id, resolved_artist_name "
                "FROM lml_cache.compilation_track_identity WHERE library_id = 1"
            )
        assert row["external_id"] == "12345"
        assert row["resolved_artist_name"] is None

    @pytest.mark.asyncio
    async def test_accepts_attempt_row_with_null_verdict(self, pg_pool):
        """A miss row -- the source answered with nothing -- is the whole point of D2."""
        async with pg_pool.acquire() as conn:
            await conn.execute(
                _INSERT_SQL,
                1,
                "csillagrablok",
                "unknown",
                "discogs",
                None,
                None,
                None,
                None,
                "Csillagrablók",
                "unknown",
                None,
            )
            row = await conn.fetchrow(
                "SELECT external_id, confidence, method "
                "FROM lml_cache.compilation_track_identity WHERE library_id = 1"
            )
        assert row["external_id"] is None
        assert row["confidence"] is None
        assert row["method"] is None

    @pytest.mark.asyncio
    async def test_rejects_confidence_without_external_id(self, pg_pool):
        with pytest.raises(asyncpg.PostgresError):
            async with pg_pool.acquire() as conn:
                await conn.execute(
                    _INSERT_SQL,
                    1,
                    "sessa",
                    "musica",
                    "discogs",
                    None,
                    0.9,
                    None,
                    None,
                    "Sessa",
                    "musica",
                    None,
                )

    @pytest.mark.asyncio
    async def test_rejects_external_id_without_method(self, pg_pool):
        with pytest.raises(asyncpg.PostgresError):
            async with pg_pool.acquire() as conn:
                await conn.execute(
                    _INSERT_SQL,
                    1,
                    "sessa",
                    "musica",
                    "discogs",
                    "999",
                    0.9,
                    None,
                    None,
                    "Sessa",
                    "musica",
                    None,
                )


@pytest.mark.pg
class TestCreditShapedKey:
    @pytest.mark.asyncio
    async def test_two_artists_on_one_track_are_two_rows(self, pg_pool):
        """The grain is per-CREDIT, which is why the key is not position-shaped.

        A 66-performer track legitimately occupies 66 rows at one position --
        the finding that killed ``(library_id, track_position)`` in
        WXYC/Backend-Service#1989 and, cross-applied, here.
        """
        async with pg_pool.acquire() as conn:
            await conn.execute(
                _INSERT_SQL,
                1,
                "the bug",
                "pressure",
                "discogs",
                "1",
                1.0,
                "exact_match",
                None,
                "The Bug",
                "Pressure",
                "A1",
            )
            await conn.execute(
                _INSERT_SQL,
                1,
                "flowdan",
                "pressure",
                "discogs",
                "2",
                1.0,
                "exact_match",
                None,
                "Flowdan",
                "Pressure",
                "A1",
            )
            rows = await conn.fetch(
                "SELECT track_artist FROM lml_cache.compilation_track_identity "
                "WHERE library_id = 1 ORDER BY track_artist"
            )
        assert [r["track_artist"] for r in rows] == ["flowdan", "the bug"]

    @pytest.mark.asyncio
    async def test_same_credit_across_sources_is_two_rows(self, pg_pool):
        async with pg_pool.acquire() as conn:
            await conn.execute(
                _INSERT_SQL,
                1,
                "the bug",
                "pressure",
                "discogs",
                "1",
                1.0,
                "exact_match",
                None,
                "The Bug",
                "Pressure",
                "A1",
            )
            await conn.execute(
                _INSERT_SQL,
                1,
                "the bug",
                "pressure",
                "musicbrainz",
                "mbid-1",
                0.9,
                "trigram",
                "The Bug",
                "The Bug",
                "Pressure",
                "A1",
            )
            row = await conn.fetchrow(
                "SELECT count(*)::int AS n FROM lml_cache.compilation_track_identity"
            )
        assert row["n"] == 2

    @pytest.mark.asyncio
    async def test_primary_key_rejects_duplicate_credit_per_source(self, pg_pool):
        async with pg_pool.acquire() as conn:
            await conn.execute(
                _INSERT_SQL,
                1,
                "the bug",
                "pressure",
                "discogs",
                "1",
                1.0,
                "exact_match",
                None,
                "The Bug",
                "Pressure",
                "A1",
            )
            with pytest.raises(asyncpg.PostgresError):
                await conn.execute(
                    _INSERT_SQL,
                    1,
                    "the bug",
                    "pressure",
                    "discogs",
                    "2",
                    1.0,
                    "exact_match",
                    None,
                    "The Bug",
                    "Pressure",
                    "B2",
                )


@pytest.mark.pg
class TestRetryCohortIndexUse:
    @pytest.mark.asyncio
    async def test_explain_uses_partial_index_for_miss_cohort(self, pg_pool):
        """The planner must be ABLE to use the partial index for the retry cohort.

        ``enable_seqscan = off`` removes small-table cost-estimate noise --
        the deterministic way to prove the index is usable. This is the one
        query shape the PK cannot serve on its own: the PK's leading column
        is ``library_id``, so it already answers ``WHERE library_id = $1``;
        what it cannot do is find the misses across the table.
        """
        async with pg_pool.acquire() as conn:
            for i in range(20):
                await conn.execute(
                    _INSERT_SQL,
                    i,
                    f"artist {i}",
                    f"title {i}",
                    "discogs",
                    None if i % 2 else str(i),
                    None if i % 2 else 1.0,
                    None if i % 2 else "exact_match",
                    None,
                    f"Artist {i}",
                    f"Title {i}",
                    None,
                )
            await conn.execute("SET enable_seqscan = off")
            try:
                plan_rows = await conn.fetch(
                    "EXPLAIN SELECT * FROM lml_cache.compilation_track_identity "
                    "WHERE library_id = $1 AND external_id IS NULL",
                    5,
                )
            finally:
                await conn.execute("RESET enable_seqscan")
        plan = "\n".join(row["QUERY PLAN"] for row in plan_rows)
        assert "idx_compilation_track_identity_misses" in plan
        assert "Seq Scan" not in plan
