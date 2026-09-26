"""Tests for the unmatched-compilation drain's three acceptance sites (LML#1353).

Album 52656 is the production artifact row this suite is built around:
``display_artist`` "Soundtracks - M", ``display_title`` "Married to the Mob"
(a 1988 soundtrack). Every lane must refuse Speaker Knockerz's "Married to the
Money" — whose title scores 89.47 — because the candidate is credited to a named
artist, so the artist axis is informative and scored 30.77.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import aiosqlite
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
    search_discogs_by_title,
)
from scripts.streaming_availability.results_db import _SCHEMA


class TestSearchDiscogsByTitle:
    """Phase 1: the Discogs-cache title search (the third copy of the shape)."""

    @pytest.mark.asyncio
    async def test_accepts_a_va_credited_release(self):
        pool = AsyncMock()
        pool.fetch.return_value = [
            {"id": 12345, "title": "Nuggets: Original Artyfacts", "artist_name": "Various"}
        ]
        match = await search_discogs_by_title(pool, "Nuggets: Original Artyfacts")
        assert match is not None
        assert match["release_id"] == 12345
        assert match["axes"] == AXES_TITLE_ONLY

    @pytest.mark.asyncio
    async def test_rejects_a_named_artist_release(self):
        """Album 52656's shape on the Discogs lane."""
        pool = AsyncMock()
        pool.fetch.return_value = [
            {"id": 999, "title": "Married to the Mob", "artist_name": "Speaker Knockerz"}
        ]
        assert await search_discogs_by_title(pool, "Married to the Mob") is None

    @pytest.mark.asyncio
    async def test_picks_the_best_title_among_va_rows(self):
        pool = AsyncMock()
        pool.fetch.return_value = [
            {"id": 1, "title": "Aluminum Tune", "artist_name": "Various"},
            {"id": 2, "title": "Aluminum Tunes", "artist_name": "Various Artists"},
        ]
        match = await search_discogs_by_title(pool, "Aluminum Tunes")
        assert match is not None
        assert match["release_id"] == 2

    @pytest.mark.asyncio
    async def test_no_rows_returns_none(self):
        pool = AsyncMock()
        pool.fetch.return_value = []
        assert await search_discogs_by_title(pool, "Aluminum Tunes") is None


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
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.executescript(_SCHEMA)
    await conn.execute(
        """INSERT INTO albums (id, normalized_artist, normalized_title, display_artist,
               display_title, library_ids, formats, is_compilation, spotify_status,
               deezer_status)
           VALUES (52656, 'soundtracks m', 'married to the mob', 'Soundtracks - M',
               'Married to the Mob', '[60671]', '[]', 1, 'skipped', 'skipped')"""
    )
    await conn.commit()
    yield conn
    await conn.close()


async def _album(db: aiosqlite.Connection) -> dict:
    cursor = await db.execute("SELECT * FROM albums WHERE id = 52656")
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
        await db.commit()

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
        await db.commit()

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
        await db.commit()

        assert decision is not None
        assert decision.axes == AXES_ARTIST_AND_TITLE
        row = await _album(db)
        assert row["deezer_status"] == STATUS_FOUND
        assert row["deezer_matched_artist"] == "Juana Molina"
        assert row["deezer_matched_title"] == "DOGA"

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
        await db.commit()

        assert decision is not None
        row = await _album(db)
        assert row["deezer_status"] == "skipped"
        assert row["deezer_url"] is None
