"""Unit tests for scripts/streaming_availability/dedup.py."""

import sqlite3
from pathlib import Path

import pytest

from scripts.streaming_availability.dedup import deduplicate_library


def _create_test_db(tmp_path: Path, rows: list[tuple]) -> str:
    """Create a test library.db with the given rows and return its path."""
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
        rows,
    )
    conn.commit()
    conn.close()
    return db_path


class TestDeduplicateLibrary:
    """Tests for library deduplication logic."""

    @pytest.mark.asyncio
    async def test_same_album_different_formats_deduplicated(self, tmp_path):
        db_path = _create_test_db(
            tmp_path,
            [
                (1, "Aluminum Tunes", "Stereolab", "S", 1, 1, "Rock", "cd", None, "Duophonic"),
                (
                    2,
                    "Aluminum Tunes",
                    "Stereolab",
                    "S",
                    1,
                    2,
                    "Rock",
                    "vinyl - LP",
                    None,
                    "Duophonic",
                ),
            ],
        )
        result = await deduplicate_library(db_path)
        assert len(result) == 1
        assert result[0].display_artist == "Stereolab"
        assert result[0].display_title == "Aluminum Tunes"
        assert sorted(result[0].library_ids) == [1, 2]
        assert sorted(result[0].formats) == ["cd", "vinyl - LP"]

    @pytest.mark.asyncio
    async def test_different_albums_not_deduplicated(self, tmp_path):
        db_path = _create_test_db(
            tmp_path,
            [
                (1, "Aluminum Tunes", "Stereolab", "S", 1, 1, "Rock", "cd", None, "Duophonic"),
                (2, "Dots and Loops", "Stereolab", "S", 1, 2, "Rock", "cd", None, "Duophonic"),
            ],
        )
        result = await deduplicate_library(db_path)
        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_compilation_artist_flagged(self, tmp_path):
        db_path = _create_test_db(
            tmp_path,
            [
                (1, "CMJ New Music", "Various Artists", "V", 1, 1, "Rock", "cd", None, None),
            ],
        )
        result = await deduplicate_library(db_path)
        assert len(result) == 1
        assert result[0].is_compilation is True

    @pytest.mark.asyncio
    async def test_format_suffix_in_title_grouped(self, tmp_path):
        """Titles with format suffixes should group with the clean version."""
        db_path = _create_test_db(
            tmp_path,
            [
                (1, "Automanikk", "A Guy Called Gerald", "A", 1, 1, "Hiphop", "cd", None, None),
                (
                    2,
                    'Automanikk 12"',
                    "A Guy Called Gerald",
                    "A",
                    1,
                    2,
                    "Hiphop",
                    'vinyl - 12"',
                    None,
                    None,
                ),
            ],
        )
        result = await deduplicate_library(db_path)
        assert len(result) == 1
        assert sorted(result[0].library_ids) == [1, 2]

    @pytest.mark.asyncio
    async def test_alternate_artist_name_preferred(self, tmp_path):
        db_path = _create_test_db(
            tmp_path,
            [
                (
                    1,
                    "Moon Pix",
                    "Cat Power",
                    "C",
                    1,
                    1,
                    "Rock",
                    "cd",
                    "Chan Marshall",
                    "Matador Records",
                ),
            ],
        )
        result = await deduplicate_library(db_path)
        assert len(result) == 1
        assert result[0].display_artist == "Chan Marshall"
        assert result[0].normalized_artist == "chan marshall"

    @pytest.mark.asyncio
    async def test_genre_and_label_from_first_row(self, tmp_path):
        db_path = _create_test_db(
            tmp_path,
            [
                (1, "Confield", "Autechre", "A", 1, 1, "Electronic", "cd", None, "Warp"),
                (2, "Confield", "Autechre", "A", 1, 2, "Electronic", "vinyl - LP", None, "Warp"),
            ],
        )
        result = await deduplicate_library(db_path)
        assert result[0].genre == "Electronic"
        assert result[0].label == "Warp"

    @pytest.mark.asyncio
    async def test_empty_database(self, tmp_path):
        db_path = _create_test_db(tmp_path, [])
        result = await deduplicate_library(db_path)
        assert result == []

    @pytest.mark.asyncio
    async def test_diacritics_normalized_for_grouping(self, tmp_path):
        """Same album with different diacritic renderings should group together."""
        db_path = _create_test_db(
            tmp_path,
            [
                (1, "Homogenic", "Björk", "B", 1, 1, "Electronic", "cd", None, None),
                (2, "Homogenic", "Bjork", "B", 1, 2, "Electronic", "vinyl - LP", None, None),
            ],
        )
        result = await deduplicate_library(db_path)
        assert len(result) == 1
        assert sorted(result[0].library_ids) == [1, 2]
