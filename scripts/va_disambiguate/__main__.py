"""Disambiguate Various Artists entries in the WXYC flowsheet and card catalog.

Resolves the actual performing artist for flowsheet entries logged under
"Various Artists" / "V/A" using Discogs track-level credits, and populates
ALTERNATE_ARTIST_NAME on compilation LIBRARY_RELEASE entries for searchability.

Usage:
    uv run python -m scripts.va_disambiguate [OPTIONS]

Options:
    --results-db PATH           SQLite tracking DB (default: va_disambiguate.db)
    --flowsheet-output PATH     Flowsheet SQL output (default: va_flowsheet_updates.sql)
    --catalog-output PATH       Catalog SQL output (default: va_catalog_updates.sql)
    --confidence-threshold FLOAT  Minimum confidence for updates (default: 0.70)
    --batch-size INT            Progress log interval (default: 100)
    --dry-run                   Extract only, don't resolve
    --stats                     Show current tracking DB stats and exit
    --apply                     Execute generated SQL via SSH+MySQL
    --verbose                   Debug logging
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import logging
import sys
from pathlib import Path

import asyncpg
from dotenv import load_dotenv

from scripts.va_disambiguate.discogs_lookup import collect_track_artists_for_release
from scripts.va_disambiguate.extract import (
    apply_sql_file,
    fetch_compilation_releases,
    fetch_library_codes,
    fetch_va_entries,
)
from scripts.va_disambiguate.library_lookup import build_library_code_index
from scripts.va_disambiguate.matching import select_primary_artist
from scripts.va_disambiguate.models import CompilationTrackArtist
from scripts.va_disambiguate.resolve import resolve_flowsheet_entry
from scripts.va_disambiguate.results_db import ResultsDB
from scripts.va_disambiguate.sql_writer import (
    generate_catalog_inserts,
    generate_flowsheet_updates,
    write_sql_file,
)

logger = logging.getLogger("va_disambiguate")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--results-db", default="va_disambiguate.db")
    parser.add_argument("--flowsheet-output", default="va_flowsheet_updates.sql")
    parser.add_argument("--catalog-output", default="va_catalog_inserts.sql")
    parser.add_argument("--confidence-threshold", type=float, default=0.70)
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--stats", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args(argv)


async def run(args: argparse.Namespace) -> None:
    load_dotenv()

    import os

    database_url = os.environ.get("DATABASE_URL_DISCOGS")
    if not database_url and not args.stats and not args.apply:
        logger.error("DATABASE_URL_DISCOGS not set")
        sys.exit(1)

    db = ResultsDB(args.results_db)
    await db.connect()

    try:
        # --stats: show current progress and exit
        if args.stats:
            stats = await db.get_stats()
            print("=== VA Disambiguation Stats ===")
            print(
                f"Flowsheet: {stats['flowsheet_total']} total, "
                f"{stats['flowsheet_resolved']} resolved, "
                f"{stats['flowsheet_unresolved']} unresolved, "
                f"{stats['flowsheet_pending']} pending"
            )
            print(
                f"Catalog:   {stats['catalog_total']} total, "
                f"{stats['catalog_enriched']} enriched, "
                f"{stats['catalog_unresolved']} unresolved, "
                f"{stats['catalog_pending']} pending"
            )
            return

        # --apply: execute generated SQL files
        if args.apply:
            for path in [args.flowsheet_output, args.catalog_output]:
                if Path(path).exists():
                    confirm = input(f"Apply {path} to production? [y/N] ")
                    if confirm.lower() == "y":
                        apply_sql_file(path)
                    else:
                        print(f"Skipped {path}")
                else:
                    print(f"{path} not found, skipping")
            return

        # Phase 0: Extract
        stats = await db.get_stats()
        if stats["flowsheet_total"] == 0:
            logger.info("Phase 0: Extracting data from MySQL...")
            entries = fetch_va_entries()
            count = await db.insert_flowsheet_entries(entries)
            logger.info(f"  Inserted {count} flowsheet entries")

            codes = fetch_library_codes()
            await db.insert_library_codes(codes)
            logger.info(f"  Inserted {len(codes)} library codes")

            releases = fetch_compilation_releases()
            count = await db.insert_compilation_releases(releases)
            logger.info(f"  Inserted {count} compilation releases")
        else:
            logger.info("Phase 0: Skipped (tracking DB already populated)")

        if args.dry_run:
            stats = await db.get_stats()
            print(
                f"Extracted {stats['flowsheet_total']} flowsheet entries "
                f"and {stats['catalog_total']} compilation releases"
            )
            return

        # Build library code index
        all_codes = await db.get_all_library_codes()
        library_index = build_library_code_index(all_codes)
        logger.info(f"Built library index with {len(library_index)} entries")

        # Connect to Discogs cache
        pool = await asyncpg.create_pool(database_url, min_size=1, max_size=5)

        try:
            # Phase 1a: Resolve flowsheet artists
            pending = await db.get_pending_flowsheet_entries()
            if pending:
                logger.info(f"Phase 1a: Resolving {len(pending)} flowsheet entries...")
                resolved_count = 0
                unresolved_count = 0

                for i, entry in enumerate(pending):
                    result = await resolve_flowsheet_entry(
                        entry,
                        pool,
                        library_index,
                        confidence_threshold=args.confidence_threshold,
                    )
                    if result:
                        await db.save_flowsheet_resolution(result)
                        resolved_count += 1
                    else:
                        reason = "no_song_title" if not entry.song_title else "no_match"
                        await db.mark_flowsheet_unresolved(entry.id, reason)
                        unresolved_count += 1

                    if (i + 1) % args.batch_size == 0:
                        logger.info(
                            f"  Progress: {i + 1}/{len(pending)} "
                            f"({resolved_count} resolved, {unresolved_count} unresolved)"
                        )

                logger.info(f"  Done: {resolved_count} resolved, {unresolved_count} unresolved")

            # Phase 1b: Collect compilation track artists
            pending_releases = await db.get_pending_catalog_releases()
            if pending_releases:
                logger.info(
                    f"Phase 1b: Collecting track artists for {len(pending_releases)} compilations..."
                )
                enriched_count = 0
                unenriched_count = 0

                for i, release in enumerate(pending_releases):
                    artists_data = await collect_track_artists_for_release(pool, release.title)
                    if artists_data:
                        track_artists = [
                            CompilationTrackArtist(
                                library_release_id=release.id,
                                artist_name=select_primary_artist([a["artist_name"]])
                                or a["artist_name"],
                                track_title=a.get("track_title"),
                            )
                            for a in artists_data
                        ]
                        await db.save_catalog_track_artists(release.id, track_artists)
                        enriched_count += 1
                    else:
                        await db.mark_catalog_unresolved(release.id, "no_discogs_match")
                        unenriched_count += 1

                    if (i + 1) % args.batch_size == 0:
                        logger.info(
                            f"  Progress: {i + 1}/{len(pending_releases)} "
                            f"({enriched_count} enriched, {unenriched_count} unresolved)"
                        )

                logger.info(f"  Done: {enriched_count} enriched, {unenriched_count} unresolved")

        finally:
            await pool.close()

        # Phase 3: Generate SQL
        logger.info("Phase 3: Generating SQL...")

        resolved = await db.get_resolved_flowsheet_entries()
        if resolved:
            lines = generate_flowsheet_updates(resolved)
            write_sql_file(lines, Path(args.flowsheet_output))

        catalog_rows = await db.get_all_track_artists()
        if catalog_rows:
            lines = generate_catalog_inserts(catalog_rows)
            write_sql_file(lines, Path(args.catalog_output))

        # Generate unresolved CSV
        unresolved_path = Path("va_disambiguate_unresolved.csv")
        assert db._db is not None
        cursor = await db._db.execute(
            "SELECT id, artist_name, song_title, release_title, unresolved_reason "
            "FROM flowsheet_entries WHERE status = 'unresolved'"
        )
        unresolved_rows = list(await cursor.fetchall())
        if unresolved_rows:
            with open(unresolved_path, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["id", "artist_name", "song_title", "release_title", "reason"])
                for row in unresolved_rows:
                    writer.writerow(dict(row).values())
            logger.info(f"Wrote {len(unresolved_rows)} unresolved entries to {unresolved_path}")

        # Summary
        stats = await db.get_stats()
        print("\n=== Summary ===")
        print(
            f"Flowsheet: {stats['flowsheet_resolved']} resolved, "
            f"{stats['flowsheet_unresolved']} unresolved of {stats['flowsheet_total']} total"
        )
        print(
            f"Catalog:   {stats['catalog_enriched']} enriched, "
            f"{stats['catalog_unresolved']} unresolved of {stats['catalog_total']} total"
        )
        if resolved:
            print(f"\nReview: {args.flowsheet_output}")
        if catalog_rows:
            print(f"Review: {args.catalog_output}")
        print("\nTo apply: uv run python -m scripts.va_disambiguate --apply")

    finally:
        await db.close()


def main():
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
