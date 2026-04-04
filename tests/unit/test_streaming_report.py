"""Unit tests for scripts/streaming_availability/report.py."""

import csv

import pytest
import pytest_asyncio

from scripts.streaming_availability.dedup import DeduplicatedAlbum
from scripts.streaming_availability.report import generate_csv_report, generate_summary
from scripts.streaming_availability.results_db import ResultsDB


@pytest_asyncio.fixture
async def populated_db(tmp_path):
    """A results DB with albums in various states."""
    db = ResultsDB(":memory:")
    await db.connect()

    albums = [
        DeduplicatedAlbum(
            normalized_artist="stereolab",
            normalized_title="aluminum tunes",
            display_artist="Stereolab",
            display_title="Aluminum Tunes",
            library_ids=[1, 2],
            formats=["cd", "vinyl - LP"],
            genre="Rock",
            label="Duophonic",
            is_compilation=False,
        ),
        DeduplicatedAlbum(
            normalized_artist="autechre",
            normalized_title="confield",
            display_artist="Autechre",
            display_title="Confield",
            library_ids=[3],
            formats=["cd"],
            genre="Electronic",
            label="Warp",
            is_compilation=False,
        ),
        DeduplicatedAlbum(
            normalized_artist="anne gillis",
            normalized_title="eyry",
            display_artist="Anne Gillis",
            display_title="Eyry",
            library_ids=[4],
            formats=["vinyl - LP"],
            genre="Rock",
            label="Art into Life",
            is_compilation=False,
        ),
        DeduplicatedAlbum(
            normalized_artist="various artists",
            normalized_title="cmj new music",
            display_artist="Various Artists",
            display_title="CMJ New Music",
            library_ids=[5],
            formats=["cd"],
            genre="Rock",
            label=None,
            is_compilation=True,
        ),
    ]
    await db.insert_albums(albums)

    # Stereolab: found on Spotify
    rows = await db.get_pending("spotify", limit=10)
    stereolab_row = next(r for r in rows if r["display_artist"] == "Stereolab")
    await db.update_result(
        stereolab_row["id"],
        "spotify",
        "found",
        url="https://open.spotify.com/album/abc",
        confidence=98.0,
        matched_artist="Stereolab",
        matched_title="Aluminum Tunes",
    )

    # Autechre: found on Spotify
    autechre_row = next(r for r in rows if r["display_artist"] == "Autechre")
    await db.update_result(
        autechre_row["id"],
        "spotify",
        "found",
        url="https://open.spotify.com/album/def",
        confidence=95.0,
        matched_artist="Autechre",
        matched_title="Confield",
    )

    # Anne Gillis: not found on Spotify, not found on Apple
    gillis_row = next(r for r in rows if r["display_artist"] == "Anne Gillis")
    await db.update_result(gillis_row["id"], "spotify", "not_found")
    await db.update_result(gillis_row["id"], "apple", "not_found")

    # Various Artists: skipped
    va_row = next(r for r in rows if r["display_artist"] == "Various Artists")
    await db.update_result(va_row["id"], "spotify", "skipped")
    await db.update_result(va_row["id"], "apple", "skipped")

    yield db
    await db.close()


class TestGenerateCsvReport:
    @pytest.mark.asyncio
    async def test_csv_contains_not_on_streaming(self, populated_db, tmp_path):
        output_path = str(tmp_path / "report.csv")
        await generate_csv_report(populated_db, output_path)

        with open(output_path) as f:
            reader = csv.DictReader(f)
            rows = list(reader)

        # Only Anne Gillis should be in the "not on streaming" report
        assert len(rows) == 1
        assert rows[0]["artist"] == "Anne Gillis"
        assert rows[0]["title"] == "Eyry"
        assert rows[0]["genre"] == "Rock"
        assert rows[0]["label"] == "Art into Life"

    @pytest.mark.asyncio
    async def test_csv_returns_stats(self, populated_db, tmp_path):
        output_path = str(tmp_path / "report.csv")
        stats = await generate_csv_report(populated_db, output_path)
        assert stats["total"] == 4


class TestGenerateSummary:
    @pytest.mark.asyncio
    async def test_summary_includes_counts(self, populated_db):
        summary = await generate_summary(populated_db)
        assert "4" in summary  # total
        assert "Spotify" in summary or "spotify" in summary.lower()
        assert "Apple" in summary or "apple" in summary.lower()
