"""Unit tests for scripts/export_to_sqlite.py."""

import sqlite3
import sys
from pathlib import Path

import pytest

# Add project root to path so we can import the script
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from scripts.export_to_sqlite import _do_export


class TestDoExport:
    """Tests for the _do_export function."""

    @pytest.fixture(autouse=True)
    def use_tmp_output(self, tmp_path, monkeypatch):
        """Redirect OUTPUT_PATH to a temp directory."""
        self.output_path = tmp_path / "library.db"
        monkeypatch.setattr("scripts.export_to_sqlite.OUTPUT_PATH", self.output_path)

    def test_exports_alternate_artist_name(self):
        """Rows with alternate_artist_name should be exported to SQLite."""
        rows = [
            {
                "id": "1",
                "title": "Drum n Bass for Papa",
                "artist": "Luke Vibert",
                "call_letters": "V",
                "artist_call_number": "15",
                "release_call_number": "1",
                "genre": "Electronic",
                "format": "CD",
                "alternate_artist_name": "Plug",
            },
        ]
        _do_export(rows)

        conn = sqlite3.connect(self.output_path)
        cursor = conn.execute("SELECT alternate_artist_name FROM library WHERE id = 1")
        row = cursor.fetchone()
        conn.close()

        assert row is not None
        assert row[0] == "Plug"

    def test_exports_null_alternate_artist_name(self):
        """Rows with NULL alternate_artist_name should store NULL in SQLite."""
        rows = [
            {
                "id": "1",
                "title": "Big Soup",
                "artist": "Luke Vibert",
                "call_letters": "V",
                "artist_call_number": "15",
                "release_call_number": "2",
                "genre": "Electronic",
                "format": "CD",
                "alternate_artist_name": None,
            },
        ]
        _do_export(rows)

        conn = sqlite3.connect(self.output_path)
        cursor = conn.execute("SELECT alternate_artist_name FROM library WHERE id = 1")
        row = cursor.fetchone()
        conn.close()

        assert row is not None
        assert row[0] is None

    def test_fts_indexes_alternate_artist_name(self):
        """The FTS5 index should include alternate_artist_name for full-text search."""
        rows = [
            {
                "id": "1",
                "title": "Drum n Bass for Papa",
                "artist": "Luke Vibert",
                "call_letters": "V",
                "artist_call_number": "15",
                "release_call_number": "1",
                "genre": "Electronic",
                "format": "CD",
                "alternate_artist_name": "Plug",
            },
        ]
        _do_export(rows)

        conn = sqlite3.connect(self.output_path)
        # Search FTS for "Plug" - should match via alternate_artist_name
        cursor = conn.execute("""
            SELECT l.id, l.artist, l.alternate_artist_name
            FROM library l
            JOIN library_fts fts ON l.id = fts.rowid
            WHERE library_fts MATCH 'Plug'
        """)
        results = cursor.fetchall()
        conn.close()

        assert len(results) == 1
        assert results[0][2] == "Plug"

    def test_alternate_artist_index_created(self):
        """An index should be created on the alternate_artist_name column."""
        rows = [
            {
                "id": "1",
                "title": "Album",
                "artist": "Artist",
                "call_letters": "A",
                "artist_call_number": "1",
                "release_call_number": "1",
                "genre": "Rock",
                "format": "CD",
                "alternate_artist_name": None,
            },
        ]
        _do_export(rows)

        conn = sqlite3.connect(self.output_path)
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_alternate_artist'"
        )
        index = cursor.fetchone()
        conn.close()

        assert index is not None

    def test_mixed_rows_with_and_without_alternate(self):
        """Mix of rows with and without alternate_artist_name should export correctly."""
        rows: list[dict] = [
            {
                "id": "1",
                "title": "Drum n Bass for Papa",
                "artist": "Luke Vibert",
                "call_letters": "V",
                "artist_call_number": "15",
                "release_call_number": "1",
                "genre": "Electronic",
                "format": "CD",
                "alternate_artist_name": "Plug",
            },
            {
                "id": "2",
                "title": "Big Soup",
                "artist": "Luke Vibert",
                "call_letters": "V",
                "artist_call_number": "15",
                "release_call_number": "2",
                "genre": "Electronic",
                "format": "CD",
                "alternate_artist_name": None,
            },
        ]
        _do_export(rows)

        conn = sqlite3.connect(self.output_path)
        cursor = conn.execute("SELECT id, alternate_artist_name FROM library ORDER BY id")
        results = cursor.fetchall()
        conn.close()

        assert len(results) == 2
        assert results[0] == (1, "Plug")
        assert results[1] == (2, None)
