"""Unit tests for library/db.py."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from library.db import LibraryDB
from library.models import LibraryItem


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_row(**kwargs):
    """Create a dict-like row object mimicking aiosqlite.Row."""
    defaults = {
        "id": 1,
        "title": "Album",
        "artist": "Artist",
        "call_letters": "A",
        "artist_call_number": 1,
        "release_call_number": 1,
        "genre": "Rock",
        "format": "CD",
    }
    defaults.update(kwargs)
    row = MagicMock()
    row.__iter__ = MagicMock(return_value=iter(defaults.items()))
    row.__getitem__ = lambda self, k: defaults[k]

    def dict_func(r=defaults):
        return r.copy()

    row.__iter__ = lambda s: iter(defaults.items())
    type(row).__iter__ = lambda s: iter(defaults.items())

    # Make dict(row) work
    class DictRow(dict):
        pass

    return DictRow(defaults)


# ---------------------------------------------------------------------------
# connect / close / is_available
# ---------------------------------------------------------------------------


class TestLibraryDBConnect:
    @pytest.mark.asyncio
    async def test_connect_file_not_found(self, tmp_path):
        db = LibraryDB(db_path=tmp_path / "nonexistent.db")
        with pytest.raises(FileNotFoundError, match="Library database not found"):
            await db.connect()

    @pytest.mark.asyncio
    @patch("library.db.aiosqlite")
    async def test_connect_success(self, mock_aiosqlite, tmp_path):
        db_file = tmp_path / "test.db"
        db_file.touch()
        mock_conn = AsyncMock()
        mock_aiosqlite.connect = AsyncMock(return_value=mock_conn)
        mock_aiosqlite.Row = "RowClass"

        db = LibraryDB(db_path=db_file)
        await db.connect()

        assert db._conn is mock_conn
        mock_aiosqlite.connect.assert_called_once_with(db_file)


class TestLibraryDBIsAvailable:
    @pytest.mark.asyncio
    async def test_no_connection(self):
        db = LibraryDB()
        db._conn = None
        assert await db.is_available() is False

    @pytest.mark.asyncio
    async def test_healthy_connection(self):
        db = LibraryDB()
        mock_cursor = AsyncMock()
        mock_cursor.fetchone = AsyncMock(return_value=(1,))

        # aiosqlite's conn.execute() returns an async context manager (not a coroutine).
        # Use MagicMock so that the call is synchronous and the result supports
        # `async with conn.execute(...) as cursor:`.
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=mock_cursor)
        ctx.__aexit__ = AsyncMock(return_value=False)

        mock_conn = AsyncMock()
        mock_conn.execute = MagicMock(return_value=ctx)

        db._conn = mock_conn
        assert await db.is_available() is True

    @pytest.mark.asyncio
    async def test_exception_returns_false(self):
        db = LibraryDB()
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(side_effect=Exception("db error"))
        ctx.__aexit__ = AsyncMock(return_value=False)

        mock_conn = AsyncMock()
        mock_conn.execute = MagicMock(return_value=ctx)
        db._conn = mock_conn
        assert await db.is_available() is False


class TestLibraryDBClose:
    @pytest.mark.asyncio
    async def test_close_with_connection(self):
        db = LibraryDB()
        mock_conn = AsyncMock()
        db._conn = mock_conn
        await db.close()
        mock_conn.close.assert_called_once()
        assert db._conn is None

    @pytest.mark.asyncio
    async def test_close_without_connection(self):
        db = LibraryDB()
        db._conn = None
        await db.close()  # Should not raise


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


class TestLibraryDBSearch:
    @pytest.mark.asyncio
    async def test_not_connected_raises(self):
        db = LibraryDB()
        db._conn = None
        with pytest.raises(RuntimeError, match="not connected"):
            await db.search(query="test")

    @pytest.mark.asyncio
    async def test_no_params_returns_empty(self):
        db = LibraryDB()
        db._conn = AsyncMock()
        result = await db.search()
        assert result == []

    @pytest.mark.asyncio
    async def test_fts_query_success(self):
        db = LibraryDB()
        row = _make_row(id=1, artist="Queen", title="The Game")
        mock_cursor = AsyncMock()
        mock_cursor.fetchall = AsyncMock(return_value=[row])
        db._conn = AsyncMock()
        db._conn.execute = AsyncMock(return_value=mock_cursor)

        results = await db.search(query="Queen Game")
        assert len(results) == 1
        assert results[0].artist == "Queen"

    @pytest.mark.asyncio
    async def test_fts_empty_falls_back_to_like(self):
        db = LibraryDB()
        row = _make_row(id=2, artist="Queen", title="The Game")

        fts_cursor = AsyncMock()
        fts_cursor.fetchall = AsyncMock(return_value=[])  # FTS empty

        like_cursor = AsyncMock()
        like_cursor.fetchall = AsyncMock(return_value=[row])

        db._conn = AsyncMock()
        db._conn.execute = AsyncMock(side_effect=[fts_cursor, like_cursor])

        results = await db.search(query="Queen Game")
        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_fts_error_falls_back_to_like(self):
        db = LibraryDB()
        row = _make_row(id=3, artist="Queen", title="Opera")

        like_cursor = AsyncMock()
        like_cursor.fetchall = AsyncMock(return_value=[row])

        db._conn = AsyncMock()
        # First call (FTS) raises, second call (LIKE) succeeds
        db._conn.execute = AsyncMock(side_effect=[Exception("FTS error"), like_cursor])

        results = await db.search(query="Queen Opera")
        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_fts_error_no_fallback_raises(self):
        db = LibraryDB()
        db._conn = AsyncMock()
        db._conn.execute = AsyncMock(side_effect=Exception("FTS error"))

        with pytest.raises(Exception, match="FTS error"):
            await db.search(query="test", fallback_to_like=False)

    @pytest.mark.asyncio
    async def test_like_empty_falls_back_to_fuzzy(self):
        db = LibraryDB()

        fts_cursor = AsyncMock()
        fts_cursor.fetchall = AsyncMock(return_value=[])

        like_cursor = AsyncMock()
        like_cursor.fetchall = AsyncMock(return_value=[])

        # Fuzzy search needs candidates
        row = _make_row(id=4, artist="Radiohead", title="OK Computer")
        fuzzy_cursor = AsyncMock()
        fuzzy_cursor.fetchall = AsyncMock(return_value=[row])

        db._conn = AsyncMock()
        db._conn.execute = AsyncMock(side_effect=[fts_cursor, like_cursor, fuzzy_cursor])

        results = await db.search(query="Radiohead Computer")
        # Fuzzy search returns results if score >= threshold
        assert isinstance(results, list)

    @pytest.mark.asyncio
    async def test_artist_filter(self):
        db = LibraryDB()
        row = _make_row(id=5, artist="Queen", title="The Game")
        mock_cursor = AsyncMock()
        mock_cursor.fetchall = AsyncMock(return_value=[row])
        db._conn = AsyncMock()
        db._conn.execute = AsyncMock(return_value=mock_cursor)

        results = await db.search(artist="Queen")
        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_title_filter(self):
        db = LibraryDB()
        row = _make_row(id=6, artist="Queen", title="The Game")
        mock_cursor = AsyncMock()
        mock_cursor.fetchall = AsyncMock(return_value=[row])
        db._conn = AsyncMock()
        db._conn.execute = AsyncMock(return_value=mock_cursor)

        results = await db.search(title="Game")
        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_artist_and_title_filter(self):
        db = LibraryDB()
        row = _make_row(id=7, artist="Queen", title="The Game")
        mock_cursor = AsyncMock()
        mock_cursor.fetchall = AsyncMock(return_value=[row])
        db._conn = AsyncMock()
        db._conn.execute = AsyncMock(return_value=mock_cursor)

        results = await db.search(artist="Queen", title="Game")
        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_fallback_disabled(self):
        db = LibraryDB()
        fts_cursor = AsyncMock()
        fts_cursor.fetchall = AsyncMock(return_value=[])
        db._conn = AsyncMock()
        db._conn.execute = AsyncMock(return_value=fts_cursor)

        results = await db.search(query="nothing", fallback_to_like=False, fallback_to_fuzzy=False)
        assert results == []


# ---------------------------------------------------------------------------
# _fallback_like_search
# ---------------------------------------------------------------------------


class TestFallbackLikeSearch:
    @pytest.mark.asyncio
    async def test_stopword_removal(self):
        db = LibraryDB()
        mock_cursor = AsyncMock()
        mock_cursor.fetchall = AsyncMock(return_value=[])
        db._conn = AsyncMock()
        db._conn.execute = AsyncMock(return_value=mock_cursor)

        result = await db._fallback_like_search("play the song Queen", limit=10)
        # "play", "the", "song" are stopwords; "queen" should remain
        assert isinstance(result, list)

    @pytest.mark.asyncio
    async def test_empty_after_normalization(self):
        db = LibraryDB()
        db._conn = AsyncMock()
        result = await db._fallback_like_search("!@#$", limit=10)
        assert result == []


# ---------------------------------------------------------------------------
# _fuzzy_search
# ---------------------------------------------------------------------------


class TestFuzzySearch:
    @pytest.mark.asyncio
    async def test_empty_query_returns_empty(self):
        db = LibraryDB()
        db._conn = AsyncMock()
        result = await db._fuzzy_search("!@#$", limit=10)
        assert result == []

    @pytest.mark.asyncio
    async def test_no_candidates_returns_empty(self):
        db = LibraryDB()
        mock_cursor = AsyncMock()
        mock_cursor.fetchall = AsyncMock(return_value=[])
        db._conn = AsyncMock()
        db._conn.execute = AsyncMock(return_value=mock_cursor)

        result = await db._fuzzy_search("Radiohead", limit=10)
        assert result == []

    @pytest.mark.asyncio
    async def test_scores_and_filters(self):
        db = LibraryDB()
        row = _make_row(id=1, artist="Radiohead", title="OK Computer")
        mock_cursor = AsyncMock()
        mock_cursor.fetchall = AsyncMock(return_value=[row])
        db._conn = AsyncMock()
        db._conn.execute = AsyncMock(return_value=mock_cursor)

        # "Radiohead Computer" vs "Radiohead OK Computer" should score high
        result = await db._fuzzy_search("Radiohead Computer", limit=10, threshold=50)
        assert len(result) >= 1

    @pytest.mark.asyncio
    async def test_threshold_filtering(self):
        db = LibraryDB()
        row = _make_row(id=1, artist="ZZZZZ", title="YYYYY")
        mock_cursor = AsyncMock()
        mock_cursor.fetchall = AsyncMock(return_value=[row])
        db._conn = AsyncMock()
        db._conn.execute = AsyncMock(return_value=mock_cursor)

        # Very different strings should not match at high threshold
        result = await db._fuzzy_search("Radiohead", limit=10, threshold=90)
        assert len(result) == 0


# ---------------------------------------------------------------------------
# find_similar_artist
# ---------------------------------------------------------------------------


class TestFindSimilarArtist:
    @pytest.mark.asyncio
    async def test_not_connected_raises(self):
        db = LibraryDB()
        db._conn = None
        with pytest.raises(RuntimeError, match="not connected"):
            await db.find_similar_artist("Queen")

    @pytest.mark.asyncio
    async def test_short_words_return_none(self):
        db = LibraryDB()
        db._conn = AsyncMock()
        result = await db.find_similar_artist("XY")
        assert result is None

    @pytest.mark.asyncio
    async def test_no_candidates_return_none(self):
        db = LibraryDB()
        mock_cursor = AsyncMock()
        mock_cursor.fetchall = AsyncMock(return_value=[])
        db._conn = AsyncMock()
        db._conn.execute = AsyncMock(return_value=mock_cursor)

        result = await db.find_similar_artist("Nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_correction_found(self):
        db = LibraryDB()
        mock_cursor = AsyncMock()
        mock_cursor.fetchall = AsyncMock(return_value=[("Living Colour",)])

        # Make rows subscriptable
        class FakeRow:
            def __init__(self, val):
                self.val = val

            def __getitem__(self, idx):
                return self.val

        mock_cursor.fetchall = AsyncMock(return_value=[FakeRow("Living Colour")])
        db._conn = AsyncMock()
        db._conn.execute = AsyncMock(return_value=mock_cursor)

        result = await db.find_similar_artist("Living Color")
        assert result == "Living Colour"

    @pytest.mark.asyncio
    async def test_exact_match_returns_none(self):
        """If the best match is the same name, return None (no correction needed)."""
        db = LibraryDB()

        class FakeRow:
            def __init__(self, val):
                self.val = val

            def __getitem__(self, idx):
                return self.val

        mock_cursor = AsyncMock()
        mock_cursor.fetchall = AsyncMock(return_value=[FakeRow("Queen")])
        db._conn = AsyncMock()
        db._conn.execute = AsyncMock(return_value=mock_cursor)

        result = await db.find_similar_artist("Queen")
        assert result is None

    @pytest.mark.asyncio
    async def test_skips_none_candidates(self):
        db = LibraryDB()

        class FakeRow:
            def __init__(self, val):
                self.val = val

            def __getitem__(self, idx):
                return self.val

        mock_cursor = AsyncMock()
        mock_cursor.fetchall = AsyncMock(return_value=[FakeRow(None), FakeRow("Radiohead")])
        db._conn = AsyncMock()
        db._conn.execute = AsyncMock(return_value=mock_cursor)

        result = await db.find_similar_artist("Radiohed")
        assert result == "Radiohead"
