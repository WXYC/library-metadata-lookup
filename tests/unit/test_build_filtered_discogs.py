"""Unit tests for scripts/build_filtered_discogs.py."""

import sqlite3
from pathlib import Path

from scripts.build_filtered_discogs import extract_library_artists


def _create_test_library(tmp_path: Path) -> str:
    """Create a minimal library.db for testing."""
    db_path = str(tmp_path / "library.db")
    conn = sqlite3.connect(db_path)
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
    conn.executemany(
        "INSERT INTO library VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (1, "Aluminum Tunes", "Stereolab", "S", 1, 1, "Rock", "cd", None, "Duophonic"),
            (2, "Dots and Loops", "Stereolab", "S", 1, 2, "Rock", "cd", None, "Duophonic"),
            (3, "Confield", "Autechre", "A", 1, 1, "Electronic", "cd", None, "Warp"),
            (4, "Moon Pix", "Cat Power", "C", 1, 1, "Rock", "cd", "Chan Marshall", "Matador"),
            (5, "CMJ 100", "Various Artists", "V", 1, 1, "Rock", "cd", None, None),
            (6, "Homogenic", "Björk", "B", 1, 1, "Electronic", "cd", None, None),
        ],
    )
    conn.commit()
    conn.close()
    return db_path


class TestExtractLibraryArtists:
    def test_extracts_unique_artists(self, tmp_path):
        db_path = _create_test_library(tmp_path)
        artists = extract_library_artists(db_path)
        assert "Stereolab" in artists
        assert "Autechre" in artists
        assert "Björk" in artists

    def test_deduplicates(self, tmp_path):
        db_path = _create_test_library(tmp_path)
        artists = extract_library_artists(db_path)
        # Stereolab appears twice in the library but should be deduplicated
        assert len([a for a in artists if a.lower() == "stereolab"]) == 1

    def test_excludes_compilations(self, tmp_path):
        db_path = _create_test_library(tmp_path)
        artists = extract_library_artists(db_path)
        assert "Various Artists" not in artists

    def test_includes_alternate_names(self, tmp_path):
        db_path = _create_test_library(tmp_path)
        artists = extract_library_artists(db_path)
        assert "Chan Marshall" in artists
        assert "Cat Power" in artists
