"""Which albums reach the local streaming index (LML#1353).

``phase_build_index`` selects the albums whose Discogs tracklists seed the
in-memory index that track resolution matches against. It asked for
``{service}_status = 'found'``, which was every collected answer until the
compilation drain started writing ``found_title_only``.

That makes it a **coverage regression against main**, not a gap: before this
change those rows reached the index as ``'found'`` — via the very laundering
``phase_album_rollup`` used to perform and no longer does — so afterwards they sit
permanently outside track resolution. The population is V/A compilations, which is
precisely what compilation track resolution exists to serve (LML#1020).
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from scripts._lib.match_decision import STATUS_FOUND, STATUS_FOUND_TITLE_ONLY
from scripts.streaming_availability.results_db import ResultsDB
from scripts.track_streaming.__main__ import phase_build_index

_ALBUM_ID = 52656
_RELEASE_ID = 777


class _FakeDiscogsPool:
    """Records the release-id batches the index build asks for."""

    def __init__(self) -> None:
        self.batches: list[list[int]] = []

    async def fetch(self, _query: str, batch: list[int]) -> list[dict]:
        self.batches.append(list(batch))
        return []

    async def close(self) -> None:
        return None


@pytest_asyncio.fixture
async def db():
    results_db = ResultsDB(":memory:")
    await results_db.connect()
    assert results_db._db is not None
    await results_db._db.execute(
        """INSERT INTO albums (id, normalized_artist, normalized_title, display_artist,
               display_title, library_ids, formats, is_compilation, discogs_release_id,
               spotify_status, deezer_status)
           VALUES (?, 'various artists blues', 'nuggets', 'Various Artists - Blues',
               'Nuggets', '[60671]', '[]', 1, ?, 'skipped', 'pending')""",
        (_ALBUM_ID, _RELEASE_ID),
    )
    await results_db._db.commit()
    yield results_db
    await results_db.close()


@pytest_asyncio.fixture
def pool(monkeypatch):
    fake = _FakeDiscogsPool()

    async def _create_pool(*_args, **_kwargs):
        return fake

    monkeypatch.setenv("DATABASE_URL_DISCOGS", "postgresql://unused/for-this-test")
    monkeypatch.setattr("asyncpg.create_pool", _create_pool)
    return fake


async def _set_status(db: ResultsDB, service: str, status: str) -> None:
    assert db._db is not None
    await db._db.execute(
        f"UPDATE albums SET {service}_status = ? WHERE id = ?", (status, _ALBUM_ID)
    )
    await db._db.commit()


class TestPhaseBuildIndexSelection:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("service", ["spotify", "deezer", "apple"])
    async def test_a_title_only_album_is_indexed(self, db, pool, service):
        """A one-axis album match is still a collected answer, so its tracks resolve.

        The index's question is "do we have a streaming answer for this album" —
        not "how good is that answer's label".
        """
        await _set_status(db, service, STATUS_FOUND_TITLE_ONLY)

        await phase_build_index(db)

        assert pool.batches == [[_RELEASE_ID]]

    @pytest.mark.asyncio
    async def test_a_found_album_is_still_indexed(self, db, pool):
        """The behaviour that must not move."""
        await _set_status(db, "spotify", STATUS_FOUND)

        await phase_build_index(db)

        assert pool.batches == [[_RELEASE_ID]]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", ["pending", "not_found", "skipped", "error"])
    async def test_an_album_with_no_answer_is_not_indexed(self, db, pool, status):
        """The predicate must not have widened into "index everything"."""
        await _set_status(db, "spotify", status)

        await phase_build_index(db)

        assert pool.batches == []
