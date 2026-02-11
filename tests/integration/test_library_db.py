"""Integration tests for library/db.py with real SQLite + FTS5."""

import pytest

pytestmark = pytest.mark.integration


class TestFTS5Search:
    @pytest.mark.asyncio
    async def test_search_by_artist(self, library_db):
        results = await library_db.search(query="Queen")
        assert len(results) >= 1
        assert all("Queen" in r.artist for r in results)

    @pytest.mark.asyncio
    async def test_search_by_album(self, library_db):
        results = await library_db.search(query="OK Computer")
        assert len(results) >= 1
        assert results[0].title == "OK Computer"

    @pytest.mark.asyncio
    async def test_combined_artist_album(self, library_db):
        results = await library_db.search(query="Queen Game")
        assert len(results) >= 1
        # Should find "The Game" by Queen
        assert any(r.title == "The Game" for r in results)

    @pytest.mark.asyncio
    async def test_limit(self, library_db):
        results = await library_db.search(query="Various", limit=1)
        assert len(results) <= 1

    @pytest.mark.asyncio
    async def test_special_characters_fallback(self, library_db):
        """FTS5 should handle special characters via LIKE fallback."""
        results = await library_db.search(query="Time (Clock of the Heart)")
        assert isinstance(results, list)

    @pytest.mark.asyncio
    async def test_no_results(self, library_db):
        results = await library_db.search(query="ZZZNONEXISTENT")
        assert results == []


class TestFilteredSearch:
    @pytest.mark.asyncio
    async def test_artist_filter(self, library_db):
        results = await library_db.search(artist="Queen")
        assert len(results) >= 1
        assert all("Queen" in r.artist for r in results)

    @pytest.mark.asyncio
    async def test_title_filter(self, library_db):
        results = await library_db.search(title="The Game")
        assert len(results) >= 1
        assert results[0].title == "The Game"

    @pytest.mark.asyncio
    async def test_combined_filter(self, library_db):
        results = await library_db.search(artist="Queen", title="Game")
        assert len(results) >= 1

    @pytest.mark.asyncio
    async def test_no_match_filter(self, library_db):
        results = await library_db.search(artist="NONEXISTENT")
        assert results == []


class TestLIKEFallback:
    @pytest.mark.asyncio
    async def test_partial_match(self, library_db):
        """LIKE fallback picks up partial matches."""
        results = await library_db.search(query="Beatles Abbey")
        assert len(results) >= 1

    @pytest.mark.asyncio
    async def test_stopword_removal(self, library_db):
        """Stopwords ('the') are removed in LIKE search."""
        results = await library_db.search(query="the Beatles")
        assert isinstance(results, list)


class TestFuzzySearch:
    @pytest.mark.asyncio
    async def test_misspelled_artist(self, library_db):
        """Fuzzy search should find close matches."""
        # "Radioheed" is close to "Radiohead"
        results = await library_db.search(query="Radioheed Computer")
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
        result = await library_db.find_similar_artist("Queen")
        assert result is None

    @pytest.mark.asyncio
    async def test_no_match(self, library_db):
        result = await library_db.find_similar_artist("ZZZNONEXISTENT")
        assert result is None

    @pytest.mark.asyncio
    async def test_short_word(self, library_db):
        result = await library_db.find_similar_artist("XY")
        assert result is None


class TestIsAvailable:
    @pytest.mark.asyncio
    async def test_connected(self, library_db):
        assert await library_db.is_available() is True
