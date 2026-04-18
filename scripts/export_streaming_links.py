"""Export streaming URLs from streaming_availability.db into library.db.

Creates a streaming_links table in library.db that maps library release IDs
to verified streaming service URLs. This makes streaming data available to
the LML API and all downstream clients.

Usage:
    .venv/bin/python scripts/export_streaming_links.py [--library-db PATH] [--streaming-db PATH] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


def main(args: argparse.Namespace) -> None:
    if not os.path.exists(args.streaming_db):
        log.error(f"Streaming database {args.streaming_db} does not exist; skipping export")
        return

    sa = sqlite3.connect(args.streaming_db)
    lib = sqlite3.connect(args.library_db)

    # Get all albums with at least one streaming URL
    rows = sa.execute("""
        SELECT library_ids, spotify_url, apple_url, deezer_url,
               bandcamp_url, tidal_url, youtube_music_url, soundcloud_url
        FROM albums
        WHERE spotify_url IS NOT NULL
           OR apple_url IS NOT NULL
           OR deezer_url IS NOT NULL
           OR bandcamp_url IS NOT NULL
           OR tidal_url IS NOT NULL
           OR youtube_music_url IS NOT NULL
           OR soundcloud_url IS NOT NULL
    """).fetchall()
    log.info(f"Albums with streaming URLs: {len(rows)}")

    # Map to library release IDs
    links: dict[int, dict] = {}
    for lib_ids_json, spotify, apple, deezer, bandcamp, tidal, ytmusic, soundcloud in rows:
        lib_ids = json.loads(lib_ids_json)
        for lib_id in lib_ids:
            if lib_id not in links:
                links[lib_id] = {}
            entry = links[lib_id]
            if spotify and "spotify_url" not in entry:
                entry["spotify_url"] = spotify
            if apple and "apple_music_url" not in entry:
                entry["apple_music_url"] = apple
            if deezer and "deezer_url" not in entry:
                entry["deezer_url"] = deezer
            if bandcamp and "bandcamp_url" not in entry:
                entry["bandcamp_url"] = bandcamp
            if tidal and "tidal_url" not in entry:
                entry["tidal_url"] = tidal
            if ytmusic and "youtube_music_url" not in entry:
                entry["youtube_music_url"] = ytmusic
            if soundcloud and "soundcloud_url" not in entry:
                entry["soundcloud_url"] = soundcloud

    log.info(f"Library release IDs with streaming links: {len(links)}")
    sa.close()

    if args.dry_run:
        # Show stats
        from collections import Counter

        service_counts: Counter[str] = Counter()
        for entry in links.values():
            for k in entry:
                service_counts[k] += 1
        log.info("Service coverage:")
        for service, count in service_counts.most_common():
            log.info(f"  {service}: {count:,}")
        return

    # Create/replace streaming_links table in library.db
    lib.execute("DROP TABLE IF EXISTS streaming_links")
    lib.execute("""
        CREATE TABLE streaming_links (
            library_id INTEGER PRIMARY KEY,
            spotify_url TEXT,
            apple_music_url TEXT,
            deezer_url TEXT,
            bandcamp_url TEXT,
            tidal_url TEXT,
            youtube_music_url TEXT,
            soundcloud_url TEXT
        )
    """)

    inserted = 0
    for lib_id, entry in links.items():
        lib.execute(
            "INSERT OR IGNORE INTO streaming_links "
            "(library_id, spotify_url, apple_music_url, deezer_url, "
            "bandcamp_url, tidal_url, youtube_music_url, soundcloud_url) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                lib_id,
                entry.get("spotify_url"),
                entry.get("apple_music_url"),
                entry.get("deezer_url"),
                entry.get("bandcamp_url"),
                entry.get("tidal_url"),
                entry.get("youtube_music_url"),
                entry.get("soundcloud_url"),
            ),
        )
        inserted += 1
        if inserted % 10000 == 0:
            lib.commit()

    lib.commit()
    log.info(f"Inserted {inserted:,} streaming_links rows into {args.library_db}")

    # Verify
    count = lib.execute("SELECT COUNT(*) FROM streaming_links").fetchone()[0]
    log.info(f"Verified: {count:,} rows in streaming_links table")
    lib.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Export streaming URLs to library.db")
    parser.add_argument(
        "--library-db", default="library.db", help="Path to library.db (default: library.db)"
    )
    parser.add_argument(
        "--streaming-db",
        default="streaming_availability.db",
        help="Path to streaming_availability.db (default: streaming_availability.db)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Show stats without writing")
    args = parser.parse_args()
    main(args)
