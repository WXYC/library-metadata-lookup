"""Integration test for cross_reference_names artist alias search (WXYC/discogs-etl#334).

Mirrors ``tests/integration/test_alternate_artist.py``'s real-SQLite pattern
but for the WXYC catalog's cataloger-recorded LIBRARY_CODE_CROSS_REFERENCE
aliases rather than the hand-typed ALTERNATE_ARTIST_NAME field. Worked
example: library.db row 57833 is filed under the band name "Burning Star
Core" -- neither ``artist`` nor ``alternate_artist_name`` ("C.S. Yeh")
prefix-matches a DJ typing the member's full personal name "C. Spencer Yeh",
but the catalog's cross-reference table links the two codes, and library.db
now carries that link as ``cross_reference_names``.

Note on scope: ``cross_reference_names`` is consulted by
``artist_matches_item`` (the post-retrieval artist *filter*), not indexed in
``library_fts`` (the retrieval layer) -- that's the Phase 3 scope this ticket
draws (``lookup/matching.py`` + ``library/db.py`` + ``library/models.py``
only). So a real-SQLite FTS/LIKE/fuzzy search using ONLY the typed artist
name as the query text ("C. Spencer Yeh") cannot retrieve row 57833 by
itself here -- none of its indexed columns (title, artist,
alternate_artist_name) contain those tokens. The fix surfaces the row on
paths where retrieval happens via a different axis (an album/track *title*
match, e.g. ``search_compilations_for_track``'s ``search_album_fuzzy`` +
``artist_matches_item`` split -- see ``tests/unit/test_orchestrator_gaps.py``
and ``tests/unit/test_orchestrator_helpers.py`` for the mocked-db end-to-end
coverage of that split) and the artist-filter step is what needs to accept
the cross-referenced name.
"""

import sqlite3

import pytest
import pytest_asyncio

from library.db import LibraryDB
from lookup.matching import filter_results_by_artist
from lookup.strategies.artist_plus_album import search_library_with_fallback
from services.parser import ParsedRequest


class TestCrossReferenceNamesIntegration:
    """End-to-end test with a real SQLite database carrying cross_reference_names."""

    @pytest_asyncio.fixture
    async def db_with_cross_reference(self, tmp_path):
        """Create a test SQLite database shaped like library.db row 57833."""
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
                format TEXT,
                alternate_artist_name TEXT,
                cross_reference_names TEXT
            )
        """)
        conn.execute("""
            CREATE VIRTUAL TABLE library_fts USING fts5(
                title, artist, alternate_artist_name,
                content='library', content_rowid='id'
            )
        """)
        # Real WXYC library.db row: C. Spencer Yeh's 7-inch, filed under his
        # band name "Burning Star Core", with the hand-typed alternate name
        # "C.S. Yeh" AND the catalog's cross-referenced personal name.
        conn.execute(
            "INSERT INTO library VALUES "
            "(57833, '\"In The Blink of an Eye\" 7-inch', 'Burning Star Core', 'BU', "
            "110, 6, 'Rock', 'vinyl - 7\"', 'C.S. Yeh', 'C. Spencer Yeh')"
        )
        conn.execute(
            "INSERT INTO library_fts(rowid, title, artist, alternate_artist_name) "
            "VALUES (57833, 'In The Blink of an Eye', 'Burning Star Core', 'C.S. Yeh')"
        )
        # A second Burning Star Core release with no cross-reference recorded,
        # to prove the match isn't accidentally artist-wide.
        conn.execute(
            "INSERT INTO library VALUES "
            "(57834, 'Challenger', 'Burning Star Core', 'BU', 110, 7, 'Rock', 'CD', NULL, NULL)"
        )
        conn.execute(
            "INSERT INTO library_fts(rowid, title, artist, alternate_artist_name) "
            "VALUES (57834, 'Challenger', 'Burning Star Core', NULL)"
        )
        conn.commit()
        conn.close()

        db = LibraryDB(db_path=db_file)
        await db.connect()
        yield db
        await db.close()

    @pytest.mark.asyncio
    async def test_filter_results_by_artist_matches_cross_reference_name(
        self, db_with_cross_reference
    ):
        """A title-retrieved candidate set, filtered by the typed personal name
        'C. Spencer Yeh', keeps only the cross-referenced row (57833) -- not its
        cross-reference-less sibling (57834)."""
        db = db_with_cross_reference

        results = await db.search(query="Blink of an Eye")
        filtered = filter_results_by_artist(results, "C. Spencer Yeh")

        assert len(filtered) == 1
        assert filtered[0].id == 57833

    @pytest.mark.asyncio
    async def test_search_by_primary_artist_still_finds_both_releases(
        self, db_with_cross_reference
    ):
        """Searching by the primary catalog name still finds both releases
        (the cross-reference extension doesn't narrow the existing primary-name path)."""
        db = db_with_cross_reference
        parsed = ParsedRequest(
            artist="Burning Star Core",
            raw_message="Burning Star Core",
        )

        results, _fallback_used = await search_library_with_fallback(db, parsed, albums=[])

        assert {r.id for r in results} == {57833, 57834}
