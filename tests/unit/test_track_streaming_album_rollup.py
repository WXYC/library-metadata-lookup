"""The album rollup must not launder a one-axis album match into a guarded one (LML#1353).

``phase_album_rollup`` sets ``spotify_status = 'found'`` for every album whose
track results fully resolve, and touches neither the URL nor the confidence nor
the provenance columns. That was correct while ``found`` was the only collected
status. It is not correct now: the compilation drain writes
``found_title_only`` for a V/A acceptance judged on the title axis alone, and
this rollup runs over exactly that population — its compilation extraction
selects ``is_compilation = 1``, and its own ``SELECT DISTINCT album_id FROM
track_results`` then picks those rows up. Promoting one leaves a
``spotify_confidence`` of 89.47 and a title-only ``matched_title`` sitting under
a status that claims both axes were compared, which is the "confidence without
axes" row LML#1353 exists to remove.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from scripts._lib.match_decision import STATUS_FOUND, STATUS_FOUND_TITLE_ONLY
from scripts.streaming_availability.results_db import ResultsDB
from scripts.track_streaming.__main__ import phase_album_rollup

_ALBUM_ID = 52656


@pytest_asyncio.fixture
async def db():
    """Album 52656 with one fully-resolved track, ready to roll up."""
    results_db = ResultsDB(":memory:")
    await results_db.connect()
    assert results_db._db is not None
    await results_db._db.execute(
        """INSERT INTO albums (id, normalized_artist, normalized_title, display_artist,
               display_title, library_ids, formats, is_compilation, spotify_status)
           VALUES (?, 'soundtracks m', 'married to the mob', 'Soundtracks - M',
               'Married to the Mob', '[60671]', '[]', 1, 'skipped')""",
        (_ALBUM_ID,),
    )
    await results_db._db.execute(
        """INSERT INTO track_results (album_id, artist, title, source, source_type,
               resolution_status, spotify_url)
           VALUES (?, 'Chris Isaak', 'Blue Spanish Sky', 'discogs', 'compilation',
               'api_match', 'https://open.spotify.com/track/x')""",
        (_ALBUM_ID,),
    )
    await results_db._db.commit()
    yield results_db
    await results_db.close()


async def _album(db: ResultsDB) -> dict:
    assert db._db is not None
    cursor = await db._db.execute("SELECT * FROM albums WHERE id = ?", (_ALBUM_ID,))
    row = await cursor.fetchone()
    assert row is not None
    return dict(row)


async def _set_spotify(db: ResultsDB, status: str) -> None:
    assert db._db is not None
    await db._db.execute(
        """UPDATE albums SET spotify_status = ?,
           spotify_url = 'https://open.spotify.com/album/title-only',
           spotify_confidence = 89.47,
           spotify_matched_artist = 'Various',
           spotify_matched_title = 'Married to the Mob (Soundtrack)' WHERE id = ?""",
        (status, _ALBUM_ID),
    )
    await db._db.commit()


class TestPhaseAlbumRollup:
    @pytest.mark.asyncio
    async def test_does_not_promote_a_title_only_row_to_found(self, db):
        """The one thing the rollup must not do: relabel a one-axis match.

        Its confidence and provenance survive the promotion untouched, so a
        promoted row asserts a two-axis match over a title-only score — and is
        then indistinguishable from a guarded one.
        """
        await _set_spotify(db, STATUS_FOUND_TITLE_ONLY)

        on_streaming, _ = await phase_album_rollup(db)

        row = await _album(db)
        assert row["spotify_status"] == STATUS_FOUND_TITLE_ONLY
        assert row["spotify_confidence"] == pytest.approx(89.47)
        assert on_streaming == 1

    @pytest.mark.asyncio
    async def test_still_promotes_an_unresolved_row(self, db):
        """The rollup's actual job is untouched: a skipped album still rolls up."""
        on_streaming, off_streaming = await phase_album_rollup(db)

        row = await _album(db)
        assert row["spotify_status"] == STATUS_FOUND
        assert (on_streaming, off_streaming) == (1, 0)

    @pytest.mark.asyncio
    async def test_leaves_an_existing_guarded_row_alone(self, db):
        """Idempotence, as before: a row already at ``found`` is not rewritten."""
        await _set_spotify(db, STATUS_FOUND)

        await phase_album_rollup(db)

        row = await _album(db)
        assert row["spotify_status"] == STATUS_FOUND
        assert row["spotify_url"] == "https://open.spotify.com/album/title-only"

    @pytest.mark.asyncio
    async def test_an_off_streaming_album_does_not_demote_a_title_only_row(self, db):
        """The miss branch only ever touched ``skipped``; it must stay that way.

        A collected title-only URL is not discarded because this album's tracks
        failed to resolve, and the PG mirror raises on that demotion anyway.
        """
        assert db._db is not None
        await db._db.execute(
            "UPDATE track_results SET resolution_status = 'not_found', spotify_url = NULL"
        )
        await _set_spotify(db, STATUS_FOUND_TITLE_ONLY)

        _, off_streaming = await phase_album_rollup(db)

        row = await _album(db)
        assert row["spotify_status"] == STATUS_FOUND_TITLE_ONLY
        assert off_streaming == 1
