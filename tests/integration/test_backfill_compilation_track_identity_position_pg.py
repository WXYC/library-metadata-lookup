"""Integration (``@pytest.mark.pg``) tests for D5 position recovery (LML#1020 slice 4).

Drives the real LATERAL join between ``lml_cache.compilation_track_identity``
and ``lml_cache.compilation_track_location`` -- the fan-out-collapse
determinism a mock cannot exercise, since it depends on Postgres's own
``ORDER BY ... LIMIT 1`` evaluation. Four fixtures pin the plan's slice-4
acceptance criteria directly:

1. a CTA credit with no position gains one;
2. a credit absent from the tracklist stays NULL;
3. a credit matching TWO recall-index rows (same artist at two positions)
   resolves to one deterministic row and stays stable across re-runs;
4. an already-resolved row gains a position on a later run while its
   verdict columns stay byte-identical (the round-2 upsert bug the plan's
   D1 split-statement design exists to make structurally unavailable).

Run with: pytest -m pg -v tests/integration/test_backfill_compilation_track_identity_position_pg.py
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from entity.compilation_track_identity import (
    get_compilation_track_identity_for_library,
    set_up_compilation_track_identity_schema,
    write_compilation_track_identity_verdict,
)
from entity.compilation_track_location import set_up_compilation_track_location_schema
from scripts.backfill_compilation_track_identity import recover_track_positions
from tests.integration.conftest import skip_if_named_tables_populated

_INSERT_LOCATION_SQL = """\
INSERT INTO lml_cache.compilation_track_location
    (library_id, track_position, track_artist, track_title, credit_role,
     discogs_release_id, artwork_url)
VALUES ($1, $2, $3, $4, 'primary', $5, NULL)\
"""


@pytest_asyncio.fixture(autouse=True)
async def set_up_schemas(pg_pool, pg_source):
    async with pg_pool.acquire() as conn:
        await skip_if_named_tables_populated(
            conn,
            (
                ("lml_cache", "compilation_track_identity"),
                ("lml_cache", "compilation_track_location"),
            ),
        )
        await conn.execute("DROP TABLE IF EXISTS lml_cache.compilation_track_identity")
        await conn.execute("DROP TABLE IF EXISTS lml_cache.compilation_track_location")
    await set_up_compilation_track_identity_schema(pg_source)
    await set_up_compilation_track_location_schema(pg_source)
    yield
    async with pg_pool.acquire() as conn:
        await conn.execute("DROP TABLE IF EXISTS lml_cache.compilation_track_identity")
        await conn.execute("DROP TABLE IF EXISTS lml_cache.compilation_track_location")


@pytest.mark.pg
class TestRecoverTrackPositions:
    @pytest.mark.asyncio
    async def test_a_credit_with_no_position_gains_one(self, pg_source, pg_pool):
        await write_compilation_track_identity_verdict(
            pg_source,
            library_id=1,
            track_artist_raw="Juana Molina",
            track_title_raw="La Paradoja",
            source="discogs",
            external_id="12345",
            confidence=1.0,
            method="exact_match",
            resolved_artist_name="Juana Molina",
        )
        async with pg_pool.acquire() as conn:
            await conn.execute(_INSERT_LOCATION_SQL, 1, "A1", "juana molina", "la paradoja", 1)

        stats = await recover_track_positions(pg_source, [1])

        rows = await get_compilation_track_identity_for_library(pg_source, 1)
        assert rows[0].track_position == "A1"
        assert stats.recovered_rows == 1
        assert stats.recall_index_covered_library_ids == 1

    @pytest.mark.asyncio
    async def test_a_credit_absent_from_the_tracklist_stays_null(self, pg_source, pg_pool):
        await write_compilation_track_identity_verdict(
            pg_source,
            library_id=1,
            track_artist_raw="Juana Molina",
            track_title_raw="La Paradoja",
            source="discogs",
            external_id="12345",
            confidence=1.0,
            method="exact_match",
            resolved_artist_name="Juana Molina",
        )
        # The comp IS covered by the recall index, but for a DIFFERENT
        # track -- the credit-string-agreement axis misses, not coverage.
        async with pg_pool.acquire() as conn:
            await conn.execute(
                _INSERT_LOCATION_SQL, 1, "A1", "someone else", "a different track", 1
            )

        stats = await recover_track_positions(pg_source, [1])

        rows = await get_compilation_track_identity_for_library(pg_source, 1)
        assert rows[0].track_position is None
        assert stats.recovered_rows == 0
        # Coverage is still counted (the comp DID match a release) --
        # distinguishing this from the pure-absence case below is the whole
        # point of reporting the two stats separately.
        assert stats.recall_index_covered_library_ids == 1

    @pytest.mark.asyncio
    async def test_uncovered_comp_has_zero_coverage_and_zero_recovery(self, pg_source):
        await write_compilation_track_identity_verdict(
            pg_source,
            library_id=1,
            track_artist_raw="Juana Molina",
            track_title_raw="La Paradoja",
            source="discogs",
            external_id=None,
            confidence=None,
            method=None,
            resolved_artist_name=None,
        )
        # No compilation_track_location rows at all for library_id=1.

        stats = await recover_track_positions(pg_source, [1])

        assert stats.recall_index_covered_library_ids == 0
        assert stats.recovered_rows == 0

    @pytest.mark.asyncio
    async def test_fan_out_collapses_to_one_deterministic_row_and_is_stable(
        self, pg_source, pg_pool
    ):
        # The same artist credited at TWO positions on one comp -- the
        # recall index's PK is (library_id, track_position, track_artist),
        # not (..., track_title), so this triple legitimately matches two
        # rows. A plain join would fan out into conflicting upserts.
        await write_compilation_track_identity_verdict(
            pg_source,
            library_id=1,
            track_artist_raw="The Bug",
            track_title_raw="Pressure",
            source="discogs",
            external_id="1",
            confidence=1.0,
            method="exact_match",
            resolved_artist_name=None,
        )
        async with pg_pool.acquire() as conn:
            await conn.execute(_INSERT_LOCATION_SQL, 1, "A1", "the bug", "pressure", 1)
            await conn.execute(_INSERT_LOCATION_SQL, 1, "B2", "the bug", "pressure", 1)

        stats_first = await recover_track_positions(pg_source, [1])
        rows_first = await get_compilation_track_identity_for_library(pg_source, 1)
        position_first = rows_first[0].track_position

        # track_position is now non-NULL, so a second run must be a no-op
        # (write_compilation_track_identity_position never overwrites a
        # populated position) -- and if it DID re-pick, the LATERAL's
        # ORDER BY makes the pick deterministic either way.
        stats_second = await recover_track_positions(pg_source, [1])
        rows_second = await get_compilation_track_identity_for_library(pg_source, 1)

        assert position_first == "A1"  # ORDER BY track_position picks 'A1' before 'B2'
        assert stats_first.recovered_rows == 1
        assert stats_second.recovered_rows == 0  # already filled -- nothing left to recover
        assert rows_second[0].track_position == position_first

    @pytest.mark.asyncio
    async def test_an_already_resolved_row_gains_a_position_with_byte_identical_verdict(
        self, pg_source, pg_pool
    ):
        # The round-2 upsert bug this design exists to make structurally
        # unavailable: a position filled on a LATER run must not touch any
        # verdict column of an already-resolved row.
        await write_compilation_track_identity_verdict(
            pg_source,
            library_id=1,
            track_artist_raw="Juana Molina",
            track_title_raw="La Paradoja",
            source="discogs",
            external_id="12345",
            confidence=1.0,
            method="exact_match",
            resolved_artist_name="Juana Molina",
        )
        rows_before = await get_compilation_track_identity_for_library(pg_source, 1)
        assert rows_before[0].track_position is None

        # The recall index only gains this comp on a LATER build-script run.
        async with pg_pool.acquire() as conn:
            await conn.execute(_INSERT_LOCATION_SQL, 1, "A1", "juana molina", "la paradoja", 1)

        await recover_track_positions(pg_source, [1])

        rows_after = await get_compilation_track_identity_for_library(pg_source, 1)
        assert rows_after[0].track_position == "A1"
        assert rows_after[0].external_id == rows_before[0].external_id
        assert rows_after[0].confidence == rows_before[0].confidence
        assert rows_after[0].method == rows_before[0].method
        assert rows_after[0].resolved_artist_name == rows_before[0].resolved_artist_name

    @pytest.mark.asyncio
    async def test_empty_library_ids_is_a_no_op(self, pg_source):
        stats = await recover_track_positions(pg_source, [])
        assert stats.candidate_rows == 0
        assert stats.recovered_rows == 0
