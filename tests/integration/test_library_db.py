"""Integration tests for library/db.py with real SQLite + FTS5."""

import aiosqlite
import pytest

from library.db import LibraryDB


class TestFTS5Search:
    @pytest.mark.asyncio
    async def test_search_by_artist(self, library_db):
        results = await library_db.search(query="Stereolab")
        assert len(results) >= 1
        assert all("Stereolab" in r.artist for r in results)

    @pytest.mark.asyncio
    async def test_search_by_album(self, library_db):
        results = await library_db.search(query="On Your Own Love Again")
        assert len(results) >= 1
        assert results[0].title == "On Your Own Love Again"

    @pytest.mark.asyncio
    async def test_combined_artist_album(self, library_db):
        results = await library_db.search(query="Stereolab Dots Loops")
        assert len(results) >= 1
        # Should find "Dots and Loops" by Stereolab
        assert any(r.title == "Dots and Loops" for r in results)

    @pytest.mark.asyncio
    async def test_limit(self, library_db):
        results = await library_db.search(query="Various", limit=1)
        assert len(results) <= 1

    @pytest.mark.asyncio
    async def test_special_characters_fallback(self, library_db):
        """FTS5 should handle special characters via LIKE fallback."""
        results = await library_db.search(query="Duke Ellington & John Coltrane")
        assert isinstance(results, list)

    @pytest.mark.asyncio
    async def test_no_results(self, library_db):
        results = await library_db.search(query="ZZZNONEXISTENT")
        assert results == []


class TestFilteredSearch:
    @pytest.mark.asyncio
    async def test_artist_filter(self, library_db):
        results = await library_db.search(artist="Stereolab")
        assert len(results) >= 1
        assert all("Stereolab" in r.artist for r in results)

    @pytest.mark.asyncio
    async def test_title_filter(self, library_db):
        results = await library_db.search(title="Dots and Loops")
        assert len(results) >= 1
        assert results[0].title == "Dots and Loops"

    @pytest.mark.asyncio
    async def test_combined_filter(self, library_db):
        results = await library_db.search(artist="Stereolab", title="Dots")
        assert len(results) >= 1

    @pytest.mark.asyncio
    async def test_no_match_filter(self, library_db):
        results = await library_db.search(artist="NONEXISTENT")
        assert results == []


class TestLIKEFallback:
    @pytest.mark.asyncio
    async def test_partial_match(self, library_db):
        """LIKE fallback picks up partial matches."""
        results = await library_db.search(query="Juana Molina DOGA")
        assert len(results) >= 1

    @pytest.mark.asyncio
    async def test_stopword_removal(self, library_db):
        """Stopwords ('the') are removed in LIKE search."""
        results = await library_db.search(query="the Bird Bee")
        assert isinstance(results, list)


class TestFuzzySearch:
    @pytest.mark.asyncio
    async def test_misspelled_artist(self, library_db):
        """Fuzzy search should find close matches."""
        # "Stereolob" is close to "Stereolab"
        results = await library_db.search(query="Stereolob Aluminum")
        # Might or might not match depending on threshold, but shouldn't crash
        assert isinstance(results, list)


class TestFindSimilarArtist:
    @pytest.mark.asyncio
    async def test_correction(self, library_db):
        """Finds 'Living Colour' from 'Living Color'."""
        result = await library_db.find_similar_artist("Living Color")
        assert result == "Living Colour"

    @pytest.mark.asyncio
    async def test_exact_match_returns_none(self, library_db):
        """Exact match returns None (no correction needed)."""
        result = await library_db.find_similar_artist("Stereolab")
        assert result is None

    @pytest.mark.asyncio
    async def test_no_match(self, library_db):
        result = await library_db.find_similar_artist("ZZZNONEXISTENT")
        assert result is None

    @pytest.mark.asyncio
    async def test_short_word(self, library_db):
        result = await library_db.find_similar_artist("XY")
        assert result is None

    @pytest.mark.asyncio
    async def test_article_prefix_artist_typo_uses_full_candidate_sql(self):
        conn = await aiosqlite.connect(":memory:")
        await conn.execute("""
            CREATE TABLE library (
                id INTEGER PRIMARY KEY,
                title TEXT NOT NULL,
                artist TEXT NOT NULL,
                call_letters TEXT,
                artist_call_number INTEGER,
                release_call_number INTEGER,
                genre TEXT,
                format TEXT,
                alternate_artist_name TEXT,
                album_artist TEXT
            )
        """)
        await conn.execute("""
            CREATE TABLE compilation_track_artist (
                library_id INTEGER NOT NULL,
                artist_name TEXT NOT NULL
            )
        """)
        await conn.execute(
            "INSERT INTO library VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                1,
                "Bytes",
                "Black Dog",
                "B",
                1,
                1,
                "Electronic",
                "CD",
                "Black Dog Productions",
                "Black Dog",
            ),
        )
        await conn.execute(
            "INSERT INTO compilation_track_artist VALUES (?, ?)",
            (1, "Black Dog"),
        )
        await conn.commit()

        db = LibraryDB()
        db._conn = conn
        db._has_alternate_artist = True
        db._has_album_artist = True
        db._has_compilation_track_artist = True

        try:
            result = await db.find_similar_artist("The Bleack Dog")
        finally:
            await conn.close()

        assert result == "Black Dog"


class TestIsAvailable:
    @pytest.mark.asyncio
    async def test_connected(self, library_db):
        assert await library_db.is_available() is True
