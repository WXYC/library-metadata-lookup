"""Unit tests for scripts/export_streaming_links.py."""

import argparse
import json
import logging
import sqlite3
from unittest.mock import patch

import pytest

from scripts.export_streaming_links import main


class TestCliArgs:
    def test_defaults(self):
        parser = argparse.ArgumentParser()
        parser.add_argument("--library-db", default="library.db")
        parser.add_argument("--streaming-db", default="streaming_availability.db")
        parser.add_argument("--dry-run", action="store_true")
        args = parser.parse_args([])
        assert args.library_db == "library.db"
        assert args.streaming_db == "streaming_availability.db"

    def test_custom_paths_override_defaults(self):
        parser = argparse.ArgumentParser()
        parser.add_argument("--library-db", default="library.db")
        parser.add_argument("--streaming-db", default="streaming_availability.db")
        parser.add_argument("--dry-run", action="store_true")
        args = parser.parse_args(["--library-db", "/tmp/foo.db", "--streaming-db", "/tmp/bar.db"])
        assert args.library_db == "/tmp/foo.db"
        assert args.streaming_db == "/tmp/bar.db"


class TestMissingStreamingDb:
    def test_exits_gracefully_when_streaming_db_missing(self, tmp_path, caplog):
        """When the streaming DB path doesn't exist, main() should log an error and return."""
        library_db = str(tmp_path / "library.db")
        missing_streaming_db = str(tmp_path / "nonexistent.db")

        args = argparse.Namespace(
            library_db=library_db,
            streaming_db=missing_streaming_db,
            dry_run=False,
        )

        import logging

        with caplog.at_level(logging.ERROR):
            main(args)

        assert "does not exist" in caplog.text
        # Should NOT have created the library.db since we bailed out
        import os

        assert not os.path.exists(library_db)


class TestFullExport:
    def test_creates_streaming_links_table(self, tmp_path):
        """Create minimal SQLite databases, run main(), verify streaming_links table."""
        streaming_db_path = str(tmp_path / "streaming_availability.db")
        library_db_path = str(tmp_path / "library.db")

        # Set up streaming_availability.db with test data
        sa = sqlite3.connect(streaming_db_path)
        sa.execute("""
            CREATE TABLE albums (
                library_ids TEXT,
                spotify_url TEXT,
                apple_url TEXT,
                deezer_url TEXT,
                bandcamp_url TEXT,
                tidal_url TEXT,
                youtube_music_url TEXT,
                soundcloud_url TEXT
            )
        """)
        sa.execute(
            "INSERT INTO albums VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                json.dumps([101, 102]),
                "https://open.spotify.com/album/stereolab-aluminum-tunes",
                "https://music.apple.com/album/stereolab-aluminum-tunes",
                None,
                None,
                None,
                None,
                None,
            ),
        )
        sa.execute(
            "INSERT INTO albums VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                json.dumps([201]),
                None,
                None,
                "https://www.deezer.com/album/autechre-confield",
                "https://autechre.bandcamp.com/album/confield",
                None,
                None,
                None,
            ),
        )
        # Album with no streaming URLs at all -- should be excluded
        sa.execute(
            "INSERT INTO albums VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                json.dumps([301]),
                None,
                None,
                None,
                None,
                None,
                None,
                None,
            ),
        )
        sa.commit()
        sa.close()

        # Set up an empty library.db
        lib = sqlite3.connect(library_db_path)
        lib.close()

        args = argparse.Namespace(
            library_db=library_db_path,
            streaming_db=streaming_db_path,
            dry_run=False,
        )
        main(args)

        # Verify streaming_links table
        lib = sqlite3.connect(library_db_path)
        rows = lib.execute(
            "SELECT library_id, spotify_url, apple_music_url, deezer_url, bandcamp_url "
            "FROM streaming_links ORDER BY library_id"
        ).fetchall()
        lib.close()

        assert len(rows) == 3  # IDs 101, 102, 201

        # Stereolab -- library_id 101
        assert rows[0] == (
            101,
            "https://open.spotify.com/album/stereolab-aluminum-tunes",
            "https://music.apple.com/album/stereolab-aluminum-tunes",
            None,
            None,
        )
        # Stereolab -- library_id 102 (same album, second library ID)
        assert rows[1] == (
            102,
            "https://open.spotify.com/album/stereolab-aluminum-tunes",
            "https://music.apple.com/album/stereolab-aluminum-tunes",
            None,
            None,
        )
        # Autechre -- library_id 201
        assert rows[2] == (
            201,
            None,
            None,
            "https://www.deezer.com/album/autechre-confield",
            "https://autechre.bandcamp.com/album/confield",
        )


# ---------------------------------------------------------------------------
# Duplicate handling
# ---------------------------------------------------------------------------


def _create_streaming_db(path, album_rows):
    """Helper to create a streaming_availability.db with the given album rows."""
    sa = sqlite3.connect(path)
    sa.execute("""
        CREATE TABLE albums (
            library_ids TEXT,
            spotify_url TEXT,
            apple_url TEXT,
            deezer_url TEXT,
            bandcamp_url TEXT,
            tidal_url TEXT,
            youtube_music_url TEXT,
            soundcloud_url TEXT
        )
    """)
    for row in album_rows:
        sa.execute("INSERT INTO albums VALUES (?, ?, ?, ?, ?, ?, ?, ?)", row)
    sa.commit()
    sa.close()


class TestDuplicateHandling:
    def test_first_url_wins_for_same_library_id(self, tmp_path):
        """Two album rows map to the same library_id with different spotify_urls. First URL wins."""
        streaming_db = str(tmp_path / "streaming.db")
        library_db = str(tmp_path / "library.db")

        _create_streaming_db(
            streaming_db,
            [
                # First row: Autechre Confield with spotify
                (
                    json.dumps([101]),
                    "https://open.spotify.com/album/autechre-confield-FIRST",
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                ),
                # Second row: same library_id, different spotify
                (
                    json.dumps([101]),
                    "https://open.spotify.com/album/autechre-confield-SECOND",
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                ),
            ],
        )
        sqlite3.connect(library_db).close()

        args = argparse.Namespace(library_db=library_db, streaming_db=streaming_db, dry_run=False)
        main(args)

        lib = sqlite3.connect(library_db)
        row = lib.execute(
            "SELECT spotify_url FROM streaming_links WHERE library_id = 101"
        ).fetchone()
        lib.close()

        assert row[0] == "https://open.spotify.com/album/autechre-confield-FIRST"

    def test_different_services_merge_for_same_id(self, tmp_path):
        """Row 1 has spotify for lib_id=101, Row 2 has apple_music for lib_id=101. Both appear."""
        streaming_db = str(tmp_path / "streaming.db")
        library_db = str(tmp_path / "library.db")

        _create_streaming_db(
            streaming_db,
            [
                # First row: spotify only
                (
                    json.dumps([101]),
                    "https://open.spotify.com/album/stereolab-aluminum-tunes",
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                ),
                # Second row: apple_music only
                (
                    json.dumps([101]),
                    None,
                    "https://music.apple.com/album/stereolab-aluminum-tunes",
                    None,
                    None,
                    None,
                    None,
                    None,
                ),
            ],
        )
        sqlite3.connect(library_db).close()

        args = argparse.Namespace(library_db=library_db, streaming_db=streaming_db, dry_run=False)
        main(args)

        lib = sqlite3.connect(library_db)
        row = lib.execute(
            "SELECT spotify_url, apple_music_url FROM streaming_links WHERE library_id = 101"
        ).fetchone()
        lib.close()

        assert row[0] == "https://open.spotify.com/album/stereolab-aluminum-tunes"
        assert row[1] == "https://music.apple.com/album/stereolab-aluminum-tunes"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_invalid_json_in_library_ids(self, tmp_path):
        """Row with library_ids='not json' should raise json.JSONDecodeError."""
        streaming_db = str(tmp_path / "streaming.db")
        library_db = str(tmp_path / "library.db")

        _create_streaming_db(
            streaming_db,
            [
                (
                    "not json",
                    "https://open.spotify.com/album/cat-power-moon-pix",
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                ),
            ],
        )
        sqlite3.connect(library_db).close()

        args = argparse.Namespace(library_db=library_db, streaming_db=streaming_db, dry_run=False)
        with pytest.raises(json.JSONDecodeError):
            main(args)

    def test_empty_array_library_ids(self, tmp_path):
        """library_ids='[]' produces no streaming_links rows for this album."""
        streaming_db = str(tmp_path / "streaming.db")
        library_db = str(tmp_path / "library.db")

        _create_streaming_db(
            streaming_db,
            [
                (
                    json.dumps([]),
                    "https://open.spotify.com/album/jessica-pratt",
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                ),
            ],
        )
        sqlite3.connect(library_db).close()

        args = argparse.Namespace(library_db=library_db, streaming_db=streaming_db, dry_run=False)
        main(args)

        lib = sqlite3.connect(library_db)
        count = lib.execute("SELECT COUNT(*) FROM streaming_links").fetchone()[0]
        lib.close()

        assert count == 0

    def test_all_services_populated(self, tmp_path):
        """Row with all 7 URL columns filled. All 7 appear in output."""
        streaming_db = str(tmp_path / "streaming.db")
        library_db = str(tmp_path / "library.db")

        urls = (
            "https://open.spotify.com/album/cat-power-moon-pix",
            "https://music.apple.com/album/cat-power-moon-pix",
            "https://www.deezer.com/album/cat-power-moon-pix",
            "https://catpower.bandcamp.com/album/moon-pix",
            "https://tidal.com/album/cat-power-moon-pix",
            "https://music.youtube.com/cat-power-moon-pix",
            "https://soundcloud.com/cat-power/moon-pix",
        )
        _create_streaming_db(
            streaming_db,
            [(json.dumps([101]), *urls)],
        )
        sqlite3.connect(library_db).close()

        args = argparse.Namespace(library_db=library_db, streaming_db=streaming_db, dry_run=False)
        main(args)

        lib = sqlite3.connect(library_db)
        row = lib.execute(
            "SELECT spotify_url, apple_music_url, deezer_url, bandcamp_url, "
            "tidal_url, youtube_music_url, soundcloud_url "
            "FROM streaming_links WHERE library_id = 101"
        ).fetchone()
        lib.close()

        assert row == urls

    def test_only_bandcamp_populated(self, tmp_path):
        """Row with only bandcamp_url set. Included in output."""
        streaming_db = str(tmp_path / "streaming.db")
        library_db = str(tmp_path / "library.db")

        _create_streaming_db(
            streaming_db,
            [
                (
                    json.dumps([201]),
                    None,
                    None,
                    None,
                    "https://autechre.bandcamp.com/album/confield",
                    None,
                    None,
                    None,
                ),
            ],
        )
        sqlite3.connect(library_db).close()

        args = argparse.Namespace(library_db=library_db, streaming_db=streaming_db, dry_run=False)
        main(args)

        lib = sqlite3.connect(library_db)
        row = lib.execute(
            "SELECT bandcamp_url FROM streaming_links WHERE library_id = 201"
        ).fetchone()
        lib.close()

        assert row[0] == "https://autechre.bandcamp.com/album/confield"

    def test_all_null_urls_excluded(self, tmp_path):
        """Row where all 7 URL columns are NULL is NOT in output (WHERE clause filters it)."""
        streaming_db = str(tmp_path / "streaming.db")
        library_db = str(tmp_path / "library.db")

        _create_streaming_db(
            streaming_db,
            [
                (json.dumps([301]), None, None, None, None, None, None, None),
            ],
        )
        sqlite3.connect(library_db).close()

        args = argparse.Namespace(library_db=library_db, streaming_db=streaming_db, dry_run=False)
        main(args)

        lib = sqlite3.connect(library_db)
        count = lib.execute("SELECT COUNT(*) FROM streaming_links").fetchone()[0]
        lib.close()

        assert count == 0


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------


class TestDryRun:
    def test_dry_run_does_not_modify_library_db(self, tmp_path):
        """Run with dry_run=True. Library DB should NOT have a streaming_links table."""
        streaming_db = str(tmp_path / "streaming.db")
        library_db = str(tmp_path / "library.db")

        _create_streaming_db(
            streaming_db,
            [
                (
                    json.dumps([101]),
                    "https://open.spotify.com/album/stereolab-aluminum-tunes",
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                ),
            ],
        )
        sqlite3.connect(library_db).close()

        args = argparse.Namespace(library_db=library_db, streaming_db=streaming_db, dry_run=True)
        main(args)

        lib = sqlite3.connect(library_db)
        tables = lib.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='streaming_links'"
        ).fetchall()
        lib.close()

        assert len(tables) == 0

    def test_dry_run_logs_coverage_stats(self, tmp_path, caplog):
        """dry_run=True logs service counts."""
        streaming_db = str(tmp_path / "streaming.db")
        library_db = str(tmp_path / "library.db")

        _create_streaming_db(
            streaming_db,
            [
                (
                    json.dumps([101]),
                    "https://open.spotify.com/album/stereolab-aluminum-tunes",
                    None,
                    None,
                    "https://stereolab.bandcamp.com/album/aluminum-tunes",
                    None,
                    None,
                    None,
                ),
            ],
        )
        sqlite3.connect(library_db).close()

        args = argparse.Namespace(library_db=library_db, streaming_db=streaming_db, dry_run=True)
        with caplog.at_level(logging.INFO):
            main(args)

        assert "spotify_url" in caplog.text
        assert "bandcamp_url" in caplog.text
        assert "Service coverage" in caplog.text


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


class TestIdempotency:
    def test_reexport_replaces_streaming_links(self, tmp_path):
        """Run main() twice with different data. Second run's data wins."""
        streaming_db = str(tmp_path / "streaming.db")
        library_db = str(tmp_path / "library.db")

        # First export: Stereolab with spotify
        _create_streaming_db(
            streaming_db,
            [
                (
                    json.dumps([101]),
                    "https://open.spotify.com/album/stereolab-aluminum-tunes",
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                ),
            ],
        )
        sqlite3.connect(library_db).close()

        args = argparse.Namespace(library_db=library_db, streaming_db=streaming_db, dry_run=False)
        main(args)

        # Verify first export
        lib = sqlite3.connect(library_db)
        count1 = lib.execute("SELECT COUNT(*) FROM streaming_links").fetchone()[0]
        lib.close()
        assert count1 == 1

        # Second export: different data (Cat Power with deezer)
        import os

        os.remove(streaming_db)
        _create_streaming_db(
            streaming_db,
            [
                (
                    json.dumps([201]),
                    None,
                    None,
                    "https://www.deezer.com/album/cat-power-moon-pix",
                    None,
                    None,
                    None,
                    None,
                ),
            ],
        )

        main(args)

        # Second run's data should replace first
        lib = sqlite3.connect(library_db)
        rows = lib.execute("SELECT library_id, deezer_url FROM streaming_links").fetchall()
        lib.close()

        assert len(rows) == 1
        assert rows[0][0] == 201
        assert rows[0][1] == "https://www.deezer.com/album/cat-power-moon-pix"


# ---------------------------------------------------------------------------
# Commits
# ---------------------------------------------------------------------------


class _CommitTrackingConnection:
    """Wrapper around sqlite3.Connection that counts commit() calls."""

    def __init__(self, real_conn):
        self._real = real_conn
        self.commit_count = 0

    def commit(self):
        self.commit_count += 1
        self._real.commit()

    def __getattr__(self, name):
        return getattr(self._real, name)


class TestCommitBehavior:
    def test_commit_called_periodically(self, tmp_path):
        """Create >10k album rows. Verify lib.commit is called more than once."""
        streaming_db = str(tmp_path / "streaming.db")
        library_db = str(tmp_path / "library.db")

        # Create streaming DB with 10,001 albums, each mapping to a unique library_id
        sa = sqlite3.connect(streaming_db)
        sa.execute("""
            CREATE TABLE albums (
                library_ids TEXT,
                spotify_url TEXT,
                apple_url TEXT,
                deezer_url TEXT,
                bandcamp_url TEXT,
                tidal_url TEXT,
                youtube_music_url TEXT,
                soundcloud_url TEXT
            )
        """)
        rows = [
            (
                json.dumps([i]),
                f"https://open.spotify.com/album/{i}",
                None,
                None,
                None,
                None,
                None,
                None,
            )
            for i in range(10_001)
        ]
        sa.executemany("INSERT INTO albums VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
        sa.commit()
        sa.close()

        sqlite3.connect(library_db).close()

        args = argparse.Namespace(library_db=library_db, streaming_db=streaming_db, dry_run=False)

        real_sa = sqlite3.connect(streaming_db)
        tracking_lib = _CommitTrackingConnection(sqlite3.connect(library_db))

        with patch("scripts.export_streaming_links.sqlite3") as mock_sqlite3:
            mock_sqlite3.connect = lambda path: real_sa if "streaming" in path else tracking_lib

            main(args)

        real_sa.close()
        tracking_lib._real.close()

        # With 10,001 rows and commits every 10,000, we expect at least 2 commits
        assert tracking_lib.commit_count > 1
