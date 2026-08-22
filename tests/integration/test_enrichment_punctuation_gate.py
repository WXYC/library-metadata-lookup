"""The search and enrichment axes must agree about who the artist is (LML#1252).

LML#1244 fixed the *search* axis: ``search_song_as_artist(db, "Melt Banana")``
now returns the hyphenated Melt-Banana rows. But enrichment authorizes artwork
and bio through a second, independent predicate — ``_artist_pair_verified``,
which scored the same pair at 54.55 against an 80 floor. The result was a half
delivery, reproduced on production before the fix: five rows returned, zero
carrying artwork.

That is the invariant this suite pins, and it is deliberately *not* the same
as the unit coverage in ``tests/unit/test_artist_pair_verified.py``. The unit
tests exercise the predicate directly with hand-written strings; these run the
real search against a real FTS5 database and then feed **the rows search
actually returned** into the enrichment gate. A future change that fixes one
axis and not the other passes both unit suites and fails here.

Companion to ``test_artist_punctuation_fold.py``, which covers the search half
of the same lane with the same fixture shape.
"""

import sqlite3

import pytest
import pytest_asyncio

from library.db import LibraryDB
from lookup.artist_resolution import _artist_pair_verified
from lookup.strategies.song_as_artist import search_song_as_artist


class TestSearchAndEnrichmentAgree:
    @pytest_asyncio.fixture
    async def db(self, tmp_path):
        """Mirrors prod rows 30762-30768 (Melt-Banana, hyphenated) plus a
        punctuation-free control artist and a near-miss distractor."""
        db_file = tmp_path / "test_library.db"
        conn = sqlite3.connect(db_file)
        conn.execute("""
            CREATE TABLE library (
                id INTEGER PRIMARY KEY, title TEXT, artist TEXT, call_letters TEXT,
                artist_call_number INTEGER, release_call_number INTEGER,
                genre TEXT, format TEXT
            )
        """)
        conn.execute("""
            CREATE VIRTUAL TABLE library_fts USING fts5(
                title, artist, content='library', content_rowid='id'
            )
        """)
        rows = [
            (30762, "Hedgehog EP", "Melt-Banana", "ME", 63, 1, "Rock", "CD"),
            (30763, "It's in the Pillowcase + 2", "Melt-Banana", "ME", 63, 2, "Rock", "CD"),
            (37332, "Cell Scape", "Melt-Banana", "ME", 63, 3, "Rock", "CD"),
            (40001, "Aluminum Tunes", "Stereolab", "ST", 1, 1, "Rock", "CD"),
            (1, "Vision Creation Newsun", "Boredoms", "B", 1, 1, "Rock", "CD"),
        ]
        conn.executemany("INSERT INTO library VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
        conn.executemany(
            "INSERT INTO library_fts(rowid, title, artist) VALUES (?, ?, ?)",
            [(r[0], r[1], r[2]) for r in rows],
        )
        conn.commit()
        conn.close()
        db = LibraryDB(db_path=db_file)
        await db.connect()
        yield db
        await db.close()

    @pytest.mark.asyncio
    async def test_every_row_search_returns_is_also_enrichment_verified(self, db):
        """The LML#1252 defect, end to end.

        Before the fix this test's search half passed and its enrichment half
        failed for all three rows — which is precisely how production returned
        five Melt-Banana records with no artwork on an ``extended=true``
        request.
        """
        items, _ = await search_song_as_artist(db, "Melt Banana")

        assert {i.id for i in items} >= {30762, 30763, 37332}, "search half regressed"
        unverified = [i.artist for i in items if not _artist_pair_verified("Melt Banana", i.artist)]
        assert not unverified, (
            f"search returned rows that enrichment refuses to authorize: {unverified}. "
            "The two axes have drifted again — artwork and bio will be dropped."
        )

    @pytest.mark.asyncio
    async def test_hyphenated_query_agrees_too(self, db):
        """The already-working spelling must keep agreeing on both axes."""
        items, _ = await search_song_as_artist(db, "Melt-Banana")

        assert {i.id for i in items} >= {30762, 30763, 37332}
        assert all(_artist_pair_verified("Melt-Banana", i.artist) for i in items)

    @pytest.mark.asyncio
    async def test_unpunctuated_artist_unaffected(self, db):
        """An artist with no punctuation exercises neither fold; both axes
        agreed before the fix and must still agree."""
        items, _ = await search_song_as_artist(db, "Stereolab")

        assert {i.id for i in items} >= {40001}
        assert all(_artist_pair_verified("Stereolab", i.artist) for i in items)

    @pytest.mark.asyncio
    async def test_gate_still_rejects_a_different_artist_in_the_same_db(self, db):
        """False positives excluded (bug-fix protocol): widening the gate for
        punctuation must not authorize an unrelated catalog artist. Boredoms
        shares the fixture and the genre; it must not verify against the
        Melt-Banana request under either spelling.
        """
        for query in ("Melt Banana", "Melt-Banana"):
            assert _artist_pair_verified(query, "Boredoms") is False
            assert _artist_pair_verified(query, "Stereolab") is False
