"""Integration (``@pytest.mark.pg``) tests for the per-track identity store helpers (LML#1020).

The unit tests mock ``PgSource``; this file drives the real D1 two-statement
upsert/update semantics -- the properties a mock cannot verify:

* the verdict writer's ``WHERE external_id IS NULL`` guard genuinely refuses
  to overwrite a resolved row on a re-attempt;
* the position writer genuinely fills only a NULL ``track_position`` and
  never touches a verdict column, on both a resolved and an unresolved row;
* the two statements really are independent -- a position can land on a row
  written in an earlier call, with byte-identical verdict columns afterward
  (the defect the plan's first two drafts each shipped).

Run with: pytest -m pg -v tests/integration/test_compilation_track_identity_store.py
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from entity.compilation_track_identity import (
    get_compilation_track_identity_for_library,
    get_compilation_track_identity_misses,
    set_up_compilation_track_identity_schema,
    write_compilation_track_identity_position,
    write_compilation_track_identity_verdict,
)
from scripts.backfill_compilation_track_identity import discogs_verdict_from_resolve_result
from tests.integration.conftest import skip_if_named_tables_populated


@pytest_asyncio.fixture(autouse=True)
async def set_up_identity_schema(pg_pool, pg_source):
    async with pg_pool.acquire() as conn:
        await skip_if_named_tables_populated(conn, (("lml_cache", "compilation_track_identity"),))
        await conn.execute("DROP TABLE IF EXISTS lml_cache.compilation_track_identity")
    await set_up_compilation_track_identity_schema(pg_source)
    yield
    async with pg_pool.acquire() as conn:
        await conn.execute("DROP TABLE IF EXISTS lml_cache.compilation_track_identity")


@pytest.mark.pg
class TestWriteVerdict:
    @pytest.mark.asyncio
    async def test_insert_then_read_back(self, pg_source):
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

        rows = await get_compilation_track_identity_for_library(pg_source, 1)
        assert len(rows) == 1
        row = rows[0]
        assert row.external_id == "12345"
        assert row.track_artist_raw == "Juana Molina"
        assert row.track_title_raw == "La Paradoja"
        assert row.track_position is None

    @pytest.mark.asyncio
    async def test_resolved_row_is_never_overwritten_by_a_later_miss(self, pg_source):
        """The D2 org data-safety rule: a re-attempt must never clobber a success."""
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

        # A later re-attempt finds nothing (e.g. a transient Discogs blip
        # resolved differently the second time) -- must not clobber the win.
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

        rows = await get_compilation_track_identity_for_library(pg_source, 1)
        assert len(rows) == 1
        assert rows[0].external_id == "12345"

    @pytest.mark.asyncio
    async def test_a_later_attempt_can_still_fill_the_original_miss(self, pg_source):
        await write_compilation_track_identity_verdict(
            pg_source,
            library_id=1,
            track_artist_raw="Sessa",
            track_title_raw="Música de Brinquedo",
            source="discogs",
            external_id=None,
            confidence=None,
            method=None,
            resolved_artist_name=None,
        )
        await write_compilation_track_identity_verdict(
            pg_source,
            library_id=1,
            track_artist_raw="Sessa",
            track_title_raw="Música de Brinquedo",
            source="discogs",
            external_id="999",
            confidence=0.9,
            method="trigram",
            resolved_artist_name="Sessa",
        )

        rows = await get_compilation_track_identity_for_library(pg_source, 1)
        assert len(rows) == 1
        assert rows[0].external_id == "999"


@pytest.mark.pg
class TestMatcherVerdictMappingInsertsCleanly:
    """LML#1020 slice 3's method/confidence mapping feeding slice 2's writer.

    A tier-1 ``identity_store`` resolution carries a non-NULL Discogs id
    alongside a NULL ``canonical_name`` (``entity.identity`` stores no
    Discogs title) -- the common resolved shape on a warm store, and the
    plan calls out the risk that this could trip the verdict-coherence
    CHECK if ``resolved_artist_name`` were bound into it. It isn't (D1), but
    this is the end-to-end proof: the matcher's own mapping function feeding
    the real writer against a real constraint, not just a shape assertion.
    """

    @pytest.mark.asyncio
    async def test_identity_store_hit_inserts_without_tripping_the_coherence_check(self, pg_source):
        resolve_result = {
            "name": "Juana Molina",
            "discogs_artist_id": 12345,
            "canonical_name": None,
            "method": "identity_store",
            "cache_corroboration": [],
            "unresolved_reason": None,
            "candidate_count": None,
        }
        verdict = discogs_verdict_from_resolve_result(resolve_result)
        assert verdict is not None

        await write_compilation_track_identity_verdict(
            pg_source,
            library_id=1,
            track_artist_raw="Juana Molina",
            track_title_raw="La Paradoja",
            source="discogs",
            external_id=verdict.external_id,
            confidence=verdict.confidence,
            method=verdict.method,
            resolved_artist_name=verdict.resolved_artist_name,
        )

        rows = await get_compilation_track_identity_for_library(pg_source, 1)
        assert rows[0].external_id == "12345"
        assert rows[0].resolved_artist_name is None
        assert rows[0].method is not None
        assert rows[0].confidence is not None


@pytest.mark.pg
class TestWritePosition:
    @pytest.mark.asyncio
    async def test_fills_null_position_without_touching_verdict_columns(self, pg_source):
        normalized_artist, normalized_title = await write_compilation_track_identity_verdict(
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

        await write_compilation_track_identity_position(
            pg_source,
            library_id=1,
            track_artist=normalized_artist,
            track_title=normalized_title,
            source="discogs",
            track_position="A1",
        )

        rows = await get_compilation_track_identity_for_library(pg_source, 1)
        assert len(rows) == 1
        row = rows[0]
        assert row.track_position == "A1"
        # Byte-identical verdict columns -- the round-2 upsert bug this test
        # is named against would have nulled one of these out.
        assert row.external_id == "12345"
        assert row.confidence == 1.0
        assert row.method == "exact_match"
        assert row.resolved_artist_name == "Juana Molina"

    @pytest.mark.asyncio
    async def test_fills_position_on_an_unresolved_row_too(self, pg_source):
        normalized_artist, normalized_title = await write_compilation_track_identity_verdict(
            pg_source,
            library_id=1,
            track_artist_raw="Csillagrablók",
            track_title_raw="Unknown",
            source="discogs",
            external_id=None,
            confidence=None,
            method=None,
            resolved_artist_name=None,
        )

        await write_compilation_track_identity_position(
            pg_source,
            library_id=1,
            track_artist=normalized_artist,
            track_title=normalized_title,
            source="discogs",
            track_position="B2",
        )

        rows = await get_compilation_track_identity_for_library(pg_source, 1)
        assert rows[0].track_position == "B2"
        assert rows[0].external_id is None

    @pytest.mark.asyncio
    async def test_never_overwrites_an_already_populated_position(self, pg_source):
        normalized_artist, normalized_title = await write_compilation_track_identity_verdict(
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
        await write_compilation_track_identity_position(
            pg_source,
            library_id=1,
            track_artist=normalized_artist,
            track_title=normalized_title,
            source="discogs",
            track_position="A1",
        )

        # A later, wrong write attempt must not clobber the already-filled position.
        await write_compilation_track_identity_position(
            pg_source,
            library_id=1,
            track_artist=normalized_artist,
            track_title=normalized_title,
            source="discogs",
            track_position="B9",
        )

        rows = await get_compilation_track_identity_for_library(pg_source, 1)
        assert rows[0].track_position == "A1"


@pytest.mark.pg
class TestGetMisses:
    @pytest.mark.asyncio
    async def test_only_returns_unresolved_rows(self, pg_source):
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
        await write_compilation_track_identity_verdict(
            pg_source,
            library_id=2,
            track_artist_raw="Sessa",
            track_title_raw="Música de Brinquedo",
            source="discogs",
            external_id=None,
            confidence=None,
            method=None,
            resolved_artist_name=None,
        )

        rows = await get_compilation_track_identity_misses(pg_source, limit=100)

        assert [row.library_id for row in rows] == [2]

    @pytest.mark.asyncio
    async def test_limit_is_respected(self, pg_source):
        for i in range(5):
            await write_compilation_track_identity_verdict(
                pg_source,
                library_id=i,
                track_artist_raw=f"Artist {i}",
                track_title_raw=f"Title {i}",
                source="discogs",
                external_id=None,
                confidence=None,
                method=None,
                resolved_artist_name=None,
            )

        rows = await get_compilation_track_identity_misses(pg_source, limit=2)
        assert len(rows) == 2


@pytest.mark.pg
class TestGetForLibrary:
    @pytest.mark.asyncio
    async def test_returns_every_source_row_for_the_library_id(self, pg_source):
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
        await write_compilation_track_identity_verdict(
            pg_source,
            library_id=1,
            track_artist_raw="The Bug",
            track_title_raw="Pressure",
            source="musicbrainz",
            external_id="mbid-1",
            confidence=0.9,
            method="trigram",
            resolved_artist_name="The Bug",
        )
        await write_compilation_track_identity_verdict(
            pg_source,
            library_id=2,
            track_artist_raw="Other Artist",
            track_title_raw="Other Title",
            source="discogs",
            external_id=None,
            confidence=None,
            method=None,
            resolved_artist_name=None,
        )

        rows = await get_compilation_track_identity_for_library(pg_source, 1)
        assert {row.source for row in rows} == {"discogs", "musicbrainz"}

    @pytest.mark.asyncio
    async def test_empty_for_a_library_id_never_visited(self, pg_source):
        rows = await get_compilation_track_identity_for_library(pg_source, 999)
        assert rows == []
