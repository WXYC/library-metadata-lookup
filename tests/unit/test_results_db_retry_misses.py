"""Unit tests for ``ResultsDB.reset_misses_to_pending`` (``--retry-misses``).

The reset behind ``scripts/streaming_availability --retry-misses`` decides
which rows a re-pass will re-query. It is the one write in the pipeline whose
job is to *discard* a stored answer, so its ``WHERE`` clause is the guard
standing between a cheap retry and a re-query of the whole catalog against a
rate-limited API. These pin which rows it touches, in both directions.

``found`` rows are never reset. That is not a tuning choice -- re-collecting a
Deezer or Spotify match costs a rate-limited round trip, so a reset that
reached ``found`` would destroy expensive data to no purpose.
"""

import pytest
import pytest_asyncio

from scripts.streaming_availability.results_db import ResultsDB


@pytest_asyncio.fixture
async def db():
    results_db = ResultsDB(":memory:")
    await results_db.connect()
    yield results_db
    await results_db.close()


async def _seed(db: ResultsDB, rows: list[tuple[str, str, str]]) -> None:
    """Insert ``(name, deezer_status, spotify_status)`` rows directly."""
    assert db._db is not None
    for name, dz, sp in rows:
        await db._db.execute(
            """INSERT INTO albums
               (normalized_artist, normalized_title, display_artist, display_title,
                library_ids, formats, deezer_status, spotify_status,
                deezer_url, spotify_url, deezer_checked_at, spotify_checked_at)
               VALUES (?, ?, ?, ?, '[1]', '[\"cd\"]', ?, ?,
                       'http://d/x', 'http://s/x', '2026-04-01', '2026-04-01')""",
            (name, name, name, name, dz, sp),
        )
    await db._db.commit()


async def _statuses(db: ResultsDB) -> dict[str, tuple[str, str]]:
    assert db._db is not None
    cur = await db._db.execute(
        "SELECT normalized_artist, deezer_status, spotify_status FROM albums"
    )
    return {r[0]: (r[1], r[2]) for r in await cur.fetchall()}


@pytest.mark.asyncio
class TestResetMissesToPending:
    async def test_found_rows_are_never_reset(self, db):
        """The invariant the whole guard exists to protect.

        A ``found`` row holds a match that cost a rate-limited API call to
        obtain. ``--retry-misses`` must never spend that again.
        """
        await _seed(db, [("keeper", "found", "found")])

        await db.reset_misses_to_pending()

        assert (await _statuses(db))["keeper"] == ("found", "found")

    async def test_auto_skipped_spotify_rows_are_reset(self, db):
        """Deezer missed, so Spotify was never asked. Both go back to pending."""
        await _seed(db, [("skipped", "not_found", "not_found")])

        dz_reset, sp_reset = await db.reset_misses_to_pending()

        assert (await _statuses(db))["skipped"] == ("pending", "pending")
        assert (dz_reset, sp_reset) == (1, 1)

    async def test_reset_clears_the_stale_answer_columns(self, db):
        """Status alone is not the whole row: a reset that left the previous
        URL and timestamp behind would leave a 'pending' row carrying a
        stale answer, which reads as a fresh result to every later query."""
        await _seed(db, [("stale", "not_found", "not_found")])

        await db.reset_misses_to_pending()

        assert db._db is not None
        cur = await db._db.execute(
            """SELECT deezer_url, spotify_url, deezer_checked_at, spotify_checked_at
               FROM albums WHERE normalized_artist = 'stale'"""
        )
        assert list(await cur.fetchone()) == [None, None, None, None]
