"""The artist+album lane's rows against a real SQLite catalog (LML#1421).

A real ``library_fts`` index, as in ``tests/unit/test_artist_shelf.py``: the
defect lived in the gap between the 50 rows the full-text search returns and
the rows the lane keeps, which a mocked ``LibraryDB`` cannot show.
"""

import pytest

from lookup.strategies.artist_plus_album import search_library_with_fallback
from services.parser import MessageType, ParsedRequest
from tests.unit.test_artist_shelf import _catalog, _crowd


async def _lane(tmp_path, rows, artist, album):
    """``((artist, title) of the lane's rows, fallback flag)`` for one typed album, no song."""
    db = await _catalog(tmp_path, rows)
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
            *_crowd(artist),
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
