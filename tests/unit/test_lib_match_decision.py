"""Tests for the shared drain match decision + provenance write (LML#1353).

The production defect these pin: ``scripts/search_unmatched_compilations.py``
accepted a candidate the guarded 80/80 matcher had *rejected* on a title-only
score, stored that title score in the ``confidence`` column, and wrote neither
``matched_artist`` nor ``matched_title`` — so album 52656 ("Married to the Mob",
a 1988 soundtrack) came to carry Speaker Knockerz's "Married to the Money" at
"89.47% confidence" with empty provenance.
"""

from __future__ import annotations

import aiosqlite
import pytest
import pytest_asyncio

from clients.streaming.matching import SCORE_MATCH_ACCEPTANCE_FLOOR
from scripts._lib.match_decision import (
    AXES_ARTIST_AND_TITLE,
    AXES_TITLE_ONLY,
    STATUS_FOUND,
    STATUS_FOUND_TITLE_ONLY,
    ServiceMatch,
    best_title_only_candidate,
    decide_service_match,
    update_service_match,
)
from scripts.streaming_availability.results_db import _SCHEMA

# Spotify-shaped rows, as ``search_unmatched_compilations`` extracts them.
_SPOTIFY_ARTIST = lambda r: r.get("artists", [{}])[0].get("name", "")  # noqa: E731
_SPOTIFY_TITLE = lambda r: r.get("name", "")  # noqa: E731
_SPOTIFY_URL = lambda r: r.get("external_urls", {}).get("spotify", "")  # noqa: E731
_SPOTIFY_ID = lambda r: r.get("id", "")  # noqa: E731


def _spotify_row(artist: str, title: str, album_id: str = "abc123") -> dict:
    return {
        "id": album_id,
        "name": title,
        "artists": [{"name": artist}],
        "external_urls": {"spotify": f"https://open.spotify.com/album/{album_id}"},
    }


def _spotify_kwargs() -> dict:
    return {
        "artist_fn": _SPOTIFY_ARTIST,
        "title_fn": _SPOTIFY_TITLE,
        "url_fn": _SPOTIFY_URL,
        "id_fn": _SPOTIFY_ID,
    }


class TestBestTitleOnlyCandidate:
    """The relaxation is scoped to the case where the artist axis says nothing."""

    def test_accepts_when_both_sides_are_va_credits(self):
        """A V/A shelf credit against a V/A-credited release: LML#1147's case."""
        rows = [_spotify_row("Various Artists", "Nuggets: Original Artyfacts")]
        winner = best_title_only_candidate(
            rows,
            query_artist="Various",
            query_title="Nuggets: Original Artyfacts",
            artist_fn=_SPOTIFY_ARTIST,
            title_fn=_SPOTIFY_TITLE,
            key_fn=_SPOTIFY_URL,
        )
        assert winner is not None
        assert winner[0] is rows[0]
        assert winner[1] == pytest.approx(100.0)

    def test_rejects_a_named_artist_candidate(self):
        """Album 52656 verbatim: the artist axis is informative and says NO."""
        rows = [_spotify_row("Speaker Knockerz", "Married to the Money")]
        assert (
            best_title_only_candidate(
                rows,
                query_artist="Soundtrack",
                query_title="Married to the Mob",
                artist_fn=_SPOTIFY_ARTIST,
                title_fn=_SPOTIFY_TITLE,
                key_fn=_SPOTIFY_URL,
            )
            is None
        )

    def test_picks_best_title_not_first_in_service_order(self):
        """Spotify orders by its own relevance, which is not the title-score order."""
        rows = [
            _spotify_row("Various Artists", "Aluminum Tune", "first"),
            _spotify_row("Various", "Aluminum Tunes", "second"),
        ]
        winner = best_title_only_candidate(
            rows,
            query_artist="Various Artists - Rock",
            query_title="Aluminum Tunes",
            artist_fn=_SPOTIFY_ARTIST,
            title_fn=_SPOTIFY_TITLE,
            key_fn=_SPOTIFY_URL,
        )
        assert winner is not None
        assert winner[0]["id"] == "second"

    def test_ties_resolve_by_ascending_key(self):
        """Deterministic across repeated identical queries (LML#1097's rule)."""
        rows = [
            _spotify_row("Various Artists", "DOGA", "zzz"),
            _spotify_row("Various Artists", "DOGA", "aaa"),
        ]
        winner = best_title_only_candidate(
            rows,
            query_artist="Various",
            query_title="DOGA",
            artist_fn=_SPOTIFY_ARTIST,
            title_fn=_SPOTIFY_TITLE,
            key_fn=_SPOTIFY_URL,
        )
        assert winner is not None
        assert winner[0]["id"] == "aaa"

    def test_rejects_below_the_floor(self):
        rows = [_spotify_row("Various Artists", "Completely Different Record")]
        assert (
            best_title_only_candidate(
                rows,
                query_artist="Various",
                query_title="Nuggets",
                artist_fn=_SPOTIFY_ARTIST,
                title_fn=_SPOTIFY_TITLE,
                key_fn=_SPOTIFY_URL,
            )
            is None
        )

    def test_honors_a_lane_specific_floor(self):
        """The Discogs cache lane keeps its historical 70 title floor."""
        rows = [{"id": 7, "title": "Songs of the Humpback", "artist_name": "Various"}]
        kwargs = {
            "query_artist": "Various",
            "query_title": "Songs of the Humpback Whale",
            "artist_fn": lambda r: r["artist_name"],
            "title_fn": lambda r: r["title"],
            "key_fn": lambda r: str(r["id"]),
        }
        assert best_title_only_candidate(rows, floor=95.0, **kwargs) is None
        assert best_title_only_candidate(rows, floor=70.0, **kwargs) is not None

    def test_skips_a_malformed_row_without_losing_the_response(self):
        """Mirrors find_best_match's LML#640 guard: one sparse row is not fatal."""
        rows = [{"name": "Aluminum Tunes"}, _spotify_row("Various Artists", "Aluminum Tunes")]
        winner = best_title_only_candidate(
            rows,
            query_artist="Various",
            query_title="Aluminum Tunes",
            artist_fn=_SPOTIFY_ARTIST,
            title_fn=_SPOTIFY_TITLE,
            key_fn=_SPOTIFY_URL,
        )
        assert winner is not None
        assert winner[0]["id"] == "abc123"

    def test_empty_results(self):
        assert (
            best_title_only_candidate(
                [],
                query_artist="Various",
                query_title="Nuggets",
                artist_fn=_SPOTIFY_ARTIST,
                title_fn=_SPOTIFY_TITLE,
                key_fn=_SPOTIFY_URL,
            )
            is None
        )


class TestDecideServiceMatch:
    def test_guarded_match_reports_both_axes(self):
        rows = [_spotify_row("Jessica Pratt", "On Your Own Love Again")]
        decision = decide_service_match(
            rows,
            query_artist="Jessica Pratt",
            query_title="On Your Own Love Again",
            **_spotify_kwargs(),
        )
        assert decision is not None
        assert decision.axes == AXES_ARTIST_AND_TITLE
        assert decision.status == STATUS_FOUND
        assert decision.confidence == pytest.approx(100.0)
        assert decision.matched_artist == "Jessica Pratt"
        assert decision.matched_title == "On Your Own Love Again"
        assert decision.service_item_id == "abc123"

    def test_title_only_match_is_never_status_found(self):
        rows = [_spotify_row("Various Artists", "Nuggets")]
        decision = decide_service_match(
            rows,
            query_artist="Various",
            query_title="Nuggets",
            **_spotify_kwargs(),
        )
        assert decision is not None
        assert decision.axes == AXES_TITLE_ONLY
        assert decision.status == STATUS_FOUND_TITLE_ONLY
        assert decision.matched_artist == "Various Artists"
        assert decision.matched_title == "Nuggets"

    def test_rejected_candidate_yields_no_decision(self):
        """The 52656 shape end to end: the drain records nothing at all."""
        rows = [_spotify_row("Speaker Knockerz", "Married to the Money")]
        assert (
            decide_service_match(
                rows,
                query_artist="Soundtrack",
                query_title="Married to the Mob",
                **_spotify_kwargs(),
            )
            is None
        )

    def test_confidence_is_the_score_of_the_axes_it_names(self):
        """One meaning per row: the axes field says what confidence measured."""
        rows = [_spotify_row("Various Artists", "Aluminum Tunez")]
        decision = decide_service_match(
            rows,
            query_artist="Various",
            query_title="Aluminum Tunes",
            **_spotify_kwargs(),
        )
        assert decision is not None
        assert decision.axes == AXES_TITLE_ONLY
        assert SCORE_MATCH_ACCEPTANCE_FLOOR <= decision.confidence < 100.0

    def test_no_results(self):
        assert (
            decide_service_match(
                [],
                query_artist="Various",
                query_title="Nuggets",
                **_spotify_kwargs(),
            )
            is None
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


class TestUpdateServiceMatch:
    """The write is the acceptance criterion: provenance lands with the URL."""

    @pytest.mark.asyncio
    async def test_title_only_write_carries_full_provenance(self, db):
        decision = ServiceMatch(
            url="https://open.spotify.com/album/nuggets",
            confidence=92.0,
            matched_artist="Various Artists",
            matched_title="Nuggets",
            axes=AXES_TITLE_ONLY,
            service_item_id="nuggets",
        )
        await update_service_match(db, album_id=52656, service="spotify", match=decision)
        await db.commit()

        row = await _album(db)
        assert row["spotify_status"] == STATUS_FOUND_TITLE_ONLY
        assert row["spotify_url"] == "https://open.spotify.com/album/nuggets"
        assert row["spotify_id"] == "nuggets"
        assert row["spotify_confidence"] == pytest.approx(92.0)
        assert row["spotify_matched_artist"] == "Various Artists"
        assert row["spotify_matched_title"] == "Nuggets"
        assert row["spotify_checked_at"]

    @pytest.mark.asyncio
    async def test_a_title_only_row_is_never_found_with_empty_provenance(self, db):
        """The exact defect: status='found' plus NULL matched_artist/matched_title."""
        decision = ServiceMatch(
            url="https://open.spotify.com/album/nuggets",
            confidence=92.0,
            matched_artist="Various Artists",
            matched_title="Nuggets",
            axes=AXES_TITLE_ONLY,
        )
        await update_service_match(db, album_id=52656, service="spotify", match=decision)
        await db.commit()

        row = await _album(db)
        assert not (
            row["spotify_status"] == STATUS_FOUND
            and not (row["spotify_matched_artist"] and row["spotify_matched_title"])
        )

    @pytest.mark.asyncio
    async def test_guarded_write_is_status_found(self, db):
        decision = ServiceMatch(
            url="https://open.spotify.com/album/doga",
            confidence=100.0,
            matched_artist="Juana Molina",
            matched_title="DOGA",
            axes=AXES_ARTIST_AND_TITLE,
        )
        await update_service_match(db, album_id=52656, service="spotify", match=decision)
        await db.commit()

        row = await _album(db)
        assert row["spotify_status"] == STATUS_FOUND
        assert row["spotify_matched_artist"] == "Juana Molina"

    @pytest.mark.asyncio
    async def test_deezer_lane_also_records_provenance(self, db):
        decision = ServiceMatch(
            url="https://www.deezer.com/album/1",
            confidence=88.0,
            matched_artist="Various Artists",
            matched_title="Nuggets",
            axes=AXES_TITLE_ONLY,
        )
        await update_service_match(db, album_id=52656, service="deezer", match=decision)
        await db.commit()

        row = await _album(db)
        assert row["deezer_status"] == STATUS_FOUND_TITLE_ONLY
        assert row["deezer_url"] == "https://www.deezer.com/album/1"
        assert row["deezer_confidence"] == pytest.approx(88.0)
        assert row["deezer_matched_artist"] == "Various Artists"
        assert row["deezer_matched_title"] == "Nuggets"
        assert row["deezer_checked_at"]

    @pytest.mark.asyncio
    async def test_title_only_never_demotes_an_existing_found_row(self, db):
        """A guarded match outranks a one-axis one, and PG raises on the demotion."""
        await db.execute(
            """UPDATE albums SET spotify_status = 'found',
               spotify_url = 'https://open.spotify.com/album/right',
               spotify_matched_artist = 'Various Artists',
               spotify_matched_title = 'Married to the Mob' WHERE id = 52656"""
        )
        decision = ServiceMatch(
            url="https://open.spotify.com/album/wrong",
            confidence=89.47,
            matched_artist="Various",
            matched_title="Married to the Mob (Soundtrack)",
            axes=AXES_TITLE_ONLY,
        )
        await update_service_match(db, album_id=52656, service="spotify", match=decision)
        await db.commit()

        row = await _album(db)
        assert row["spotify_status"] == STATUS_FOUND
        assert row["spotify_url"] == "https://open.spotify.com/album/right"

    @pytest.mark.asyncio
    async def test_unknown_service_is_refused(self, db):
        decision = ServiceMatch(
            url="https://music.apple.com/album/1",
            confidence=88.0,
            matched_artist="Various Artists",
            matched_title="Nuggets",
            axes=AXES_TITLE_ONLY,
        )
        with pytest.raises(ValueError, match="apple"):
            await update_service_match(db, album_id=52656, service="apple", match=decision)
