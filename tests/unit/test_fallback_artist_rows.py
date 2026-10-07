"""The artist-only and artist+song fallbacks against a real SQLite catalog (LML#1445).

Both fallbacks read the artist's own rows ahead of the 50-row search window,
through ``lookup/artist_rows.py``. Real ``library_fts`` index
(``tests/factories.py::make_library_catalog``): the defect lived in the gap
between the rows the window returns and the artist's own.
"""

import pytest

from lookup.strategies.artist_plus_album import runs_album_resolution, search_library_with_fallback
from services.parser import MessageType, ParsedRequest
from tests.factories import crowd_rows, make_library_catalog


def _parsed(artist, album=None, song=None):
    return ParsedRequest(
        artist=artist,
        album=album,
        song=song,
        raw_message=f"{artist} - {song or album}",
        is_request=True,
        message_type=MessageType.REQUEST,
    )


async def _fallback(tmp_path, rows, artist, album=None, song=None, albums=()):
    db = await make_library_catalog(tmp_path, rows)
    try:
        results, fallback = await search_library_with_fallback(
            db, _parsed(artist, album, song), list(albums)
        )
    finally:
        await db.close()
    return [(r.artist, r.title) for r in results], fallback


class TestArtistOnlyFallback:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("artist", ["Agnes", "Heart", "Love", "Spirit"])
    async def test_own_row_leads_a_crowded_window(self, tmp_path, artist):
        rows = [*crowd_rows(artist), (artist, "Second Album")]

        got, fallback = await _fallback(tmp_path, rows, artist, song="Unfindable Song")

        assert fallback is True
        assert got[0] == (artist, "Second Album")

    @pytest.mark.asyncio
    async def test_credited_rows_follow_own_rows_and_prefix_only_rows_drop(self, tmp_path):
        rows = [
            ("Afel Bocoum Band", "Other"),
            ("Various Artists", "Mali Music", "Afel Bocoum"),
            ("Afel Bocoum", "Alkibar"),
            ("Afel Bocoum", "Niger"),
        ]

        got, _ = await _fallback(tmp_path, rows, "Afel Bocoum")

        assert got == [
            ("Afel Bocoum", "Alkibar"),
            ("Afel Bocoum", "Niger"),
            ("Various Artists", "Mali Music"),
        ]

    @pytest.mark.asyncio
    async def test_artist_without_a_shelf_reaches_a_prefix_matched_artist(self, tmp_path):
        rows = [("Sun Ra Arkestra", "Lanquidity")]

        assert await _fallback(tmp_path, rows, "Sun Ra") == (
            [("Sun Ra Arkestra", "Lanquidity")],
            True,
        )

    @pytest.mark.asyncio
    async def test_typed_album_still_filters_own_rows(self, tmp_path):
        rows = [("Can", "Tago Mago"), ("Can", "Ege Bamyasi")]

        got, _ = await _fallback(tmp_path, rows, "Can", album="Tago Mago")

        assert got == [("Can", "Tago Mago")]

    @pytest.mark.asyncio
    async def test_various_artists_shelves_are_not_listed_whole(self, tmp_path):
        rows = [("Various Artists", "One"), ("Various Artists - Rock - H", "Two")]

        got, _ = await _fallback(tmp_path, rows, "Various Artists")

        assert got == [("Various Artists", "One")]


class TestArtistSongFallback:
    @pytest.mark.asyncio
    async def test_song_titled_after_a_record_leads_with_the_artists_row(self, tmp_path):
        rows = [
            ("Canibus", "Tago Mago Remixed"),
            *crowd_rows("Can"),
            ("Can", "Ege Bamyasi"),
            ("Can", "Tago Mago"),
        ]

        got, fallback = await _fallback(tmp_path, rows, "Can", song="Tago Mago")

        assert fallback is True
        assert got[0] == ("Can", "Tago Mago")
        assert ("Canibus", "Tago Mago Remixed") not in got

    @pytest.mark.asyncio
    async def test_song_in_title_ranks_first_within_own_rows(self, tmp_path):
        rows = [("Can", "Tago Mago Live"), ("Can", "Tago Mago Sessions Tago Mago")]

        got, _ = await _fallback(tmp_path, rows, "Can", song="Sessions Tago Mago")

        assert got[0] == ("Can", "Tago Mago Sessions Tago Mago")


class TestPlaceholderGuardAgreement:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("artist", ["Heart", "Spirit"])
    async def test_guard_and_artist_only_fallback_both_see_the_literal_s_t_row(
        self, tmp_path, artist
    ):
        """Decision 4: a crowded artist with a literal "S/T" row. The guard skips
        step 2 because the row exists, and the fallback lists that same row."""
        rows = [*crowd_rows(artist), (artist, "S/T"), (artist, "Second Album")]
        db = await make_library_catalog(tmp_path, rows)
        try:
            guard_runs = await runs_album_resolution(_parsed(artist, album="S/T"), db)
            got, _ = await search_library_with_fallback(db, _parsed(artist), [])
        finally:
            await db.close()

        assert guard_runs is False
        assert (artist, "S/T") in [(r.artist, r.title) for r in got]
