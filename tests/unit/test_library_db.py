"""Unit tests for library/db.py."""

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from library.db import LibraryDB, _to_fts_match_query

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


class TestLibraryDBSchemaValidation:
    """Tests for schema validation against wxyc_etl.schema constants."""

    @pytest.mark.asyncio
    @patch("library.db.aiosqlite")
    async def test_connect_validates_expected_columns(self, mock_aiosqlite, tmp_path):
        """connect() should warn if expected schema columns are missing."""
        db_file = tmp_path / "test.db"
        db_file.touch()

        mock_conn = AsyncMock()
        mock_aiosqlite.connect = AsyncMock(return_value=mock_conn)
        mock_aiosqlite.Row = "RowClass"

        # Simulate PRAGMA returning only a subset of expected columns (missing 'genre')
        pragma_cursor = AsyncMock()
        pragma_cursor.fetchall = AsyncMock(
            return_value=[
                (0, "id", "INTEGER", 0, None, 1),
                (1, "title", "TEXT", 0, None, 0),
                (2, "artist", "TEXT", 0, None, 0),
                (3, "call_letters", "TEXT", 0, None, 0),
                (4, "artist_call_number", "INTEGER", 0, None, 0),
                (5, "release_call_number", "INTEGER", 0, None, 0),
                (6, "format", "TEXT", 0, None, 0),
                # 'genre' is missing
            ]
        )
        tables_cursor = AsyncMock()
        tables_cursor.fetchone = AsyncMock(return_value=None)

        async def _execute_side_effect(sql, *args):
            if "PRAGMA" in sql:
                return pragma_cursor
            return tables_cursor

        mock_conn.execute = AsyncMock(side_effect=_execute_side_effect)

        db = LibraryDB(db_path=db_file)

        with patch("library.db.logger") as mock_logger:
            await db.connect()
            # Should log a warning about the missing column
            warning_calls = [
                call for call in mock_logger.warning.call_args_list if "genre" in str(call)
            ]
            assert len(warning_calls) > 0, "Expected warning about missing 'genre' column"

    @pytest.mark.asyncio
    @patch("library.db.aiosqlite")
    async def test_connect_no_warning_when_all_columns_present(self, mock_aiosqlite, tmp_path):
        """connect() should not warn when all expected columns are present."""
        db_file = tmp_path / "test.db"
        db_file.touch()

        mock_conn = AsyncMock()
        mock_aiosqlite.connect = AsyncMock(return_value=mock_conn)
        mock_aiosqlite.Row = "RowClass"

        # Return all expected columns
        pragma_cursor = AsyncMock()
        pragma_cursor.fetchall = AsyncMock(
            return_value=[
                (0, "id", "INTEGER", 0, None, 1),
                (1, "title", "TEXT", 0, None, 0),
                (2, "artist", "TEXT", 0, None, 0),
                (3, "call_letters", "TEXT", 0, None, 0),
                (4, "artist_call_number", "INTEGER", 0, None, 0),
                (5, "release_call_number", "INTEGER", 0, None, 0),
                (6, "genre", "TEXT", 0, None, 0),
                (7, "format", "TEXT", 0, None, 0),
                (8, "alternate_artist_name", "TEXT", 0, None, 0),
            ]
        )
        tables_cursor = AsyncMock()
        tables_cursor.fetchone = AsyncMock(return_value=None)

        async def _execute_side_effect(sql, *args):
            if "PRAGMA" in sql:
                return pragma_cursor
            return tables_cursor

        mock_conn.execute = AsyncMock(side_effect=_execute_side_effect)

        db = LibraryDB(db_path=db_file)

        with patch("library.db.logger") as mock_logger:
            await db.connect()
            warning_calls = [
                call
                for call in mock_logger.warning.call_args_list
                if "missing" in str(call).lower() and "column" in str(call).lower()
            ]
            assert len(warning_calls) == 0, f"Unexpected schema warnings: {warning_calls}"


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
# _to_fts_match_query (LML#972)
# ---------------------------------------------------------------------------


class TestToFtsMatchQuery:
    """Pure helper: quotes each whitespace-separated term as an FTS5 string
    literal so metacharacters (. " * : ( ) etc.) are treated as literal
    content rather than query syntax."""

    def test_single_term_gets_quoted(self):
        assert _to_fts_match_query("M.I.A.") == '"M.I.A."'

    def test_multiple_terms_each_quoted(self):
        assert _to_fts_match_query("J. Mascis") == '"J." "Mascis"'

    def test_embedded_quote_is_doubled(self):
        assert _to_fts_match_query('a "quoted" thing') == '"a" """quoted""" "thing"'

    def test_other_metacharacters_pass_through_inside_quotes(self):
        assert _to_fts_match_query("wild*card") == '"wild*card"'
        assert _to_fts_match_query("colon:test") == '"colon:test"'
        assert _to_fts_match_query("(paren)") == '"(paren)"'

    def test_clean_multiword_query_quoted_per_term(self):
        assert _to_fts_match_query("Stereolab Dots Loops") == '"Stereolab" "Dots" "Loops"'

    def test_empty_query_yields_empty_string(self):
        assert _to_fts_match_query("") == ""

    def test_whitespace_only_query_yields_empty_string(self):
        assert _to_fts_match_query("   ") == ""
        assert _to_fts_match_query("\t\n") == ""


class TestSearchEmptyMatchQueryGuard:
    """LML#972: an empty/all-whitespace query must never reach `MATCH ''`

    (also a syntax error: `fts5: syntax error near ""`). It must be guarded
    so the FTS branch never executes `MATCH ''` at all. For a whitespace-only
    query, ``_fallback_like_search``/``_fuzzy_search`` also find no
    significant words and short-circuit to ``[]`` before touching the
    database (see their own ``if not significant_words / words: return []``
    guards) -- so the whole call must be a no-op that returns a list without
    ever calling ``_conn.execute``.
    """

    @pytest.mark.asyncio
    async def test_whitespace_query_never_calls_execute(self):
        db = LibraryDB()
        db._conn = AsyncMock()

        results = await db.search(query="   ")

        assert results == []
        db._conn.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_tabs_and_newlines_only_query_never_calls_execute(self):
        db = LibraryDB()
        db._conn = AsyncMock()

        results = await db.search(query="\t\n")

        assert results == []
        db._conn.execute.assert_not_called()


class TestSearchFallbackLogLevels:
    """#798: the per-candidate search-fallback breadcrumbs log at DEBUG, not
    INFO, so a single VA-compilation ``/lookup`` doesn't emit one INFO line per
    token and flood Railway's 500/sec replica cap during saturation storms.

    Capture at DEBUG and assert every fallback breadcrumb is recorded there and
    none at INFO.
    """

    @staticmethod
    def _fallback_records(caplog):
        markers = ("LIKE fallback", "fuzzy fallback", "Fuzzy search for")
        return [
            r
            for r in caplog.records
            if r.name == "library.db" and any(m in r.getMessage() for m in markers)
        ]

    @pytest.mark.asyncio
    async def test_empty_fts_fallback_chain_logs_at_debug(self, caplog):
        db = LibraryDB()
        fts_cursor = AsyncMock()
        fts_cursor.fetchall = AsyncMock(return_value=[])  # FTS empty
        like_cursor = AsyncMock()
        like_cursor.fetchall = AsyncMock(return_value=[])  # LIKE empty
        row = _make_row(id=1, artist="Stereolab", title="Aluminum Tunes")
        fuzzy_cursor = AsyncMock()
        fuzzy_cursor.fetchall = AsyncMock(return_value=[row])
        db._conn = AsyncMock()
        db._conn.execute = AsyncMock(side_effect=[fts_cursor, like_cursor, fuzzy_cursor])

        with caplog.at_level(logging.DEBUG, logger="library.db"):
            await db.search(query="Stereolab Aluminum")

        records = self._fallback_records(caplog)
        assert records, "expected FTS/LIKE/fuzzy fallback breadcrumbs to be emitted"
        assert all(r.levelno == logging.DEBUG for r in records), [
            (r.levelname, r.getMessage()) for r in records
        ]

    @pytest.mark.asyncio
    async def test_fts_error_fallback_chain_logs_at_debug(self, caplog):
        db = LibraryDB()
        like_cursor = AsyncMock()
        like_cursor.fetchall = AsyncMock(return_value=[])  # LIKE empty
        row = _make_row(id=1, artist="Juana Molina", title="DOGA")
        fuzzy_cursor = AsyncMock()
        fuzzy_cursor.fetchall = AsyncMock(return_value=[row])
        db._conn = AsyncMock()
        # First call (FTS) raises, then LIKE (empty), then fuzzy (row).
        db._conn.execute = AsyncMock(
            side_effect=[Exception("FTS syntax error"), like_cursor, fuzzy_cursor]
        )

        with caplog.at_level(logging.DEBUG, logger="library.db"):
            await db.search(query="Juana Molina DOGA")

        records = self._fallback_records(caplog)
        assert records, "expected error-path FTS/LIKE/fuzzy fallback breadcrumbs to be emitted"
        assert all(r.levelno == logging.DEBUG for r in records), [
            (r.levelname, r.getMessage()) for r in records
        ]


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


class TestFallbackLikeNormalization:
    """Tests that _fallback_like_search normalizes diacritics before the ASCII regex."""

    @pytest.mark.asyncio
    async def test_bjork_produces_correct_like_params(self, mock_library_db_real):
        """'bjork' should normalize to 'bjork', not 'bj rk'."""
        db = mock_library_db_real
        db._conn.execute.return_value.__aenter__.return_value = db._conn.execute.return_value
        db._conn.execute.return_value.fetchall.return_value = []

        await db._fallback_like_search("björk", limit=10)

        # Verify the SQL params contain "%bjork%" not "%bj%" and "%rk%"
        call_args = db._conn.execute.call_args
        params = call_args[0][1]
        assert "%bjork%" in params, f"Expected '%bjork%' in params, got {params}"

    @pytest.mark.asyncio
    async def test_sigur_ros_produces_correct_like_params(self, mock_library_db_real):
        """'sigur ros' should normalize to 'sigur' and 'ros', not 'r' and 's'."""
        db = mock_library_db_real
        db._conn.execute.return_value.__aenter__.return_value = db._conn.execute.return_value
        db._conn.execute.return_value.fetchall.return_value = []

        await db._fallback_like_search("sigur rós", limit=10)

        call_args = db._conn.execute.call_args
        params = call_args[0][1]
        # "sigur" and "ros" should both be present as LIKE params
        param_str = str(params)
        assert "%sigur%" in param_str, f"Expected '%sigur%' in params, got {params}"
        assert "%ros%" in param_str, f"Expected '%ros%' in params, got {params}"

    @pytest.mark.asyncio
    async def test_motorhead_produces_correct_like_params(self, mock_library_db_real):
        """'motorhead' should normalize to 'motorhead', not 'mot rhead'."""
        db = mock_library_db_real
        db._conn.execute.return_value.__aenter__.return_value = db._conn.execute.return_value
        db._conn.execute.return_value.fetchall.return_value = []

        await db._fallback_like_search("motörhead", limit=10)

        call_args = db._conn.execute.call_args
        params = call_args[0][1]
        assert "%motorhead%" in params, f"Expected '%motorhead%' in params, got {params}"


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


class TestFuzzySearchNormalization:
    """Tests that _fuzzy_search normalizes diacritics before the ASCII regex."""

    @pytest.mark.asyncio
    async def test_bjork_fuzzy_uses_correct_prefix(self, mock_library_db_real):
        """'bjork' should use prefix 'bjo' for candidate search, not 'bj'."""
        db = mock_library_db_real
        db._conn.execute.return_value.__aenter__.return_value = db._conn.execute.return_value
        db._conn.execute.return_value.fetchall.return_value = []

        await db._fuzzy_search("björk", limit=10)

        call_args = db._conn.execute.call_args
        params = call_args[0][1]
        # The prefix should be "bjo" (from "bjork"), not "bj" (from "bj rk")
        assert "%bjo%" in params, f"Expected '%bjo%' in params, got {params}"


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

    @pytest.mark.asyncio
    async def test_short_name_not_corrected_to_similar(self):
        """Short name 'Plug' should NOT be corrected to 'Plugz'.

        For short names, a single character difference is proportionally large,
        so the threshold should be raised to prevent false corrections.
        """
        db = LibraryDB()

        class FakeRow:
            def __init__(self, val):
                self.val = val

            def __getitem__(self, idx):
                return self.val

        mock_cursor = AsyncMock()
        mock_cursor.fetchall = AsyncMock(return_value=[FakeRow("Plugz")])
        db._conn = AsyncMock()
        db._conn.execute = AsyncMock(return_value=mock_cursor)

        result = await db.find_similar_artist("Plug")
        assert result is None, (
            f"Expected None (no correction), got '{result}'. "
            "Short names should not be corrected to similar-but-different artists."
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "misspelled, candidate, expected",
        [
            ("Living Color", "Living Colour", "Living Colour"),
            ("Radiohed", "Radiohead", "Radiohead"),
        ],
    )
    async def test_long_name_still_corrected(self, misspelled, candidate, expected):
        """Regression guard: long names with typos are still corrected."""
        db = LibraryDB()

        class FakeRow:
            def __init__(self, val):
                self.val = val

            def __getitem__(self, idx):
                return self.val

        mock_cursor = AsyncMock()
        mock_cursor.fetchall = AsyncMock(return_value=[FakeRow(candidate)])
        db._conn = AsyncMock()
        db._conn.execute = AsyncMock(return_value=mock_cursor)

        result = await db.find_similar_artist(misspelled)
        assert result == expected

    @pytest.mark.asyncio
    async def test_leading_article_skipped_for_candidate_prefix(self):
        """Leading articles should not make the candidate query miss filed artist names."""
        db = LibraryDB()

        class FakeRow:
            def __init__(self, val):
                self.val = val

            def __getitem__(self, idx):
                return self.val

        mock_cursor = AsyncMock()
        mock_cursor.fetchall = AsyncMock(return_value=[FakeRow("Black Dog")])
        db._conn = AsyncMock()
        db._conn.execute = AsyncMock(return_value=mock_cursor)

        result = await db.find_similar_artist("The Bleack Dog")

        assert result == "Black Dog"
        _, params = db._conn.execute.await_args.args
        assert params == ["ble%", "%dog%"]
        assert "the%" not in params

    @pytest.mark.asyncio
    async def test_leading_article_exact_after_strip_is_not_corrected(self):
        """Exact matches after stripping an article should not mutate the parsed artist."""
        db = LibraryDB()

        class FakeRow:
            def __init__(self, val):
                self.val = val

            def __getitem__(self, idx):
                return self.val

        mock_cursor = AsyncMock()
        mock_cursor.fetchall = AsyncMock(return_value=[FakeRow("Black Dog")])
        db._conn = AsyncMock()
        db._conn.execute = AsyncMock(return_value=mock_cursor)

        result = await db.find_similar_artist("The Black Dog")

        assert result is None


# ---------------------------------------------------------------------------
# alternate_artist_name column
# ---------------------------------------------------------------------------


class TestAlternateArtistColumn:
    """Tests for alternate_artist_name column detection and queries."""

    @pytest.mark.asyncio
    async def test_connect_detects_alternate_artist_column(self, tmp_path):
        """connect() should detect the alternate_artist_name column and set flag."""
        import sqlite3

        db_file = tmp_path / "test.db"
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
        conn.commit()
        conn.close()

        db = LibraryDB(db_path=db_file)
        await db.connect()
        assert db._has_alternate_artist is True
        await db.close()

    @pytest.mark.asyncio
    async def test_connect_without_alternate_artist_column(self, tmp_path):
        """connect() should set flag to False when column is absent."""
        import sqlite3

        db_file = tmp_path / "test.db"
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
        conn.commit()
        conn.close()

        db = LibraryDB(db_path=db_file)
        await db.connect()
        assert db._has_alternate_artist is False
        await db.close()

    @pytest.mark.asyncio
    async def test_filtered_search_matches_alternate_artist(self, tmp_path):
        """Artist filter search should also match alternate_artist_name."""
        import sqlite3

        db_file = tmp_path / "test.db"
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
        conn.execute(
            "INSERT INTO library VALUES (1, 'Drum n Bass for Papa', 'Luke Vibert', 'V', 1, 1, 'Electronic', 'CD', 'Plug')"
        )
        conn.execute(
            "INSERT INTO library_fts(rowid, title, artist, alternate_artist_name) VALUES (1, 'Drum n Bass for Papa', 'Luke Vibert', 'Plug')"
        )
        conn.commit()
        conn.close()

        db = LibraryDB(db_path=db_file)
        await db.connect()

        results = await db.search(artist="Plug")
        assert len(results) == 1
        assert results[0].alternate_artist_name == "Plug"
        assert results[0].artist == "Luke Vibert"
        await db.close()

    @pytest.mark.asyncio
    async def test_find_similar_artist_includes_alternates(self, tmp_path):
        """find_similar_artist should also consider alternate_artist_name."""
        import sqlite3

        db_file = tmp_path / "test.db"
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
        conn.execute(
            "INSERT INTO library VALUES (1, 'Album', 'Luke Vibert', 'V', 1, 1, 'Electronic', 'CD', 'Wagon Christ')"
        )
        conn.commit()
        conn.close()

        db = LibraryDB(db_path=db_file)
        await db.connect()

        # "Wagon Chrst" is a typo for "Wagon Christ" - should match the alternate name
        result = await db.find_similar_artist("Wagon Chrst")
        assert result == "Wagon Christ"
        await db.close()


# ---------------------------------------------------------------------------
# label column
# ---------------------------------------------------------------------------


class TestLabelColumn:
    """Tests for label column detection and queries."""

    @pytest.mark.asyncio
    async def test_connect_detects_label_column(self, tmp_path):
        """connect() should detect the label column and set flag."""
        import sqlite3

        db_file = tmp_path / "test.db"
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
                label TEXT
            )
        """)
        conn.execute("""
            CREATE VIRTUAL TABLE library_fts USING fts5(
                title, artist, alternate_artist_name,
                content='library', content_rowid='id'
            )
        """)
        conn.commit()
        conn.close()

        db = LibraryDB(db_path=db_file)
        await db.connect()
        assert db._has_label is True
        await db.close()

    @pytest.mark.asyncio
    async def test_connect_without_label_column(self, tmp_path):
        """connect() should set flag to False when label column is absent."""
        import sqlite3

        db_file = tmp_path / "test.db"
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
        conn.commit()
        conn.close()

        db = LibraryDB(db_path=db_file)
        await db.connect()
        assert db._has_label is False
        await db.close()

    @pytest.mark.asyncio
    async def test_search_returns_label_when_present(self, tmp_path):
        """Search results should include label data when column exists."""
        import sqlite3

        db_file = tmp_path / "test.db"
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
                label TEXT
            )
        """)
        conn.execute("""
            CREATE VIRTUAL TABLE library_fts USING fts5(
                title, artist, alternate_artist_name,
                content='library', content_rowid='id'
            )
        """)
        conn.execute(
            "INSERT INTO library VALUES "
            "(1, 'Moon Pix', 'Cat Power', 'C', 1, 1, 'Rock', 'CD', NULL, 'Matador Records')"
        )
        conn.execute(
            "INSERT INTO library_fts(rowid, title, artist, alternate_artist_name) "
            "VALUES (1, 'Moon Pix', 'Cat Power', NULL)"
        )
        conn.commit()
        conn.close()

        db = LibraryDB(db_path=db_file)
        await db.connect()
        results = await db.search(artist="Cat Power")
        assert len(results) == 1
        assert results[0].label == "Matador Records"
        await db.close()

    def test_select_columns_includes_label(self):
        db = LibraryDB()
        db._has_alternate_artist = True
        db._has_label = True
        cols = db._select_columns()
        assert "label" in cols

    def test_select_columns_excludes_label(self):
        db = LibraryDB()
        db._has_alternate_artist = True
        db._has_label = False
        cols = db._select_columns()
        assert "label" not in cols


# ---------------------------------------------------------------------------
# compilation_track_artist table
# ---------------------------------------------------------------------------


def _create_db_with_compilation_track_artists(db_file, *, include_cta_table: bool = True):
    """Create a test library.db with optional compilation_track_artist table."""
    import sqlite3

    conn = sqlite3.connect(db_file)
    conn.execute("""
        CREATE TABLE library (
            id INTEGER PRIMARY KEY, title TEXT, artist TEXT,
            call_letters TEXT, artist_call_number INTEGER,
            release_call_number INTEGER, genre TEXT, format TEXT,
            alternate_artist_name TEXT
        )
    """)
    conn.execute("""
        CREATE VIRTUAL TABLE library_fts USING fts5(
            title, artist, alternate_artist_name,
            content='library', content_rowid='id'
        )
    """)
    # A regular artist release
    conn.execute(
        "INSERT INTO library VALUES "
        "(1, 'Aluminum Tunes', 'Stereolab', 'S', 1, 1, 'Rock', 'CD', NULL)"
    )
    conn.execute(
        "INSERT INTO library_fts(rowid, title, artist, alternate_artist_name) "
        "VALUES (1, 'Aluminum Tunes', 'Stereolab', NULL)"
    )
    # A compilation release
    conn.execute(
        "INSERT INTO library VALUES "
        "(2, 'Vintage Palmwine', 'various', 'Z-', 0, 1, 'World', 'CD', NULL)"
    )
    conn.execute(
        "INSERT INTO library_fts(rowid, title, artist, alternate_artist_name) "
        "VALUES (2, 'Vintage Palmwine', 'various', NULL)"
    )

    if include_cta_table:
        conn.execute("""
            CREATE TABLE compilation_track_artist (
                library_release_id INTEGER NOT NULL,
                artist_name TEXT NOT NULL,
                track_title TEXT
            )
        """)
        conn.execute("CREATE INDEX idx_cta_artist ON compilation_track_artist(artist_name)")
        conn.execute("INSERT INTO compilation_track_artist VALUES (2, 'Koo Nimo', 'Odo Akosomo')")
        conn.execute("INSERT INTO compilation_track_artist VALUES (2, 'T.O. Jazz', 'Waytime Ama')")
        conn.execute("INSERT INTO compilation_track_artist VALUES (2, 'Kwaa Mensah', 'Obra Ye Ku')")

    conn.commit()
    conn.close()


class TestCompilationTrackArtistSearch:
    @pytest.mark.asyncio
    async def test_connect_detects_table(self, tmp_path):
        db_file = tmp_path / "test.db"
        _create_db_with_compilation_track_artists(db_file, include_cta_table=True)
        db = LibraryDB(db_path=db_file)
        await db.connect()
        assert db._has_compilation_track_artist is True
        await db.close()

    @pytest.mark.asyncio
    async def test_connect_without_table(self, tmp_path):
        db_file = tmp_path / "test.db"
        _create_db_with_compilation_track_artists(db_file, include_cta_table=False)
        db = LibraryDB(db_path=db_file)
        await db.connect()
        assert db._has_compilation_track_artist is False
        await db.close()

    @pytest.mark.asyncio
    async def test_artist_search_surfaces_compilation(self, tmp_path):
        """Searching for 'Koo Nimo' should return 'Vintage Palmwine' compilation."""
        db_file = tmp_path / "test.db"
        _create_db_with_compilation_track_artists(db_file)
        db = LibraryDB(db_path=db_file)
        await db.connect()
        results = await db.search(artist="Koo Nimo")
        assert len(results) >= 1
        titles = [r.title for r in results]
        assert "Vintage Palmwine" in titles
        await db.close()

    @pytest.mark.asyncio
    async def test_artist_search_still_finds_regular_releases(self, tmp_path):
        """Searching for 'Stereolab' should still find their own releases."""
        db_file = tmp_path / "test.db"
        _create_db_with_compilation_track_artists(db_file)
        db = LibraryDB(db_path=db_file)
        await db.connect()
        results = await db.search(artist="Stereolab")
        assert len(results) >= 1
        titles = [r.title for r in results]
        assert "Aluminum Tunes" in titles
        await db.close()

    @pytest.mark.asyncio
    async def test_graceful_degradation_without_table(self, tmp_path):
        """Without compilation_track_artist table, search still works normally."""
        db_file = tmp_path / "test.db"
        _create_db_with_compilation_track_artists(db_file, include_cta_table=False)
        db = LibraryDB(db_path=db_file)
        await db.connect()
        # Should NOT find Vintage Palmwine for Koo Nimo without the table
        results = await db.search(artist="Koo Nimo")
        titles = [r.title for r in results]
        assert "Vintage Palmwine" not in titles
        # Should still find regular releases
        results = await db.search(artist="Stereolab")
        assert len(results) >= 1
        await db.close()

    @pytest.mark.asyncio
    async def test_find_similar_artist_includes_compilation_artists(self, tmp_path):
        """find_similar_artist should check compilation_track_artist for candidates."""
        db_file = tmp_path / "test.db"
        _create_db_with_compilation_track_artists(db_file)
        db = LibraryDB(db_path=db_file)
        await db.connect()
        # "Koo Nimo" is only in compilation_track_artist, not in library.artist
        result = await db.find_similar_artist("Koo Nmo")  # typo
        assert result == "Koo Nimo"
        await db.close()


# ---------------------------------------------------------------------------
# In-memory caching
# ---------------------------------------------------------------------------


class TestLibraryDBSearchCache:
    @pytest.mark.asyncio
    async def test_search_cache_hit_skips_db(self):
        """Second identical search should return cached result without DB call."""
        db = LibraryDB()
        row = _make_row(id=1, artist="Stereolab", title="Aluminum Tunes")
        mock_cursor = AsyncMock()
        mock_cursor.fetchall = AsyncMock(return_value=[row])
        db._conn = AsyncMock()
        db._conn.execute = AsyncMock(return_value=mock_cursor)

        result1 = await db.search(query="Stereolab Aluminum")
        result2 = await db.search(query="Stereolab Aluminum")

        assert len(result1) == 1
        assert result1 == result2
        # DB should only have been called once (second call uses cache)
        assert db._conn.execute.call_count == 1

    @pytest.mark.asyncio
    async def test_search_cache_different_queries_both_hit_db(self):
        """Different queries should both hit the DB."""
        db = LibraryDB()
        row = _make_row(id=1, artist="Stereolab", title="Aluminum Tunes")
        mock_cursor = AsyncMock()
        mock_cursor.fetchall = AsyncMock(return_value=[row])
        db._conn = AsyncMock()
        db._conn.execute = AsyncMock(return_value=mock_cursor)

        await db.search(query="Stereolab")
        await db.search(query="Cat Power")

        assert db._conn.execute.call_count == 2

    @pytest.mark.asyncio
    async def test_search_cache_empty_results_cached(self):
        """Empty results should also be cached."""
        db = LibraryDB()
        mock_cursor = AsyncMock()
        mock_cursor.fetchall = AsyncMock(return_value=[])
        db._conn = AsyncMock()
        db._conn.execute = AsyncMock(return_value=mock_cursor)

        result1 = await db.search(
            query="Nonexistent", fallback_to_like=False, fallback_to_fuzzy=False
        )
        result2 = await db.search(
            query="Nonexistent", fallback_to_like=False, fallback_to_fuzzy=False
        )

        assert result1 == []
        assert result2 == []
        assert db._conn.execute.call_count == 1


class TestLibraryDBFindSimilarArtistCache:
    @pytest.mark.asyncio
    async def test_find_similar_artist_cache_hit(self):
        """Second identical lookup should return cached result."""
        db = LibraryDB()

        class FakeRow:
            def __init__(self, val):
                self.val = val

            def __getitem__(self, idx):
                return self.val

        mock_cursor = AsyncMock()
        mock_cursor.fetchall = AsyncMock(return_value=[FakeRow("Living Colour")])
        db._conn = AsyncMock()
        db._conn.execute = AsyncMock(return_value=mock_cursor)

        result1 = await db.find_similar_artist("Living Color")
        result2 = await db.find_similar_artist("Living Color")

        assert result1 == "Living Colour"
        assert result1 == result2
        assert db._conn.execute.call_count == 1

    @pytest.mark.asyncio
    async def test_find_similar_artist_caches_none(self):
        """None results (no correction) should also be cached."""
        db = LibraryDB()
        mock_cursor = AsyncMock()
        mock_cursor.fetchall = AsyncMock(return_value=[])
        db._conn = AsyncMock()
        db._conn.execute = AsyncMock(return_value=mock_cursor)

        result1 = await db.find_similar_artist("Nonexistent")
        result2 = await db.find_similar_artist("Nonexistent")

        assert result1 is None
        assert result2 is None
        assert db._conn.execute.call_count == 1


class TestLibraryDBCacheInvalidation:
    @pytest.mark.asyncio
    async def test_close_clears_caches(self):
        """Closing the DB should clear all library caches."""
        from library.db import _get_library_caches

        artist_cache, search_cache = _get_library_caches()
        artist_cache["test_key"] = "test_value"
        search_cache["test_key"] = "test_value"

        db = LibraryDB()
        db._conn = AsyncMock()
        await db.close()

        # Caches should be cleared after close
        artist_cache, search_cache = _get_library_caches()
        assert len(artist_cache) == 0
        assert len(search_cache) == 0


class TestStreamingLinks:
    """Tests for streaming_links table access."""

    @pytest.mark.asyncio
    async def test_get_streaming_links_returns_urls(self, tmp_path):
        """When streaming_links has a row, return a dict of URLs."""
        db_path = tmp_path / "test.db"
        import aiosqlite

        async with aiosqlite.connect(db_path) as conn:
            await conn.execute(
                "CREATE TABLE library (id INTEGER PRIMARY KEY, title TEXT, artist TEXT, "
                "call_letters TEXT, artist_call_number INTEGER, release_call_number INTEGER, "
                "genre TEXT, format TEXT)"
            )
            await conn.execute(
                "INSERT INTO library VALUES (100, 'Aluminum Tunes', 'Stereolab', "
                "'St', 1, 1, 'Rock', 'CD')"
            )
            await conn.execute(
                "CREATE TABLE streaming_links ("
                "library_id INTEGER PRIMARY KEY, spotify_url TEXT, apple_music_url TEXT, "
                "deezer_url TEXT, bandcamp_url TEXT, tidal_url TEXT, "
                "youtube_music_url TEXT, soundcloud_url TEXT)"
            )
            await conn.execute(
                "INSERT INTO streaming_links VALUES "
                "(100, 'https://open.spotify.com/album/abc', NULL, NULL, "
                "'https://stereolab.bandcamp.com/album/aluminum-tunes', NULL, NULL, NULL)"
            )
            await conn.commit()

        db = LibraryDB(db_path)
        await db.connect()
        links = await db.get_streaming_links(100)
        await db.close()

        assert links is not None
        assert links["spotify_url"] == "https://open.spotify.com/album/abc"
        assert links["bandcamp_url"] == "https://stereolab.bandcamp.com/album/aluminum-tunes"
        assert links["apple_music_url"] is None

    @pytest.mark.asyncio
    async def test_get_streaming_links_returns_none_when_not_found(self, tmp_path):
        """When no streaming_links row exists, return None."""
        db_path = tmp_path / "test.db"
        import aiosqlite

        async with aiosqlite.connect(db_path) as conn:
            await conn.execute(
                "CREATE TABLE library (id INTEGER PRIMARY KEY, title TEXT, artist TEXT, "
                "call_letters TEXT, artist_call_number INTEGER, release_call_number INTEGER, "
                "genre TEXT, format TEXT)"
            )
            await conn.execute(
                "CREATE TABLE streaming_links ("
                "library_id INTEGER PRIMARY KEY, spotify_url TEXT, apple_music_url TEXT, "
                "deezer_url TEXT, bandcamp_url TEXT, tidal_url TEXT, "
                "youtube_music_url TEXT, soundcloud_url TEXT)"
            )
            await conn.commit()

        db = LibraryDB(db_path)
        await db.connect()
        links = await db.get_streaming_links(999)
        await db.close()

        assert links is None

    @pytest.mark.asyncio
    async def test_get_streaming_links_graceful_without_table(self, tmp_path):
        """When streaming_links table doesn't exist, return None."""
        db_path = tmp_path / "test.db"
        import aiosqlite

        async with aiosqlite.connect(db_path) as conn:
            await conn.execute(
                "CREATE TABLE library (id INTEGER PRIMARY KEY, title TEXT, artist TEXT, "
                "call_letters TEXT, artist_call_number INTEGER, release_call_number INTEGER, "
                "genre TEXT, format TEXT)"
            )
            await conn.commit()

        db = LibraryDB(db_path)
        await db.connect()
        links = await db.get_streaming_links(100)
        await db.close()

        assert links is None

    @pytest.mark.asyncio
    async def test_all_null_urls_returns_dict_with_nones(self, tmp_path):
        """Insert streaming_links row where every URL is NULL. Returns dict with all None values."""
        db_path = tmp_path / "test.db"
        import aiosqlite

        async with aiosqlite.connect(db_path) as conn:
            await conn.execute(
                "CREATE TABLE library (id INTEGER PRIMARY KEY, title TEXT, artist TEXT, "
                "call_letters TEXT, artist_call_number INTEGER, release_call_number INTEGER, "
                "genre TEXT, format TEXT)"
            )
            await conn.execute(
                "INSERT INTO library VALUES (100, 'Confield', 'Autechre', "
                "'EL', 16, 1, 'Electronic', 'CD')"
            )
            await conn.execute(
                "CREATE TABLE streaming_links ("
                "library_id INTEGER PRIMARY KEY, spotify_url TEXT, apple_music_url TEXT, "
                "deezer_url TEXT, bandcamp_url TEXT, tidal_url TEXT, "
                "youtube_music_url TEXT, soundcloud_url TEXT)"
            )
            await conn.execute("INSERT INTO streaming_links (library_id) VALUES (100)")
            await conn.commit()

        db = LibraryDB(db_path)
        await db.connect()
        links = await db.get_streaming_links(100)
        await db.close()

        assert links is not None
        assert all(v is None for v in links.values())

    @pytest.mark.asyncio
    async def test_has_streaming_links_flag_true_on_connect(self, tmp_path):
        """Create DB with streaming_links table. After connect(), flag is True."""
        db_path = tmp_path / "test.db"
        import aiosqlite

        async with aiosqlite.connect(db_path) as conn:
            await conn.execute(
                "CREATE TABLE library (id INTEGER PRIMARY KEY, title TEXT, artist TEXT, "
                "call_letters TEXT, artist_call_number INTEGER, release_call_number INTEGER, "
                "genre TEXT, format TEXT)"
            )
            await conn.execute(
                "CREATE TABLE streaming_links ("
                "library_id INTEGER PRIMARY KEY, spotify_url TEXT, apple_music_url TEXT, "
                "deezer_url TEXT, bandcamp_url TEXT, tidal_url TEXT, "
                "youtube_music_url TEXT, soundcloud_url TEXT)"
            )
            await conn.commit()

        db = LibraryDB(db_path)
        await db.connect()
        assert db._has_streaming_links is True
        await db.close()

    @pytest.mark.asyncio
    async def test_has_streaming_links_flag_false_without_table(self, tmp_path):
        """Create DB without streaming_links table. Flag is False."""
        db_path = tmp_path / "test.db"
        import aiosqlite

        async with aiosqlite.connect(db_path) as conn:
            await conn.execute(
                "CREATE TABLE library (id INTEGER PRIMARY KEY, title TEXT, artist TEXT, "
                "call_letters TEXT, artist_call_number INTEGER, release_call_number INTEGER, "
                "genre TEXT, format TEXT)"
            )
            await conn.commit()

        db = LibraryDB(db_path)
        await db.connect()
        assert db._has_streaming_links is False
        await db.close()

    @pytest.mark.asyncio
    async def test_returns_none_when_conn_is_none(self):
        """Set db._conn = None. get_streaming_links() returns None."""
        db = LibraryDB()
        db._conn = None
        db._has_streaming_links = True

        result = await db.get_streaming_links(100)
        assert result is None


class TestStreamingStatus:
    """Tests for get_streaming_status() batch method."""

    @pytest.mark.asyncio
    async def test_returns_true_for_ids_with_links(self, tmp_path):
        db_path = tmp_path / "test.db"
        import aiosqlite

        async with aiosqlite.connect(db_path) as conn:
            await conn.execute(
                "CREATE TABLE library (id INTEGER PRIMARY KEY, title TEXT, artist TEXT, "
                "call_letters TEXT, artist_call_number INTEGER, release_call_number INTEGER, "
                "genre TEXT, format TEXT)"
            )
            await conn.execute(
                "CREATE TABLE streaming_links ("
                "library_id INTEGER PRIMARY KEY, spotify_url TEXT, apple_music_url TEXT, "
                "deezer_url TEXT, bandcamp_url TEXT, tidal_url TEXT, "
                "youtube_music_url TEXT, soundcloud_url TEXT)"
            )
            await conn.execute(
                "INSERT INTO streaming_links (library_id, spotify_url) VALUES (100, 'https://x')"
            )
            await conn.execute(
                "INSERT INTO streaming_links (library_id, bandcamp_url) VALUES (200, 'https://y')"
            )
            await conn.commit()

        db = LibraryDB(db_path)
        await db.connect()
        status = await db.get_streaming_status([100, 200, 300])
        await db.close()

        assert status == {100: True, 200: True}

    @pytest.mark.asyncio
    async def test_returns_empty_for_missing_ids(self, tmp_path):
        db_path = tmp_path / "test.db"
        import aiosqlite

        async with aiosqlite.connect(db_path) as conn:
            await conn.execute(
                "CREATE TABLE library (id INTEGER PRIMARY KEY, title TEXT, artist TEXT, "
                "call_letters TEXT, artist_call_number INTEGER, release_call_number INTEGER, "
                "genre TEXT, format TEXT)"
            )
            await conn.execute(
                "CREATE TABLE streaming_links ("
                "library_id INTEGER PRIMARY KEY, spotify_url TEXT, apple_music_url TEXT, "
                "deezer_url TEXT, bandcamp_url TEXT, tidal_url TEXT, "
                "youtube_music_url TEXT, soundcloud_url TEXT)"
            )
            await conn.commit()

        db = LibraryDB(db_path)
        await db.connect()
        status = await db.get_streaming_status([999])
        await db.close()

        assert status == {}

    @pytest.mark.asyncio
    async def test_graceful_without_table(self, tmp_path):
        db_path = tmp_path / "test.db"
        import aiosqlite

        async with aiosqlite.connect(db_path) as conn:
            await conn.execute(
                "CREATE TABLE library (id INTEGER PRIMARY KEY, title TEXT, artist TEXT, "
                "call_letters TEXT, artist_call_number INTEGER, release_call_number INTEGER, "
                "genre TEXT, format TEXT)"
            )
            await conn.commit()

        db = LibraryDB(db_path)
        await db.connect()
        status = await db.get_streaming_status([100])
        await db.close()

        assert status == {}

    @pytest.mark.asyncio
    async def test_streaming_status_empty_list(self, tmp_path):
        """get_streaming_status([]) returns {}."""
        db_path = tmp_path / "test.db"
        import aiosqlite

        async with aiosqlite.connect(db_path) as conn:
            await conn.execute(
                "CREATE TABLE library (id INTEGER PRIMARY KEY, title TEXT, artist TEXT, "
                "call_letters TEXT, artist_call_number INTEGER, release_call_number INTEGER, "
                "genre TEXT, format TEXT)"
            )
            await conn.execute(
                "CREATE TABLE streaming_links ("
                "library_id INTEGER PRIMARY KEY, spotify_url TEXT, apple_music_url TEXT, "
                "deezer_url TEXT, bandcamp_url TEXT, tidal_url TEXT, "
                "youtube_music_url TEXT, soundcloud_url TEXT)"
            )
            await conn.commit()

        db = LibraryDB(db_path)
        await db.connect()
        status = await db.get_streaming_status([])
        await db.close()

        assert status == {}

    @pytest.mark.asyncio
    async def test_streaming_status_when_flag_false(self):
        """_has_streaming_links=False returns {}."""
        db = LibraryDB()
        db._conn = AsyncMock()
        db._has_streaming_links = False

        status = await db.get_streaming_status([100, 200])
        assert status == {}

    @pytest.mark.asyncio
    async def test_streaming_status_when_conn_none(self):
        """_conn=None returns {}."""
        db = LibraryDB()
        db._conn = None
        db._has_streaming_links = True

        status = await db.get_streaming_status([100, 200])
        assert status == {}
