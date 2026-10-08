"""The artist-only and artist+song fallbacks against a real SQLite catalog (LML#1445).

Both fallbacks read the artist's own rows ahead of the 50-row search window,
through ``lookup/artist_rows.py``. Real ``library_fts`` index
(``tests/factories.py::make_library_catalog``): the defect lived in the gap
between the rows the window returns and the artist's own.
"""

import pytest

from lookup.alternate_credit_hint import alternate_credit_hint
from lookup.matching import _FETCH_LIMIT
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


UNRESOLVABLE = "Qzxv Wjkpl"
"""A song no row's words match: the artist+song read finds no own row."""
MALI_MUSIC_CREDIT = "Afel Bocoum, Damon Albarn, Toumani Diabate and friends"


class TestUnresolvableSongReadsTheShelf:
    """A shelved artist whose own rows miss the song's words: of the artist+song
    window's LIKE/fuzzy rows only its own answer, followed by the artist-only
    read's credited rows, or the artist-only rows when there are none (LML#1425:
    Afel Bocoum, and Junior Varsity -> Junior Varsity KM)."""

    @pytest.mark.asyncio
    async def test_afel_bocoum_lists_his_rows_then_the_tagged_credit(self, tmp_path):
        rows = [
            ("Afel Bocoum Band", "Other"),
            ("Damon Albarn", "Mali Music", MALI_MUSIC_CREDIT),
            ("Afel Bocoum", "Alkibar"),
            ("Afel Bocoum", "Niger"),
        ]
        db = await make_library_catalog(tmp_path, rows)
        try:
            results, fallback = await search_library_with_fallback(
                db, _parsed("Afel Bocoum", song=UNRESOLVABLE), []
            )
        finally:
            await db.close()

        got = [(r.artist, r.title) for r in results]
        assert fallback is True
        assert sorted(got[:2]) == [("Afel Bocoum", "Alkibar"), ("Afel Bocoum", "Niger")]
        assert got[2:] == [("Damon Albarn", "Mali Music")]
        hints = [alternate_credit_hint(r, "Afel Bocoum") for r in results]
        assert hints[:2] == [None, None]
        assert hints[2] is not None and hints[2][0].matched_variant == MALI_MUSIC_CREDIT

    @pytest.mark.asyncio
    async def test_own_row_leads_another_artists_prefix_row(self, tmp_path):
        rows = [("Junior Varsity KM", "You're Fabulous"), ("Junior Varsity", "Pep Rally Rock!")]

        got = await _fallback(tmp_path, rows, "Junior Varsity", song=UNRESOLVABLE)

        assert got == ([("Junior Varsity", "Pep Rally Rock!")], True)

    @pytest.mark.asyncio
    async def test_a_truncated_song_still_leads_with_the_row_it_names(self, tmp_path):
        """The window's LIKE fallback reaches "Emperor Tomato Ketchup" from a song
        cut short. That own row answers alone: padding it with the shelf would cost
        artwork lookups and buy nothing."""
        rows = [("Stereolab", "Peng!"), ("Stereolab", "Emperor Tomato Ketchup")]

        got = await _fallback(tmp_path, rows, "Stereolab", song="Emperor Tomato Ketchu")

        assert got == ([("Stereolab", "Emperor Tomato Ketchup")], True)

    @pytest.mark.asyncio
    async def test_artist_without_a_shelf_still_reads_the_window(self, tmp_path):
        rows = [("Junior Varsity KM", "You're Fabulous")]

        got = await _fallback(tmp_path, rows, "Junior Varsity", song=UNRESOLVABLE)

        assert got == ([("Junior Varsity KM", "You're Fabulous")], True)


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
    async def test_own_read_stops_at_the_window_size_with_no_album(self, tmp_path):
        """Various Artists' plain shelf holds 3,113 rows; main's window returned 49."""
        rows = [("Stereolab", f"Record {i}") for i in range(_FETCH_LIMIT + 10)]

        got, _ = await _fallback(tmp_path, rows, "Stereolab")

        assert got == rows[:_FETCH_LIMIT]

    @pytest.mark.asyncio
    async def test_typed_album_reads_every_own_row_before_the_floor(self, tmp_path):
        rows = [
            *(("Stereolab", f"Record {i}") for i in range(_FETCH_LIMIT)),
            ("Stereolab", "Dots and Loops"),
        ]

        got, _ = await _fallback(tmp_path, rows, "Stereolab", album="Dots and Loops")

        assert got == [("Stereolab", "Dots and Loops")]

    @pytest.mark.asyncio
    async def test_various_artists_shelves_are_not_listed_whole(self, tmp_path):
        rows = [("Various Artists", "One"), ("Various Artists - Rock - H", "Two")]

        got, _ = await _fallback(tmp_path, rows, "Various Artists")

        assert got == [("Various Artists", "One")]


class TestArtistSongFallback:
    @pytest.mark.asyncio
    async def test_song_titled_after_a_record_leads_with_the_artists_row(self, tmp_path):
        """The catalog's collision: "Can-i-bus" tokenizes to "can", so Canibus'
        row and the crowd fill the "Can Can" window ahead of Can's self-titled
        record, and main answered with Canibus alone."""
        rows = [
            ("Canibus", "Can-i-bus"),
            *crowd_rows("Can"),
            ("Can", "Ege Bamyasi"),
            ("Can", "Can"),
        ]

        got, fallback = await _fallback(tmp_path, rows, "Can", song="Can")

        assert fallback is True
        assert got[0] == ("Can", "Can")
        assert ("Canibus", "Can-i-bus") not in got

    @pytest.mark.asyncio
    async def test_song_in_title_ranks_first_within_own_rows(self, tmp_path):
        """Both rows hold every word of the song, so the search returns both in
        id order; only the later one holds the song as written."""
        rows = [("Can", "Tago Mago Sessions"), ("Can", "Sessions Tago Mago Live")]

        got, _ = await _fallback(tmp_path, rows, "Can", song="Sessions Tago Mago")

        assert got == [("Can", "Sessions Tago Mago Live"), ("Can", "Tago Mago Sessions")]

    @pytest.mark.asyncio
    async def test_various_artists_song_reaches_a_compilation_shelf(self, tmp_path):
        """The shelves are read with the song alone: their names never hold the
        song's words, so the artist + song query would miss them."""
        rows = [("Various Artists", "Other"), ("Various Artists - Rock - S", "Sister Ray")]

        got = await _fallback(tmp_path, rows, "Various Artists", song="Sister Ray")

        assert got == ([("Various Artists - Rock - S", "Sister Ray")], True)


class TestPlaceholderGuardAgreement:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("artist", ["Heart", "Spirit"])
    async def test_guard_and_artist_only_fallback_both_see_the_literal_s_t_row(
        self, tmp_path, artist
    ):
        """Decision 4: a crowded artist with a literal "S/T" row. The guard skips
        step 2 because the row exists, and the fallback, with no step-2 albums,
        lists that same row for the same request."""
        rows = [*crowd_rows(artist), (artist, "S/T"), (artist, "Second Album")]
        parsed = _parsed(artist, album="S/T", song="Some Song")
        db = await make_library_catalog(tmp_path, rows)
        try:
            guard_runs = await runs_album_resolution(parsed, db)
            got, _ = await search_library_with_fallback(db, parsed, [])
        finally:
            await db.close()

        assert guard_runs is False
        assert (artist, "S/T") in [(r.artist, r.title) for r in got]
