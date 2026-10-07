"""``artist_rows`` against a real SQLite catalog (LML#1452).

The ordering the artist+album lane applies, parameterized so other lanes can
reuse it: own rows lead, Various Artists' compilation shelves follow them, and
the window's rows credited to the artist come last.
"""

import pytest

from library.models import LibraryItem
from lookup.artist_rows import artist_rows
from lookup.artist_shelf import artist_spellings
from tests.factories import make_library_catalog


async def _rows(tmp_path, rows, artist, title):
    """``artist_rows`` the way the album lane calls it, keeping rows titled ``title``.

    The window is the artist+title search filtered by the same ``keep``, and the
    compilation-shelf query is the title alone, as ``album_rows`` passes them.
    """

    def keep(found: list[LibraryItem]) -> list[LibraryItem]:
        return [row for row in found if row.title == title]

    db = await make_library_catalog(tmp_path, rows)
    try:
        got = await artist_rows(
            db,
            query=f"{artist} {title}",
            keep=keep,
            shelf_query=title,
            window=keep(await db.search(query=f"{artist} {title}", limit=50)),
            lib_artist=artist,
            spellings=await artist_spellings(db, artist),
        )
    finally:
        await db.close()
    return [(r.artist, r.title) for r in got]


class TestArtistRows:
    @pytest.mark.asyncio
    async def test_own_rows_lead_then_credited_rows_and_prefix_only_rows_drop(self, tmp_path):
        rows = [
            ("Sun Ra Arkestra", "Lanquidity"),
            ("Various Artists", "Lanquidity", "Sun Ra"),
            ("Sun Ra", "Lanquidity"),
        ]

        assert await _rows(tmp_path, rows, "Sun Ra", "Lanquidity") == [
            ("Sun Ra", "Lanquidity"),
            ("Various Artists", "Lanquidity"),
        ]

    @pytest.mark.asyncio
    async def test_window_answers_when_no_own_row_survives_the_filter(self, tmp_path):
        # The full-text query reaches Sun Ra's own "Lanquidity Live", and the
        # filter rejects it: the window answers, prefix-only row included.
        rows = [("Sun Ra Arkestra", "Lanquidity"), ("Sun Ra", "Lanquidity Live")]

        assert await _rows(tmp_path, rows, "Sun Ra", "Lanquidity") == [
            ("Sun Ra Arkestra", "Lanquidity")
        ]

    @pytest.mark.asyncio
    async def test_an_own_row_the_window_also_credits_appears_once(self, tmp_path):
        rows = [("Sun Ra", "Lanquidity", "Sun Ra & His Arkestra")]

        assert await _rows(tmp_path, rows, "Sun Ra", "Lanquidity") == [("Sun Ra", "Lanquidity")]

    @pytest.mark.asyncio
    async def test_various_artists_shelf_rows_sit_between_own_and_credited_rows(self, tmp_path):
        # "Soundtracks - S" lacks the words "various artists", so only the
        # title-only shelf query reaches it. The filter drops "Nuggets Vol. 2"
        # from the shelf rows as it does from own rows.
        rows = [
            ("Various Artists - Rock - H", "Nuggets"),
            ("Various Artists - Rock - N", "Nuggets Vol. 2"),
            ("Soundtracks - S", "Nuggets"),
            ("Various Artists", "Nuggets"),
            ("Pebbles", "Nuggets", "Various Artists"),
        ]

        assert await _rows(tmp_path, rows, "Various Artists", "Nuggets") == [
            ("Various Artists", "Nuggets"),
            ("Various Artists - Rock - H", "Nuggets"),
            ("Soundtracks - S", "Nuggets"),
            ("Pebbles", "Nuggets"),
        ]
