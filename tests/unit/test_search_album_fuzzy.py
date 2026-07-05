"""Behavior tests for ``lookup.strategies.track_release_matching.search_album_fuzzy``.

The reproducer comes from a real lookup miss: ``Tenant by Siouxsie and the
Banshees``. The library has three releases literally titled "Kaleidoscope"
(Kaleidoscope by DJ Food, by Siouxsie and the Banshees, and by Peter
Batchelor), plus several near-titled releases ("Kaleidoscope World",
"Keyboard Kaleidoscope", "Greetings from Cartoonistan" by an artist named
Kaleidoscope, etc.). Discogs surfaces "Tenant" on Siouxsie's *Kaleidoscope*,
and downstream library matching is expected to recover Siouxsie's row.

The bug: ``search_album_fuzzy`` truncates the FTS5 candidate set to the first
five rowids (no ``ORDER BY rank``), which drops the highest-rowid row even
when the title is a literal exact match.

These tests verify the public behavior: any library row whose title literally
matches the query (case-insensitive) is returned, regardless of how many
fuzzy near-titles outrank it by rowid in the library.

The end-to-end perform_lookup test reproduces the production miss against a
real fixture library so the actual SQLite/FTS5 ordering quirk participates in
the trace.
"""

import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from discogs.models import DiscogsSearchResponse, ReleaseInfo, TrackReleasesResponse
from library.db import LibraryDB
from lookup.models import LookupRequest
from lookup.orchestrator import perform_lookup
from lookup.strategies.track_release_matching import search_album_fuzzy
from tests.conftest import make_lml_telemetry


def _create_kaleidoscope_library(tmp_path: Path) -> Path:
    """Build a small FTS-backed library that triggers the rowid-truncation bug.

    Row layout (rowid order):

        100   Kaleidoscope        DJ Food                     <-- low rowid exact-title
        200   Kaleidoscope World  Chills                       <-- decoy, fuzzy-only
        201   Kaleidoscope World  Chills                       <-- decoy, fuzzy-only (dup)
        300   Greetings from ...  Kaleidoscope (band name)    <-- decoy, artist-only match
        400   Keyboard Kaleido... Dick Hyman                   <-- decoy, fuzzy-only
        4990  The Scream         Siouxsie and the Banshees     <-- artist-fallback decoy
        ...
        4994  Nocturne           Siouxsie and the Banshees     <-- artist-fallback decoy
        5000  Kaleidoscope        Siouxsie and the Banshees   <-- the one we lose
        60000 Kaleidoscope        Peter Batchelor              <-- exact, also high rowid

    The five low-rowid Siouxsie rows (4990-4994) mirror SI 11/1-11/5 in
    production: they fill ``limit_results(...)`` at MAX_SEARCH_RESULTS=5 and
    hide SI 11/6 (Siouxsie's *Kaleidoscope*) from the artist-only fallback.
    """
    db_file = tmp_path / "library.db"
    conn = sqlite3.connect(db_file)
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
    conn.execute("""
        CREATE VIRTUAL TABLE library_fts USING fts5(
            title, artist, alternate_artist_name,
            content='library', content_rowid='id'
        )
    """)

    rows = [
        (100, "Kaleidoscope", "DJ Food", "DJ", 1, 1, "Electronic", "cd", "", "Ninja Tune"),
        (200, "Kaleidoscope World", "Chills", "CH", 2, 1, "Rock", "cd", "", "Flying Nun"),
        (201, "Kaleidoscope World", "Chills", "CH", 2, 2, "Rock", "cd", "", "Flying Nun"),
        (
            300,
            "Greetings from Cartoonistan",
            "Kaleidoscope",
            "KA",
            3,
            1,
            "Rock",
            "cd",
            "",
            "self-released",
        ),
        (400, "Keyboard Kaleidoscope", "Dick Hyman", "HY", 4, 1, "Jazz", "cd", "", "Project 3"),
        (
            4990,
            "The Scream",
            "Siouxsie and the Banshees",
            "SI",
            11,
            1,
            "Rock",
            "vinyl",
            "",
            "Polydor",
        ),
        (4991, "Juju", "Siouxsie and the Banshees", "SI", 11, 2, "Rock", "vinyl", "", "Polydor"),
        (
            4992,
            "Once Upon a Time: The Singles",
            "Siouxsie and the Banshees",
            "SI",
            11,
            3,
            "Rock",
            "vinyl",
            "",
            "Polydor",
        ),
        (
            4993,
            "Dear Prudence",
            "Siouxsie and the Banshees",
            "SI",
            11,
            4,
            "Rock",
            "vinyl",
            "",
            "Polydor",
        ),
        (
            4994,
            "Nocturne",
            "Siouxsie and the Banshees",
            "SI",
            11,
            5,
            "Rock",
            "vinyl",
            "",
            "Polydor",
        ),
        (
            5000,
            "Kaleidoscope",
            "Siouxsie and the Banshees",
            "SI",
            11,
            6,
            "Rock",
            "vinyl",
            "",
            "Polydor",
        ),
        (
            60000,
            "Kaleidoscope",
            "Peter Batchelor",
            "BA",
            5,
            1,
            "Electronic",
            "cd",
            "",
            "empreintes DIGITALes",
        ),
    ]
    conn.executemany(
        "INSERT INTO library VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.executemany(
        "INSERT INTO library_fts(rowid, title, artist, alternate_artist_name) VALUES (?, ?, ?, ?)",
        [(r[0], r[1], r[2], r[8]) for r in rows],
    )
    conn.commit()
    conn.close()
    return db_file


class TestSearchAlbumFuzzyExactTitle:
    """``search_album_fuzzy`` must return every literal-title match.

    Discogs hands us an album title; we expect the library row whose title is
    exactly that string to be a candidate every time, even when there are
    enough fuzzy near-titles to bury it in the FTS rowid stream.
    """

    @pytest.mark.asyncio
    async def test_returns_all_three_literal_kaleidoscope_rows(self, tmp_path):
        db = LibraryDB(db_path=_create_kaleidoscope_library(tmp_path))
        await db.connect()
        try:
            results = await search_album_fuzzy(db, "Kaleidoscope")
            returned_ids = {item.id for item in results}
        finally:
            await db.close()

        # All three rows whose title is literally "Kaleidoscope" must be in the
        # candidate set, regardless of rowid position. The bug drops id=5000
        # and id=60000 because the FTS5 LIMIT 5 + rowid ordering cuts them off
        # before any title-based filtering runs.
        assert {100, 5000, 60000} <= returned_ids, (
            f"expected exact-title rows {{100, 5000, 60000}} in results, got {sorted(returned_ids)}"
        )

    @pytest.mark.asyncio
    async def test_strips_whitespace_around_discogs_titles(self, tmp_path):
        """Discogs payloads occasionally carry trailing whitespace; an
        unnormalized literal-equality check would otherwise quietly fall
        through to the FTS5 path and re-expose the rowid-truncation bug."""
        db = LibraryDB(db_path=_create_kaleidoscope_library(tmp_path))
        await db.connect()
        try:
            results = await search_album_fuzzy(db, "  Kaleidoscope  ")
            returned_ids = {item.id for item in results}
        finally:
            await db.close()

        assert {100, 5000, 60000} <= returned_ids, (
            "Whitespace-padded titles must match the literal-title pre-pass; "
            f"got {sorted(returned_ids)}"
        )

    @pytest.mark.asyncio
    async def test_matches_when_query_case_differs_from_library(self, tmp_path):
        """Library cataloguing uses Title Case; Discogs occasionally hands us
        a lower-case or all-caps title. The pre-pass must be case-insensitive
        so a casing mismatch doesn't fall through to FTS5."""
        # Build a tiny library with the row stored Title Case; query lower-case.
        db_file = tmp_path / "library.db"
        conn = sqlite3.connect(db_file)
        conn.execute("""
            CREATE TABLE library (
                id INTEGER PRIMARY KEY, title TEXT, artist TEXT, call_letters TEXT,
                artist_call_number INTEGER, release_call_number INTEGER, genre TEXT,
                format TEXT, alternate_artist_name TEXT, label TEXT
            )
        """)
        conn.execute("""
            CREATE VIRTUAL TABLE library_fts USING fts5(
                title, artist, alternate_artist_name,
                content='library', content_rowid='id'
            )
        """)
        rows = [
            (1, "Kaleidoscope", "DJ Food", "DJ", 1, 1, "Electronic", "cd", "", "Ninja Tune"),
        ]
        conn.executemany("INSERT INTO library VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
        conn.executemany(
            "INSERT INTO library_fts(rowid, title, artist, alternate_artist_name) VALUES (?, ?, ?, ?)",
            [(r[0], r[1], r[2], r[8]) for r in rows],
        )
        conn.commit()
        conn.close()

        db = LibraryDB(db_path=db_file)
        await db.connect()
        try:
            results = await search_album_fuzzy(db, "kaleidoscope")
            ids_lower = {r.id for r in results}
            results_upper = await search_album_fuzzy(db, "KALEIDOSCOPE")
            ids_upper = {r.id for r in results_upper}
        finally:
            await db.close()

        assert 1 in ids_lower, f"lower-case query must match Title Case row; got {ids_lower}"
        assert 1 in ids_upper, f"upper-case query must match Title Case row; got {ids_upper}"

    @pytest.mark.asyncio
    async def test_does_not_truncate_common_titles(self, tmp_path):
        """Library has 150+ "Greatest Hits" / "Best Of" / "Live" rows in
        production; the literal-title pre-pass must return all of them so
        downstream artist filters can find the right one. A LIMIT cap would
        recreate the rowid-truncation bug at a higher threshold."""
        db_file = tmp_path / "library.db"
        conn = sqlite3.connect(db_file)
        conn.execute("""
            CREATE TABLE library (
                id INTEGER PRIMARY KEY, title TEXT, artist TEXT, call_letters TEXT,
                artist_call_number INTEGER, release_call_number INTEGER, genre TEXT,
                format TEXT, alternate_artist_name TEXT, label TEXT
            )
        """)
        conn.execute("""
            CREATE VIRTUAL TABLE library_fts USING fts5(
                title, artist, alternate_artist_name,
                content='library', content_rowid='id'
            )
        """)
        rows = [
            (i, "Greatest Hits", f"Artist {i:03d}", "X", 1, 1, "Rock", "cd", "", "Label")
            for i in range(1, 101)  # 100 rows literally titled "Greatest Hits"
        ]
        conn.executemany("INSERT INTO library VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
        conn.executemany(
            "INSERT INTO library_fts(rowid, title, artist, alternate_artist_name) VALUES (?, ?, ?, ?)",
            [(r[0], r[1], r[2], r[8]) for r in rows],
        )
        conn.commit()
        conn.close()

        db = LibraryDB(db_path=db_file)
        await db.connect()
        try:
            results = await search_album_fuzzy(db, "Greatest Hits")
        finally:
            await db.close()

        assert len(results) == 100, (
            f"all 100 literal-title rows must surface so artist filtering can pick the right one; "
            f"got {len(results)}"
        )

    @pytest.mark.asyncio
    async def test_returns_high_rowid_exact_title_match(self, tmp_path):
        """Tracer-bullet shape of the production miss.

        Mirrors the production lookup for ``Tenant by Siouxsie and the
        Banshees`` after Discogs surfaces *Kaleidoscope* as the track-bearing
        release. The library row for Siouxsie's *Kaleidoscope* sits at a high
        rowid (5000 in the fixture, 43595 in production); pre-fix, FTS5 with
        ``LIMIT 5`` and no ``ORDER BY rank`` drops it.
        """
        db = LibraryDB(db_path=_create_kaleidoscope_library(tmp_path))
        await db.connect()
        try:
            results = await search_album_fuzzy(db, "Kaleidoscope")
        finally:
            await db.close()

        siouxsie_rows = [
            item
            for item in results
            if (item.artist or "").startswith("Siouxsie")
            and (item.title or "").lower() == "kaleidoscope"
        ]
        assert siouxsie_rows, (
            "search_album_fuzzy must surface Siouxsie's Kaleidoscope row; "
            f"got titles {[(r.id, r.title, r.artist) for r in results]}"
        )


class TestPerformLookupRecoversTenantOnKaleidoscope:
    """End-to-end reproducer for the Tenant / Siouxsie / Kaleidoscope miss.

    Discogs surfaces ``Kaleidoscope`` as a release containing the track
    ``Tenant`` by Siouxsie and the Banshees. ``search_compilations_for_track``
    then calls ``search_album_fuzzy(db, "Kaleidoscope")`` to map that Discogs
    title back to a WXYC library row. Pre-fix, FTS5's rowid-ordered ``LIMIT 5``
    truncation drops Siouxsie's library row before any artist filter runs;
    post-fix, the exact-title pre-pass recovers it.

    Drives ``perform_lookup`` against a real fixture library so the actual
    SQLite/FTS5 ordering quirk participates in the trace, end to end.
    """

    @pytest.mark.asyncio
    async def test_promotes_kaleidoscope_when_discogs_surfaces_release(self, tmp_path):
        db = LibraryDB(db_path=_create_kaleidoscope_library(tmp_path))
        await db.connect()
        try:
            telemetry = make_lml_telemetry()
            discogs = AsyncMock()
            # Discogs's artist-scoped track probe returns the canonical
            # Kaleidoscope release — what Discogs production would return for
            # "Tenant by Siouxsie and the Banshees".
            kaleidoscope_release = ReleaseInfo(
                album="Kaleidoscope",
                artist="Siouxsie and the Banshees",
                release_id=104287,
                release_url="https://discogs.com/release/104287",
                is_compilation=False,
            )
            discogs.search_releases_by_track = AsyncMock(
                return_value=TrackReleasesResponse(
                    track="Tenant",
                    artist="Siouxsie and the Banshees",
                    releases=[kaleidoscope_release],
                    total=1,
                    cached=False,
                )
            )
            # The release truly contains the track.
            discogs.validate_track_on_release = AsyncMock(return_value=True)
            # Artwork search is irrelevant for this assertion.
            discogs.search = AsyncMock(return_value=DiscogsSearchResponse(results=[]))
            # No resolver pre-pass interference.
            discogs.cache_service = None

            request = LookupRequest(
                artist="Siouxsie and the Banshees",
                song="Tenant",
                raw_message="Tenant, Siouxsie and the Banshees",
            )

            with patch(
                "lookup.orchestrator.lookup_releases_by_track",
                new_callable=AsyncMock,
                return_value=[],
            ):
                response = await perform_lookup(request, db, discogs, telemetry)

            promoted_titles = [r.library_item.title for r in response.results]
            promoted_artists = [r.library_item.artist for r in response.results]
        finally:
            await db.close()

        assert "Kaleidoscope" in promoted_titles, (
            "Discogs surfaced Kaleidoscope; library matching must recover "
            f"Siouxsie's row. got titles {promoted_titles}"
        )
        kaleidoscope_artists = [
            promoted_artists[i] for i, t in enumerate(promoted_titles) if t == "Kaleidoscope"
        ]
        assert any((a or "").startswith("Siouxsie") for a in kaleidoscope_artists), (
            "Recovered Kaleidoscope must be Siouxsie's, not DJ Food's or "
            f"Peter Batchelor's; got artists {kaleidoscope_artists}"
        )
        assert response.song_not_found is False, (
            "A confirmed track-bearing album should clear song_not_found"
        )
