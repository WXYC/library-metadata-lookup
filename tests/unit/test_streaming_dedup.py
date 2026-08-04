"""Unit tests for scripts/streaming_availability/dedup.py."""

import logging
import sqlite3
from pathlib import Path
from unittest.mock import patch

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
    async def test_diverging_genre_logs_warning_but_still_merges(self, tmp_path, caplog):
        """LML#1095: two distinct library_ids that collapse to the same
        normalized (artist, title) key with materially different genre are a
        silent collision today -- the second row's genre is dropped with no
        signal. Detect and log it (so operators can audit real frequency)
        without changing the merge itself: measured against the real WXYC
        catalog, this pattern is at least as often a data-quality artifact
        (the same physical item re-catalogued with an inconsistent genre tag)
        as it is two genuinely distinct items, so auto-splitting on this
        signal alone isn't warranted."""
        db_path = _create_test_db(
            tmp_path,
            [
                (1, "Orion", "Orion", "O", 1, 1, "Electronic", "cd", None, None),
                (2, "Orion", "Orion", "O", 1, 2, "Rock", "vinyl - LP", None, None),
            ],
        )
        with caplog.at_level(logging.WARNING):
            result = await deduplicate_library(db_path)
        assert len(result) == 1
        assert sorted(result[0].library_ids) == [1, 2]
        assert result[0].genre == "Electronic"  # first-row-wins, unchanged
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "1" in warnings[0].getMessage() and "2" in warnings[0].getMessage()

    @pytest.mark.asyncio
    async def test_diverging_label_logs_warning(self, tmp_path, caplog):
        db_path = _create_test_db(
            tmp_path,
            [
                (1, "Black Book", "Greg Osby", "G", 1, 1, "Jazz", "cd", None, "Blue Note"),
                (2, "Black Book", "Greg Osby", "G", 1, 2, "Jazz", "vinyl - LP", None, "JMT"),
            ],
        )
        with caplog.at_level(logging.WARNING):
            await deduplicate_library(db_path)
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1

    @pytest.mark.asyncio
    async def test_one_sided_null_genre_does_not_warn(self, tmp_path, caplog):
        """Incomplete metadata on one row (a common real shape -- one catalog
        entry never got a genre) is not treated as a divergence signal."""
        db_path = _create_test_db(
            tmp_path,
            [
                (1, "Aluminum Tunes", "Stereolab", "S", 1, 1, None, "cd", None, "Duophonic"),
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
        with caplog.at_level(logging.WARNING):
            await deduplicate_library(db_path)
        assert not any(r.levelno == logging.WARNING for r in caplog.records)

    @pytest.mark.asyncio
    async def test_matching_genre_label_does_not_warn(self, tmp_path, caplog):
        """Sanity check: the existing identical-metadata multi-format case
        (test_genre_and_label_from_first_row) must not spuriously warn."""
        db_path = _create_test_db(
            tmp_path,
            [
                (1, "Confield", "Autechre", "A", 1, 1, "Electronic", "cd", None, "Warp"),
                (2, "Confield", "Autechre", "A", 1, 2, "Electronic", "vinyl - LP", None, "Warp"),
            ],
        )
        with caplog.at_level(logging.WARNING):
            await deduplicate_library(db_path)
        assert not any(r.levelno == logging.WARNING for r in caplog.records)

    @pytest.mark.asyncio
    async def test_diverging_is_compilation_logs_warning(self, tmp_path, caplog):
        """is_compilation is derived per-row from is_compilation_artist(artist)
        -- a collision where that verdict flips is exactly as much a signal
        as a genre/label conflict."""
        db_path = _create_test_db(
            tmp_path,
            [
                (1, "Rarities", "Overlap Artist", "O", 1, 1, "Rock", "cd", None, None),
                (2, "Rarities", "Overlap Artist", "O", 1, 2, "Rock", "vinyl - LP", None, None),
            ],
        )
        with patch(
            "scripts.streaming_availability.dedup.is_compilation_artist",
            side_effect=[False, True],
        ):
            with caplog.at_level(logging.WARNING):
                await deduplicate_library(db_path)
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1

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

    @pytest.mark.asyncio
    async def test_vinyl_12_inch_flagged_as_single(self, tmp_path):
        db_path = _create_test_db(
            tmp_path,
            [(1, 'Feel It 12"', "The Afros", "A", 1, 1, "Hiphop", 'vinyl - 12"', None, None)],
        )
        result = await deduplicate_library(db_path)
        assert result[0].is_single is True

    @pytest.mark.asyncio
    async def test_vinyl_7_inch_flagged_as_single(self, tmp_path):
        db_path = _create_test_db(
            tmp_path,
            [(1, "Blue Monday", "New Order", "N", 1, 1, "Rock", 'vinyl - 7"', None, None)],
        )
        result = await deduplicate_library(db_path)
        assert result[0].is_single is True

    @pytest.mark.asyncio
    async def test_cd_not_flagged_as_single(self, tmp_path):
        db_path = _create_test_db(
            tmp_path,
            [(1, "Aluminum Tunes", "Stereolab", "S", 1, 1, "Rock", "cd", None, None)],
        )
        result = await deduplicate_library(db_path)
        assert result[0].is_single is False

    @pytest.mark.asyncio
    async def test_album_with_cd_and_single_not_flagged(self, tmp_path):
        db_path = _create_test_db(
            tmp_path,
            [
                (1, "Moon Pix", "Cat Power", "C", 1, 1, "Rock", "cd", None, None),
                (2, "Moon Pix", "Cat Power", "C", 1, 2, "Rock", 'vinyl - 12"', None, None),
            ],
        )
        result = await deduplicate_library(db_path)
        assert result[0].is_single is False
