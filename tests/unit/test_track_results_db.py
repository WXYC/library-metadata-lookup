"""Tests for track_results table CRUD in ResultsDB."""

import pytest
import pytest_asyncio

from scripts.streaming_availability.results_db import ResultsDB


@pytest_asyncio.fixture
async def db(tmp_path):
    results_db = ResultsDB(str(tmp_path / "test.db"))
    await results_db.connect()
    yield results_db
    await results_db.close()


class TestTrackResultsSchema:
    """Test that the track_results table is created on connect."""

    @pytest.mark.asyncio
    async def test_table_exists(self, db):
        cursor = await db._db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='track_results'"
        )
        row = await cursor.fetchone()
        assert row is not None

    @pytest.mark.asyncio
    async def test_table_columns(self, db):
        cursor = await db._db.execute("PRAGMA table_info(track_results)")
        columns = {row[1] for row in await cursor.fetchall()}
        expected = {
            "id",
            "album_id",
            "artist",
            "title",
            "position",
            "source",
            "source_type",
            "resolution_status",
            "resolved_via",
            "resolved_album_id",
            "resolved_release_id",
            "spotify_url",
            "spotify_confidence",
            "deezer_url",
            "deezer_confidence",
            "checked_at",
            "created_at",
        }
        assert expected.issubset(columns)


class TestInsertTracks:
    @pytest.mark.asyncio
    async def test_insert_and_count(self, db):
        tracks = [
            {
                "album_id": 1,
                "artist": "Codeine",
                "title": "Pickup Song",
                "position": "A",
                "source": "title_parse",
                "source_type": "single",
            },
            {
                "album_id": 1,
                "artist": "Codeine",
                "title": "3 Angels",
                "position": "B",
                "source": "title_parse",
                "source_type": "single",
            },
        ]
        inserted = await db.insert_tracks(tracks)
        assert inserted == 2

    @pytest.mark.asyncio
    async def test_insert_deduplicates(self, db):
        track = {
            "album_id": 1,
            "artist": "Codeine",
            "title": "Pickup Song",
            "position": "A",
            "source": "title_parse",
            "source_type": "single",
        }
        await db.insert_tracks([track])
        inserted = await db.insert_tracks([track])
        assert inserted == 0


class TestGetPendingTracks:
    @pytest.mark.asyncio
    async def test_returns_pending_only(self, db):
        tracks = [
            {
                "album_id": 1,
                "artist": "Codeine",
                "title": "Pickup Song",
                "position": "A",
                "source": "title_parse",
                "source_type": "single",
            },
            {
                "album_id": 1,
                "artist": "Codeine",
                "title": "3 Angels",
                "position": "B",
                "source": "title_parse",
                "source_type": "single",
            },
        ]
        await db.insert_tracks(tracks)
        # Resolve one track
        await db.update_track_resolution(
            track_id=1,
            status="local_match",
            resolved_via="local_index",
            resolved_album_id=99,
            spotify_url="https://open.spotify.com/track/abc",
            spotify_confidence=95.0,
        )
        pending = await db.get_pending_tracks(limit=100)
        assert len(pending) == 1
        assert pending[0]["title"] == "3 Angels"


class TestUpdateTrackResolution:
    @pytest.mark.asyncio
    async def test_local_match(self, db):
        await db.insert_tracks(
            [
                {
                    "album_id": 1,
                    "artist": "Codeine",
                    "title": "Pickup Song",
                    "position": "A",
                    "source": "title_parse",
                    "source_type": "single",
                }
            ]
        )
        await db.update_track_resolution(
            track_id=1,
            status="local_match",
            resolved_via="local_index",
            resolved_album_id=42,
            spotify_url="https://open.spotify.com/track/xyz",
            spotify_confidence=90.0,
        )
        cursor = await db._db.execute("SELECT * FROM track_results WHERE id = 1")
        row = await cursor.fetchone()
        assert row["resolution_status"] == "local_match"
        assert row["resolved_via"] == "local_index"
        assert row["resolved_album_id"] == 42
        assert row["spotify_url"] == "https://open.spotify.com/track/xyz"
        assert row["checked_at"] is not None

    @pytest.mark.asyncio
    async def test_not_found(self, db):
        await db.insert_tracks(
            [
                {
                    "album_id": 1,
                    "artist": "Codeine",
                    "title": "Pickup Song",
                    "position": "A",
                    "source": "title_parse",
                    "source_type": "single",
                }
            ]
        )
        await db.update_track_resolution(track_id=1, status="not_found")
        cursor = await db._db.execute("SELECT * FROM track_results WHERE id = 1")
        row = await cursor.fetchone()
        assert row["resolution_status"] == "not_found"
        assert row["resolved_via"] is None


class TestTrackStats:
    @pytest.mark.asyncio
    async def test_stats_by_status(self, db):
        tracks = [
            {
                "album_id": 1,
                "artist": "A",
                "title": "T1",
                "position": None,
                "source": "title_parse",
                "source_type": "single",
            },
            {
                "album_id": 1,
                "artist": "A",
                "title": "T2",
                "position": None,
                "source": "title_parse",
                "source_type": "single",
            },
            {
                "album_id": 2,
                "artist": "B",
                "title": "T3",
                "position": None,
                "source": "discogs_tracklist",
                "source_type": "compilation",
            },
        ]
        await db.insert_tracks(tracks)
        await db.update_track_resolution(
            track_id=1, status="local_match", resolved_via="local_index"
        )
        stats = await db.get_track_stats()
        assert stats["pending"] == 2
        assert stats["local_match"] == 1
        assert stats["total"] == 3


class TestAlbumTrackSummary:
    @pytest.mark.asyncio
    async def test_any_resolved_means_on_streaming(self, db):
        tracks = [
            {
                "album_id": 10,
                "artist": "X",
                "title": "A",
                "position": None,
                "source": "title_parse",
                "source_type": "single",
            },
            {
                "album_id": 10,
                "artist": "X",
                "title": "B",
                "position": None,
                "source": "title_parse",
                "source_type": "single",
            },
        ]
        await db.insert_tracks(tracks)
        await db.update_track_resolution(
            track_id=1, status="local_match", resolved_via="local_index"
        )
        await db.update_track_resolution(track_id=2, status="not_found")
        summary = await db.get_album_track_summary(album_id=10)
        assert summary["total"] == 2
        assert summary["resolved"] == 1
        assert summary["not_found"] == 1
        assert summary["on_streaming"] is True

    @pytest.mark.asyncio
    async def test_all_not_found_means_off_streaming(self, db):
        await db.insert_tracks(
            [
                {
                    "album_id": 20,
                    "artist": "Y",
                    "title": "C",
                    "position": None,
                    "source": "title_parse",
                    "source_type": "single",
                }
            ]
        )
        await db.update_track_resolution(track_id=1, status="not_found")
        summary = await db.get_album_track_summary(album_id=20)
        assert summary["on_streaming"] is False
