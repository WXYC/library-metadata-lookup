"""Unit tests for library/db.py."""

import asyncio
import logging
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
import pytest

from library.db import (
    _ARTIST_CORRECTION_DISTANCE,
    LIBRARY_FTS_CREATE_SQL,
    LibraryDB,
    _artist_correction_edit_cap,
    _fts_normalize,
    _scan_artist_name_pool,
    _to_fts_match_query,
    clear_library_caches,
)

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


# ---------------------------------------------------------------------------
# _fts_normalize
# ---------------------------------------------------------------------------


class TestFtsNormalize:
    """Pure helper shared by ``_fallback_like_search`` and ``_fuzzy_search``:
    strips diacritics via wxyc_etl's ``to_match_form`` and then collapses any
    remaining non-alphanumeric character to a space, so the LIKE/fuzzy
    fallback chain tokenizes on plain ASCII words."""

    @pytest.mark.parametrize(
        "query, expected",
        [
            # Diacritics are stripped to their ASCII base letters.
            ("Nilüfer Yanya", "nilufer yanya"),
            ("Hermanos Gutiérrez", "hermanos gutierrez"),
            # Punctuation collapses to whitespace rather than disappearing,
            # so words on either side don't fuse together.
            ("Chuquimamani-Condori", "chuquimamani condori"),
            ("Duke Ellington & John Coltrane", "duke ellington   john coltrane"),
            # Empty and whitespace-only input yields empty output.
            ("", ""),
            ("   ", ""),
        ],
    )
    def test_normalizes_query(self, query, expected):
        assert _fts_normalize(query) == expected


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
        mock_cursor.fetchone = AsyncMock(return_value=None)

        result = await db.find_similar_artist("Nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_correction_found(self, tmp_path):
        """Built through ``connect()`` rather than by assigning ``_conn``.

        LML#1245 moved candidate generation from a per-call SQL query to the
        connect-time pool, so a ``LibraryDB`` handed a mock connection has no
        pool, is not ``usable``, and correctly declines to correct. A mocked
        fetchall no longer stands in for a catalog.
        """
        clear_library_caches()
        db = await _connected_db(tmp_path, ["Nilufer Yanya", "Sessa"])
        try:
            assert await db.find_similar_artist("Nilufer Yanyaa") == "Nilufer Yanya"
        finally:
            await db.close()

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
        mock_cursor.fetchone = AsyncMock(return_value=None)

        result = await db.find_similar_artist("Queen")
        assert result is None

    @pytest.mark.asyncio
    async def test_null_source_values_do_not_reach_the_pool(self, tmp_path):
        """A NULL in an optional source column is filtered at build time.

        Previously asserted on a mocked row set containing ``None``; the
        exclusion now happens in the pool build's ``IS NOT NULL``, so this
        seeds a real catalog whose ``album_artist`` is NULL on one row and
        checks the scan is unaffected.
        """
        clear_library_caches()
        db = await _connected_db(
            tmp_path,
            ["Jessica Pratt", "Sessa"],
            with_album_artist=True,
            album_artists=[None, None],
        )
        try:
            assert None not in db._artist_name_pool_names
            assert await db.find_similar_artist("Jessica Prattt") == "Jessica Pratt"
        finally:
            await db.close()

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
        mock_cursor.fetchone = AsyncMock(return_value=None)

        result = await db.find_similar_artist("Plug")
        assert result is None, (
            f"Expected None (no correction), got '{result}'. "
            "Short names should not be corrected to similar-but-different artists."
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "misspelled, candidate",
        [
            ("Chuquimamani-Condor", "Chuquimamani-Condori"),
            ("Hermanos Gutierez", "Hermanos Gutierrez"),
            ("Duke Ellingtno", "Duke Ellington"),
        ],
    )
    async def test_long_name_still_corrected(self, tmp_path, misspelled, candidate):
        """Regression guard: long names with typos are still corrected.

        Now over a real catalog -- the correction has to survive the LML#1245
        margin and edit-cap guards, not just clear the ratio floor.
        """
        clear_library_caches()
        db = await _connected_db(tmp_path, [candidate, "Sessa"])
        try:
            assert await db.find_similar_artist(misspelled) == candidate
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_leading_article_query_still_reaches_a_filed_name(self, tmp_path):
        """A query carrying a leading article still matches the filed form.

        This previously asserted on the LIKE parameter list -- ``["ble%",
        "%dog%", ...]`` with no ``"the%"`` -- pinning the shape of a prefilter
        LML#1245 deleted. What that prefilter existed to achieve is the
        behaviour kept here: the article must not stop the query reaching a
        catalog name filed without one. The full-pool scan gets this from the
        second scoring axis rather than from candidate-term selection.
        """
        clear_library_caches()
        db = await _connected_db(tmp_path, ["Black Dog", "Sessa"])
        try:
            assert await db.find_similar_artist("The Bleack Dog") == "Black Dog"
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_leading_article_exact_after_strip_is_not_corrected(self, tmp_path):
        """Exact matches after stripping an article should not mutate the parsed artist."""
        clear_library_caches()
        db = await _connected_db(tmp_path, ["Black Dog", "Sessa"])
        try:
            assert await db.find_similar_artist("The Black Dog") is None
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_exact_library_artist_short_circuits_before_candidate_search(self):
        """An artist that's an exact library match is never 'corrected' to a
        different, merely-similar-scoring distractor.

        Regression for LML#857: the "John Lennon" -> "Don Lennon" false
        correction. Even if the candidate query would (nondeterministically)
        surface a distractor as the only row, the exact-match short-circuit
        must fire first and the candidate query must never run.

        LML#1248: the exact-check is answered from the in-memory pool built
        once at ``connect()`` time rather than a per-call SQL scan, so the
        short-circuit now issues *no* SQL at all -- not even the single
        exact-check query the old implementation ran.
        """
        db = LibraryDB()
        db._conn = AsyncMock()
        db._artist_name_pool_lower = {"john lennon"}
        db._artist_name_pool_usable = True

        result = await db.find_similar_artist("John Lennon")

        assert result is None
        db._conn.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_exact_library_artist_resolves_via_real_db(self, tmp_path):
        """John Lennon / Don Lennon reproduces the exact bug scenario end to end.

        A distractor artist ("Don Lennon") that would otherwise win the
        candidate-cap race must not cause "John Lennon" -- already an exact
        library artist -- to be corrected away.

        LML#1248: the short-circuit is now answered from the in-memory pool,
        so it issues no SQL at all -- not even the single exact-check query
        the old implementation ran. The ``execute`` spy below pins that.
        """
        db_file = tmp_path / "test.db"
        _create_library_db(db_file, ["John Lennon", "Don Lennon"])

        db = LibraryDB(db_path=db_file)
        await db.connect()

        async def _fail_if_called(*args, **kwargs):
            raise AssertionError("_artist_exists_exactly must not issue SQL")

        db._conn.execute = _fail_if_called  # type: ignore[method-assign]

        result = await db.find_similar_artist("John Lennon")
        assert result is None
        await db.close()


# ---------------------------------------------------------------------------
# In-memory exact-name pool (LML#1248)
# ---------------------------------------------------------------------------


def _nth(values, index):
    """``values[index]`` when supplied and long enough, else ``None``."""
    if not values or index >= len(values):
        return None
    return values[index]


def _create_library_db(
    db_file,
    artists,
    *,
    with_alternate_artist=False,
    with_album_artist=False,
    with_cta_table=False,
    cta_artist_names=None,
    cta_missing_artist_name_column=False,
    omit_artist_column=False,
    alternate_artist_names=None,
    album_artists=None,
):
    """Build a minimal library.db for pool-construction tests.

    ``artists`` seeds ``library.artist``; ``alternate_artist_names`` and
    ``album_artists`` are parallel lists seeding those optional columns (short
    lists pad with ``None``), so a test asserting the pool unions all four
    sources can actually put a distinct name in each. The optional-column and
    optional-table flags mirror the schema variations ``adopt_connection``
    probes for.

    ``cta_missing_artist_name_column=True`` creates a
    ``compilation_track_artist`` table that exists but has no ``artist_name``
    column -- the drift a table-name-only probe cannot catch.
    ``omit_artist_column=True`` drops ``library.artist`` entirely, so the pool
    build's one indispensable source fails.
    """
    import sqlite3

    conn = sqlite3.connect(db_file)
    columns = [
        "id INTEGER PRIMARY KEY",
        "title TEXT",
        "artist TEXT",
        "call_letters TEXT",
        "artist_call_number INTEGER",
        "release_call_number INTEGER",
        "genre TEXT",
        "format TEXT",
    ]
    if omit_artist_column:
        columns.remove("artist TEXT")
    if with_alternate_artist:
        columns.append("alternate_artist_name TEXT")
    if with_album_artist:
        columns.append("album_artist TEXT")
    conn.execute(f"CREATE TABLE library ({', '.join(columns)})")
    if not omit_artist_column:
        # The shared DDL, not a hand-rolled one: its unicode61 tokenizer
        # config is load-bearing (see LIBRARY_FTS_TOKENIZER) and a local copy
        # would silently drift from what production indexes.
        conn.execute(LIBRARY_FTS_CREATE_SQL)

    for i, artist in enumerate(artists, start=1):
        values = [str(i), f"Album {i}", "A", str(i), str(i), "Rock", "LP"]
        if not omit_artist_column:
            # Positional insert: artist sits third, after id and title. Build
            # it by position rather than removing it by value -- an artist
            # literally named "1" would otherwise delete the id.
            values.insert(2, artist)
        if with_alternate_artist:
            values.append(_nth(alternate_artist_names, i - 1))
        if with_album_artist:
            values.append(_nth(album_artists, i - 1))
        placeholders = ", ".join("?" for _ in values)
        conn.execute(f"INSERT INTO library VALUES ({placeholders})", values)

    if with_cta_table:
        if cta_missing_artist_name_column:
            # Table exists (passes the sqlite_master name probe) but lacks
            # artist_name -- schema drift the name-probe alone can't catch.
            conn.execute("""
                CREATE TABLE compilation_track_artist (
                    library_release_id INTEGER NOT NULL,
                    track_title TEXT
                )
            """)
        else:
            conn.execute("""
                CREATE TABLE compilation_track_artist (
                    library_release_id INTEGER NOT NULL,
                    artist_name TEXT NOT NULL,
                    track_title TEXT
                )
            """)
            for name in cta_artist_names or []:
                conn.execute(
                    "INSERT INTO compilation_track_artist VALUES (?, ?, ?)",
                    (1, name, "Track"),
                )

    conn.commit()
    conn.close()


class TestArtistNameExactMatchPool:
    """LML#1248: the exact-existence check is answered from an in-memory
    ``set[str]`` built once at ``connect()``, not a per-call SQL scan."""

    @pytest.mark.asyncio
    async def test_predicate_not_widened_by_a_stripped_catalog_pool(self, tmp_path):
        """Only the QUERY is article-stripped, never the catalog names.

        A catalog holding only "The Sea and Cake" (raw, unstripped) must NOT
        report the bare query "Sea and Cake" as an exact match. If the pool
        were ALSO built from article-stripped catalog names, it would hold
        ``"sea and cake"``, this would wrongly return True, and a correction
        that is free to fire today would be silently suppressed. Both query
        forms are identical here because "Sea and Cake" carries no leading
        article of its own to strip.

        Note this builds the pool from a real database rather than injecting
        one: injecting the pool would test the membership expression while
        leaving ``_build_artist_name_pool`` -- the thing that could actually
        introduce the stripping -- unguarded.
        """
        db_file = tmp_path / "test.db"
        _create_library_db(db_file, ["The Sea and Cake"])
        db = LibraryDB(db_path=db_file)
        await db.connect()

        assert db._artist_name_pool_lower == {"the sea and cake"}
        assert await db._artist_exists_exactly("sea and cake", "sea and cake") is False
        # ...and the SQL it replaced agrees, which is the point.
        assert await db._exists_exactly_via_sql("sea and cake", "sea and cake") is False
        await db.close()

    @pytest.mark.asyncio
    async def test_predicate_unchanged_for_ticket_example(self, tmp_path):
        """Ticket-specified negative test, in the shape the ticket meant.

        A catalog holding the bare "Sea and Cake" against a query of "The Sea
        and Cake" must behave exactly as it does today. Verified against the
        pre-#1248 SQL: the query's article-stripped form ("sea and cake")
        matches the raw catalog value, so the exact-check already fires and no
        correction is offered. #1248 must reproduce that, not flip it.

        This is the example the ticket used to illustrate the widening hazard,
        and it does NOT discriminate a correct implementation from a widened
        one -- it returns True either way. Kept as coverage of today's
        behavior; ``test_predicate_not_widened_by_a_stripped_catalog_pool`` is
        the actual guard.
        """
        db_file = tmp_path / "test.db"
        _create_library_db(db_file, ["Sea and Cake"])
        db = LibraryDB(db_path=db_file)
        await db.connect()

        assert await db._artist_exists_exactly("the sea and cake", "sea and cake") is True
        assert await db.find_similar_artist("The Sea and Cake") is None
        await db.close()

    @pytest.mark.asyncio
    async def test_old_schema_missing_optional_columns_still_answers_exact_check(self, tmp_path):
        """A library.db predating alternate_artist_name/album_artist/
        compilation_track_artist must still short-circuit on its base
        ``library.artist`` column alone."""
        db_file = tmp_path / "test.db"
        _create_library_db(db_file, ["Stereolab"])
        db = LibraryDB(db_path=db_file)
        await db.connect()

        assert db._has_alternate_artist is False
        assert db._has_album_artist is False
        assert db._has_compilation_track_artist is False
        assert db._artist_name_pool_lower == {"stereolab"}

        result = await db.find_similar_artist("Stereolab")

        assert result is None
        await db.close()

    @pytest.mark.asyncio
    async def test_pool_includes_all_four_present_sources(self, tmp_path):
        """The pool is the union of all four sources, one distinct name each.

        Each source seeds a name that appears in NO other source, so dropping
        any one branch from ``_artist_name_sources`` fails this test. Seeding
        the optional columns NULL, as an earlier version did, made the
        alternate_artist_name and album_artist halves unguarded.
        """
        db_file = tmp_path / "test.db"
        _create_library_db(
            db_file,
            ["Stereolab"],
            with_alternate_artist=True,
            alternate_artist_names=["Juana Molina"],
            with_album_artist=True,
            album_artists=["Jessica Pratt"],
            with_cta_table=True,
            cta_artist_names=["Koo Nimo"],
        )
        db = LibraryDB(db_path=db_file)
        await db.connect()

        assert db._artist_name_pool_lower == {
            "stereolab",
            "juana molina",
            "jessica pratt",
            "koo nimo",
        }
        assert db._artist_name_pool_usable is True

        # Answered purely from the pool for a compilation-only artist too.
        result = await db.find_similar_artist("Koo Nimo")
        assert result is None
        await db.close()

    @pytest.mark.asyncio
    async def test_compilation_track_artist_schema_drift_is_caught_by_the_probe(self, tmp_path):
        """A same-named, differently-shaped CTA table must be detected as absent.

        ``_has_compilation_track_artist`` used to be set from a
        ``sqlite_master`` table-NAME probe, which a table lacking
        ``artist_name`` sails straight past -- and THREE readers then select
        that column: the pool build, the candidate UNION in
        ``_find_similar_artist_uncached``, and ``search()``'s CTA join. Only
        the first was guarded, so drift still raised
        ``no such column: artist_name`` from the other two, on the request
        path. Probing the column instead makes the flag mean what its readers
        assume and fixes all three at once.
        """
        db_file = tmp_path / "test.db"
        _create_library_db(
            db_file,
            ["Stereolab"],
            with_cta_table=True,
            cta_missing_artist_name_column=True,
        )
        db = LibraryDB(db_path=db_file)
        await db.connect()  # must not raise

        assert db._has_compilation_track_artist is False
        assert ("compilation_track_artist", "artist_name") not in db._artist_name_sources()
        # The pool is complete for the sources that do exist, so no fallback.
        assert db._artist_name_pool_lower == {"stereolab"}
        assert db._artist_name_pool_usable is True

        # The two other readers of artist_name no longer raise either.
        assert await db.find_similar_artist("Stereolb") == "Stereolab"
        assert await db.search(artist="Stereolab") != []
        result = await db.find_similar_artist("Stereolab")
        assert result is None
        await db.close()

    @pytest.mark.asyncio
    async def test_unbuildable_pool_falls_back_to_sql_rather_than_failing_open(self, tmp_path):
        """A source that will not load costs latency, never correctness.

        See :meth:`LibraryDB._build_artist_name_pool` for why neither serving
        a partial pool nor raising is acceptable here.
        """
        db_file = tmp_path / "test.db"
        _create_library_db(db_file, ["Stereolab"], with_alternate_artist=True)
        db = LibraryDB(db_path=db_file)
        await db.connect()  # must not raise

        # Simulate a source that reads cleanly at probe time but not at build
        # time (e.g. a row of invalid UTF-8 aborting fetchall for the column).
        original = db._artist_name_sources
        db._artist_name_sources = lambda: [*original(), ("library", "no_such_column")]  # type: ignore[method-assign]
        await db._build_artist_name_pool()
        db._artist_name_sources = original  # type: ignore[method-assign]

        assert db._artist_name_pool_usable is False
        # Still exactly right, now via SQL rather than the pool.
        assert await db._artist_exists_exactly("stereolab", "stereolab") is True
        assert await db._artist_exists_exactly("not in the library", "not in the library") is False

        await db.close()

    @pytest.mark.asyncio
    async def test_non_text_catalog_value_disqualifies_the_pool(self, tmp_path):
        """A BLOB-stored name must not silently shrink the pool.

        ``artist`` has TEXT affinity, so SQLite coerces numerics on insert,
        but a BLOB stays ``bytes`` -- and the old SQL coerced it, so
        ``LOWER(artist)`` matched. See
        :meth:`LibraryDB._build_artist_name_pool` for why that has to
        disqualify the pool rather than quietly shrink it.
        """
        db_file = tmp_path / "test.db"
        _create_library_db(db_file, ["Stereolab"])
        import sqlite3

        conn = sqlite3.connect(db_file)
        conn.execute(
            "INSERT INTO library VALUES (2, 'Album 2', ?, 'A', 2, 2, 'Rock', 'LP')",
            (b"Cat Power",),
        )
        conn.commit()
        assert conn.execute("SELECT typeof(artist) FROM library WHERE id=2").fetchone()[0] == "blob"
        conn.close()

        db = LibraryDB(db_path=db_file)
        await db.connect()

        assert db._artist_name_pool_usable is False
        # The SQL fallback still matches it, exactly as before #1248.
        assert await db._artist_exists_exactly("cat power", "cat power") is True
        await db.close()

    @pytest.mark.asyncio
    async def test_pool_case_folds_non_ascii_catalog_names(self, tmp_path):
        """Deliberate, measured divergence from the old SQL: full Unicode folding.

        SQLite's ``LOWER()`` is ASCII-only, so the pre-#1248 SQL compared
        ``"NILÜFER YANYA"`` (unchanged by ``LOWER``) against a Python-lowered
        query and never matched -- the #857 guard silently missed an artist
        the library genuinely owns. Python's ``str.lower()`` folds the whole
        string, so the pool matches. The widening is toward the guard's intent
        and is pinned here so it stays a decision rather than an accident.

        Asserted on the predicate itself, NOT on ``find_similar_artist``:
        that returns ``None`` here either way, because the tail identity guard
        in ``_find_similar_artist_uncached`` already folds with Python. Only
        the two predicate assertions discriminate this change from ``main``.
        """
        db_file = tmp_path / "test.db"
        _create_library_db(db_file, ["NILÜFER YANYA"])
        db = LibraryDB(db_path=db_file)
        await db.connect()

        assert db._artist_name_pool_lower == {"nilüfer yanya"}
        assert await db._artist_exists_exactly("nilüfer yanya", "nilüfer yanya") is True
        # The SQL this replaced did NOT match, which is the divergence.
        assert await db._exists_exactly_via_sql("nilüfer yanya", "nilüfer yanya") is False
        await db.close()

    @pytest.mark.asyncio
    async def test_adopt_connection_derives_the_state_connect_would(self, tmp_path):
        """A ``LibraryDB`` wired by assigning ``_conn`` must still answer correctly.

        Six fixtures across the integration and e2e tiers build a
        ``LibraryDB`` over an already-open in-memory connection to skip
        ``connect()``'s path check. Assigning ``db._conn`` directly, as they
        used to, skipped the ``_has_*`` schema probes as well -- which is why
        one of them hand-set three flags afterwards and the rest silently ran
        with all of them ``False``. The pool made that pre-existing gap
        load-bearing: a ``LibraryDB`` without one answers
        :meth:`_artist_exists_exactly` ``False`` for every artist, voiding the
        #857 guard across both tiers while the tests stay green, because the
        downstream ``best_match.lower() != artist_lower`` fallthrough returns
        ``None`` anyway. ``adopt_connection`` is the supported seam: it
        derives everything ``connect()`` would, over a connection it did not
        open.
        """
        db_file = tmp_path / "test.db"
        _create_library_db(
            db_file,
            ["Stereolab"],
            with_alternate_artist=True,
            alternate_artist_names=["Juana Molina"],
        )

        conn = await aiosqlite.connect(db_file)
        conn.row_factory = aiosqlite.Row
        db = LibraryDB()
        await db.adopt_connection(conn)

        # Schema flags derived, not left at their constructor defaults.
        assert db._has_alternate_artist is True
        assert db._artist_name_pool_lower == {"stereolab", "juana molina"}
        assert db._artist_name_pool_usable is True
        assert await db._artist_exists_exactly("stereolab", "stereolab") is True

        await conn.close()

    @pytest.mark.asyncio
    async def test_pool_admits_empty_strings_exactly_as_the_old_sql_did(self, tmp_path):
        """The pool's inclusion rule is the SQL's ``IS NOT NULL``, nothing narrower.

        ``alternate_artist_name`` is empty-string-filled rather than NULL for
        most of the catalog (59,816 of 64,676 rows in production), and the
        replaced SQL's only filter was ``IS NOT NULL`` -- so ``LOWER(...) = ''``
        matched. Filtering the pool on truthiness instead would drop those and
        make the predicate narrower than the SQL it claims to reproduce. This
        is inert in practice (``_step_prepare_request`` guards on
        ``if parsed.artist``, and ``strip_leading_article(...) or artist_lower``
        keeps ``artist_compare`` from collapsing to ``""`` on its own), but the
        PR's load-bearing claim is exact equivalence, so pin it rather than
        rely on the caller to keep the difference unreachable.
        """
        db_file = tmp_path / "test.db"
        _create_library_db(db_file, ["Stereolab"], with_alternate_artist=True)
        import sqlite3

        conn = sqlite3.connect(db_file)
        conn.execute("UPDATE library SET alternate_artist_name = '' WHERE artist = 'Stereolab'")
        conn.commit()
        conn.close()

        db = LibraryDB(db_path=db_file)
        await db.connect()

        assert "" in db._artist_name_pool_lower
        assert await db._artist_exists_exactly("", "") is True
        await db.close()

    @pytest.mark.asyncio
    async def test_retained_pool_is_not_left_over_allocated(self, tmp_path):
        """The retained pool is not left over-allocated by incremental growth.

        A set grown by repeated ``update`` overshoots its table: 27,518
        production names land in 131,072 slots (2.00 MiB) where a set built
        at final size takes 65,536 (1.00 MiB). The pool is retained for the
        whole process lifetime, per worker, so the slack is permanent --
        rebuilding it once at the end costs ~0.4 ms and returns half of it.
        """
        db_file = tmp_path / "test.db"
        _create_library_db(db_file, [f"Artist {i}" for i in range(2000)])
        db = LibraryDB(db_path=db_file)
        await db.connect()

        pool = db._artist_name_pool_lower
        assert len(pool) == 2000
        assert sys.getsizeof(pool) == sys.getsizeof(set(pool))
        await db.close()

    @pytest.mark.asyncio
    async def test_close_clears_the_pool(self, tmp_path):
        """``close()`` releases the pool's memory with the connection."""
        db_file = tmp_path / "test.db"
        _create_library_db(db_file, ["Stereolab"])
        db = LibraryDB(db_path=db_file)
        await db.connect()
        assert db._artist_name_pool_lower

        await db.close()

        assert db._artist_name_pool_lower == set()
        assert db._artist_name_pool_usable is False


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
# cross_reference_names column (WXYC/discogs-etl#334)
# ---------------------------------------------------------------------------


class TestCrossReferenceNamesColumn:
    """Tests for cross_reference_names column detection and queries."""

    @pytest.mark.asyncio
    async def test_connect_detects_cross_reference_names_column(self, tmp_path):
        """connect() should detect the cross_reference_names column and set flag."""
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
                label TEXT,
                cross_reference_names TEXT
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
        assert db._has_cross_reference_names is True
        await db.close()

    @pytest.mark.asyncio
    async def test_connect_without_cross_reference_names_column(self, tmp_path):
        """connect() should set flag to False when cross_reference_names is absent.

        Back-compat: a library.db predating WXYC/discogs-etl#334 must still load.
        """
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
        assert db._has_cross_reference_names is False
        await db.close()

    @pytest.mark.asyncio
    async def test_search_returns_cross_reference_names_when_present(self, tmp_path):
        """Search results should include cross_reference_names data when the column exists."""
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
                cross_reference_names TEXT
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
            "(57833, '\"In The Blink of an Eye\" 7-inch', 'Burning Star Core', 'BU', "
            "110, 6, 'Rock', 'vinyl - 7\"', 'C.S. Yeh', 'C. Spencer Yeh')"
        )
        conn.execute(
            "INSERT INTO library_fts(rowid, title, artist, alternate_artist_name) "
            "VALUES (57833, 'In The Blink of an Eye', 'Burning Star Core', 'C.S. Yeh')"
        )
        conn.commit()
        conn.close()

        db = LibraryDB(db_path=db_file)
        await db.connect()
        results = await db.search(artist="Burning Star Core")
        assert len(results) == 1
        assert results[0].cross_reference_names == "C. Spencer Yeh"
        await db.close()

    def test_select_columns_includes_cross_reference_names(self):
        db = LibraryDB()
        db._has_alternate_artist = True
        db._has_cross_reference_names = True
        cols = db._select_columns()
        assert "cross_reference_names" in cols

    def test_select_columns_excludes_cross_reference_names(self):
        db = LibraryDB()
        db._has_alternate_artist = True
        db._has_cross_reference_names = False
        cols = db._select_columns()
        assert "cross_reference_names" not in cols


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
    """The per-process TTL cache in front of the scan.

    Both tests previously counted ``_conn.execute`` calls on a mocked
    connection. LML#1245 moved candidate generation off SQL entirely -- a
    correction now issues no query at all -- so the call count no longer
    distinguishes a cache hit from a miss. They count invocations of the
    uncached scan instead, which is what the cache is actually there to
    avoid, and run against a real catalog so the scan has something to find.
    """

    @pytest.mark.asyncio
    async def test_find_similar_artist_cache_hit(self, tmp_path):
        """Second identical lookup returns the cached result without re-scanning."""
        clear_library_caches()
        db = await _connected_db(tmp_path, ["Nilufer Yanya", "Sessa"])
        try:
            with patch.object(
                LibraryDB,
                "_find_similar_artist_uncached",
                wraps=db._find_similar_artist_uncached,
            ) as scan:
                first = await db.find_similar_artist("Nilufer Yanyaa")
                second = await db.find_similar_artist("Nilufer Yanyaa")

            assert first == "Nilufer Yanya"
            assert second == first
            assert scan.await_count == 1
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_find_similar_artist_caches_none(self, tmp_path):
        """A ``None`` verdict is cached too -- the expensive case to repeat.

        A miss scores the whole pool and finds nothing, so it costs strictly
        more than a hit, which can stop early. Caching only successes would
        cache the cheap half.
        """
        clear_library_caches()
        db = await _connected_db(tmp_path, ["Nilufer Yanya", "Sessa"])
        try:
            with patch.object(
                LibraryDB,
                "_find_similar_artist_uncached",
                wraps=db._find_similar_artist_uncached,
            ) as scan:
                first = await db.find_similar_artist("Zzzqqx Nonexistent")
                second = await db.find_similar_artist("Zzzqqx Nonexistent")

            assert first is None
            assert second is None
            assert scan.await_count == 1
        finally:
            await db.close()


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


class TestGetItemsByIds:
    """Tests for get_items_by_ids() -- the shelf-metadata bulk lookup the LML#1022
    location union uses to bind a recall-index row's library_id back to its
    shelf artist/album title."""

    @pytest.mark.asyncio
    async def test_returns_items_keyed_by_id(self, tmp_path):
        db_path = tmp_path / "test.db"
        import aiosqlite

        async with aiosqlite.connect(db_path) as conn:
            await conn.execute(
                "CREATE TABLE library (id INTEGER PRIMARY KEY, title TEXT, artist TEXT, "
                "call_letters TEXT, artist_call_number INTEGER, release_call_number INTEGER, "
                "genre TEXT, format TEXT)"
            )
            await conn.execute(
                "INSERT INTO library (id, title, artist, call_letters, "
                "artist_call_number, release_call_number, genre, format) "
                "VALUES (60654, 'Lost in Translation', 'Soundtracks - L', 'L', 1, 1, 'Soundtrack', 'LP')"
            )
            await conn.commit()

        db = LibraryDB(db_path)
        await db.connect()
        items = await db.get_items_by_ids([60654, 99999])
        await db.close()

        assert set(items) == {60654}
        assert items[60654].artist == "Soundtracks - L"
        assert items[60654].title == "Lost in Translation"

    @pytest.mark.asyncio
    async def test_empty_id_list_short_circuits_without_query(self, tmp_path):
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
        items = await db.get_items_by_ids([])
        await db.close()

        assert items == {}

    @pytest.mark.asyncio
    async def test_conn_none_returns_empty_map(self):
        db = LibraryDB()
        db._conn = None

        items = await db.get_items_by_ids([1, 2])
        assert items == {}


# ---------------------------------------------------------------------------
# LML#1245 -- full-pool correction with an edit-cap and a margin guard
# ---------------------------------------------------------------------------


async def _connected_db(tmp_path, artists, **kwargs):
    """A ``LibraryDB`` over a real seeded catalog, connected the supported way.

    Goes through ``adopt_connection`` rather than assigning ``_conn``, because
    the correction path reads state that only ``adopt_connection`` derives --
    a test that injects the connection exercises the degraded lane and proves
    nothing about the fast one (LML#1248 review).
    """
    db_file = tmp_path / "test.db"
    _create_library_db(db_file, artists, **kwargs)
    db = LibraryDB(db_path=db_file)
    await db.connect()
    return db


class TestFullPoolCandidateGeneration:
    """The two candidate-generation defects LML#1245 was filed for.

    Both live upstream of scoring: when the true match reached ``fuzz.ratio``
    it won comfortably, so no threshold change could have reached either.
    """

    @pytest.mark.asyncio
    async def test_match_not_anchored_to_the_start_of_the_stored_name(self, tmp_path):
        """The old prefilter anchored ``'{first_word[:3]}%'`` to the string start.

        "22 Pistepirkko" contributed no pattern at all -- "22" is two
        characters -- so "22-Pistepirkko" was unreachable at any cap size.
        """
        clear_library_caches()
        db = await _connected_db(tmp_path, ["22-Pistepirkko", "Sessa", "Stereolab"])
        try:
            assert await db.find_similar_artist("22 Pistepirkko") == "22-Pistepirkko"
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_true_match_beyond_the_old_hundred_row_cap(self, tmp_path):
        """The old query sliced candidates at ``LIMIT 100`` in arbitrary order.

        A multi-word query routinely generated thousands of candidates, and
        the true match fell outside the slice. Here 150 decoys share the
        "asa" prefix, so the true match cannot be in the first 100 rows by
        storage order.
        """
        clear_library_caches()
        decoys = [f"Asa Chang Tribute Band {i:03d}" for i in range(150)]
        db = await _connected_db(tmp_path, [*decoys, "Asa-Chang and Junray"])
        try:
            assert await db.find_similar_artist("Asa Chang and Junray") == "Asa-Chang and Junray"
        finally:
            await db.close()


class TestArtistCorrectionMarginGuard:
    """The winner must beat the best *distinct* runner-up by a fixed margin.

    A dense neighbourhood is exactly where a full-pool scan is most dangerous:
    the scan reaches candidates the old prefilter never surfaced, and some of
    them are genuinely different artists rather than spellings of one.
    """

    @pytest.mark.asyncio
    async def test_two_close_catalog_names_suppress_the_correction(self, tmp_path):
        """Juana Molina and Juan Molina are different artists, both real.

        A query one edit from the first and two from the second scores 91.7
        and 87.0 -- a 4.7-point margin. The scan cannot tell which was meant,
        so it must answer None rather than pick. This is the guard's cost as
        well as its point: a genuine typo for "Juana Molina" is refused here
        too.
        """
        clear_library_caches()
        db = await _connected_db(tmp_path, ["Juana Molina", "Juan Molina", "Sessa"])
        try:
            assert await db.find_similar_artist("Juana Molino") is None
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_an_isolated_winner_still_corrects(self, tmp_path):
        """Same query shape, no close second: the correction fires.

        The pairing with the test above is the whole evidence that the guard
        keys on neighbourhood density rather than on the winner's own score --
        the winner is identical in both.
        """
        clear_library_caches()
        db = await _connected_db(tmp_path, ["Juana Molina", "Sessa", "Stereolab"])
        try:
            assert await db.find_similar_artist("Juana Molino") == "Juana Molina"
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_the_same_name_in_two_sources_is_not_a_runner_up(self, tmp_path):
        """A name reachable on both scoring axes, or from two columns, is one
        candidate -- not a winner plus a runner-up that cancels it."""
        clear_library_caches()
        db = await _connected_db(
            tmp_path,
            ["Juana Molina", "Sessa"],
            with_album_artist=True,
            album_artists=["Juana Molina", None],
        )
        try:
            assert await db.find_similar_artist("Juana Molino") == "Juana Molina"
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_case_variant_spellings_cannot_hide_a_real_rival(self, tmp_path):
        """Duplicate casings of the winner must not crowd the rival out of the scan.

        The same neighbourhood as test_two_close_catalog_names_suppress_the_
        correction, plus two extra casings of the winner. If spellings that
        fold to the same lowercase form occupy separate pool entries, they
        fill the scan's top slots on both axes, the distinct-runner-up search
        finds nothing, ``runner_up_score`` stays 0.0, and the margin guard
        silently passes a correction the single-casing catalog refuses.
        """
        clear_library_caches()
        db = await _connected_db(
            tmp_path,
            ["Juana Molina", "JUANA MOLINA", "Juana molina", "Juan Molina"],
        )
        try:
            assert await db.find_similar_artist("Juana Molino") is None
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_a_rival_seven_points_back_still_suppresses_the_correction(self, tmp_path):
        """The margin is 8, tuned on the CTA-bearing prod catalog -- not 6.

        A real observed misroute from the condition-1 discharge run: the
        out-of-library query "Johnny Mitchell" scores 92.9 against
        'John Mitchell' with 'Joni Mitchell' 7.1 points back -- three
        genuinely different artists. At margin 6 the correction fires and
        routes the LML#626 library channel into John Mitchell's catalog; a
        rival inside 8 points means the neighbourhood is too dense to pick.
        """
        clear_library_caches()
        db = await _connected_db(tmp_path, ["John Mitchell", "Joni Mitchell", "Sessa"])
        try:
            assert await db.find_similar_artist("Johnny Mitchell") is None
        finally:
            await db.close()

    def test_scan_duplicate_entries_of_one_artist_neither_zero_nor_bypass_the_margin(self):
        """Scan-level pin, independent of the pool build's own dedupe layer.

        Duplicate entries of one artist -- however they reach the pool -- are
        one identity to the margin: not rivals to each other (which would
        zero the margin), and not able to crowd the genuine rival out of the
        retained set (which would report ``runner_up_score`` 0.0 and bypass
        the guard entirely -- the top-3 extraction's confirmed failure).
        """
        pool_lower = ["juana molina", "juana molina", "juana molina", "juan molina"]
        winner_index, score, runner_up = _scan_artist_name_pool(
            "juana molino", "juana molino", pool_lower, pool_lower, 79.0
        )
        assert pool_lower[winner_index] == "juana molina"
        assert score == pytest.approx(91.67, abs=0.01)
        assert runner_up == pytest.approx(86.96, abs=0.01)  # the genuine rival, never 0.0

    @pytest.mark.asyncio
    async def test_an_artists_own_article_pair_filing_is_not_its_rival(self, tmp_path):
        """An artist filed both with and without a leading article still corrects.

        "Black Dog" and "The Black Dog" are one artist filed twice -- the real
        catalog holds 59 such pairs across artist/alternate_artist_name. On
        the article-stripped axis the two filings score identically, so a
        runner-up rule keyed on lowercase equality counts the artist as its
        own rival, zeroes the margin, and permanently suppresses every
        correction toward it -- a recall regression against the LIKE-era
        code, which corrected these. Rivalry is keyed on the article-stripped
        form instead; the raw axis then picks whichever filing sits closest
        to the query's own shape, which is why the two probes below land on
        different filings of the same artist.
        """
        clear_library_caches()
        db = await _connected_db(tmp_path, ["Black Dog", "The Black Dog", "Sessa"])
        try:
            assert await db.find_similar_artist("Black Dogg") == "Black Dog"
            assert await db.find_similar_artist("The Bleack Dog") == "The Black Dog"
        finally:
            await db.close()


class TestArtistCorrectionEditCap:
    """An absolute edit budget scaled to query length.

    ``fuzz.ratio`` is length-relative, so a fixed ratio floor buys more
    absolute error the longer the string: eight separate mistakes in a 50
    character credit still scores 86. The cap is the lever ratio lacks.
    """

    @pytest.mark.parametrize(
        "query_len,expected",
        [(1, 1), (5, 1), (12, 1), (13, 2), (24, 2), (25, 3), (50, 5)],
    )
    def test_cap_scales_with_query_length_with_a_floor_of_one(self, query_len, expected):
        assert _artist_correction_edit_cap("x" * query_len) == expected

    @pytest.mark.parametrize(
        "query,candidate,distance",
        [
            ("stereolab", "stereolba", 1),
            ("jessica pratt", "jessica prtat", 1),
        ],
    )
    def test_a_transposition_costs_one_edit_not_two(self, query, candidate, distance):
        """The reason the cap is counted with OSA rather than Levenshtein.

        Plain Levenshtein charges a transposition two edits -- a delete plus an
        insert -- so a cap tight enough to be useful refuses a whole typo
        class, and recall starts depending on the divisor for reasons
        unrelated to how wrong the candidate is. Measured on prod-20260719 at
        margin 6, tightening len/6 -> len/12 costs 93.1% -> 79.5% recall under
        Levenshtein and nothing at all under OSA.
        """
        assert _ARTIST_CORRECTION_DISTANCE.distance(query, candidate) == distance

    @pytest.mark.asyncio
    async def test_a_high_ratio_winner_with_too_many_edits_is_refused(self, tmp_path):
        """50 characters, eight substitutions: ratio 86.0, cap 7.

        Clears the 85 floor and the margin guard, and is still refused --
        eight independent errors is not a typo, whatever the ratio says.
        """
        clear_library_caches()
        db = await _connected_db(
            tmp_path,
            ["Chuquimamani-Condori and Joshua Chuquimia Crampton", "Sessa"],
        )
        try:
            garbled = "Chuudimsmaei-Candori ane Joshua thuquimia Clampton"
            assert await db.find_similar_artist(garbled) is None
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_the_same_long_name_with_one_edit_still_corrects(self, tmp_path):
        """The paired control: length alone is not what the cap refuses."""
        clear_library_caches()
        canonical = "Chuquimamani-Condori and Joshua Chuquimia Crampton"
        db = await _connected_db(tmp_path, [canonical, "Sessa"])
        try:
            typo = canonical.replace("Crampton", "Crampfon")
            assert await db.find_similar_artist(typo) == canonical
        finally:
            await db.close()


class TestArtistCorrectionInvariantsHold:
    """The eleven pins from LML#1245's measurement, as behaviour."""

    @pytest.mark.asyncio
    async def test_an_exact_library_artist_is_never_corrected(self, tmp_path):
        clear_library_caches()
        db = await _connected_db(tmp_path, ["Juana Molina", "Juan Molina"])
        try:
            assert await db.find_similar_artist("Juana Molina") is None
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_a_short_name_one_edit_away_is_not_snapped(self, tmp_path):
        """The LML#626 canonical trap: "Plug" must not become "Plugz".

        One edit, inside the cap. What refuses it is the short-name effective
        threshold (92 for a four-character query), which predates this change
        and is deliberately left alone.
        """
        clear_library_caches()
        db = await _connected_db(tmp_path, ["Plugz", "Sessa"])
        try:
            assert await db.find_similar_artist("Plug") is None
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_a_non_library_artist_is_not_corrected(self, tmp_path):
        clear_library_caches()
        db = await _connected_db(tmp_path, ["Juana Molina", "Sessa", "Stereolab"])
        try:
            assert await db.find_similar_artist("Zzzqqx Nonexistent") is None
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_a_genuine_single_edit_typo_still_corrects(self, tmp_path):
        clear_library_caches()
        db = await _connected_db(tmp_path, ["Stereolab", "Sessa"])
        try:
            assert await db.find_similar_artist("Stereolb") == "Stereolab"
        finally:
            await db.close()


class TestArtistCorrectionPoolDegradation:
    """The degraded modes are ASYMMETRIC, and deliberately so.

    Both consumers read one pool. Exact-existence fails **closed** to SQL,
    because answering "not present" from an incomplete pool re-enables the
    LML#857 wrong-artist corrections. Fuzzy correction degrades to **no
    correction**, because the alternative -- scoring against a pool that is
    missing names -- is how a query gets snapped onto the nearest survivor.
    Neither may adopt the other's posture.
    """

    @pytest.mark.asyncio
    async def test_an_unusable_pool_suppresses_correction_rather_than_guessing(self, tmp_path):
        clear_library_caches()
        db = await _connected_db(tmp_path, ["Stereolab", "Sessa"])
        try:
            assert await db.find_similar_artist("Stereolb") == "Stereolab"
            clear_library_caches()
            db._artist_name_pool_usable = False
            assert await db.find_similar_artist("Stereolb") is None
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_the_exact_check_still_falls_back_to_sql(self, tmp_path):
        """The other half of the asymmetry, on the same unusable pool."""
        clear_library_caches()
        db = await _connected_db(tmp_path, ["Stereolab", "Sessa"])
        try:
            db._artist_name_pool_usable = False
            assert await db._artist_exists_exactly("stereolab", "stereolab") is True
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_a_hot_swap_landing_mid_scan_does_not_crash_the_lookup(self, tmp_path):
        """``close()`` during an in-flight scan must not turn the lookup into a 500.

        The LML#706/#834 hot-swap path calls ``close_library_db()`` while a
        ``/lookup`` can be parked inside the off-loop scan. The scan thread
        holds the old lists safely; what broke was the resume, which indexed
        the instance attributes ``close()`` had just emptied. The correction
        path must read the same snapshot it scanned.
        """
        clear_library_caches()
        db = await _connected_db(tmp_path, ["Stereolab", "Sessa"])
        # Captured before patching: `library.db.asyncio` IS the asyncio
        # module, so the patch below replaces the global attribute and a
        # wrapper calling `asyncio.to_thread` would recurse into itself.
        real_to_thread = asyncio.to_thread

        async def _to_thread_then_hot_swap(fn, *args):
            result = await real_to_thread(fn, *args)
            await db.close()  # lands while the request is still mid-correction
            return result

        try:
            with patch("library.db.asyncio.to_thread", side_effect=_to_thread_then_hot_swap):
                assert await db.find_similar_artist("Stereolb") == "Stereolab"
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_close_releases_the_correction_projections(self, tmp_path):
        clear_library_caches()
        db = await _connected_db(tmp_path, ["Stereolab", "Sessa"])
        assert db._artist_name_pool_names
        await db.close()
        assert db._artist_name_pool_names == []
        assert db._artist_name_pool_lower_list == []
        assert db._artist_name_pool_compare == []
        assert db._artist_name_pool_lower == set()
        assert db._artist_name_pool_usable is False


class TestArtistCorrectionOneReadTwoViews:
    """Both consumers are served from a single catalog read (LML#1248/#1245)."""

    @pytest.mark.asyncio
    async def test_the_projections_are_index_aligned(self, tmp_path):
        clear_library_caches()
        db = await _connected_db(
            tmp_path,
            ["Stereolab", "The Sea and Cake"],
            with_album_artist=True,
            album_artists=["Juana Molina", None],
        )
        try:
            names = db._artist_name_pool_names
            assert len(names) == len(db._artist_name_pool_lower_list)
            assert len(names) == len(db._artist_name_pool_compare)
            for i, name in enumerate(names):
                assert db._artist_name_pool_lower_list[i] == name.lower()
            assert "juana molina" in db._artist_name_pool_lower_list
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_the_exact_set_shares_its_strings_with_the_lower_projection(self, tmp_path):
        """``set(lower_list)`` rather than a second lowercasing pass.

        Not a micro-optimisation: at the production CTA-bearing pool shape a
        duplicated projection is megabytes per worker, and the pool is rebuilt
        inline on the LML#706 hot-swap reconnect.
        """
        clear_library_caches()
        db = await _connected_db(tmp_path, ["Stereolab", "Sessa"])
        try:
            for lowered in db._artist_name_pool_lower_list:
                assert any(lowered is member for member in db._artist_name_pool_lower)
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_the_scan_runs_off_the_event_loop(self, tmp_path):
        """Scoring the whole pool is CPU work; it must not block the loop."""
        clear_library_caches()
        db = await _connected_db(tmp_path, ["Stereolab", "Sessa"])
        try:
            with patch("library.db.asyncio.to_thread", wraps=asyncio.to_thread) as spy:
                await db.find_similar_artist("Stereolb")
            assert spy.await_count == 1
        finally:
            await db.close()
