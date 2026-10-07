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


def _keep_titled(title):
    def keep(rows: list[LibraryItem]) -> list[LibraryItem]:
        return [row for row in rows if row.title == title]

    return keep


async def _rows(tmp_path, rows, artist, title, window_titles=None, shelf_query=None):
    db = await make_library_catalog(tmp_path, rows)
    try:
        spellings = await artist_spellings(db, artist)
        window = [
            r
            for r in await db.search(query=f"{artist} {title}", limit=50)
            if window_titles is None or r.title in window_titles
        ]
        got = await artist_rows(
            db,
            f"{artist} {title}",
            _keep_titled(title),
            shelf_query or title,
            window,
            artist,
            spellings,
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
        rows = [("Sun Ra Arkestra", "Lanquidity"), ("Sun Ra", "Nuits de la Fondation Maeght")]

        assert await _rows(tmp_path, rows, "Sun Ra", "Lanquidity") == [
            ("Sun Ra Arkestra", "Lanquidity")
        ]

    @pytest.mark.asyncio
    async def test_various_artists_shelf_rows_sit_between_own_and_credited_rows(self, tmp_path):
        rows = [
            ("Various Artists - Rock - H", "Nuggets"),
            ("Various Artists", "Nuggets"),
            ("Pebbles", "Nuggets", "Various Artists"),
        ]

        assert await _rows(tmp_path, rows, "Various Artists", "Nuggets") == [
            ("Various Artists", "Nuggets"),
            ("Various Artists - Rock - H", "Nuggets"),
            ("Pebbles", "Nuggets"),
        ]
