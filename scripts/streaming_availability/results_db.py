"""SQLite results database for streaming availability analysis."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import aiosqlite

from clients.streaming.matching import normalize_album_title, normalize_artist_name
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
    is_single INTEGER NOT NULL DEFAULT 0,
    discogs_artist TEXT,
    discogs_title TEXT,
    discogs_release_id INTEGER,
    discogs_status TEXT NOT NULL DEFAULT 'pending',
    deezer_status TEXT NOT NULL DEFAULT 'pending',
    deezer_url TEXT,
    deezer_confidence REAL,
    deezer_matched_artist TEXT,
    deezer_matched_title TEXT,
    deezer_checked_at TEXT,
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

CREATE TABLE IF NOT EXISTS track_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    album_id INTEGER NOT NULL,
    artist TEXT NOT NULL,
    title TEXT NOT NULL,
    position TEXT,
    source TEXT NOT NULL,
    source_type TEXT NOT NULL,
    resolution_status TEXT NOT NULL DEFAULT 'pending',
    resolved_via TEXT,
    resolved_album_id INTEGER,
    resolved_release_id INTEGER,
    spotify_url TEXT,
    spotify_confidence REAL,
    deezer_url TEXT,
    deezer_confidence REAL,
    checked_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(album_id, artist, title)
);

CREATE INDEX IF NOT EXISTS idx_track_results_status ON track_results(resolution_status);
CREATE INDEX IF NOT EXISTS idx_track_results_album ON track_results(album_id);
"""


class ResultsDB:
    """Async SQLite client for the streaming availability results database.

    Uses an asyncio.Lock to serialize writes, since SQLite doesn't support
    concurrent writers from multiple coroutines.
    """

    def __init__(self, db_path: str = "streaming_availability.db"):
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None
        self._write_lock: asyncio.Lock = asyncio.Lock()

    async def connect(self) -> None:
        self._db = await aiosqlite.connect(self._db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(_SCHEMA)
        await self._migrate()
        await self._db.commit()

    async def _migrate(self) -> None:
        """Add columns that may not exist in older databases."""
        assert self._db is not None
        cursor = await self._db.execute("PRAGMA table_info(albums)")
        columns = {row[1] for row in await cursor.fetchall()}
        migrations = [
            ("is_single", "INTEGER NOT NULL DEFAULT 0"),
            ("discogs_artist", "TEXT"),
            ("discogs_title", "TEXT"),
            ("discogs_release_id", "INTEGER"),
            ("discogs_status", "TEXT NOT NULL DEFAULT 'pending'"),
            ("deezer_status", "TEXT NOT NULL DEFAULT 'pending'"),
            ("deezer_url", "TEXT"),
            ("deezer_confidence", "REAL"),
            ("deezer_matched_artist", "TEXT"),
            ("deezer_matched_title", "TEXT"),
            ("deezer_checked_at", "TEXT"),
            ("bandcamp_slug", "TEXT"),
            ("bandcamp_url", "TEXT"),
            ("bandcamp_status", "TEXT NOT NULL DEFAULT 'pending'"),
            ("bandcamp_checked_at", "TEXT"),
            # YouTube Music coverage drain (#1056). The drain fills youtube_music_url
            # offline; export_streaming_links.py already carries it into
            # library.db.streaming_links and /lookup already surfaces it. On prod the
            # url column predates ResultsDB (earlier pipeline era) so this ALTER is a
            # no-op there and only the status/checked_at markers are added.
            ("youtube_music_url", "TEXT"),
            ("youtube_music_status", "TEXT NOT NULL DEFAULT 'pending'"),
            ("youtube_music_checked_at", "TEXT"),
        ]
        bandcamp_status_added = "bandcamp_status" not in columns
        youtube_music_status_added = "youtube_music_status" not in columns
        for col_name, col_type in migrations:
            if col_name not in columns:
                await self._db.execute(f"ALTER TABLE albums ADD COLUMN {col_name} {col_type}")
        # Keep Phase-2 pending scans cheap, mirroring spotify/apple. Created
        # after the ALTER loop so the column exists on fresh databases too.
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_albums_bandcamp_status ON albums(bandcamp_status)"
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_albums_youtube_music_status "
            "ON albums(youtube_music_status)"
        )
        if bandcamp_status_added:
            # The ALTER stamps 'pending' on every existing row; rows that already
            # carry a resolved URL are really 'found'. Backfill so the marker is
            # trustworthy for attempted-vs-resolved reporting (#661).
            await self._db.execute(
                "UPDATE albums SET bandcamp_status = 'found' "
                "WHERE bandcamp_url IS NOT NULL AND bandcamp_status = 'pending'"
            )
        if youtube_music_status_added:
            # Same #661 backfill for the youtube_music marker: a prod row carrying a
            # url from the earlier pipeline era is 'found', not 'pending', so a
            # fill-only drain skips it and reporting stays trustworthy.
            await self._db.execute(
                "UPDATE albums SET youtube_music_status = 'found' "
                "WHERE youtube_music_url IS NOT NULL AND youtube_music_status = 'pending'"
            )

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    async def insert_albums(self, albums: list[DeduplicatedAlbum]) -> int:
        """Bulk insert albums. Uses INSERT OR IGNORE for idempotent resume. Returns count inserted."""
        assert self._db is not None
        async with self._write_lock:
            return await self._insert_albums_locked(albums)

    async def _insert_albums_locked(self, albums: list[DeduplicatedAlbum]) -> int:
        assert self._db is not None
        inserted = 0
        for album in albums:
            cursor = await self._db.execute(
                """INSERT OR IGNORE INTO albums
                   (normalized_artist, normalized_title, display_artist, display_title,
                    library_ids, formats, genre, label, is_compilation, is_single)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                    1 if album.is_single else 0,
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

    async def get_pending_discogs(self, limit: int = 100) -> list[aiosqlite.Row]:
        """Get albums not yet enriched with Discogs names."""
        assert self._db is not None
        cursor = await self._db.execute(
            "SELECT * FROM albums WHERE discogs_status = 'pending' AND is_compilation = 0 LIMIT ?",
            (limit,),
        )
        return list(await cursor.fetchall())

    async def update_discogs_result(
        self,
        album_id: int,
        status: str,
        *,
        artist: str | None = None,
        title: str | None = None,
        release_id: int | None = None,
    ) -> None:
        """Update the Discogs enrichment result for an album."""
        assert self._db is not None
        async with self._write_lock:
            await self._db.execute(
                """UPDATE albums SET
                   discogs_status = ?, discogs_artist = ?, discogs_title = ?, discogs_release_id = ?
                   WHERE id = ?""",
                (status, artist, title, release_id, album_id),
            )
            await self._db.commit()

    async def get_deezer_hits_pending_spotify(self, limit: int = 100) -> list[aiosqlite.Row]:
        """Get albums found on Deezer that haven't been checked on Spotify."""
        assert self._db is not None
        cursor = await self._db.execute(
            "SELECT * FROM albums WHERE deezer_status = 'found' AND spotify_status = 'pending' LIMIT ?",
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
        """Update the result for a service (spotify, apple, or deezer)."""
        assert self._db is not None
        async with self._write_lock:
            now = datetime.now(UTC).isoformat()
            if service == "spotify":
                await self._db.execute(
                    """UPDATE albums SET
                       spotify_status = ?, spotify_url = ?, spotify_id = ?,
                       spotify_confidence = ?, spotify_matched_artist = ?,
                       spotify_matched_title = ?, spotify_checked_at = ?
                       WHERE id = ?""",
                    (
                        status,
                        url,
                        spotify_id,
                        confidence,
                        matched_artist,
                        matched_title,
                        now,
                        album_id,
                    ),
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
            elif service == "deezer":
                await self._db.execute(
                    """UPDATE albums SET
                       deezer_status = ?, deezer_url = ?,
                       deezer_confidence = ?, deezer_matched_artist = ?,
                       deezer_matched_title = ?, deezer_checked_at = ?
                       WHERE id = ?""",
                    (status, url, confidence, matched_artist, matched_title, now, album_id),
                )
            await self._db.commit()

    async def get_stats(self) -> dict:
        """Return counts by status for each service."""
        assert self._db is not None
        stats: dict = {
            "spotify": {},
            "apple": {},
            "discogs": {},
            "deezer": {},
            "bandcamp": {},
            "youtube_music": {},
            "total": 0,
        }

        cursor = await self._db.execute("SELECT COUNT(*) FROM albums")
        row = await cursor.fetchone()
        stats["total"] = row[0] if row else 0

        for service in ("spotify", "apple", "discogs", "deezer", "bandcamp", "youtube_music"):
            col = f"{service}_status"
            cursor = await self._db.execute(
                f"SELECT {col}, COUNT(*) FROM albums GROUP BY {col}"  # noqa: S608
            )
            for row in await cursor.fetchall():
                stats[service][row[0]] = row[1]

        return stats

    async def get_not_on_streaming(self) -> list[aiosqlite.Row]:
        """Get albums not found on any checked streaming platform."""
        assert self._db is not None
        cursor = await self._db.execute("""SELECT * FROM albums
               WHERE spotify_status = 'not_found' AND is_compilation = 0
               ORDER BY display_artist, display_title""")
        return list(await cursor.fetchall())

    async def update_bandcamp_slug(self, album_id: int, slug: str) -> None:
        """Set the Bandcamp slug for an album."""
        assert self._db is not None
        async with self._write_lock:
            await self._db.execute(
                "UPDATE albums SET bandcamp_slug = ? WHERE id = ?",
                (slug, album_id),
            )
            await self._db.commit()

    async def update_bandcamp_url(self, album_id: int, url: str) -> None:
        """Set the Bandcamp album URL and mark the album resolved.

        Writes the ``found`` Phase-2 marker alongside the URL so the album is
        excluded from future pending scans (#661).
        """
        assert self._db is not None
        async with self._write_lock:
            now = datetime.now(UTC).isoformat()
            await self._db.execute(
                "UPDATE albums SET bandcamp_url = ?, bandcamp_status = 'found', "
                "bandcamp_checked_at = ? WHERE id = ?",
                (url, now, album_id),
            )
            await self._db.commit()

    async def mark_bandcamp_not_found(self, album_id: int) -> None:
        """Durably record a Phase-2 attempt that found no album match.

        Distinguishes "tried, no match" (status ``not_found``) from "not yet
        tried" (status ``pending``) so a bulk drain is resumable and re-runs
        skip already-attempted slugs (#661). Leaves ``bandcamp_url`` NULL.
        """
        assert self._db is not None
        async with self._write_lock:
            now = datetime.now(UTC).isoformat()
            await self._db.execute(
                "UPDATE albums SET bandcamp_status = 'not_found', "
                "bandcamp_checked_at = ? WHERE id = ?",
                (now, album_id),
            )
            await self._db.commit()

    async def mark_youtube_music_not_found(self, album_id: int) -> bool:
        """Durably record a YTM drain attempt that found no match -- fill-only guarded.

        Mirrors mark_bandcamp_not_found (#661) but adds `AND youtube_music_url IS NULL`
        so a transient miss can never downgrade a row already resolved to 'found'
        (Data Safety; the #669 fill-only invariant). Leaves youtube_music_url NULL.

        Returns ``True`` if the row was marked ``not_found``, ``False`` if it was
        a no-op (already resolved) -- lets callers (the #1070 drain's
        ``execute_write``) distinguish a genuine miss from a moot one instead of
        overcounting a not_found tally when the guard silently blocks the write.
        """
        assert self._db is not None
        async with self._write_lock:
            now = datetime.now(UTC).isoformat()
            cursor = await self._db.execute(
                "UPDATE albums SET youtube_music_status = 'not_found', "
                "youtube_music_checked_at = ? WHERE id = ? AND youtube_music_url IS NULL",
                (now, album_id),
            )
            await self._db.commit()
            return cursor.rowcount > 0

    async def update_youtube_music_url(self, album_id: int, url: str) -> bool:
        """Fill in a verified YouTube Music album URL -- **fill-only**.

        The write only lands when ``youtube_music_url IS NULL``, so an already
        resolved (top-priority) link is never clobbered by a later, lower-value
        pass (Data Safety). On a fill it also stamps the ``found`` status marker
        and ``checked_at`` so the row is excluded from future pending scans and
        attempted-vs-resolved reporting stays trustworthy (the #669/#661 lesson).

        Returns ``True`` if a row was filled, ``False`` if it was a no-op
        (already had a url, or no such album) -- lets the #1056 drain tally
        written-vs-already-present without a follow-up read.
        """
        assert self._db is not None
        async with self._write_lock:
            now = datetime.now(UTC).isoformat()
            cursor = await self._db.execute(
                "UPDATE albums SET youtube_music_url = ?, youtube_music_status = 'found', "
                "youtube_music_checked_at = ? WHERE id = ? AND youtube_music_url IS NULL",
                (url, now, album_id),
            )
            await self._db.commit()
            return cursor.rowcount > 0

    async def get_album_id_by_names(self, artist: str, title: str) -> int | None:
        """Resolve an album's id from raw ``(artist, title)``.

        Normalizes with the same ``normalize_artist_name`` /
        ``normalize_album_title`` the dedup pipeline keyed rows with (dedup.py),
        so a producer resolving names from any source (e.g. the #1056 drain's
        discogs-cache join) maps to the stored row with zero normalization
        drift. Returns ``None`` when no row matches. The ``UNIQUE(normalized_
        artist, normalized_title)`` constraint guarantees at most one match.
        """
        assert self._db is not None
        cursor = await self._db.execute(
            "SELECT id FROM albums WHERE normalized_artist = ? AND normalized_title = ?",
            (normalize_artist_name(artist), normalize_album_title(title)),
        )
        row = await cursor.fetchone()
        return int(row[0]) if row else None

    async def get_artists_without_bandcamp_slug(
        self,
        *,
        not_on_streaming_only: bool = True,
        limit: int | None = None,
    ) -> list[aiosqlite.Row]:
        """Get distinct artists with no bandcamp_slug set.

        Args:
            not_on_streaming_only: If True, only return artists not found on
                Spotify (matching the not-on-streaming scope).
            limit: Maximum number of distinct artists to return.
        """
        assert self._db is not None
        query = (
            "SELECT DISTINCT display_artist FROM albums "
            "WHERE bandcamp_slug IS NULL AND is_compilation = 0"
        )
        if not_on_streaming_only:
            query += " AND spotify_status = 'not_found'"
        if limit is not None:
            query += f" LIMIT {limit}"
        cursor = await self._db.execute(query)
        return list(await cursor.fetchall())

    async def get_pending_bandcamp_lookup(
        self, limit: int | None = None, slug: str | None = None
    ) -> list[aiosqlite.Row]:
        """Get albums with a slug but no album-level bandcamp_url.

        Args:
            limit: Maximum number of rows to return.
            slug: If provided, restrict to albums with this exact
                ``bandcamp_slug``. The concurrent consumer uses this to process
                only a newly discovered slug instead of re-scanning the whole
                pending set on every queue event (#125).

        Only albums still in ``bandcamp_status = 'pending'`` are returned;
        albums marked ``not_found`` by a prior attempt are skipped so a bulk
        drain is resumable (#661).
        """
        assert self._db is not None
        query = (
            "SELECT * FROM albums "
            "WHERE bandcamp_slug IS NOT NULL AND bandcamp_slug != '' "
            "AND bandcamp_url IS NULL AND bandcamp_status = 'pending' "
            "AND is_compilation = 0"
        )
        params: list = []
        if slug is not None:
            query += " AND bandcamp_slug = ?"
            params.append(slug)
        if limit is not None:
            query += f" LIMIT {limit}"
        cursor = await self._db.execute(query, params)
        return list(await cursor.fetchall())

    async def get_pending_album_search(
        self, *, include_not_found: bool = False, limit: int | None = None
    ) -> list[aiosqlite.Row]:
        """Get candidates for album-first Bandcamp discovery (LML#1069).

        Targets rows the existing artist-first Phase 2 backlog
        (``get_pending_bandcamp_lookup``) does not own: no ``bandcamp_url``,
        not a compilation, and either never searched (``bandcamp_slug IS
        NULL``) or artist-searched-with-no-band-found (the ``''`` sentinel
        slug). A row with a *real* recorded slug stays Phase-2's -- album-
        search must not mark it ``not_found`` and pre-empt a pending catalog
        scrape.

        Args:
            include_not_found: when True, also include rows already marked
                ``bandcamp_status = 'not_found'`` (previously exhausted by
                the artist-first path only). Default restricts to
                ``pending`` -- a real-slug row stays excluded either way.
            limit: maximum rows to return.
        """
        assert self._db is not None
        statuses = "('pending', 'not_found')" if include_not_found else "('pending')"
        query = (
            "SELECT * FROM albums WHERE bandcamp_url IS NULL AND is_compilation = 0 "
            f"AND bandcamp_status IN {statuses} "  # noqa: S608
            "AND (bandcamp_slug IS NULL OR bandcamp_slug = '')"
        )
        if limit is not None:
            query += f" LIMIT {limit}"
        cursor = await self._db.execute(query)
        return list(await cursor.fetchall())

    async def get_all_results(self) -> list[aiosqlite.Row]:
        """Get all albums ordered by artist and title."""
        assert self._db is not None
        cursor = await self._db.execute(
            "SELECT * FROM albums ORDER BY display_artist, display_title"
        )
        return list(await cursor.fetchall())

    # -----------------------------------------------------------------------
    # Track-level results
    # -----------------------------------------------------------------------

    async def insert_tracks(self, tracks: list[dict]) -> int:
        """Bulk insert track results. Uses INSERT OR IGNORE for idempotent resume."""
        assert self._db is not None
        inserted = 0
        async with self._write_lock:
            for t in tracks:
                cursor = await self._db.execute(
                    """INSERT OR IGNORE INTO track_results
                       (album_id, artist, title, position, source, source_type)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        t["album_id"],
                        t["artist"],
                        t["title"],
                        t.get("position"),
                        t["source"],
                        t["source_type"],
                    ),
                )
                if cursor.rowcount > 0:
                    inserted += 1
            await self._db.commit()
        return inserted

    async def get_pending_tracks(self, limit: int = 100) -> list[aiosqlite.Row]:
        """Get tracks with resolution_status = 'pending'."""
        assert self._db is not None
        cursor = await self._db.execute(
            "SELECT * FROM track_results WHERE resolution_status = 'pending' LIMIT ?",
            (limit,),
        )
        return list(await cursor.fetchall())

    async def get_local_miss_tracks(self, limit: int = 100) -> list[aiosqlite.Row]:
        """Get tracks that missed local resolution, ready for API search."""
        assert self._db is not None
        cursor = await self._db.execute(
            "SELECT * FROM track_results WHERE resolution_status = 'local_miss' LIMIT ?",
            (limit,),
        )
        return list(await cursor.fetchall())

    async def update_track_resolution(
        self,
        track_id: int,
        status: str,
        *,
        resolved_via: str | None = None,
        resolved_album_id: int | None = None,
        resolved_release_id: int | None = None,
        spotify_url: str | None = None,
        spotify_confidence: float | None = None,
        deezer_url: str | None = None,
        deezer_confidence: float | None = None,
    ) -> None:
        """Update resolution status for a track."""
        assert self._db is not None
        now = datetime.now(UTC).isoformat()
        async with self._write_lock:
            await self._db.execute(
                """UPDATE track_results SET
                   resolution_status = ?, resolved_via = ?,
                   resolved_album_id = ?, resolved_release_id = ?,
                   spotify_url = ?, spotify_confidence = ?,
                   deezer_url = ?, deezer_confidence = ?,
                   checked_at = ?
                   WHERE id = ?""",
                (
                    status,
                    resolved_via,
                    resolved_album_id,
                    resolved_release_id,
                    spotify_url,
                    spotify_confidence,
                    deezer_url,
                    deezer_confidence,
                    now,
                    track_id,
                ),
            )
            await self._db.commit()

    async def get_track_stats(self) -> dict:
        """Get counts of track results by resolution status."""
        assert self._db is not None
        cursor = await self._db.execute(
            "SELECT resolution_status, COUNT(*) FROM track_results GROUP BY resolution_status"
        )
        stats = {row[0]: row[1] for row in await cursor.fetchall()}
        stats["total"] = sum(stats.values())
        return stats

    async def get_album_track_summary(self, album_id: int) -> dict:
        """Summarize track resolution for a single album."""
        assert self._db is not None
        cursor = await self._db.execute(
            "SELECT resolution_status, COUNT(*) FROM track_results WHERE album_id = ? GROUP BY resolution_status",
            (album_id,),
        )
        counts = {row[0]: row[1] for row in await cursor.fetchall()}
        total = sum(counts.values())
        resolved = (
            total
            - counts.get("pending", 0)
            - counts.get("local_miss", 0)
            - counts.get("not_found", 0)
            - counts.get("false_positive", 0)
            - counts.get("error", 0)
        )
        return {
            "total": total,
            "resolved": resolved,
            "not_found": counts.get("not_found", 0),
            "pending": counts.get("pending", 0),
            "on_streaming": resolved > 0,
        }
