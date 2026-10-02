"""``lookup/artist_shelf.py`` against a real SQLite catalog (LML#1406).

A real ``library_fts`` index, built with the repo's own DDL, rather than a
mocked ``LibraryDB``: the defect this module fixes lived in the gap between
what the full-text index returns and what the lane keeps, which a mock cannot
show.
"""

import sqlite3

import pytest

from library.db import LIBRARY_FTS_CREATE_SQL, LibraryDB
from lookup.artist_shelf import rows_for_artist
from lookup.matching import _FETCH_LIMIT
from lookup.shelf_fallback import _rows_by_artist

CROWD = 60
"""Rows by other artists sharing a word with the artist under test. More than
the 50-row ``db.search`` window the shelf lane used to read."""


async def _catalog(tmp_path, rows: list[tuple[str, str]]) -> LibraryDB:
    """A connected ``LibraryDB`` over ``rows`` of ``(artist, title)``, ids from 1."""
    db_file = tmp_path / "library.db"
    conn = sqlite3.connect(db_file)
    conn.execute(
        "CREATE TABLE library (id INTEGER PRIMARY KEY, title TEXT, artist TEXT, "
        "call_letters TEXT, artist_call_number INTEGER, release_call_number INTEGER, "
        "genre TEXT, format TEXT)"
    )
    conn.execute(LIBRARY_FTS_CREATE_SQL)
    conn.executemany(
        "INSERT INTO library VALUES (?, ?, ?, 'A', 1, 1, 'Rock', 'LP')",
        [(i, title, artist) for i, (artist, title) in enumerate(rows, start=1)],
    )
    conn.execute("INSERT INTO library_fts(library_fts) VALUES ('rebuild')")
    conn.commit()
    conn.close()
    db = LibraryDB(db_path=db_file)
    await db.connect()
    return db


def _crowd(word: str) -> list[tuple[str, str]]:
    """``CROWD`` rows that match ``word`` in full-text search and are not by it."""
    half = CROWD // 2
    return [(f"{word} Ensemble {i}", f"Volume {i}") for i in range(half)] + [
        (f"Orchestra {i}", f"{word} Suite {i}") for i in range(CROWD - half)
    ]


class TestCrowdedOutArtists:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("artist", ["Love", "The The", "Heart", "The Band"])
    async def test_artist_behind_a_full_search_window_gets_its_own_rows(self, tmp_path, artist):
        """The artist's rows sit after 60 rows that share its name's words.
        The 50-row window holds none of them; the artist-keyed read holds all,
        and nothing else."""
        db = await _catalog(
            tmp_path,
            [*_crowd(artist), (artist, "First Album"), (artist, "Second Album")],
        )
        try:
            window = await db.search(query=artist, limit=_FETCH_LIMIT)
            assert _rows_by_artist(window, artist) == []

            rows = await rows_for_artist(db, artist)
        finally:
            await db.close()

        assert [(row.artist, row.title) for row in rows] == [
            (artist, "First Album"),
            (artist, "Second Album"),
        ]


class TestThisArtistOnly:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("typed", "catalog", "expected"),
        [
            pytest.param(
                "Can",
                [("Canibus", "Can-I-Bus"), ("Can", "Ege Bamyasi"), ("Tin Can Phone", "Hello")],
                [("Can", "Ege Bamyasi")],
                id="prefix-and-containing-names-are-other-artists",
            ),
            pytest.param(
                "Low",
                [("Low Profile", "We're in This Together"), ("The Low Numbers", "Twist Again")],
                [],
                id="only-longer-names-shelved",
            ),
            pytest.param(
                "Sun Ra",
                [("Sun Ra", "Lanquidity"), ("Sun Ra Arkestra", "Swirling")],
                [("Sun Ra", "Lanquidity")],
                id="longer-credit-is-a-different-artist",
            ),
            pytest.param(
                "nilufer yanya",
                [("Nilüfer Yanya", "PAINLESS"), ("NILÜFER YANYA", "My Method Actor")],
                [("Nilüfer Yanya", "PAINLESS"), ("NILÜFER YANYA", "My Method Actor")],
                id="case-and-diacritics-are-one-artist",
            ),
            pytest.param(
                "Stereolab",
                [("Stereolab", "Dots and Loops"), ("Cat Power", "Stereolab Covers")],
                [("Stereolab", "Dots and Loops")],
                id="name-in-another-artists-title-is-not-a-row",
            ),
            pytest.param("   ", [("Stereolab", "Dots and Loops")], [], id="blank-artist"),
            pytest.param("!!!", [("Stereolab", "Dots and Loops")], [], id="no-searchable-term"),
        ],
    )
    async def test_exact_rung(self, tmp_path, typed, catalog, expected):
        db = await _catalog(tmp_path, catalog)
        try:
            rows = await rows_for_artist(db, typed)
        finally:
            await db.close()

        assert [(row.artist, row.title) for row in rows] == expected


class TestTolerantRungs:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("typed", "catalog", "expected"),
        [
            pytest.param(
                "Clientele",
                [("The Clientele", "Suburban Light"), ("Clientele Records", "Sampler")],
                ["The Clientele"],
                id="article-dropped-by-the-listener",
            ),
            pytest.param(
                "The Stereolab",
                [("Stereolab", "Dots and Loops")],
                ["Stereolab"],
                id="article-added-by-the-listener",
            ),
            pytest.param(
                "Melt Banana",
                [("Melt-Banana", "Fetch")],
                ["Melt-Banana"],
                id="punctuation-dropped-by-the-listener",
            ),
            pytest.param(
                "Chuquimamani Condori",
                [("Chuquimamani-Condori", "Edits")],
                ["Chuquimamani-Condori"],
                id="hyphen-typed-as-a-space",
            ),
            pytest.param(
                "Clientele!",
                [("The Clientele", "Suburban Light")],
                ["The Clientele"],
                id="article-and-punctuation-together",
            ),
            pytest.param(
                "The Girls",
                [("Girls", "Album"), ("The Girls", "Reunion")],
                ["The Girls"],
                id="exact-spelling-beats-the-article-rung",
            ),
            pytest.param(
                "Girls",
                [("Girls", "Album"), ("The Girls", "Reunion")],
                ["Girls"],
                id="exact-spelling-beats-the-article-rung-reversed",
            ),
            pytest.param(
                "Frames",
                [("A Frames", "Black Forest"), ("The Frames", "Fitzcarraldo")],
                [],
                id="article-rung-matching-two-artists-returns-neither",
            ),
            pytest.param(
                "Alaska",
                [("Alaska!", "Emotions"), ("Alaska?", "Rescue")],
                [],
                id="punctuation-rung-matching-two-artists-returns-neither",
            ),
            pytest.param(
                "A-Ha",
                [("Ha", "Laughing"), ("A-Ha", "Hunting High and Low")],
                ["A-Ha"],
                id="initial-is-not-an-article",
            ),
        ],
    )
    async def test_variant_spellings(self, tmp_path, typed, catalog, expected):
        db = await _catalog(tmp_path, catalog)
        try:
            rows = await rows_for_artist(db, typed)
        finally:
            await db.close()

        assert [row.artist for row in rows] == expected

    @pytest.mark.asyncio
    async def test_tolerant_rung_keeps_case_variants_of_one_artist(self, tmp_path):
        """Two stored casings are one artist under the exact rung, so a
        tolerant rung matching both is not ambiguous."""
        db = await _catalog(
            tmp_path, [("The Clientele", "Suburban Light"), ("THE CLIENTELE", "Strange Geometry")]
        )
        try:
            rows = await rows_for_artist(db, "Clientele")
        finally:
            await db.close()

        assert [row.title for row in rows] == ["Suburban Light", "Strange Geometry"]
