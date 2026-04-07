"""Tests for the VA disambiguation tracking database."""

import pytest
import pytest_asyncio

from scripts.va_disambiguate.models import (
    CompilationRelease,
    CompilationTrackArtist,
    FlowsheetEntry,
    LibraryCodeEntry,
    ResolutionResult,
)
from scripts.va_disambiguate.results_db import ResultsDB


@pytest_asyncio.fixture
async def db(tmp_path):
    """Create a temporary results database."""
    rdb = ResultsDB(str(tmp_path / "test.db"))
    await rdb.connect()
    yield rdb
    await rdb.close()


@pytest.mark.asyncio
class TestFlowsheetEntries:
    async def test_insert_and_count(self, db: ResultsDB):
        entries = [
            FlowsheetEntry(
                id=1, artist_name="V/A", song_title="odo akosomo", release_title="Vintage Palmwine"
            ),
            FlowsheetEntry(
                id=2,
                artist_name="Various Artists",
                song_title="Pineapple",
                release_title="Indian Soundscapes",
            ),
        ]
        count = await db.insert_flowsheet_entries(entries)
        assert count == 2
        assert await db.count_flowsheet_entries() == 2

    async def test_insert_is_idempotent(self, db: ResultsDB):
        entry = FlowsheetEntry(id=1, artist_name="V/A", song_title="test")
        await db.insert_flowsheet_entries([entry])
        await db.insert_flowsheet_entries([entry])  # duplicate
        assert await db.count_flowsheet_entries() == 1

    async def test_get_pending_entries(self, db: ResultsDB):
        entries = [
            FlowsheetEntry(id=1, artist_name="V/A", song_title="track1"),
            FlowsheetEntry(id=2, artist_name="V/A", song_title="track2"),
        ]
        await db.insert_flowsheet_entries(entries)
        pending = await db.get_pending_flowsheet_entries()
        assert len(pending) == 2

    async def test_mark_resolved_removes_from_pending(self, db: ResultsDB):
        entries = [
            FlowsheetEntry(id=1, artist_name="V/A", song_title="track1"),
            FlowsheetEntry(id=2, artist_name="V/A", song_title="track2"),
        ]
        await db.insert_flowsheet_entries(entries)
        result = ResolutionResult(
            entry_id=1,
            resolved_artist="Koo Nimo",
            confidence=0.90,
            strategy="exact",
        )
        await db.save_flowsheet_resolution(result)
        pending = await db.get_pending_flowsheet_entries()
        assert len(pending) == 1
        assert pending[0].id == 2

    async def test_mark_unresolved(self, db: ResultsDB):
        entry = FlowsheetEntry(id=1, artist_name="V/A", song_title="unknown")
        await db.insert_flowsheet_entries([entry])
        await db.mark_flowsheet_unresolved(1, "no_discogs_match")
        pending = await db.get_pending_flowsheet_entries()
        assert len(pending) == 0

    async def test_get_resolved_entries(self, db: ResultsDB):
        entry = FlowsheetEntry(
            id=1, artist_name="V/A", song_title="odo akosomo", release_title="Vintage Palmwine"
        )
        await db.insert_flowsheet_entries([entry])
        result = ResolutionResult(
            entry_id=1,
            resolved_artist="Koo Nimo",
            confidence=0.90,
            strategy="exact",
            library_code_id=42,
        )
        await db.save_flowsheet_resolution(result)
        resolved = await db.get_resolved_flowsheet_entries()
        assert len(resolved) == 1
        assert resolved[0]["resolved_artist"] == "Koo Nimo"
        assert resolved[0]["library_code_id"] == 42


@pytest.mark.asyncio
class TestCompilationReleases:
    async def test_insert_and_count(self, db: ResultsDB):
        releases = [
            CompilationRelease(id=100, title="Vintage Palmwine", library_code_id=50),
            CompilationRelease(id=200, title="Indian Soundscapes", library_code_id=51),
        ]
        count = await db.insert_compilation_releases(releases)
        assert count == 2

    async def test_get_pending_releases(self, db: ResultsDB):
        releases = [
            CompilationRelease(id=100, title="Vintage Palmwine", library_code_id=50),
        ]
        await db.insert_compilation_releases(releases)
        pending = await db.get_pending_catalog_releases()
        assert len(pending) == 1

    async def test_save_track_artists_removes_from_pending(self, db: ResultsDB):
        releases = [
            CompilationRelease(id=100, title="Vintage Palmwine", library_code_id=50),
        ]
        await db.insert_compilation_releases(releases)
        track_artists = [
            CompilationTrackArtist(
                library_release_id=100, artist_name="Koo Nimo", track_title="Odo Akosomo"
            ),
            CompilationTrackArtist(
                library_release_id=100, artist_name="T.O. Jazz", track_title="Waytime Ama"
            ),
        ]
        await db.save_catalog_track_artists(100, track_artists)
        pending = await db.get_pending_catalog_releases()
        assert len(pending) == 0

    async def test_get_all_track_artists(self, db: ResultsDB):
        releases = [CompilationRelease(id=100, title="Vintage Palmwine", library_code_id=50)]
        await db.insert_compilation_releases(releases)
        track_artists = [
            CompilationTrackArtist(
                library_release_id=100, artist_name="Koo Nimo", track_title="Odo Akosomo"
            ),
            CompilationTrackArtist(
                library_release_id=100, artist_name="T.O. Jazz", track_title="Waytime Ama"
            ),
        ]
        await db.save_catalog_track_artists(100, track_artists)
        all_ta = await db.get_all_track_artists()
        assert len(all_ta) == 2
        assert all_ta[0]["artist_name"] == "Koo Nimo"
        assert all_ta[0]["track_title"] == "Odo Akosomo"


@pytest.mark.asyncio
class TestLibraryCodes:
    async def test_insert_and_retrieve(self, db: ResultsDB):
        codes = [
            LibraryCodeEntry(id=1, presentation_name="Stereolab", call_letters="S-"),
            LibraryCodeEntry(id=2, presentation_name="Cat Power", call_letters="C-"),
        ]
        await db.insert_library_codes(codes)
        all_codes = await db.get_all_library_codes()
        assert len(all_codes) == 2


@pytest.mark.asyncio
class TestStats:
    async def test_stats_empty_db(self, db: ResultsDB):
        stats = await db.get_stats()
        assert stats["flowsheet_total"] == 0
        assert stats["flowsheet_resolved"] == 0
        assert stats["flowsheet_unresolved"] == 0
        assert stats["flowsheet_pending"] == 0
        assert stats["catalog_total"] == 0
        assert stats["catalog_enriched"] == 0
        assert stats["catalog_pending"] == 0
