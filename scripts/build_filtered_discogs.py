#!/usr/bin/env python3
"""Build a filtered subset of the Discogs database containing only WXYC-relevant releases.

Extracts unique artist names from library.db, finds matching releases in the full
Discogs PostgreSQL database, and copies them into a 'wxyc' schema with trigram indexes
for fast fuzzy search.

Usage:
    uv run python scripts/build_filtered_discogs.py [OPTIONS]

Options:
    --library-db PATH       Path to library.db (default: library.db)
    --discogs-url URL       Discogs PG URL (default: from DATABASE_URL_DISCOGS env var)
    --drop                  Drop and recreate the wxyc schema (default: skip if exists)

Requires:
    - library.db (WXYC library catalog)
    - DATABASE_URL_DISCOGS (PostgreSQL with full Discogs data + pg_trgm + unaccent)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sqlite3
import sys
import time
from pathlib import Path

import asyncpg
from dotenv import load_dotenv

from core.matching import is_compilation_artist

logger = logging.getLogger("build_filtered_discogs")

COMPILATION_KEYWORDS = frozenset({"various", "soundtrack", "compilation", "v/a", "v.a."})


def extract_library_artists(library_db_path: str) -> list[str]:
    """Extract unique artist names from library.db, excluding compilations.

    Includes both primary artist names and alternate artist names.
    """
    conn = sqlite3.connect(library_db_path)
    cur = conn.cursor()

    artists: set[str] = set()

    # Primary artists
    cur.execute("SELECT DISTINCT artist FROM library WHERE artist IS NOT NULL")
    for (artist,) in cur.fetchall():
        if artist and not is_compilation_artist(artist):
            artists.add(artist)

    # Alternate artist names
    cur.execute(
        "SELECT DISTINCT alternate_artist_name FROM library WHERE alternate_artist_name IS NOT NULL"
    )
    for (alt,) in cur.fetchall():
        if alt and not is_compilation_artist(alt):
            artists.add(alt)

    conn.close()
    return sorted(artists)


_CREATE_SCHEMA = """
CREATE SCHEMA IF NOT EXISTS wxyc;

CREATE TABLE IF NOT EXISTS wxyc.release (
    id INTEGER PRIMARY KEY,
    title TEXT,
    release_year INTEGER,
    country TEXT,
    artwork_url TEXT,
    master_id INTEGER
);

CREATE TABLE IF NOT EXISTS wxyc.release_artist (
    release_id INTEGER NOT NULL,
    artist_id INTEGER,
    artist_name TEXT NOT NULL,
    extra INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS wxyc.release_label (
    release_id INTEGER NOT NULL,
    label_name TEXT
);

CREATE TABLE IF NOT EXISTS wxyc.release_track (
    release_id INTEGER NOT NULL,
    sequence INTEGER NOT NULL,
    position TEXT,
    title TEXT,
    duration TEXT
);
"""

_CREATE_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_wxyc_release_artist_name_trgm
    ON wxyc.release_artist USING gin (lower(f_unaccent(artist_name)) gin_trgm_ops);

CREATE INDEX IF NOT EXISTS idx_wxyc_release_title_trgm
    ON wxyc.release USING gin (lower(f_unaccent(title)) gin_trgm_ops);

CREATE INDEX IF NOT EXISTS idx_wxyc_release_artist_name_lower
    ON wxyc.release_artist (lower(left(artist_name, 200)));

CREATE INDEX IF NOT EXISTS idx_wxyc_release_artist_release_id
    ON wxyc.release_artist (release_id);

CREATE INDEX IF NOT EXISTS idx_wxyc_release_label_release_id
    ON wxyc.release_label (release_id);

CREATE INDEX IF NOT EXISTS idx_wxyc_release_track_release_id
    ON wxyc.release_track (release_id);
"""


async def _populate_filtered_releases(
    pool: asyncpg.Pool,
    artists: list[str],
    batch_size: int = 500,
) -> int:
    """Find releases matching library artists and copy to wxyc schema.

    Uses exact artist match (fast btree) first, then trigram for the rest.
    Returns total releases copied.
    """
    # Load artist names into a temp table for efficient JOIN
    async with pool.acquire() as conn:
        await conn.execute("CREATE TEMP TABLE IF NOT EXISTS lib_artists (name TEXT)")
        await conn.execute("DELETE FROM lib_artists")
        await conn.copy_records_to_table(
            "lib_artists",
            records=[(a,) for a in artists],
            columns=["name"],
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_lib_artists ON lib_artists (lower(name))"
        )
        logger.info("Loaded %d library artists into temp table", len(artists))

    # Phase 1: Exact match (fast, uses btree index)
    logger.info("Phase 1: Exact artist match...")
    t0 = time.time()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO wxyc.release (id, title, release_year, country, artwork_url, master_id)
            SELECT DISTINCT r.id, r.title, r.release_year, r.country, r.artwork_url, r.master_id
            FROM release r
            JOIN release_artist ra ON ra.release_id = r.id AND ra.extra = 0
            JOIN lib_artists la ON lower(left(ra.artist_name, 200)) = lower(la.name)
            ON CONFLICT (id) DO NOTHING
        """)
        exact_count = await conn.fetchval("SELECT COUNT(*) FROM wxyc.release")
    logger.info("Phase 1 complete: %d releases in %.1fs", exact_count, time.time() - t0)

    total_count = exact_count

    # Copy related data for matched releases
    logger.info("Copying release_artist...")
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO wxyc.release_artist (release_id, artist_id, artist_name, extra)
            SELECT ra.release_id, ra.artist_id, ra.artist_name, ra.extra
            FROM release_artist ra
            WHERE ra.release_id IN (SELECT id FROM wxyc.release)
        """)

    logger.info("Copying release_label...")
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO wxyc.release_label (release_id, label_name)
            SELECT rl.release_id, rl.label_name
            FROM release_label rl
            WHERE rl.release_id IN (SELECT id FROM wxyc.release)
        """)

    logger.info("Copying release_track...")
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO wxyc.release_track (release_id, sequence, position, title, duration)
            SELECT rt.release_id, rt.sequence, rt.position, rt.title, rt.duration
            FROM release_track rt
            WHERE rt.release_id IN (SELECT id FROM wxyc.release)
        """)

    return total_count


async def run(args: argparse.Namespace) -> None:
    discogs_url = args.discogs_url or os.environ.get("DATABASE_URL_DISCOGS")
    if not discogs_url:
        logger.error("No Discogs database URL. Set DATABASE_URL_DISCOGS or use --discogs-url")
        sys.exit(1)

    library_path = args.library_db
    if not Path(library_path).is_file():
        logger.error("library.db not found at %s", library_path)
        sys.exit(1)

    # Extract artists
    logger.info("Extracting artists from %s...", library_path)
    artists = extract_library_artists(library_path)
    logger.info("Found %d unique non-compilation artists", len(artists))

    # Connect to Discogs PG
    pool = await asyncpg.create_pool(discogs_url, min_size=2, max_size=5)

    try:
        # Create or reset schema
        async with pool.acquire() as conn:
            if args.drop:
                logger.info("Dropping existing wxyc schema...")
                await conn.execute("DROP SCHEMA IF EXISTS wxyc CASCADE")

            # Check if already populated
            exists = await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM information_schema.schemata WHERE schema_name = 'wxyc')"
            )
            if exists:
                count = await conn.fetchval("SELECT COUNT(*) FROM wxyc.release")
                if count > 0 and not args.drop:
                    logger.info(
                        "wxyc schema already exists with %d releases. Use --drop to rebuild.",
                        count,
                    )
                    return

            logger.info("Creating wxyc schema...")
            await conn.execute(_CREATE_SCHEMA)

        # Populate
        total = await _populate_filtered_releases(pool, artists)
        logger.info("Total releases in filtered database: %d", total)

        # Create indexes
        logger.info("Creating trigram indexes...")
        t0 = time.time()
        async with pool.acquire() as conn:
            await conn.execute(_CREATE_INDEXES)
        logger.info("Indexes created in %.1fs", time.time() - t0)

        # Stats
        async with pool.acquire() as conn:
            artist_count = await conn.fetchval(
                "SELECT COUNT(DISTINCT artist_name) FROM wxyc.release_artist WHERE extra = 0"
            )
            track_count = await conn.fetchval("SELECT COUNT(*) FROM wxyc.release_track")
        logger.info(
            "Done: %d releases, %d artists, %d tracks",
            total,
            artist_count,
            track_count,
        )

    finally:
        await pool.close()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build filtered Discogs database for WXYC library matching.",
    )
    parser.add_argument(
        "--library-db",
        default="library.db",
        help="Path to library.db (default: library.db)",
    )
    parser.add_argument(
        "--discogs-url",
        default=None,
        help="Discogs PG URL (default: DATABASE_URL_DISCOGS env var)",
    )
    parser.add_argument(
        "--drop",
        action="store_true",
        help="Drop and recreate the wxyc schema",
    )
    return parser.parse_args(argv)


def main():
    load_dotenv()
    args = parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    asyncio.run(run(args))


if __name__ == "__main__":
    main()
