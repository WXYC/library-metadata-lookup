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


class TestFTS5PunctuationEscaping:
    """LML#972: FTS5 metacharacters in a query (``.``, ``"``, ``*``, ``:``, ``(``, ``)``)

    must not raise ``fts5: syntax error`` and silently degrade to the LIKE-AND
    fallback -- that fallback requires every significant token to co-occur on
    one row, which drops real matches like "M.I.A." (whose album is "Kala",
    sharing no token with the artist query).
    """

    @pytest.mark.asyncio
    async def test_punctuated_artist_name_matches_via_fts(self, library_db):
        """A query for 'M.I.A.' finds the seeded row via a real FTS match.

        Before the fix: MATCH 'M.I.A.' raises `fts5: syntax error near "."`,
        caught by the broad except and degraded to LIKE-AND, which requires
        "kala" AND "m" AND "i" AND "a" to all appear on one row -- so it
        returns []. After the fix, the escaped MATCH tokenizes "M.I.A." into
        `m i a`, which the FTS index actually contains for this row.

        ``fallback_to_fuzzy=False`` isolates the FTS/LIKE path being fixed
        here: with the default ``fallback_to_fuzzy=True``, this tiny seed
        corpus lets the *fuzzy* fallback's permissive single-character-prefix
        LIKE scan (``LIKE '%m%'``, then rapidfuzz-scored) coincidentally
        catch "M.I.A." too, which would mask the FTS regression this test
        exists to catch.
        """
        results = await library_db.search(query="M.I.A.", fallback_to_fuzzy=False)
        assert any(r.artist == "M.I.A." for r in results), results

    @pytest.mark.asyncio
    async def test_punctuated_artist_name_matches_with_default_fallback_chain(self, library_db):
        """Same query through the full default fallback chain (fuzzy included)."""
        results = await library_db.search(query="M.I.A.")
        assert any(r.artist == "M.I.A." for r in results), results

    @pytest.mark.parametrize(
        "query",
        [
            "M.I.A.",
            'a "quoted" thing',
            "wild*card",
            "colon:test",
            "(paren)",
            "St. Vincent",
        ],
    )
    @pytest.mark.asyncio
    async def test_metacharacter_queries_do_not_raise(self, library_db, query):
        """None of the FTS5 metacharacter class should raise; all return a list."""
        results = await library_db.search(query=query)
        assert isinstance(results, list)

    @pytest.mark.parametrize("query", ["", "   ", "\t\n"])
    @pytest.mark.asyncio
    async def test_empty_or_whitespace_query_guarded(self, library_db, query):
        """An empty/all-whitespace query must never reach `MATCH ''` (also a syntax error).

        It should be guarded and routed straight to the LIKE/fuzzy fallback
        chain instead, returning a (possibly empty) list without raising.
        """
        results = await library_db.search(query=query)
        assert isinstance(results, list)

    @pytest.mark.asyncio
    async def test_clean_multiword_query_unaffected(self, library_db):
        """Escaping is equivalence-preserving: a clean multi-word query still
        returns the same rows as before the fix (same rows as
        ``test_combined_artist_album``)."""
        results = await library_db.search(query="Stereolab Dots Loops")
        assert len(results) >= 1
        assert any(r.title == "Dots and Loops" for r in results)


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
    async def test_genuine_misspelling_still_corrected(self, library_db):
        """Regression guard: a real typo of a library artist still binds (LML#857)."""
        result = await library_db.find_similar_artist("Stereolb")
        assert result == "Stereolab"

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
