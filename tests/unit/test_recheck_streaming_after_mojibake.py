"""Tests for the post-mojibake streaming recheck script.

V012 corrects mojibake'd artist names in tubafrenzy MySQL (e.g. 'Î¼-ziq' → 'μ-ziq').
After library.db is re-synced, releases by these artists need a fresh streaming
availability check because earlier runs queried Spotify/Deezer with the corrupted
form and recorded `not_found`.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from scripts.recheck_streaming_after_mojibake import (
    MOJIBAKE_CORRECTED_ARTISTS,
    AffectedRelease,
    find_affected_releases,
    recheck_releases,
    write_report,
)


def _build_library_db(path: Path, rows: list[tuple]) -> None:
    """Build a minimal library.db with the columns the script reads."""
    conn = sqlite3.connect(str(path))
    conn.execute(
        """CREATE TABLE library (
            id INTEGER PRIMARY KEY,
            artist TEXT,
            title TEXT,
            album_artist TEXT
        )"""
    )
    conn.executemany(
        "INSERT INTO library (id, artist, title, album_artist) VALUES (?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()


class TestMojibakeCorrectedArtists:
    """The hardcoded list mirrors V012's ARTIST_NAME corrections."""

    def test_includes_v012_artist_name_corrections(self):
        # Source: /tmp/V012__fix-mojibake-extended.sql, ARTIST_NAME block
        assert "μ-ziq" in MOJIBAKE_CORRECTED_ARTISTS
        assert "Σtella, Las Palabras" in MOJIBAKE_CORRECTED_ARTISTS
        assert "Luboš Fišer" in MOJIBAKE_CORRECTED_ARTISTS
        assert "GrOun士 + Kabamix" in MOJIBAKE_CORRECTED_ARTISTS
        assert "Mustafah 'Abd Al'-Azīz" in MOJIBAKE_CORRECTED_ARTISTS


class TestFindAffectedReleases:
    def test_matches_artist_column(self, tmp_path):
        db = tmp_path / "library.db"
        _build_library_db(
            db,
            [
                (1, "μ-ziq", "Lunatic Harness", None),
                (2, "Stereolab", "Aluminum Tunes", None),
            ],
        )
        affected = find_affected_releases(db, ["μ-ziq"])
        assert [r.library_id for r in affected] == [1]
        assert affected[0].artist == "μ-ziq"
        assert affected[0].title == "Lunatic Harness"

    def test_matches_album_artist_column(self, tmp_path):
        db = tmp_path / "library.db"
        _build_library_db(
            db,
            [
                (1, "Various Artists", "Some Comp", "Σtella, Las Palabras"),
                (2, "Other", "Other", "Different"),
            ],
        )
        affected = find_affected_releases(db, ["Σtella, Las Palabras"])
        assert [r.library_id for r in affected] == [1]
        # Prefer the matching album_artist value over the generic 'Various Artists'.
        assert affected[0].artist == "Σtella, Las Palabras"

    def test_skips_unrelated_releases(self, tmp_path):
        db = tmp_path / "library.db"
        _build_library_db(
            db,
            [
                (1, "Stereolab", "Aluminum Tunes", None),
                (2, "Cat Power", "Moon Pix", None),
            ],
        )
        affected = find_affected_releases(db, ["μ-ziq", "Σtella, Las Palabras"])
        assert affected == []

    def test_dedupes_when_artist_and_album_artist_both_match(self, tmp_path):
        db = tmp_path / "library.db"
        _build_library_db(db, [(1, "μ-ziq", "Lunatic Harness", "μ-ziq")])
        affected = find_affected_releases(db, ["μ-ziq"])
        assert len(affected) == 1

    def test_empty_corrected_list_returns_empty(self, tmp_path):
        db = tmp_path / "library.db"
        _build_library_db(db, [(1, "μ-ziq", "Lunatic Harness", None)])
        assert find_affected_releases(db, []) == []

    def test_works_when_album_artist_column_missing(self, tmp_path):
        db = tmp_path / "library.db"
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE library (id INTEGER PRIMARY KEY, artist TEXT, title TEXT)")
        conn.execute(
            "INSERT INTO library (id, artist, title) VALUES (1, 'μ-ziq', 'Lunatic Harness')"
        )
        conn.commit()
        conn.close()
        affected = find_affected_releases(db, ["μ-ziq"])
        assert [r.library_id for r in affected] == [1]


class TestRecheckReleases:
    @pytest.mark.asyncio
    async def test_calls_streaming_check_with_corrected_artist(self):
        release = AffectedRelease(library_id=1, artist="μ-ziq", title="Lunatic Harness")
        spotify = AsyncMock()
        spotify.search_album = AsyncMock(
            return_value=[
                {
                    "artists": [{"name": "μ-ziq"}],
                    "name": "Lunatic Harness",
                    "external_urls": {"spotify": "https://open.spotify.com/album/x"},
                    "id": "x",
                }
            ]
        )

        results = await recheck_releases([release], spotify=spotify)

        assert len(results) == 1
        assert results[0].library_id == 1
        assert results[0].on_streaming is True
        assert results[0].sources["spotify"] == "https://open.spotify.com/album/x"
        spotify.search_album.assert_awaited_with("μ-ziq", "Lunatic Harness")

    @pytest.mark.asyncio
    async def test_returns_false_when_no_match(self):
        release = AffectedRelease(library_id=2, artist="μ-ziq", title="Bluff Limbo")
        spotify = AsyncMock()
        spotify.search_album = AsyncMock(return_value=[])

        results = await recheck_releases([release], spotify=spotify)

        assert results[0].on_streaming is False
        assert results[0].sources["spotify"] is None

    @pytest.mark.asyncio
    async def test_returns_none_when_no_clients(self):
        release = AffectedRelease(library_id=1, artist="μ-ziq", title="Lunatic Harness")
        results = await recheck_releases([release])
        assert results[0].on_streaming is None


class TestWriteReport:
    def test_writes_csv_with_header_and_results(self, tmp_path):
        from scripts.recheck_streaming_after_mojibake import RecheckResult

        results = [
            RecheckResult(
                library_id=1,
                artist="μ-ziq",
                title="Lunatic Harness",
                on_streaming=True,
                sources={
                    "spotify": "https://open.spotify.com/album/x",
                    "deezer": None,
                    "apple_music": None,
                    "bandcamp": None,
                },
            ),
            RecheckResult(
                library_id=2,
                artist="μ-ziq",
                title="Bluff Limbo",
                on_streaming=False,
                sources={"spotify": None, "deezer": None, "apple_music": None, "bandcamp": None},
            ),
        ]
        out = tmp_path / "report.csv"
        write_report(results, out)

        text = out.read_text(encoding="utf-8")
        # Diacritics survive the round-trip.
        assert "μ-ziq" in text
        assert "Lunatic Harness" in text
        assert "https://open.spotify.com/album/x" in text
        # First non-header row is the True result; CSV writes 1/0 for the boolean.
        lines = text.splitlines()
        assert lines[0].startswith("library_id,")
        assert ",1," in lines[1]
        assert ",0," in lines[2]
