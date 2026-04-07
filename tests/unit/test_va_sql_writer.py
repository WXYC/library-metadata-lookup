"""Tests for SQL generation."""

import pytest

from scripts.va_disambiguate.sql_writer import (
    generate_catalog_inserts,
    generate_flowsheet_updates,
)


class TestGenerateFlowsheetUpdates:
    def test_basic_update_with_artist_id(self):
        rows = [{"id": 1, "resolved_artist": "Koo Nimo", "library_code_id": 42, "confidence": 0.90, "strategy": "exact"}]
        lines = generate_flowsheet_updates(rows)
        sql = "\n".join(lines)
        assert "UPDATE FLOWSHEET_ENTRY_PROD" in sql
        assert "ARTIST_NAME = 'Koo Nimo'" in sql
        assert "ARTIST_ID = 42" in sql
        assert "WHERE ID = 1" in sql

    def test_update_without_artist_id(self):
        rows = [{"id": 1, "resolved_artist": "Unknown DJ", "library_code_id": None, "confidence": 0.80, "strategy": "fuzzy"}]
        lines = generate_flowsheet_updates(rows)
        sql = "\n".join(lines)
        assert "ARTIST_NAME = 'Unknown DJ'" in sql
        assert "ARTIST_ID" not in sql

    def test_escapes_single_quotes(self):
        rows = [{"id": 1, "resolved_artist": "Shlomo Gronich &The Sheba Choir's", "library_code_id": None, "confidence": 0.95, "strategy": "embedded"}]
        lines = generate_flowsheet_updates(rows)
        sql = "\n".join(lines)
        assert "Shlomo Gronich &The Sheba Choir\\'s" in sql

    def test_batch_delimiter_comments(self):
        rows = [
            {"id": i, "resolved_artist": f"Artist {i}", "library_code_id": None, "confidence": 0.90, "strategy": "exact"}
            for i in range(1, 6)
        ]
        lines = generate_flowsheet_updates(rows, batch_size=3)
        sql = "\n".join(lines)
        assert "-- Batch 1:" in sql
        assert "-- Batch 2:" in sql

    def test_empty_input(self):
        assert generate_flowsheet_updates([]) == []


class TestGenerateCatalogInserts:
    def test_basic_insert_with_track(self):
        rows = [{"library_release_id": 100, "artist_name": "Koo Nimo", "track_title": "Odo Akosomo"}]
        lines = generate_catalog_inserts(rows)
        sql = "\n".join(lines)
        assert "INSERT INTO COMPILATION_TRACK_ARTIST" in sql
        assert "VALUES (100, 'Koo Nimo', 'Odo Akosomo')" in sql

    def test_insert_without_track_title(self):
        rows = [{"library_release_id": 100, "artist_name": "Koo Nimo", "track_title": None}]
        lines = generate_catalog_inserts(rows)
        sql = "\n".join(lines)
        assert "LIBRARY_RELEASE_ID, ARTIST_NAME)" in sql
        assert "VALUES (100, 'Koo Nimo')" in sql
        assert "TRACK_TITLE" not in sql

    def test_escapes_single_quotes(self):
        rows = [{"library_release_id": 100, "artist_name": "O'Brien", "track_title": "It's Time"}]
        lines = generate_catalog_inserts(rows)
        sql = "\n".join(lines)
        assert "O\\'Brien" in sql
        assert "It\\'s Time" in sql

    def test_batch_delimiter_comments(self):
        rows = [
            {"library_release_id": 100, "artist_name": f"Artist {i}", "track_title": None}
            for i in range(1, 6)
        ]
        lines = generate_catalog_inserts(rows, batch_size=3)
        sql = "\n".join(lines)
        assert "-- Batch 1:" in sql
        assert "-- Batch 2:" in sql

    def test_empty_input(self):
        assert generate_catalog_inserts([]) == []
