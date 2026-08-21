"""Integration test for artist-name punctuation tolerance (LML#1244).

The punctuation analogue of #364's leading-article integration coverage in
``test_alternate_artist.py``: a real SQLite database with FTS5, mirroring the
production ``library.db`` schema, proves the fix end-to-end through
``search_song_as_artist`` -- not just the pure ``artist_matches_item``
predicate covered in ``tests/unit/test_orchestrator_helpers.py``.

Real prod repro (WXYC/library-metadata-lookup#1244): WXYC's library.db files
Melt-Banana's nine releases under the hyphenated artist "Melt-Banana".
``search_song_as_artist(db, "Melt-Banana")`` returns them; before the fix,
``search_song_as_artist(db, "Melt Banana")`` (the punctuation-free spoken
form a listener types) returned zero -- FTS5 retrieves the rows (it
tokenizes on punctuation), but the artist filter discarded every one.
"""

import sqlite3

import pytest
import pytest_asyncio

from library.db import LibraryDB
from lookup.strategies.song_as_artist import search_song_as_artist


class TestPunctuationFoldIntegration:
    """End-to-end test with a real SQLite database containing a hyphenated
    artist row, mirroring the WXYC library.db Melt-Banana filing."""

    @pytest_asyncio.fixture
    async def db_with_melt_banana(self, tmp_path):
        """Create a test SQLite database seeded with the Melt-Banana shape."""
        db_file = tmp_path / "test_library.db"
        conn = sqlite3.connect(db_file)
        conn.execute("""
            CREATE TABLE library (
                id INTEGER PRIMARY KEY,
                title TEXT,
                artist TEXT,
                call_letters TEXT,
                artist_call_number INTEGER,
                release_call_number INTEGER,
                genre TEXT,
                format TEXT
            )
        """)
        conn.execute("""
            CREATE VIRTUAL TABLE library_fts USING fts5(
                title, artist,
                content='library', content_rowid='id'
            )
        """)
        # Mirrors prod rows 30762-30768: WXYC shelves Melt-Banana under the
        # hyphenated artist name.
        rows = [
            (30762, "Hedgehog EP", "Melt-Banana", "ME", 63, 1, "Rock", "CD"),
            (30763, "It's in the Pillowcase + 2", "Melt-Banana", "ME", 63, 2, "Rock", "CD"),
            (37332, "Cell Scape", "Melt-Banana", "ME", 63, 3, "Rock", "CD"),
        ]
        # An unrelated row that must NOT surface for a "Melt Banana" query.
        rows.append((1, "Vision Creation Newsun", "Boredoms", "B", 1, 1, "Rock", "CD"))
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
    async def test_song_only_lane_finds_hyphenated_artist_via_spoken_query(
        self, db_with_melt_banana
    ):
        """LML#1244: the song-only lane -- no ``find_similar_artist`` safety
        net runs here, so this lane was strictly worse-off than the artist
        lane before the fix -- must surface the Melt-Banana rows for the
        punctuation-free spoken query 'Melt Banana'."""
        items, discogs_titles = await search_song_as_artist(db_with_melt_banana, "Melt Banana")

        assert discogs_titles is None
        found_ids = {item.id for item in items}
        assert {30762, 30763, 37332}.issubset(found_ids), (
            f"Expected the Melt-Banana rows in {found_ids}"
        )
        assert all(item.artist == "Melt-Banana" for item in items)

    @pytest.mark.asyncio
    async def test_song_only_lane_still_finds_hyphenated_query(self, db_with_melt_banana):
        """Sanity check: the pre-existing hyphenated query still works
        (nothing in the fix regresses the already-matching path)."""
        items, _ = await search_song_as_artist(db_with_melt_banana, "Melt-Banana")

        found_ids = {item.id for item in items}
        assert {30762, 30763, 37332}.issubset(found_ids)
