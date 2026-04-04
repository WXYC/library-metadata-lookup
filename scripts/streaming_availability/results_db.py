"""SQLite results database for streaming availability analysis."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import aiosqlite

from scripts.streaming_availability.dedup import DeduplicatedAlbum

_SCHEMA = """
CREATE TABLE IF NOT EXISTS albums (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    normalized_artist TEXT NOT NULL,
    normalized_title TEXT NOT NULL,
    display_artist TEXT NOT NULL,
    display_title TEXT NOT NULL,
    library_ids TEXT NOT NULL,
    formats TEXT NOT NULL,
    genre TEXT,
    label TEXT,
    is_compilation INTEGER NOT NULL DEFAULT 0,
    spotify_status TEXT NOT NULL DEFAULT 'pending',
    spotify_url TEXT,
    spotify_id TEXT,
    spotify_confidence REAL,
    spotify_matched_artist TEXT,
    spotify_matched_title TEXT,
    spotify_checked_at TEXT,
    apple_status TEXT NOT NULL DEFAULT 'pending',
    apple_url TEXT,
    apple_confidence REAL,
    apple_matched_artist TEXT,
    apple_matched_title TEXT,
    apple_checked_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(normalized_artist, normalized_title)
);

CREATE INDEX IF NOT EXISTS idx_albums_spotify_status ON albums(spotify_status);
CREATE INDEX IF NOT EXISTS idx_albums_apple_status ON albums(apple_status);
"""


class ResultsDB:
    """Async SQLite client for the streaming availability results database."""

    def __init__(self, db_path: str = "streaming_availability.db"):
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

    async def insert_albums(self, albums: list[DeduplicatedAlbum]) -> int:
        """Bulk insert albums. Uses INSERT OR IGNORE for idempotent resume. Returns count inserted."""
        assert self._db is not None
        inserted = 0
        for album in albums:
            cursor = await self._db.execute(
                """INSERT OR IGNORE INTO albums
                   (normalized_artist, normalized_title, display_artist, display_title,
                    library_ids, formats, genre, label, is_compilation)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    album.normalized_artist,
                    album.normalized_title,
                    album.display_artist,
                    album.display_title,
                    json.dumps(album.library_ids),
                    json.dumps(album.formats),
                    album.genre,
                    album.label,
                    1 if album.is_compilation else 0,
                ),
            )
            if cursor.rowcount > 0:
                inserted += 1
        await self._db.commit()
        return inserted

    async def get_pending(self, service: str, limit: int = 100) -> list[aiosqlite.Row]:
        """Get albums not yet checked on the given service."""
        assert self._db is not None
        col = f"{service}_status"
        cursor = await self._db.execute(
            f"SELECT * FROM albums WHERE {col} = 'pending' LIMIT ?",  # noqa: S608
            (limit,),
        )
        return list(await cursor.fetchall())

    async def get_spotify_misses_pending_apple(self, limit: int = 100) -> list[aiosqlite.Row]:
        """Get albums not found on Spotify that haven't been checked on Apple Music."""
        assert self._db is not None
        cursor = await self._db.execute(
            "SELECT * FROM albums WHERE spotify_status = 'not_found' AND apple_status = 'pending' LIMIT ?",
            (limit,),
        )
        return list(await cursor.fetchall())

    async def update_result(
        self,
        album_id: int,
        service: str,
        status: str,
        *,
        url: str | None = None,
        spotify_id: str | None = None,
        confidence: float | None = None,
        matched_artist: str | None = None,
        matched_title: str | None = None,
    ) -> None:
        """Update the result for a service (spotify or apple)."""
        assert self._db is not None
        now = datetime.now(UTC).isoformat()
        if service == "spotify":
            await self._db.execute(
                """UPDATE albums SET
                   spotify_status = ?, spotify_url = ?, spotify_id = ?,
                   spotify_confidence = ?, spotify_matched_artist = ?,
                   spotify_matched_title = ?, spotify_checked_at = ?
                   WHERE id = ?""",
                (status, url, spotify_id, confidence, matched_artist, matched_title, now, album_id),
            )
        elif service == "apple":
            await self._db.execute(
                """UPDATE albums SET
                   apple_status = ?, apple_url = ?,
                   apple_confidence = ?, apple_matched_artist = ?,
                   apple_matched_title = ?, apple_checked_at = ?
                   WHERE id = ?""",
                (status, url, confidence, matched_artist, matched_title, now, album_id),
            )
        await self._db.commit()

    async def get_stats(self) -> dict:
        """Return counts by status for each service."""
        assert self._db is not None
        stats: dict = {"spotify": {}, "apple": {}, "total": 0}

        cursor = await self._db.execute("SELECT COUNT(*) FROM albums")
        row = await cursor.fetchone()
        stats["total"] = row[0] if row else 0

        for service in ("spotify", "apple"):
            col = f"{service}_status"
            cursor = await self._db.execute(
                f"SELECT {col}, COUNT(*) FROM albums GROUP BY {col}"  # noqa: S608
            )
            for row in await cursor.fetchall():
                stats[service][row[0]] = row[1]

        return stats

    async def get_not_on_streaming(self) -> list[aiosqlite.Row]:
        """Get albums not found on either Spotify or Apple Music."""
        assert self._db is not None
        cursor = await self._db.execute("""SELECT * FROM albums
               WHERE spotify_status = 'not_found' AND apple_status = 'not_found'
               ORDER BY display_artist, display_title""")
        return list(await cursor.fetchall())

    async def get_all_results(self) -> list[aiosqlite.Row]:
        """Get all albums ordered by artist and title."""
        assert self._db is not None
        cursor = await self._db.execute(
            "SELECT * FROM albums ORDER BY display_artist, display_title"
        )
        return list(await cursor.fetchall())
