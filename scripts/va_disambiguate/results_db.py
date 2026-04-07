"""SQLite tracking database for VA disambiguation progress.

Stores extracted flowsheet entries, compilation releases, and library codes.
Tracks resolution status so the script can resume from where it left off.
"""

from __future__ import annotations

import aiosqlite

from scripts.va_disambiguate.models import (
    CompilationRelease,
    CompilationTrackArtist,
    FlowsheetEntry,
    LibraryCodeEntry,
    ResolutionResult,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS flowsheet_entries (
    id INTEGER PRIMARY KEY,
    artist_name TEXT NOT NULL,
    song_title TEXT,
    release_title TEXT,
    library_release_id INTEGER,
    artist_id INTEGER,
    status TEXT NOT NULL DEFAULT 'pending',
    resolved_artist TEXT,
    confidence REAL,
    strategy TEXT,
    library_code_id INTEGER,
    unresolved_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_flowsheet_status ON flowsheet_entries(status);

CREATE TABLE IF NOT EXISTS compilation_releases (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    library_code_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    artist_count INTEGER,
    unresolved_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_catalog_status ON compilation_releases(status);

CREATE TABLE IF NOT EXISTS compilation_track_artists (
    library_release_id INTEGER NOT NULL,
    artist_name TEXT NOT NULL,
    track_title TEXT,
    UNIQUE(library_release_id, artist_name, track_title)
);
CREATE INDEX IF NOT EXISTS idx_cta_release ON compilation_track_artists(library_release_id);

CREATE TABLE IF NOT EXISTS library_codes (
    id INTEGER PRIMARY KEY,
    presentation_name TEXT NOT NULL,
    call_letters TEXT NOT NULL,
    genre_id INTEGER
);
"""


class ResultsDB:
    """Async SQLite client for the VA disambiguation tracking database."""

    def __init__(self, db_path: str = "va_disambiguate.db"):
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._db = await aiosqlite.connect(self._db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(_SCHEMA)
        await self._db.commit()

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    # ── Flowsheet entries ──

    async def insert_flowsheet_entries(self, entries: list[FlowsheetEntry]) -> int:
        """Bulk insert flowsheet entries. Idempotent (INSERT OR IGNORE). Returns count inserted."""
        assert self._db is not None
        inserted = 0
        for entry in entries:
            cursor = await self._db.execute(
                """INSERT OR IGNORE INTO flowsheet_entries
                   (id, artist_name, song_title, release_title, library_release_id, artist_id)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (entry.id, entry.artist_name, entry.song_title, entry.release_title,
                 entry.library_release_id, entry.artist_id),
            )
            inserted += cursor.rowcount
        await self._db.commit()
        return inserted

    async def count_flowsheet_entries(self) -> int:
        assert self._db is not None
        row = await self._db.execute_fetchall("SELECT COUNT(*) FROM flowsheet_entries")
        return row[0][0]

    async def get_pending_flowsheet_entries(self) -> list[FlowsheetEntry]:
        assert self._db is not None
        cursor = await self._db.execute(
            "SELECT id, artist_name, song_title, release_title, library_release_id, artist_id "
            "FROM flowsheet_entries WHERE status = 'pending'"
        )
        rows = await cursor.fetchall()
        return [FlowsheetEntry(**dict(row)) for row in rows]

    async def save_flowsheet_resolution(self, result: ResolutionResult) -> None:
        assert self._db is not None
        await self._db.execute(
            """UPDATE flowsheet_entries
               SET status = 'resolved', resolved_artist = ?, confidence = ?,
                   strategy = ?, library_code_id = ?
               WHERE id = ?""",
            (result.resolved_artist, result.confidence, result.strategy,
             result.library_code_id, result.entry_id),
        )
        await self._db.commit()

    async def mark_flowsheet_unresolved(self, entry_id: int, reason: str) -> None:
        assert self._db is not None
        await self._db.execute(
            "UPDATE flowsheet_entries SET status = 'unresolved', unresolved_reason = ? WHERE id = ?",
            (reason, entry_id),
        )
        await self._db.commit()

    async def get_resolved_flowsheet_entries(self) -> list[dict]:
        assert self._db is not None
        cursor = await self._db.execute(
            "SELECT id, artist_name, song_title, release_title, resolved_artist, "
            "confidence, strategy, library_code_id "
            "FROM flowsheet_entries WHERE status = 'resolved'"
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    # ── Compilation releases ──

    async def insert_compilation_releases(self, releases: list[CompilationRelease]) -> int:
        assert self._db is not None
        inserted = 0
        for release in releases:
            cursor = await self._db.execute(
                """INSERT OR IGNORE INTO compilation_releases
                   (id, title, library_code_id)
                   VALUES (?, ?, ?)""",
                (release.id, release.title, release.library_code_id),
            )
            inserted += cursor.rowcount
        await self._db.commit()
        return inserted

    async def get_pending_catalog_releases(self) -> list[CompilationRelease]:
        assert self._db is not None
        cursor = await self._db.execute(
            "SELECT id, title, library_code_id "
            "FROM compilation_releases WHERE status = 'pending'"
        )
        rows = await cursor.fetchall()
        return [CompilationRelease(**dict(row)) for row in rows]

    async def save_catalog_track_artists(
        self, release_id: int, track_artists: list[CompilationTrackArtist]
    ) -> None:
        """Save resolved track artists for a compilation release."""
        assert self._db is not None
        for ta in track_artists:
            await self._db.execute(
                """INSERT OR IGNORE INTO compilation_track_artists
                   (library_release_id, artist_name, track_title)
                   VALUES (?, ?, ?)""",
                (ta.library_release_id, ta.artist_name, ta.track_title),
            )
        await self._db.execute(
            "UPDATE compilation_releases SET status = 'enriched', artist_count = ? WHERE id = ?",
            (len(track_artists), release_id),
        )
        await self._db.commit()

    async def mark_catalog_unresolved(self, release_id: int, reason: str) -> None:
        assert self._db is not None
        await self._db.execute(
            "UPDATE compilation_releases SET status = 'unresolved', unresolved_reason = ? WHERE id = ?",
            (reason, release_id),
        )
        await self._db.commit()

    async def get_all_track_artists(self) -> list[dict]:
        """Get all resolved track artists for SQL generation."""
        assert self._db is not None
        cursor = await self._db.execute(
            "SELECT library_release_id, artist_name, track_title "
            "FROM compilation_track_artists ORDER BY library_release_id"
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    # ── Library codes ──

    async def insert_library_codes(self, codes: list[LibraryCodeEntry]) -> None:
        assert self._db is not None
        for code in codes:
            await self._db.execute(
                "INSERT OR REPLACE INTO library_codes (id, presentation_name, call_letters, genre_id) "
                "VALUES (?, ?, ?, ?)",
                (code.id, code.presentation_name, code.call_letters, code.genre_id),
            )
        await self._db.commit()

    async def get_all_library_codes(self) -> list[dict]:
        assert self._db is not None
        cursor = await self._db.execute(
            "SELECT id, presentation_name, call_letters, genre_id FROM library_codes"
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    # ── Stats ──

    async def get_stats(self) -> dict:
        assert self._db is not None
        stats = {}

        for table, prefix in [("flowsheet_entries", "flowsheet"), ("compilation_releases", "catalog")]:
            row = await self._db.execute_fetchall(f"SELECT COUNT(*) FROM {table}")
            stats[f"{prefix}_total"] = row[0][0]

            row = await self._db.execute_fetchall(
                f"SELECT COUNT(*) FROM {table} WHERE status = 'resolved'" if prefix == "flowsheet"
                else f"SELECT COUNT(*) FROM {table} WHERE status = 'enriched'"
            )
            stats[f"{prefix}_{'resolved' if prefix == 'flowsheet' else 'enriched'}"] = row[0][0]

            row = await self._db.execute_fetchall(
                f"SELECT COUNT(*) FROM {table} WHERE status = 'unresolved'"
            )
            stats[f"{prefix}_unresolved"] = row[0][0]

            row = await self._db.execute_fetchall(
                f"SELECT COUNT(*) FROM {table} WHERE status = 'pending'"
            )
            stats[f"{prefix}_pending"] = row[0][0]

        return stats
