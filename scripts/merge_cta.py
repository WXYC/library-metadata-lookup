"""Merge Discogs-sourced compilation track artists into tubafrenzy CTA table.

Maps compilation_track_artists.json entries to library_release_ids and generates
INSERT SQL for the tubafrenzy COMPILATION_TRACK_ARTIST table.

Usage:
    .venv/bin/python scripts/merge_cta.py [--dry-run] [--apply]
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sqlite3
import subprocess

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

SQLITE_PATH = "streaming_availability.db"
SSH_HOST = "tubafrenzy@horus.kattare.com"
MYSQL_HOST = "dc1-mysql-01.kattare.com"
MYSQL_USER = "tubafrenzy"
MYSQL_PASS = "db-wxycdb03"
MYSQL_DB = "wxycmusic"

# Strip Discogs disambiguation suffixes: "Artist Name (2)" -> "Artist Name"
DISCOGS_SUFFIX_RE = re.compile(r"\s*\(\d+\)$")


def strip_discogs_suffix(name: str) -> str:
    return DISCOGS_SUFFIX_RE.sub("", name).strip()


def mysql_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("'", "\\'")


def run_mysql_query(query: str) -> str:
    cmd = f"mysql -h {MYSQL_HOST} -u {MYSQL_USER} -p'{MYSQL_PASS}' -e \"{query}\" {MYSQL_DB}"
    result = subprocess.run(
        ["ssh", SSH_HOST, cmd],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result.stdout


def main(args: argparse.Namespace) -> None:
    # Load our Discogs-sourced compilation data
    with open("compilation_track_artists.json") as f:
        compilations = json.load(f)
    log.info(f"Loaded {len(compilations)} compilations from compilation_track_artists.json")

    # Map comp_id -> library_release_ids via streaming_availability.db
    conn = sqlite3.connect(SQLITE_PATH)
    comp_to_lib = {}
    for comp in compilations:
        row = conn.execute(
            "SELECT library_ids FROM albums WHERE id = ?", (comp["comp_id"],)
        ).fetchone()
        if row:
            lib_ids = json.loads(row[0])
            comp_to_lib[comp["comp_id"]] = lib_ids
    conn.close()

    total_lib_ids = sum(len(ids) for ids in comp_to_lib.values())
    log.info(
        f"Mapped to {total_lib_ids} library_release_ids across {len(comp_to_lib)} compilations"
    )

    # Get existing CTA library_release_ids from prod
    log.info("Querying prod CTA for existing library_release_ids...")
    stdout = run_mysql_query("SELECT DISTINCT LIBRARY_RELEASE_ID FROM COMPILATION_TRACK_ARTIST")
    existing_ids = set()
    for line in stdout.strip().split("\n")[1:]:  # skip header
        try:
            existing_ids.add(int(line.strip()))
        except ValueError:
            continue
    log.info(f"Prod CTA has {len(existing_ids)} distinct library_release_ids")

    # Generate INSERT statements for new entries only
    inserts = []
    new_comp_count = 0
    skipped_comp_count = 0

    for comp in compilations:
        lib_ids = comp_to_lib.get(comp["comp_id"], [])
        if not lib_ids:
            continue

        # Use the first library_release_id (most compilations have one)
        lib_id = lib_ids[0]
        if lib_id in existing_ids:
            skipped_comp_count += 1
            continue

        if not comp.get("tracks"):
            continue

        new_comp_count += 1
        for track in comp["tracks"]:
            for artist in track.get("artists", []):
                clean_artist = strip_discogs_suffix(artist)
                title = track.get("title", "")
                position = track.get("position", "")
                inserts.append(
                    f"INSERT IGNORE INTO COMPILATION_TRACK_ARTIST "
                    f"(LIBRARY_RELEASE_ID, ARTIST_NAME, TRACK_TITLE, TRACK_POSITION) VALUES "
                    f"({lib_id}, '{mysql_escape(clean_artist)}', "
                    f"'{mysql_escape(title)}', '{mysql_escape(position)}')"
                )

    log.info(f"New compilations: {new_comp_count} | Skipped (already in CTA): {skipped_comp_count}")
    log.info(f"Generated {len(inserts)} INSERT statements")

    # Write SQL file
    sql_path = "cta_inserts.sql"
    with open(sql_path, "w") as f:
        f.write(f"-- Merge {new_comp_count} compilations ({len(inserts)} track-artist entries)\n")
        f.write("-- Generated from compilation_track_artists.json (Discogs-sourced)\n\n")
        for stmt in inserts:
            f.write(stmt + ";\n")
    log.info(f"Saved to {sql_path}")

    if args.dry_run:
        log.info("Dry run — showing sample INSERTs:")
        for stmt in inserts[:10]:
            log.info(f"  {stmt[:120]}...")
        return

    if args.apply:
        log.info("Applying to prod MySQL...")
        # Get count before
        before = run_mysql_query("SELECT COUNT(*) FROM COMPILATION_TRACK_ARTIST")
        before_count = int(before.strip().split("\n")[1])
        log.info(f"  Before: {before_count} rows")

        # Apply in batches of 500
        batch_size = 500
        for i in range(0, len(inserts), batch_size):
            batch = inserts[i : i + batch_size]
            batch_sql = "; ".join(batch)
            run_mysql_query(batch_sql)
            log.info(f"  Applied {min(i + batch_size, len(inserts))}/{len(inserts)}")

        # Get count after
        after = run_mysql_query("SELECT COUNT(*) FROM COMPILATION_TRACK_ARTIST")
        after_count = int(after.strip().split("\n")[1])
        log.info(f"  After: {after_count} rows (added {after_count - before_count})")
    else:
        log.info(f"Review {sql_path}, then run with --apply to execute")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Merge compilation track artists into CTA")
    parser.add_argument("--dry-run", action="store_true", help="Show stats and sample SQL only")
    parser.add_argument("--apply", action="store_true", help="Apply SQL to prod MySQL")
    args = parser.parse_args()
    main(args)
