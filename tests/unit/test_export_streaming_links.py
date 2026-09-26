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
            # The streaming artifact is opened with a `file:...?mode=ro` URI plus
            # uri=True, so this stub has to tolerate the extra kwarg.
            mock_sqlite3.connect = lambda path, **kw: (
                real_sa if "streaming" in path else tracking_lib
            )

            main(args)

        real_sa.close()
        tracking_lib._real.close()

        # With 10,001 rows and commits every 10,000, we expect at least 2 commits
        assert tracking_lib.commit_count > 1


# ---------------------------------------------------------------------------
# Spotify match-provenance gate (LML#1352 / LML#1353)
# ---------------------------------------------------------------------------

# The `albums` columns the production artifact carries, in the order used by the
# helper below. Deliberately a *subset* of the real schema (results_db.py holds
# the full one) -- just enough that the provenance gate has something to read
# and the display columns make a fixture legible.
_PROVENANCE_ALBUMS_DDL = """
    CREATE TABLE albums (
        id INTEGER PRIMARY KEY,
        display_artist TEXT,
        display_title TEXT,
        library_ids TEXT,
        is_compilation INTEGER NOT NULL DEFAULT 0,
        spotify_status TEXT,
        spotify_url TEXT,
        spotify_confidence REAL,
        spotify_matched_artist TEXT,
        spotify_matched_title TEXT,
        spotify_checked_at TEXT,
        apple_url TEXT,
        deezer_url TEXT,
        bandcamp_url TEXT,
        tidal_url TEXT,
        youtube_music_url TEXT,
        soundcloud_url TEXT
    )
"""


def _create_provenance_streaming_db(path, albums, track_results=None):
    """Create a streaming_availability.db whose `albums` table carries provenance.

    `albums` is a list of dicts keyed by column name; anything omitted is NULL.
    `track_results`, when given, creates the supplement table too.
    """
    sa = sqlite3.connect(path)
    sa.execute(_PROVENANCE_ALBUMS_DDL)
    for album in albums:
        cols = ", ".join(album)
        placeholders = ", ".join("?" for _ in album)
        sa.execute(
            f"INSERT INTO albums ({cols}) VALUES ({placeholders})",  # noqa: S608 (test fixture)
            tuple(album.values()),
        )
    if track_results is not None:
        sa.execute("""
            CREATE TABLE track_results (
                id INTEGER PRIMARY KEY,
                album_id INTEGER NOT NULL,
                resolution_status TEXT,
                spotify_url TEXT,
                deezer_url TEXT
            )
        """)
        for track in track_results:
            cols = ", ".join(track)
            placeholders = ", ".join("?" for _ in track)
            sa.execute(
                f"INSERT INTO track_results ({cols}) VALUES ({placeholders})",  # noqa: S608
                tuple(track.values()),
            )
    sa.commit()
    sa.close()


def _export(tmp_path, albums, track_results=None, dry_run=False):
    """Run main() over a provenance-shaped fixture; return the library.db path."""
    streaming_db = str(tmp_path / "streaming.db")
    library_db = str(tmp_path / "library.db")
    _create_provenance_streaming_db(streaming_db, albums, track_results)
    sqlite3.connect(library_db).close()
    args = argparse.Namespace(library_db=library_db, streaming_db=streaming_db, dry_run=dry_run)
    main(args)
    return library_db


def _streaming_link(library_db, library_id, column="spotify_url"):
    conn = sqlite3.connect(library_db)
    try:
        row = conn.execute(
            f"SELECT {column} FROM streaming_links WHERE library_id = ?",  # noqa: S608
            (library_id,),
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row else None


class TestSpotifyProvenanceGate:
    """A spotify_url with no record of what was matched must not be exported.

    See LML#1352: `lookup/enrichment/item.py` serves the exported URL and labels it
    `streaming_status.spotify = "verified"`. A row whose `spotify_matched_artist`
    and `spotify_matched_title` are both absent carries no record of what was ever
    compared, so nothing downstream can audit it -- and 363 of them are every
    compilation row in the artifact that has a Spotify URL (LML#1353).
    """

    @pytest.mark.parametrize(
        "matched_artist,matched_title",
        [
            (None, None),
            ("", ""),
            ("   ", "\t"),
        ],
        ids=["both-null", "both-empty", "both-whitespace"],
    )
    def test_absent_provenance_skips_spotify_url(self, tmp_path, matched_artist, matched_title):
        """The three blankness shapes that are distinct computations.

        `(None, "")` and `("", None)` are deliberately absent: the predicate is
        ``(x or "").strip()`` per axis, so they reduce to the same two evaluations
        as `("", "")` and could not fail independently of it. The production row
        this gate was written for is pinned separately, by
        `test_row_52656_married_to_the_mob_regression`.
        """
        library_db = _export(
            tmp_path,
            [
                {
                    "id": 1,
                    "display_artist": "Sessa",
                    "display_title": "Pequena Vertigem de Amor",
                    "library_ids": json.dumps([601]),
                    "spotify_url": "https://open.spotify.com/album/unverifiable",
                    "spotify_matched_artist": matched_artist,
                    "spotify_matched_title": matched_title,
                    "apple_url": "https://music.apple.com/album/pequena-vertigem-de-amor",
                }
            ],
        )
        assert _streaming_link(library_db, 601) is None

    def test_absent_provenance_leaves_other_services_alone(self, tmp_path):
        """The gate is Spotify-only: every other service's URL for the row survives."""
        library_db = _export(
            tmp_path,
            [
                {
                    "id": 1,
                    "display_artist": "Soundtracks - M",
                    "display_title": "Married to the Mob",
                    "library_ids": json.dumps([601]),
                    "is_compilation": 1,
                    "spotify_url": "https://open.spotify.com/album/wrong-album",
                    "spotify_matched_artist": None,
                    "spotify_matched_title": None,
                    "apple_url": "https://music.apple.com/album/married-to-the-mob",
                    "deezer_url": "https://www.deezer.com/album/married-to-the-mob",
                    "bandcamp_url": "https://various.bandcamp.com/album/married-to-the-mob",
                    "tidal_url": "https://tidal.com/album/married-to-the-mob",
                    "youtube_music_url": "https://music.youtube.com/married-to-the-mob",
                    "soundcloud_url": "https://soundcloud.com/va/married-to-the-mob",
                }
            ],
        )
        conn = sqlite3.connect(library_db)
        row = conn.execute(
            "SELECT spotify_url, apple_music_url, deezer_url, bandcamp_url, tidal_url, "
            "youtube_music_url, soundcloud_url FROM streaming_links WHERE library_id = 601"
        ).fetchone()
        conn.close()
        assert row == (
            None,
            "https://music.apple.com/album/married-to-the-mob",
            "https://www.deezer.com/album/married-to-the-mob",
            "https://various.bandcamp.com/album/married-to-the-mob",
            "https://tidal.com/album/married-to-the-mob",
            "https://music.youtube.com/married-to-the-mob",
            "https://soundcloud.com/va/married-to-the-mob",
        )

    def test_full_provenance_exports_spotify_url(self, tmp_path):
        library_db = _export(
            tmp_path,
            [
                {
                    "id": 1,
                    "display_artist": "Stereolab",
                    "display_title": "Aluminum Tunes",
                    "library_ids": json.dumps([701]),
                    "spotify_url": "https://open.spotify.com/album/aluminum-tunes",
                    "spotify_matched_artist": "Stereolab",
                    "spotify_matched_title": "Aluminum Tunes",
                }
            ],
        )
        assert _streaming_link(library_db, 701) == "https://open.spotify.com/album/aluminum-tunes"

    @pytest.mark.parametrize("source_tag", ["backfill-wiki (spotify)", "llm+wikidata"])
    def test_source_tag_without_matched_title_still_exports(self, tmp_path, source_tag):
        """The gate is NOT widened to the 18,281 tag rows (LML#1353's second problem).

        Those rows hold a writer tag rather than an artist in `spotify_matched_artist`
        and nothing in `spotify_matched_title`. Their measured defect is URL *shape*
        (Spotify artist pages), fixed at the serve seam by the sibling branch
        `fix/streaming-link-album-shape-guard` -- not absent provenance. Nulling 39%
        of Spotify coverage on a provenance technicality is not warranted by anything
        measured, so one non-empty field is enough to pass this gate.

        Two representative tag shapes, not the whole roster: the gate never inspects
        the value, so more tags would exercise the same branch. One carries a
        parenthesized service suffix and one does not; the full measured vocabulary
        is recorded in `_has_match_provenance`'s docstring.
        """
        library_db = _export(
            tmp_path,
            [
                {
                    "id": 1,
                    "display_artist": "Juana Molina",
                    "display_title": "DOGA",
                    "library_ids": json.dumps([702]),
                    "spotify_url": "https://open.spotify.com/album/doga",
                    "spotify_matched_artist": source_tag,
                    "spotify_matched_title": "",
                }
            ],
        )
        assert _streaming_link(library_db, 702) == "https://open.spotify.com/album/doga"

    def test_matched_title_alone_still_exports(self, tmp_path):
        """Provenance is "entirely absent" only when BOTH fields are empty."""
        library_db = _export(
            tmp_path,
            [
                {
                    "id": 1,
                    "display_artist": "Jessica Pratt",
                    "display_title": "On Your Own Love Again",
                    "library_ids": json.dumps([703]),
                    "spotify_url": "https://open.spotify.com/album/on-your-own-love-again",
                    "spotify_matched_artist": None,
                    "spotify_matched_title": "On Your Own Love Again",
                }
            ],
        )
        assert (
            _streaming_link(library_db, 703)
            == "https://open.spotify.com/album/on-your-own-love-again"
        )

    def test_row_52656_married_to_the_mob_regression(self, tmp_path):
        """The production case a DJ hit on 2026-09-25 (LML#1352).

        artifact row `albums.id = 52656`: display_artist "Soundtracks - M",
        display_title "Married to the Mob", is_compilation 1, spotify_confidence
        89.4736842105263, provenance columns all NULL, and a spotify_url pointing at
        Speaker Knockerz' "Married to the Money" (2013).

        SCOPED, and the scope is the gate's real limit: this fixture has no
        `track_results` row, and the track supplement runs AFTER the gate and refills
        an empty Spotify slot. So this pins "the gated ALBUM-level URL is not
        exported", not "this release ends up with no Spotify URL" -- see
        `test_gate_does_not_block_the_track_level_supplement` for the other half. The
        supplement exists for singles and compilations, which is 363 of the 388 gated
        rows, so the overlap between the gated rows and `track_results` rows with
        `resolution_status IN ('local_match','api_match')` is what decides how much of
        the reported symptom this change actually removes. That number is NOT measured
        here and is not knowable from this repo; it is routed with the rest of the
        serve-seam decision rather than assumed to be zero.
        """
        library_db = _export(
            tmp_path,
            [
                {
                    "id": 52656,
                    "display_artist": "Soundtracks - M",
                    "display_title": "Married to the Mob",
                    "library_ids": json.dumps([60671]),
                    "is_compilation": 1,
                    "spotify_status": "found",
                    "spotify_url": "https://open.spotify.com/album/0JSLTbVe6Z70EQkOLL0WPi",
                    "spotify_confidence": 89.4736842105263,
                    "spotify_matched_artist": None,
                    "spotify_matched_title": None,
                    "spotify_checked_at": None,
                }
            ],
        )
        conn = sqlite3.connect(library_db)
        urls = [r[0] for r in conn.execute("SELECT spotify_url FROM streaming_links").fetchall()]
        conn.close()
        assert "https://open.spotify.com/album/0JSLTbVe6Z70EQkOLL0WPi" not in urls

    @pytest.mark.parametrize(
        "album_id,library_id,display_artist,display_title,matched_artist,matched_title,url",
        [
            (
                53632,
                53632001,
                "Lower Dens",
                "Nootropics",
                "Lower Dens",
                "Nootropics",
                "https://open.spotify.com/album/nootropics",
            ),
            (
                18555,
                18555001,
                "Don Covay & the Jefferson Lemon Blues Band",
                "The House of Blue Lights",
                "Don Covay & The Jefferson Lemon Blues Band",
                "House of the Blue Lights",
                "https://open.spotify.com/album/house-of-the-blue-lights",
            ),
            (
                56785,
                56785001,
                "Richard Fearless",
                "Kollektion 04: bureau B",
                "Richard Fearless",
                "Kollektion 04: Bureau B",
                "https://open.spotify.com/album/kollektion-04-bureau-b",
            ),
        ],
        ids=["lower-dens-nootropics", "don-covay-blue-lights", "kollektion-04"],
    )
    def test_artist_axis_mismatch_shapes_survive(
        self,
        tmp_path,
        album_id,
        library_id,
        display_artist,
        display_title,
        matched_artist,
        matched_title,
        url,
    ):
        """Correct links whose artist string disagrees with the WXYC shelf credit.

        LML#1147: for compilations, curator credits and expanded credits the artist
        axis carries no usable signal, so an 80/80 floor at this seam would false-
        reject these three. The provenance gate must not touch them -- all three have
        full provenance and confidence 100 in the artifact. LML#1352's acceptance
        criteria require this pin.
        """
        library_db = _export(
            tmp_path,
            [
                {
                    "id": album_id,
                    "display_artist": display_artist,
                    "display_title": display_title,
                    "library_ids": json.dumps([library_id]),
                    "spotify_status": "found",
                    "spotify_url": url,
                    "spotify_confidence": 100.0,
                    "spotify_matched_artist": matched_artist,
                    "spotify_matched_title": matched_title,
                    "spotify_checked_at": "2026-04-18T12:09:00",
                }
            ],
        )
        assert _streaming_link(library_db, library_id) == url

    def test_gate_logs_the_skipped_count(self, tmp_path, caplog):
        """The daily sync must leave evidence of what the gate removed.

        The fourth album pins the `and spotify` short-circuit: a row with no
        spotify_url has nothing for the gate to refuse, so it must not inflate the
        count even though its provenance is just as absent. Asserting the full
        message rather than a bare "3" matters because `caplog.text` also carries
        `Albums with streaming URLs: 4` and a `filename:lineno` per record, so a
        digit-only assertion passes even when the gate skips nothing.
        """
        albums = [
            {
                "id": i,
                "library_ids": json.dumps([800 + i]),
                "spotify_url": f"https://open.spotify.com/album/{i}",
                "spotify_matched_artist": None,
                "spotify_matched_title": None,
            }
            for i in range(3)
        ]
        albums.append(
            {
                "id": 3,
                "library_ids": json.dumps([803]),
                "apple_url": "https://music.apple.com/album/no-spotify-url-at-all",
                "spotify_matched_artist": None,
                "spotify_matched_title": None,
            }
        )
        with caplog.at_level(logging.INFO):
            _export(tmp_path, albums)
        assert "absent match provenance: 3" in caplog.text

    def test_gate_is_active_when_the_columns_are_declared_in_another_case(self, tmp_path, caplog):
        """SQLite column names are case-insensitive; the gate's column check must be too.

        `PRAGMA table_info` reports whatever case the column was DECLARED in, while
        the `SELECT` that reads it resolves case-insensitively. A case-sensitive
        presence check therefore fails open -- the gate goes inert on a database it
        could in fact have queried, which is the silent-disable path that matters in
        practice (a re-declared or migrated artifact, not a dropped column).
        """
        streaming_db = str(tmp_path / "streaming.db")
        library_db = str(tmp_path / "library.db")
        sa = sqlite3.connect(streaming_db)
        sa.execute("""
            CREATE TABLE albums (
                id INTEGER PRIMARY KEY,
                library_ids TEXT,
                spotify_url TEXT,
                Spotify_Matched_Artist TEXT,
                Spotify_Matched_Title TEXT,
                apple_url TEXT,
                deezer_url TEXT,
                bandcamp_url TEXT,
                tidal_url TEXT,
                youtube_music_url TEXT,
                soundcloud_url TEXT
            )
        """)
        sa.execute(
            "INSERT INTO albums (id, library_ids, spotify_url) VALUES (?, ?, ?)",
            (1, json.dumps([1201]), "https://open.spotify.com/album/unverifiable"),
        )
        sa.commit()
        sa.close()
        sqlite3.connect(library_db).close()

        args = argparse.Namespace(library_db=library_db, streaming_db=streaming_db, dry_run=False)
        with caplog.at_level(logging.WARNING):
            main(args)

        assert _streaming_link(library_db, 1201) is None
        assert "inert" not in caplog.text

    @pytest.mark.parametrize("gated_row_first", [True, False], ids=["gated-first", "gated-second"])
    def test_a_shared_library_id_keeps_the_provenance_bearing_url(self, tmp_path, gated_row_first):
        """One library_id covered by both a gated and a provenance-bearing album row.

        The gate runs inside the first-URL-wins merge loop, so its placement relative
        to that merge decides the outcome for a shared `library_id`. The good URL must
        win in either row order: gating must null the bad row's contribution rather
        than let it occupy the slot the good row would fill.
        """
        gated = {
            "id": 1,
            "library_ids": json.dumps([1301]),
            "spotify_url": "https://open.spotify.com/album/unverifiable",
            "spotify_matched_artist": None,
            "spotify_matched_title": None,
        }
        verifiable = {
            "id": 2,
            "library_ids": json.dumps([1301]),
            "spotify_url": "https://open.spotify.com/album/aluminum-tunes",
            "spotify_matched_artist": "Stereolab",
            "spotify_matched_title": "Aluminum Tunes",
        }
        albums = [gated, verifiable] if gated_row_first else [verifiable, gated]
        library_db = _export(tmp_path, albums)
        assert _streaming_link(library_db, 1301) == "https://open.spotify.com/album/aluminum-tunes"

    def test_gate_leaves_no_all_null_streaming_links_row(self, tmp_path):
        """A row whose only URL was the gated Spotify one is dropped, not blanked."""
        library_db = _export(
            tmp_path,
            [
                {
                    "id": 1,
                    "library_ids": json.dumps([901]),
                    "spotify_url": "https://open.spotify.com/album/unverifiable",
                    "spotify_matched_artist": "",
                    "spotify_matched_title": "",
                }
            ],
        )
        conn = sqlite3.connect(library_db)
        count = conn.execute("SELECT COUNT(*) FROM streaming_links").fetchone()[0]
        conn.close()
        assert count == 0

    def test_gate_is_inert_on_a_schema_without_provenance_columns(self, tmp_path, caplog):
        """A legacy/fixture `albums` table without the provenance columns still exports.

        discogs-etl's `tests/e2e/test_sync_library_e2e.py` builds exactly that shape
        and asserts the Spotify URL lands in `streaming_links`. Treating "column
        absent" as "provenance absent" would strip every URL from such a database.
        """
        streaming_db = str(tmp_path / "streaming.db")
        library_db = str(tmp_path / "library.db")
        _create_streaming_db(
            streaming_db,
            [
                (
                    json.dumps([1001]),
                    "https://open.spotify.com/album/stereolab-aluminum",
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                )
            ],
        )
        sqlite3.connect(library_db).close()
        args = argparse.Namespace(library_db=library_db, streaming_db=streaming_db, dry_run=False)
        with caplog.at_level(logging.WARNING):
            main(args)

        assert (
            _streaming_link(library_db, 1001) == "https://open.spotify.com/album/stereolab-aluminum"
        )
        assert "provenance" in caplog.text.lower()

    def test_dry_run_reports_coverage_after_the_gate(self, tmp_path, caplog):
        """--dry-run's service coverage counts the gated row out, so the delta is visible."""
        albums = [
            {
                "id": 1,
                "library_ids": json.dumps([1101]),
                "spotify_url": "https://open.spotify.com/album/unverifiable",
                "spotify_matched_artist": None,
                "spotify_matched_title": None,
                "apple_url": "https://music.apple.com/album/verifiable",
            },
            {
                "id": 2,
                "library_ids": json.dumps([1102]),
                "spotify_url": "https://open.spotify.com/album/verifiable",
                "spotify_matched_artist": "Sessa",
                "spotify_matched_title": "Pequena Vertigem de Amor",
            },
        ]
        with caplog.at_level(logging.INFO):
            _export(tmp_path, albums, dry_run=True)

        assert "spotify_url: 1" in caplog.text
        assert "apple_music_url: 1" in caplog.text

    def test_gate_does_not_block_the_track_level_supplement(self, tmp_path):
        """DEFERRED, pinned so it cannot drift silently (LML#1353).

        The track supplement writes a `/track/`-shaped `tr.spotify_url` into the
        *album* field `entry["spotify_url"]`, which is the origin of the 841
        `/track/` values Backend-Service serves (WXYC/Backend-Service#2689). Gating
        the album-level URL leaves that slot open, so a gated row with a resolved
        track can be refilled from the supplement. Whether it should be is a
        coverage decision routed separately -- this test records the behavior as it
        stands rather than changing it.
        """
        library_db = _export(
            tmp_path,
            [
                {
                    "id": 52656,
                    "library_ids": json.dumps([60671]),
                    "spotify_url": "https://open.spotify.com/album/0JSLTbVe6Z70EQkOLL0WPi",
                    "spotify_matched_artist": None,
                    "spotify_matched_title": None,
                }
            ],
            track_results=[
                {
                    "id": 1,
                    "album_id": 52656,
                    "resolution_status": "api_match",
                    "spotify_url": "https://open.spotify.com/track/a-real-track",
                }
            ],
        )
        assert _streaming_link(library_db, 60671) == "https://open.spotify.com/track/a-real-track"

    def test_streaming_db_is_never_written(self, tmp_path):
        """The artifact is precious and read-only to this script.

        `streaming_availability.db` is the single bucket-canonical copy of
        rate-limited Apple/Spotify/Deezer results. The gate decides what to *export*;
        it must not repair, null or otherwise touch the source. Hash the file rather
        than trusting the absence of an UPDATE by inspection.
        """
        import hashlib

        streaming_db = tmp_path / "streaming.db"
        library_db = str(tmp_path / "library.db")
        _create_provenance_streaming_db(
            str(streaming_db),
            [
                {
                    "id": 52656,
                    "library_ids": json.dumps([60671]),
                    "spotify_url": "https://open.spotify.com/album/0JSLTbVe6Z70EQkOLL0WPi",
                    "spotify_matched_artist": None,
                    "spotify_matched_title": None,
                },
                {
                    "id": 53632,
                    "library_ids": json.dumps([53632001]),
                    "spotify_url": "https://open.spotify.com/album/nootropics",
                    "spotify_matched_artist": "Lower Dens",
                    "spotify_matched_title": "Nootropics",
                },
            ],
        )
        before = hashlib.sha256(streaming_db.read_bytes()).hexdigest()
        sqlite3.connect(library_db).close()

        args = argparse.Namespace(
            library_db=library_db, streaming_db=str(streaming_db), dry_run=False
        )
        main(args)

        assert hashlib.sha256(streaming_db.read_bytes()).hexdigest() == before
        assert not (tmp_path / "streaming.db-wal").exists()
        assert not (tmp_path / "streaming.db-journal").exists()

    def test_streaming_db_is_opened_read_only(self, tmp_path):
        """ "Never written" must be structural, not a happy-path observation.

        The sha256 test above only proves no write happened to a cleanly-closed
        file. Opening a SQLite database read-WRITE also performs hot-journal
        rollback on open, so if the upstream streaming pipeline died mid-transaction,
        merely connecting would mutate the bucket-canonical artifact -- a file
        CLAUDE.md classes as expensive-to-recollect. Asserting the handle refuses
        writes pins the guarantee to the open mode rather than to this script
        happening to contain no UPDATE.
        """
        streaming_db = str(tmp_path / "streaming.db")
        library_db = str(tmp_path / "library.db")
        _create_provenance_streaming_db(
            streaming_db,
            [
                {
                    "id": 1,
                    "library_ids": json.dumps([1401]),
                    "spotify_url": "https://open.spotify.com/album/aluminum-tunes",
                    "spotify_matched_artist": "Stereolab",
                    "spotify_matched_title": "Aluminum Tunes",
                }
            ],
        )
        sqlite3.connect(library_db).close()

        real_connect = sqlite3.connect
        calls: list[tuple] = []

        def _tracking_connect(*a, **kw):
            calls.append((a, kw))
            return real_connect(*a, **kw)

        args = argparse.Namespace(library_db=library_db, streaming_db=streaming_db, dry_run=False)
        with patch("scripts.export_streaming_links.sqlite3.connect", _tracking_connect):
            main(args)

        streaming_calls = [c for c in calls if "streaming.db" in str(c[0][0])]
        assert streaming_calls, "the script never opened the streaming artifact"
        # Re-open the artifact exactly the way the script did; that handle must
        # refuse a write. This asserts the open MODE, not a particular URI spelling.
        a, kw = streaming_calls[0]
        probe = real_connect(*a, **kw)
        try:
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                probe.execute("CREATE TABLE write_probe (x)")
        finally:
            probe.close()
