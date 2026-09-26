"""Tests for the unmatched-compilation drain's three acceptance sites (LML#1353).

Album 52656 is the production artifact row this suite is built around:
``display_artist`` "Soundtracks - M", ``display_title`` "Married to the Mob"
(a 1988 soundtrack). Every lane must refuse Speaker Knockerz's "Married to the
Money" — whose title scores 89.47 — because the candidate is credited to a named
artist, so the artist axis is informative and scored 30.77.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from scripts._lib.match_decision import (
    AXES_ARTIST_AND_TITLE,
    AXES_TITLE_ONLY,
    STATUS_FOUND,
    STATUS_FOUND_TITLE_ONLY,
)
from scripts.search_unmatched_compilations import (
    _DEEZER_ARTIST,
    _DEEZER_TITLE,
    _DEEZER_URL,
    StreamingLane,
    resolve_lane,
    run,
    search_discogs_by_title,
)
from scripts.streaming_availability.results_db import ResultsDB


class TestSearchDiscogsByTitle:
    """Phase 1: the Discogs-cache title search (the third copy of the shape)."""

    @pytest.mark.asyncio
    async def test_accepts_a_va_credited_release(self):
        pool = AsyncMock()
        pool.fetch.return_value = [
            {"id": 12345, "title": "Nuggets: Original Artyfacts", "artist_name": "Various"}
        ]
        match = await search_discogs_by_title(
            pool, "Nuggets: Original Artyfacts", query_artist="Various"
        )
        assert match is not None
        assert match["release_id"] == 12345
        assert match["axes"] == AXES_TITLE_ONLY

    @pytest.mark.asyncio
    async def test_a_recovered_artist_name_still_matches_on_both_axes(self):
        """``build_compilation_query`` does not always reduce to a V/A credit.

        Its ``_ARTIST_PLUS_VA_RE`` arm ("X mixes various artists") and its final
        "real artist misclassified as a compilation" fall-through both return a
        real name. For those rows the V/A relaxation is False against every
        candidate, so without a guarded pass this lane would refuse even an exact
        artist+title match and lose the ``discogs_release_id`` it had on main.
        """
        pool = AsyncMock()
        pool.fetch.return_value = [{"id": 3, "title": "Aluminum Tunes", "artist_name": "Stereolab"}]
        match = await search_discogs_by_title(pool, "Aluminum Tunes", query_artist="Stereolab")
        assert match is not None
        assert match["release_id"] == 3
        assert match["axes"] == AXES_ARTIST_AND_TITLE

    @pytest.mark.asyncio
    async def test_rejects_a_named_artist_release(self):
        """Album 52656's shape on the Discogs lane."""
        pool = AsyncMock()
        pool.fetch.return_value = [
            {"id": 999, "title": "Married to the Mob", "artist_name": "Speaker Knockerz"}
        ]
        assert (
            await search_discogs_by_title(pool, "Married to the Mob", query_artist="Soundtrack")
            is None
        )

    @pytest.mark.asyncio
    async def test_picks_the_best_title_among_va_rows(self):
        pool = AsyncMock()
        pool.fetch.return_value = [
            {"id": 1, "title": "Aluminum Tune", "artist_name": "Various"},
            {"id": 2, "title": "Aluminum Tunes", "artist_name": "Various Artists"},
        ]
        match = await search_discogs_by_title(pool, "Aluminum Tunes", query_artist="Various")
        assert match is not None
        assert match["release_id"] == 2

    @pytest.mark.asyncio
    async def test_one_release_id_per_credit_still_resolves_deterministically(self):
        """``release JOIN release_artist`` yields one row per primary credit.

        So a multi-credit release appears several times under one ``r.id``, equal
        titles tie, and a tie-break keyed on the id alone would follow whatever
        order the unordered ``LIMIT 20`` happened to return — the LML#1097
        determinism this lane claims. The key carries the credit for that reason.
        """
        rows = [
            {"id": 5, "title": "Nuggets", "artist_name": "Various"},
            {"id": 5, "title": "Nuggets", "artist_name": "Various Artists"},
        ]
        pool = AsyncMock()
        pool.fetch.return_value = rows
        first = await search_discogs_by_title(pool, "Nuggets", query_artist="Various")
        pool.fetch.return_value = list(reversed(rows))
        second = await search_discogs_by_title(pool, "Nuggets", query_artist="Various")
        assert first is not None
        assert first == second

    @pytest.mark.asyncio
    async def test_no_rows_returns_none(self):
        pool = AsyncMock()
        pool.fetch.return_value = []
        assert await search_discogs_by_title(pool, "Aluminum Tunes", query_artist="Various") is None


class TestRunArtifactGuard:
    @pytest.mark.asyncio
    async def test_refuses_a_db_path_that_does_not_exist(self, tmp_path):
        """A typo'd ``--db-path`` must not be answered with a fresh empty schema.

        The drain opens the artifact through ``ResultsDB``, whose ``connect`` runs
        ``CREATE TABLE IF NOT EXISTS`` — so without this guard a mistyped path
        reports "Loaded 0 unmatched compilations" and looks like a finished run.
        """
        missing = tmp_path / "not-the-artifact.db"
        args = SimpleNamespace(
            db_path=str(missing), limit=1, dry_run=True, discogs_only=True, max_streaming=1
        )
        with pytest.raises(SystemExit, match="not-the-artifact.db"):
            await run(args)
        assert not missing.exists()


def _deezer_row(artist: str, title: str, album_id: int = 1) -> dict:
    return {
        "id": album_id,
        "title": title,
        "artist": {"name": artist},
        "link": f"https://www.deezer.com/album/{album_id}",
    }


def _lane(results: list[dict]) -> StreamingLane:
    async def search(artist: str, title: str) -> list[dict]:
        return results

    return StreamingLane(
        service="deezer",
        search=search,
        artist_fn=_DEEZER_ARTIST,
        title_fn=_DEEZER_TITLE,
        url_fn=_DEEZER_URL,
    )


@pytest_asyncio.fixture
async def db():
    """The artifact as the drain opens it: through its owner, album 52656 seeded."""
    results_db = ResultsDB(":memory:")
    await results_db.connect()
    assert results_db._db is not None
    await results_db._db.execute(
        """INSERT INTO albums (id, normalized_artist, normalized_title, display_artist,
               display_title, library_ids, formats, is_compilation, spotify_status,
               deezer_status)
           VALUES (52656, 'soundtracks m', 'married to the mob', 'Soundtracks - M',
               'Married to the Mob', '[60671]', '[]', 1, 'skipped', 'skipped')"""
    )
    await results_db._db.commit()
    yield results_db
    await results_db.close()


async def _album(db: ResultsDB) -> dict:
    assert db._db is not None
    cursor = await db._db.execute("SELECT * FROM albums WHERE id = 52656")
    row = await cursor.fetchone()
    assert row is not None
    return dict(row)


class TestResolveLane:
    """Phase 2: one streaming lane, end to end from response to written row."""

    @pytest.mark.asyncio
    async def test_the_52656_candidate_is_not_recorded_at_all(self, db):
        lane = _lane([_deezer_row("Speaker Knockerz", "Married to the Money")])
        decision = await resolve_lane(
            lane,
            db,
            album_id=52656,
            search_artist="Soundtrack",
            search_title="Married to the Mob",
            dry_run=False,
        )

        assert decision is None
        row = await _album(db)
        assert row["deezer_status"] == "skipped"
        assert row["deezer_url"] is None

    @pytest.mark.asyncio
    async def test_a_va_title_only_hit_is_recorded_with_provenance(self, db):
        lane = _lane([_deezer_row("Various Artists", "Nuggets")])
        decision = await resolve_lane(
            lane,
            db,
            album_id=52656,
            search_artist=None,
            search_title="Nuggets",
            dry_run=False,
        )

        assert decision is not None
        assert decision.axes == AXES_TITLE_ONLY
        row = await _album(db)
        assert row["deezer_status"] == STATUS_FOUND_TITLE_ONLY
        assert row["deezer_url"] == "https://www.deezer.com/album/1"
        assert row["deezer_matched_artist"] == "Various Artists"
        assert row["deezer_matched_title"] == "Nuggets"
        assert row["deezer_checked_at"]

    @pytest.mark.asyncio
    async def test_a_guarded_hit_is_recorded_as_found(self, db):
        lane = _lane([_deezer_row("Juana Molina", "DOGA")])
        decision = await resolve_lane(
            lane,
            db,
            album_id=52656,
            search_artist="Juana Molina",
            search_title="DOGA",
            dry_run=False,
        )

        assert decision is not None
        assert decision.axes == AXES_ARTIST_AND_TITLE
        row = await _album(db)
        assert row["deezer_status"] == STATUS_FOUND
        assert row["deezer_matched_artist"] == "Juana Molina"
        assert row["deezer_matched_title"] == "DOGA"

    @pytest.mark.asyncio
    async def test_a_refused_demotion_is_not_reported_as_a_hit(self, db):
        """Phase 2 selects on ``spotify_status``, so the Deezer lane sees found rows.

        A title-only decision must not overwrite one, and the drain must not count
        or log a write that never landed — the hit-rate line is the only thing
        telling an operator what the run did.
        """
        assert db._db is not None
        await db._db.execute(
            """UPDATE albums SET deezer_status = 'found',
               deezer_url = 'https://www.deezer.com/album/right' WHERE id = 52656"""
        )
        await db._db.commit()

        lane = _lane([_deezer_row("Various Artists", "Nuggets")])
        decision = await resolve_lane(
            lane,
            db,
            album_id=52656,
            search_artist=None,
            search_title="Nuggets",
            dry_run=False,
        )

        assert decision is None
        row = await _album(db)
        assert row["deezer_status"] == STATUS_FOUND
        assert row["deezer_url"] == "https://www.deezer.com/album/right"

    @pytest.mark.asyncio
    async def test_dry_run_writes_nothing(self, db):
        lane = _lane([_deezer_row("Various Artists", "Nuggets")])
        decision = await resolve_lane(
            lane,
            db,
            album_id=52656,
            search_artist=None,
            search_title="Nuggets",
            dry_run=True,
        )

        assert decision is not None
        row = await _album(db)
        assert row["deezer_status"] == "skipped"
        assert row["deezer_url"] is None
