"""The artist+album lane's rows against a real SQLite catalog (LML#1421).

A real ``library_fts`` index (``tests/factories.py::make_library_catalog``): the
defect lived in the gap between the 50 rows the full-text search returns and
the rows the lane keeps, which a mocked ``LibraryDB`` cannot show.
"""

import pytest

from lookup.strategies.artist_plus_album import search_library_with_fallback
from services.parser import MessageType, ParsedRequest
from tests.factories import crowd_rows, make_library_catalog


async def _lane(tmp_path, rows, artist, album):
    """``((artist, title) of the lane's rows, fallback flag)`` for one typed album, no song."""
    db = await make_library_catalog(tmp_path, rows)
    parsed = ParsedRequest(
        artist=artist,
        album=album,
        raw_message=f"{artist} - {album}",
        is_request=True,
        message_type=MessageType.REQUEST,
    )
    try:
        results, fallback = await search_library_with_fallback(db, parsed, [album])
    finally:
        await db.close()
    return [(r.artist, r.title) for r in results], fallback


class TestOwnRowsBehindAFullSearchWindow:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("artist", ["God", "Heads", "James", "Spirit", "The Band"])
    async def test_self_titled_record_is_found(self, tmp_path, artist):
        """Production, 2026-10-02: God / "God" answered with God Rifle's record
        as an artist fallback. Every row sharing the name's words, including
        another artist's record titled after itself, sits ahead of the
        artist's own, so the 50-row window never reached them."""
        rows = [
            (f"{artist} Rifle", f"{artist} Rifle"),
            *crowd_rows(artist),
            (artist, artist),
            (artist, "Second Album"),
        ]

        assert await _lane(tmp_path, rows, artist, artist) == ([(artist, artist)], False)


class TestOwnRowsBeforeAnotherArtists:
    @pytest.mark.asyncio
    async def test_another_artists_record_of_the_same_title_is_dropped(self, tmp_path):
        """The prefix match admits Alias & Ehren for "Alias". The band Alias has
        the album on its own shelf, so only that row answers."""
        rows = [("Alias & Ehren", "Lillian"), ("Alias", "Lillian")]

        assert await _lane(tmp_path, rows, "Alias", "Lillian") == ([("Alias", "Lillian")], False)

    @pytest.mark.asyncio
    async def test_artist_without_a_shelf_reaches_a_prefix_matched_artist(self, tmp_path):
        rows = [("Sun Ra Arkestra", "Lanquidity"), ("Sun Ra Arkestra", "Second Album")]

        assert await _lane(tmp_path, rows, "Sun Ra", "Lanquidity") == (
            [("Sun Ra Arkestra", "Lanquidity")],
            False,
        )

    @pytest.mark.asyncio
    async def test_album_missing_from_the_own_shelf_reaches_a_prefix_matched_artist(self, tmp_path):
        """Sun Ra's shelf has no "Lanquidity", so the search window answers, as
        before LML#1421."""
        rows = [("Sun Ra", "Space Is the Place"), ("Sun Ra Arkestra", "Lanquidity")]

        assert await _lane(tmp_path, rows, "Sun Ra", "Lanquidity") == (
            [("Sun Ra Arkestra", "Lanquidity")],
            False,
        )


class TestOwnRowsUnderTheSameSearch:
    @pytest.mark.asyncio
    async def test_title_filter_alone_does_not_add_a_shelf_row(self, tmp_path):
        """The title filter would accept "DOGA" for "DOGA (Deluxe Edition)",
        but the full-text search never returned it, because its row lacks
        "deluxe" and "edition". Reading the shelf must not add it: every added
        row costs the caller an artwork lookup."""
        rows = [("Juana Molina", "DOGA"), ("Juana Molina", "DOGA (Deluxe Edition)")]

        assert await _lane(tmp_path, rows, "Juana Molina", "DOGA (Deluxe Edition)") == (
            [("Juana Molina", "DOGA (Deluxe Edition)")],
            False,
        )


class TestAlternateNameRowsAfterOwnRows:
    """LML#1425 decisions 1 and 5, applied to the album lane: while the
    artist's own rows answer, a row filed under another artist stays only when
    its ``alternate_artist_name`` credits the typed artist as a whole name at
    its start, and it follows the own rows."""

    MALI_MUSIC_CREDIT = "Afel Bocoum, Damon Albarn, Toumani Diabate and friends"

    @pytest.mark.asyncio
    async def test_alternate_credit_row_follows_the_own_row(self, tmp_path):
        rows = [
            ("Damon Albarn", "Mali Music", self.MALI_MUSIC_CREDIT),
            ("Afel Bocoum", "Mali Music"),
        ]

        assert await _lane(tmp_path, rows, "Afel Bocoum", "Mali Music") == (
            [("Afel Bocoum", "Mali Music"), ("Damon Albarn", "Mali Music")],
            False,
        )

    @pytest.mark.asyncio
    async def test_prefix_only_row_is_dropped_while_the_artist_has_the_album(self, tmp_path):
        """ "Sun Ra Arkestra" reaches "Sun Ra" only by the artist filter's
        prefix match, and Sun Ra has "Lanquidity" on its own shelf."""
        rows = [("Sun Ra Arkestra", "Lanquidity"), ("Sun Ra", "Lanquidity")]

        assert await _lane(tmp_path, rows, "Sun Ra", "Lanquidity") == (
            [("Sun Ra", "Lanquidity")],
            False,
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("artist", "credit"),
        [
            pytest.param("Damon Albarn", MALI_MUSIC_CREDIT, id="named-mid-list"),
            pytest.param("Agnes", "Agnes Obel & Friends", id="prefix-of-a-longer-name"),
        ],
    )
    async def test_credit_that_does_not_start_with_the_whole_name_is_dropped(
        self, tmp_path, artist, credit
    ):
        rows = [("Toumani Diabate", "Mali Music", credit), (artist, "Mali Music")]

        assert await _lane(tmp_path, rows, artist, "Mali Music") == (
            [(artist, "Mali Music")],
            False,
        )
