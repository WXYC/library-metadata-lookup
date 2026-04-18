"""Unit tests for scripts/export_streaming_links.py."""

import argparse
import json
import sqlite3

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
