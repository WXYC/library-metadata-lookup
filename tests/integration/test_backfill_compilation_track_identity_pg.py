"""Integration (``@pytest.mark.pg``) tests for the full backfill drain (LML#1020 slice 5).

Drains a fixture-scale CTA population end to end against real PostgreSQL --
schema bootstrap, ``run_backfill``'s fan-back writes, and D5 position
recovery -- with a FAKE Discogs ``post_batch`` (no HTTP, no live Discogs
call anywhere in this suite, per the run's hard boundary) and, where noted,
a fake MusicBrainz ``PgSourceProtocol`` (this suite never touches a real
musicbrainz-cache schema -- the MB SQL itself is covered against real PG in
``tests/unit/test_backfill_compilation_track_identity.py`` via
``fetch_mb_artist_candidates``'s own coverage in
``tests/unit/test_external_artist_search.py``).

Run with: pytest -m pg -v tests/integration/test_backfill_compilation_track_identity_pg.py
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from wxyc_etl.text import to_match_form

from entity.compilation_track_identity import (
    get_compilation_track_identity_for_library,
    set_up_compilation_track_identity_schema,
)
from entity.compilation_track_location import set_up_compilation_track_location_schema
from scripts.backfill_compilation_track_identity import (
    CtaCredit,
    run_backfill,
    select_credit_population,
)
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


def _resolved(name: str, discogs_artist_id: int) -> dict:
    return {
        "name": name,
        "discogs_artist_id": discogs_artist_id,
        "canonical_name": name,
        "method": "api_search",
        "cache_corroboration": [],
        "unresolved_reason": None,
        "candidate_count": 1,
    }


def _not_found(name: str) -> dict:
    return {
        "name": name,
        "discogs_artist_id": None,
        "canonical_name": None,
        "method": None,
        "cache_corroboration": [],
        "unresolved_reason": "not_found",
        "candidate_count": 0,
    }


# A small WXYC-representative fixture population: a compilation ("various-artists-comp")
# crediting Juana Molina and Chuquimamani-Condori, plus one credit that never resolves.
_FIXTURE_CREDITS = [
    CtaCredit(library_id=101, track_artist="Juana Molina", track_title="La Paradoja"),
    CtaCredit(library_id=101, track_artist="Chuquimamani-Condori", track_title="Call Your Name"),
    CtaCredit(library_id=102, track_artist="Juana Molina", track_title="Eras"),
    CtaCredit(library_id=103, track_artist="Nobody Findable", track_title="Untitled"),
]

_FIXTURE_VERDICTS = {
    "Juana Molina": _resolved("Juana Molina", 111),
    "Chuquimamani-Condori": _resolved("Chuquimamani-Condori", 222),
    "Nobody Findable": _not_found("Nobody Findable"),
}


def _fake_post_batch(verdicts_by_name: dict[str, dict]):
    calls: list[list[str]] = []

    async def post_batch(names: list[str], dry_run: bool) -> list[dict]:
        calls.append(list(names))
        return [verdicts_by_name[name] for name in names]

    post_batch.calls = calls  # type: ignore[attr-defined]
    return post_batch


@pytest.mark.pg
class TestFixtureScaleDrain:
    @pytest.mark.asyncio
    async def test_drains_the_fixture_population_and_fans_back_every_row(self, pg_source):
        post_batch = _fake_post_batch(_FIXTURE_VERDICTS)

        stats = await run_backfill(
            credits=_FIXTURE_CREDITS,
            post_batch=post_batch,
            mb_pg=None,
            pg=pg_source,
            dry_run_write=False,
            dry_run_http=True,
        )

        assert stats.candidates == 4
        assert stats.distinct_credits == 3  # Juana Molina credited twice, once distinct
        assert stats.rows_written == 4  # one discogs row per CTA credit, fanned back

        rows_101 = await get_compilation_track_identity_for_library(pg_source, 101)
        assert len(rows_101) == 2
        by_artist = {row.track_artist: row for row in rows_101}
        assert by_artist[to_match_form("Juana Molina")].external_id == "111"
        assert by_artist[to_match_form("Chuquimamani-Condori")].external_id == "222"

        rows_102 = await get_compilation_track_identity_for_library(pg_source, 102)
        assert rows_102[0].external_id == "111"  # same artist, second comp, fanned back

        rows_103 = await get_compilation_track_identity_for_library(pg_source, 103)
        assert rows_103[0].external_id is None  # not_found -- a miss row, not silence

    @pytest.mark.asyncio
    async def test_a_second_incremental_run_never_re_asks_discogs(self, pg_source):
        post_batch = _fake_post_batch(_FIXTURE_VERDICTS)
        await run_backfill(
            credits=_FIXTURE_CREDITS,
            post_batch=post_batch,
            mb_pg=None,
            pg=pg_source,
            dry_run_write=False,
            dry_run_http=True,
        )

        discogs_credits, _mb = await select_credit_population(
            pg_source, _FIXTURE_CREDITS, mode="incremental", mb_configured=False
        )
        assert discogs_credits == []  # every credit already has a discogs row

    @pytest.mark.asyncio
    async def test_retry_misses_only_re_asks_the_genuine_miss(self, pg_source):
        post_batch = _fake_post_batch(_FIXTURE_VERDICTS)
        await run_backfill(
            credits=_FIXTURE_CREDITS,
            post_batch=post_batch,
            mb_pg=None,
            pg=pg_source,
            dry_run_write=False,
            dry_run_http=True,
        )

        discogs_credits, _mb = await select_credit_population(
            pg_source, _FIXTURE_CREDITS, mode="retry-misses", mb_configured=False
        )
        assert [c.track_artist for c in discogs_credits] == ["Nobody Findable"]

    @pytest.mark.asyncio
    async def test_position_recovery_runs_after_the_write_pass(self, pg_source, pg_pool):
        async with pg_pool.acquire() as conn:
            await conn.execute(_INSERT_LOCATION_SQL, 101, "A1", "juana molina", "la paradoja", 500)

        post_batch = _fake_post_batch(_FIXTURE_VERDICTS)
        stats = await run_backfill(
            credits=_FIXTURE_CREDITS,
            post_batch=post_batch,
            mb_pg=None,
            pg=pg_source,
            dry_run_write=False,
            dry_run_http=True,
        )

        assert stats.position_stats is not None
        assert stats.position_stats.recovered_rows == 1
        rows_101 = await get_compilation_track_identity_for_library(pg_source, 101)
        by_artist = {row.track_artist: row for row in rows_101}
        assert by_artist[to_match_form("Juana Molina")].track_position == "A1"
        assert by_artist[to_match_form("Chuquimamani-Condori")].track_position is None

    @pytest.mark.asyncio
    async def test_mb_unconfigured_then_configured_writes_mb_rows_on_the_second_pass(
        self, pg_source
    ):
        """The plan's explicit slice-5 pin, at the PG layer: a credit resolved
        on Discogs while DATABASE_URL_MUSICBRAINZ was unset must still be a
        candidate for the MB leg once it's configured -- leaving it out would
        make MB coverage unbackfillable forever.
        """
        post_batch = _fake_post_batch(_FIXTURE_VERDICTS)
        await run_backfill(
            credits=_FIXTURE_CREDITS,
            post_batch=post_batch,
            mb_pg=None,  # unconfigured on the first pass
            pg=pg_source,
            dry_run_write=False,
            dry_run_http=True,
        )
        rows_before = await get_compilation_track_identity_for_library(pg_source, 101)
        assert {row.source for row in rows_before} == {"discogs"}

        mb_pg = AsyncMock()
        mb_pg.fetchall = AsyncMock(
            return_value=[{"id": "mb-uuid-1", "name": "Juana Molina", "score": 0.95}]
        )
        discogs_credits, mb_credits = await select_credit_population(
            pg_source, _FIXTURE_CREDITS, mode="incremental", mb_configured=True
        )
        assert discogs_credits == []  # Discogs already attempted -- not re-asked
        assert len(mb_credits) == 4  # every credit is still an MB candidate

        await run_backfill(
            credits=_FIXTURE_CREDITS,
            post_batch=post_batch,
            mb_pg=mb_pg,
            pg=pg_source,
            dry_run_write=False,
            dry_run_http=True,
            discogs_credits=discogs_credits,
            musicbrainz_credits=mb_credits,
        )

        rows_after = await get_compilation_track_identity_for_library(pg_source, 101)
        assert {row.source for row in rows_after} == {"discogs", "musicbrainz"}
        mb_row = next(row for row in rows_after if row.source == "musicbrainz")
        assert mb_row.external_id == "mb-uuid-1"
