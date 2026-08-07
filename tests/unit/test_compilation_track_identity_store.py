"""Unit tests for the per-track identity store read/write helpers (LML#1020 slice 2).

Three helpers, colocated with the DDL in ``entity/compilation_track_identity.py``
exactly as the sibling recall index colocates its read helper:

* the D1 two-statement writer -- ``write_compilation_track_identity_verdict``
  (statement 1, the attempt row, never overwritten once ``external_id`` is
  non-null) and ``fill_compilation_track_identity_positions_from_recall_index``
  (statement 2, set-based: fills NULL ``track_position``s from the LML#1019
  recall index on ANY row -- resolved or not -- and never touches a verdict
  column). Split into two statements (not one) because D1's finding is that
  position and verdict arrive on different schedules: a credit resolved
  before its comp entered the recall index must still be able to gain a
  position on a LATER run, with no verdict fields to resupply.
* ``get_compilation_track_identity_misses`` -- the ``WHERE external_id IS
  NULL`` retry-cohort reader; the ONE definition of the miss cohort, which
  the backfill's ``--retry-misses`` mode drains through ``load_miss_keys``.
* ``get_compilation_track_identity_for_library`` -- the per-``library_id``
  read LML#1021 will consume to compose ``tracks[]``.

PG is mocked; ``tests/integration/test_compilation_track_identity_store.py``
drives the real SQL (upsert-never-overwrites, position-fills-without-
touching-verdict, the fan-out-collapse determinism) against PostgreSQL.

Plan: ``docs/plans/lml-1020-per-track-identity-matcher.md``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from wxyc_etl.text import to_match_form

from entity.compilation_track_identity import (
    CompilationTrackIdentityRow,
    fill_compilation_track_identity_positions_from_recall_index,
    get_compilation_track_identity_for_libraries,
    get_compilation_track_identity_for_library,
    get_compilation_track_identity_misses,
    write_compilation_track_identity_verdict,
)
from entity.sources import PgSource


@pytest.mark.asyncio
class TestWriteVerdict:
    async def test_normalizes_artist_and_title_into_the_key(self):
        pg = AsyncMock(spec=PgSource)
        pg.execute = AsyncMock(return_value="INSERT 0 1")

        normalized = await write_compilation_track_identity_verdict(
            pg,
            library_id=1,
            track_artist_raw="Juana Molina",
            track_title_raw="La Paradoja",
            source="discogs",
            external_id="12345",
            confidence=1.0,
            method="exact_match",
            resolved_artist_name="Juana Molina",
        )

        assert normalized == (to_match_form("Juana Molina"), to_match_form("La Paradoja"))
        sql, *binds = pg.execute.await_args.args
        assert "INSERT INTO lml_cache.compilation_track_identity" in sql
        assert "ON CONFLICT" in sql
        assert "WHERE compilation_track_identity.external_id IS NULL" in sql
        assert binds == [
            1,
            to_match_form("Juana Molina"),
            to_match_form("La Paradoja"),
            "discogs",
            "12345",
            1.0,
            "exact_match",
            "Juana Molina",
            "Juana Molina",
            "La Paradoja",
        ]

    async def test_none_title_normalizes_key_to_empty_string_but_raw_stays_none(self):
        # CTA's title column is nullable upstream; NULL cannot sit in a PK, so
        # the normalized key column gets "" while the raw echo preserves the
        # true NULL for the wire.
        pg = AsyncMock(spec=PgSource)
        pg.execute = AsyncMock(return_value="INSERT 0 1")

        normalized = await write_compilation_track_identity_verdict(
            pg,
            library_id=1,
            track_artist_raw="Csillagrablók",
            track_title_raw=None,
            source="discogs",
            external_id=None,
            confidence=None,
            method=None,
            resolved_artist_name=None,
        )

        assert normalized == (to_match_form("Csillagrablók"), "")
        _sql, *binds = pg.execute.await_args.args
        assert binds[2] == ""  # normalized title key
        assert binds[9] is None  # track_title_raw

    async def test_write_failure_propagates(self):
        # Unlike every other lml_cache.* write helper, this one does NOT
        # swallow: D3's abort-on-outage rule needs a PG failure here to stop
        # the backfill run rather than silently recording a mass of false
        # misses that --retry-misses would then decline to revisit.
        pg = AsyncMock(spec=PgSource)
        pg.execute = AsyncMock(side_effect=RuntimeError("pg down"))

        with pytest.raises(RuntimeError):
            await write_compilation_track_identity_verdict(
                pg,
                library_id=1,
                track_artist_raw="Sessa",
                track_title_raw="Música de Brinquedo",
                source="discogs",
                external_id=None,
                confidence=None,
                method=None,
                resolved_artist_name=None,
            )


@pytest.mark.asyncio
class TestFillPositions:
    async def test_single_set_based_statement_with_the_never_overwrite_guards(self):
        # One UPDATE ... FROM with the LATERAL inside it -- not a SELECT plus a
        # round-trip per recovered row. Both never-overwrite guards must be in
        # the statement itself: the inner candidate scan's and the outer
        # UPDATE's `track_position IS NULL` (the outer one is what makes a
        # concurrent fill race-safe), and no verdict column in the SET list.
        pg = AsyncMock(spec=PgSource)
        pg.fetchall = AsyncMock(return_value=[])

        await fill_compilation_track_identity_positions_from_recall_index(pg, [1, 2])

        sql, *binds = pg.fetchall.await_args.args
        assert "UPDATE lml_cache.compilation_track_identity" in sql
        assert "JOIN LATERAL" in sql
        assert "ORDER BY ctl.track_position" in sql
        assert sql.count("track_position IS NULL") == 2
        assert "SET src" not in sql
        assert "external_id" not in sql.split("SET")[1].split("FROM")[0]
        assert "RETURNING" in sql
        assert binds == [[1, 2]]

    async def test_empty_library_ids_short_circuits(self):
        pg = AsyncMock(spec=PgSource)
        pg.fetchall = AsyncMock(return_value=[])

        filled = await fill_compilation_track_identity_positions_from_recall_index(pg, [])

        assert filled == []
        pg.fetchall.assert_not_awaited()

    async def test_write_failure_propagates(self):
        pg = AsyncMock(spec=PgSource)
        pg.fetchall = AsyncMock(side_effect=RuntimeError("pg down"))

        with pytest.raises(RuntimeError):
            await fill_compilation_track_identity_positions_from_recall_index(pg, [1])


_SAMPLE_ROW = {
    "library_id": 1,
    "track_artist": "juana molina",
    "track_title": "la paradoja",
    "source": "discogs",
    "external_id": None,
    "confidence": None,
    "method": None,
    "resolved_artist_name": None,
    "track_artist_raw": "Juana Molina",
    "track_title_raw": "La Paradoja",
    "track_position": None,
}


@pytest.mark.asyncio
class TestGetMisses:
    async def test_returns_rows_from_the_retry_cohort_query(self):
        pg = AsyncMock(spec=PgSource)
        pg.fetchall = AsyncMock(return_value=[_SAMPLE_ROW])

        rows = await get_compilation_track_identity_misses(pg, limit=500)

        assert rows == [CompilationTrackIdentityRow(**_SAMPLE_ROW)]
        sql, *binds = pg.fetchall.await_args.args
        assert "WHERE external_id IS NULL" in sql
        assert binds == [None, 500]

    async def test_source_scopes_the_cohort_and_none_limit_is_unbounded(self):
        # --retry-misses evaluates per (credit, source) -- D2's grain -- so
        # the reader must scope by source; and a full retry pass needs the
        # WHOLE cohort, which LIMIT NULL gives (unbounded in PostgreSQL).
        pg = AsyncMock(spec=PgSource)
        pg.fetchall = AsyncMock(return_value=[_SAMPLE_ROW])

        rows = await get_compilation_track_identity_misses(pg, source="discogs")

        assert rows == [CompilationTrackIdentityRow(**_SAMPLE_ROW)]
        sql, *binds = pg.fetchall.await_args.args
        assert "source = $1::text" in sql
        assert binds == ["discogs", None]

    async def test_empty_on_no_misses(self):
        pg = AsyncMock(spec=PgSource)
        pg.fetchall = AsyncMock(return_value=[])

        rows = await get_compilation_track_identity_misses(pg, limit=500)

        assert rows == []


@pytest.mark.asyncio
class TestGetForLibrary:
    async def test_returns_every_row_for_the_library_id(self):
        pg = AsyncMock(spec=PgSource)
        pg.fetchall = AsyncMock(return_value=[_SAMPLE_ROW])

        rows = await get_compilation_track_identity_for_library(pg, library_id=1)

        assert rows == [CompilationTrackIdentityRow(**_SAMPLE_ROW)]
        sql, *binds = pg.fetchall.await_args.args
        assert "WHERE library_id = $1" in sql
        assert binds == [1]

    async def test_orders_deterministically_by_the_full_pk(self):
        """No ORDER BY means Postgres may return rows in any physical order
        -- LML#1021's per-track composer needs a deterministic base order
        for `sources[]` wire stability (review finding), independent of
        query plan. Ordering by the whole PK (library_id, track_artist,
        track_title, source) is a total order -- no two rows can tie."""
        pg = AsyncMock(spec=PgSource)
        pg.fetchall = AsyncMock(return_value=[_SAMPLE_ROW])

        await get_compilation_track_identity_for_library(pg, library_id=1)

        sql, *_ = pg.fetchall.await_args.args
        assert "ORDER BY library_id, track_artist, track_title, source" in sql


@pytest.mark.asyncio
class TestGetForLibraries:
    """The LML#1021 batched read: ONE query for a whole bulk-resolve batch's
    compilation ``library_id``s (here, the legacy-space id the store is keyed
    on -- see the module docstring's F2 id-space warning), not one per row.
    """

    async def test_one_query_for_multiple_library_ids(self):
        pg = AsyncMock(spec=PgSource)
        pg.fetchall = AsyncMock(return_value=[_SAMPLE_ROW])

        rows = await get_compilation_track_identity_for_libraries(pg, [1, 2, 3])

        assert rows == [CompilationTrackIdentityRow(**_SAMPLE_ROW)]
        assert pg.fetchall.await_count == 1
        sql, *binds = pg.fetchall.await_args.args
        assert "WHERE library_id = ANY($1::int[])" in sql
        assert binds == [[1, 2, 3]]

    async def test_orders_deterministically_by_the_full_pk(self):
        """Same determinism requirement as the singular reader -- LML#1021's
        composer sorts `sources[]` off this base order (review finding: a
        cross-source confidence tie must not depend on database row order
        or query plan)."""
        pg = AsyncMock(spec=PgSource)
        pg.fetchall = AsyncMock(return_value=[_SAMPLE_ROW])

        await get_compilation_track_identity_for_libraries(pg, [1, 2, 3])

        sql, *_ = pg.fetchall.await_args.args
        assert "ORDER BY library_id, track_artist, track_title, source" in sql

    async def test_empty_library_ids_short_circuits_before_the_round_trip(self):
        pg = AsyncMock(spec=PgSource)
        pg.fetchall = AsyncMock(return_value=[_SAMPLE_ROW])

        rows = await get_compilation_track_identity_for_libraries(pg, [])

        assert rows == []
        pg.fetchall.assert_not_awaited()
