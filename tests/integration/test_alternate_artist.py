"""Integration test for alternate artist name search."""

import sqlite3

import pytest
import pytest_asyncio

from library.db import LibraryDB
from lookup.orchestrator import filter_results_by_artist


class TestAlternateArtistIntegration:
    """End-to-end test with a real SQLite database containing alternate artist names."""

    @pytest_asyncio.fixture
    async def db_with_alternate_artist(self, tmp_path):
        """Create a test SQLite database with alternate_artist_name column."""
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
                alternate_artist_name TEXT
            )
        """)
        conn.execute("""
            CREATE VIRTUAL TABLE library_fts USING fts5(
                title, artist, alternate_artist_name,
                content='library', content_rowid='id'
            )
        """)
        # Luke Vibert released "Drum 'n' Bass for Papa" under the alias "Plug"
        conn.execute(
            "INSERT INTO library VALUES (1, 'Drum ''n'' Bass for Papa (+ Plug EPs 1,2 & 3)', 'Luke Vibert', 'V', 15, 1, 'Electronic', 'CD', 'Plug')"
        )
        conn.execute(
            "INSERT INTO library_fts(rowid, title, artist, alternate_artist_name) VALUES (1, 'Drum ''n'' Bass for Papa (+ Plug EPs 1,2 & 3)', 'Luke Vibert', 'Plug')"
        )
        # Another album by Luke Vibert without an alternate name
        conn.execute(
            "INSERT INTO library VALUES (2, 'Big Soup', 'Luke Vibert', 'V', 15, 2, 'Electronic', 'CD', NULL)"
        )
        conn.execute(
            "INSERT INTO library_fts(rowid, title, artist, alternate_artist_name) VALUES (2, 'Big Soup', 'Luke Vibert', NULL)"
        )
        conn.commit()
        conn.close()

        db = LibraryDB(db_path=db_file)
        await db.connect()
        yield db
        await db.close()

    @pytest.mark.asyncio
    async def test_search_by_alternate_artist_finds_release(self, db_with_alternate_artist):
        """Searching for 'Plug' should find the album filed under 'Luke Vibert'."""
        db = db_with_alternate_artist

        # FTS search for "Plug" should match via the alternate_artist_name in FTS
        results = await db.search(query="Plug")
        # filter by artist "Plug" should keep the result because of alternate_artist_name
        filtered = filter_results_by_artist(results, "Plug")

        assert len(filtered) >= 1
        assert any("Drum" in (r.title or "") for r in filtered)

    @pytest.mark.asyncio
    async def test_filtered_artist_search_finds_alternate(self, db_with_alternate_artist):
        """Artist-filtered search for 'Plug' should match alternate_artist_name."""
        db = db_with_alternate_artist

        results = await db.search(artist="Plug")
        assert len(results) == 1
        assert results[0].alternate_artist_name == "Plug"

    @pytest.mark.asyncio
    async def test_search_by_primary_artist_still_works(self, db_with_alternate_artist):
        """Searching for 'Luke Vibert' should still find all albums."""
        db = db_with_alternate_artist

        results = await db.search(query="Luke Vibert")
        filtered = filter_results_by_artist(results, "Luke Vibert")
        assert len(filtered) == 2
