"""Analyze streaming availability for the WXYC library catalog.

Searches Spotify and Apple Music to identify albums NOT on streaming platforms.

Usage:
    uv run python -m scripts.streaming_availability [OPTIONS]

Options:
    --library-db PATH     Path to library.db (default: library.db)
    --results-db PATH     Path to results database (default: streaming_availability.db)
    --output PATH         CSV output path (default: streaming_report.csv)
    --spotify-only        Skip Apple Music phase
    --batch-size INT      Albums per progress log (default: 100)
    --dry-run             Populate albums table only, no API calls
    --stats               Show current stats and exit
    --retry-errors        Re-check albums with status 'error'
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

from dotenv import load_dotenv

from scripts.streaming_availability.apple_music_client import AppleMusicClient
from scripts.streaming_availability.dedup import deduplicate_library
from scripts.streaming_availability.matching import (
    is_acceptable_match,
    score_match,
    strip_format_suffix,
)
from scripts.streaming_availability.report import generate_csv_report, generate_summary
from scripts.streaming_availability.results_db import ResultsDB
from scripts.streaming_availability.spotify_client import SpotifyClient

logger = logging.getLogger("streaming_availability")

# Graceful shutdown flag
_shutdown_requested = False


def _handle_signal(signum, frame):
    global _shutdown_requested
    if _shutdown_requested:
        logger.warning("Force quit requested, exiting immediately")
        sys.exit(1)
    logger.info("Shutdown requested, finishing current batch...")
    _shutdown_requested = True


async def _process_spotify(
    results_db: ResultsDB,
    spotify: SpotifyClient,
    batch_size: int,
    retry_errors: bool = False,
) -> None:
    """Process all pending albums against Spotify."""
    stats = await results_db.get_stats()
    spotify_stats = stats.get("spotify", {})
    total_pending = spotify_stats.get("pending", 0)
    if retry_errors:
        total_pending += spotify_stats.get("error", 0)
    if total_pending == 0:
        logger.info("No pending Spotify checks")
        return

    logger.info("Spotify: %d albums to check", total_pending)
    processed = 0
    found = 0
    not_found = 0
    errors = 0

    while not _shutdown_requested:
        rows = await results_db.get_pending("spotify", limit=batch_size)
        if not rows:
            break

        for row in rows:
            if _shutdown_requested:
                break

            album_id = row["id"]
            artist = row["display_artist"]
            title = strip_format_suffix(row["display_title"])

            try:
                results = await spotify.search_album(artist, title)
                if not results:
                    await results_db.update_result(album_id, "spotify", "not_found")
                    not_found += 1
                else:
                    best = _find_best_spotify_match(artist, title, results)
                    if best:
                        await results_db.update_result(
                            album_id,
                            "spotify",
                            "found",
                            url=best["url"],
                            spotify_id=best["id"],
                            confidence=best["confidence"],
                            matched_artist=best["matched_artist"],
                            matched_title=best["matched_title"],
                        )
                        found += 1
                    else:
                        await results_db.update_result(album_id, "spotify", "not_found")
                        not_found += 1
            except Exception:
                logger.exception("Error processing %s - %s", artist, title)
                await results_db.update_result(album_id, "spotify", "error")
                errors += 1

            processed += 1
            if processed % batch_size == 0:
                pct = processed / total_pending * 100
                logger.info(
                    "  Spotify: %d / %d (%.1f%%) | found: %d | not found: %d | errors: %d",
                    processed,
                    total_pending,
                    pct,
                    found,
                    not_found,
                    errors,
                )

    logger.info(
        "Spotify complete: %d processed | found: %d | not found: %d | errors: %d",
        processed,
        found,
        not_found,
        errors,
    )


async def _process_apple_music(
    results_db: ResultsDB,
    apple: AppleMusicClient,
    batch_size: int,
) -> None:
    """Process Spotify misses against Apple Music."""
    rows = await results_db.get_spotify_misses_pending_apple(limit=1)
    if not rows:
        logger.info("No albums to check on Apple Music")
        return

    # Get total count for progress reporting
    all_pending = await results_db.get_spotify_misses_pending_apple(limit=100_000)
    total_pending = len(all_pending)
    logger.info("Apple Music: %d albums to check (Spotify misses)", total_pending)

    processed = 0
    found = 0
    not_found = 0
    errors = 0

    while not _shutdown_requested:
        rows = await results_db.get_spotify_misses_pending_apple(limit=batch_size)
        if not rows:
            break

        for row in rows:
            if _shutdown_requested:
                break

            album_id = row["id"]
            artist = row["display_artist"]
            title = strip_format_suffix(row["display_title"])

            try:
                results = await apple.search_album(artist, title)
                if not results:
                    await results_db.update_result(album_id, "apple", "not_found")
                    not_found += 1
                else:
                    best = _find_best_apple_match(artist, title, results)
                    if best:
                        await results_db.update_result(
                            album_id,
                            "apple",
                            "found",
                            url=best["url"],
                            confidence=best["confidence"],
                            matched_artist=best["matched_artist"],
                            matched_title=best["matched_title"],
                        )
                        found += 1
                    else:
                        await results_db.update_result(album_id, "apple", "not_found")
                        not_found += 1
            except Exception:
                logger.exception("Error processing %s - %s", artist, title)
                await results_db.update_result(album_id, "apple", "error")
                errors += 1

            processed += 1
            if processed % batch_size == 0:
                pct = processed / total_pending * 100
                logger.info(
                    "  Apple Music: %d / %d (%.1f%%) | found: %d | not found: %d | errors: %d",
                    processed,
                    total_pending,
                    pct,
                    found,
                    not_found,
                    errors,
                )

    logger.info(
        "Apple Music complete: %d processed | found: %d | not found: %d | errors: %d",
        processed,
        found,
        not_found,
        errors,
    )


def _find_best_spotify_match(artist: str, title: str, results: list[dict]) -> dict | None:
    """Find the best matching album from Spotify search results."""
    best: dict | None = None
    best_score = 0.0

    for album in results:
        spotify_artist = album.get("artists", [{}])[0].get("name", "")
        spotify_title = album.get("name", "")
        artist_score = score_match(artist, spotify_artist)
        title_score = score_match(title, spotify_title)

        if not is_acceptable_match(artist_score, title_score):
            continue

        combined = (artist_score + title_score) / 2
        if combined > best_score:
            best_score = combined
            best = {
                "url": album.get("external_urls", {}).get("spotify", ""),
                "id": album.get("id", ""),
                "confidence": combined,
                "matched_artist": spotify_artist,
                "matched_title": spotify_title,
            }

    return best


def _find_best_apple_match(artist: str, title: str, results: list[dict]) -> dict | None:
    """Find the best matching album from iTunes search results."""
    best: dict | None = None
    best_score = 0.0

    for album in results:
        apple_artist = album.get("artistName", "")
        apple_title = album.get("collectionName", "")
        artist_score = score_match(artist, apple_artist)
        title_score = score_match(title, apple_title)

        if not is_acceptable_match(artist_score, title_score):
            continue

        combined = (artist_score + title_score) / 2
        if combined > best_score:
            best_score = combined
            best = {
                "url": album.get("collectionViewUrl", ""),
                "confidence": combined,
                "matched_artist": apple_artist,
                "matched_title": apple_title,
            }

    return best


async def _skip_compilations(results_db: ResultsDB) -> int:
    """Mark all compilations as skipped on both services."""
    db = results_db._db
    assert db is not None
    cursor = await db.execute(
        """UPDATE albums SET spotify_status = 'skipped', apple_status = 'skipped'
           WHERE is_compilation = 1 AND (spotify_status = 'pending' OR apple_status = 'pending')"""
    )
    await db.commit()
    return cursor.rowcount


async def run(args: argparse.Namespace) -> None:
    """Main orchestration."""
    results_db = ResultsDB(args.results_db)
    await results_db.connect()

    try:
        # Show stats and exit if requested
        if args.stats:
            summary = await generate_summary(results_db)
            print(summary)
            return

        # Step 1: Deduplicate library
        library_path = args.library_db
        if not Path(library_path).is_file():
            logger.error("library.db not found at %s", library_path)
            sys.exit(1)

        logger.info("Reading library from %s...", library_path)
        albums = await deduplicate_library(library_path)
        logger.info(
            "Deduplicated: %d unique albums from library",
            len(albums),
        )

        compilations = sum(1 for a in albums if a.is_compilation)
        logger.info("Compilations: %d (will be skipped)", compilations)

        # Step 2: Insert into results DB
        inserted = await results_db.insert_albums(albums)
        logger.info("Inserted %d new albums into results DB (duplicates ignored)", inserted)

        # Skip compilations
        skipped = await _skip_compilations(results_db)
        if skipped:
            logger.info("Marked %d compilations as skipped", skipped)

        if args.dry_run:
            logger.info("Dry run complete. Use --stats to see current state.")
            return

        # Step 3: Spotify
        client_id = os.environ.get("SPOTIFY_CLIENT_ID")
        client_secret = os.environ.get("SPOTIFY_CLIENT_SECRET")
        if not client_id or not client_secret:
            logger.error("SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET required in .env")
            sys.exit(1)

        spotify = SpotifyClient(client_id, client_secret)
        try:
            await _process_spotify(
                results_db, spotify, args.batch_size, retry_errors=args.retry_errors
            )
        finally:
            await spotify.close()

        # Step 4: Apple Music (only for Spotify misses)
        if not args.spotify_only and not _shutdown_requested:
            apple = AppleMusicClient()
            try:
                await _process_apple_music(results_db, apple, args.batch_size)
            finally:
                await apple.close()

        # Step 5: Generate report
        logger.info("Generating CSV report: %s", args.output)
        await generate_csv_report(results_db, args.output)

        # Step 6: Summary
        summary = await generate_summary(results_db)
        print(summary)

    finally:
        await results_db.close()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Analyze streaming availability for the WXYC library catalog.",
    )
    parser.add_argument(
        "--library-db",
        default="library.db",
        help="Path to library.db (default: library.db)",
    )
    parser.add_argument(
        "--results-db",
        default="streaming_availability.db",
        help="Path to results database (default: streaming_availability.db)",
    )
    parser.add_argument(
        "--output",
        default="streaming_report.csv",
        help="CSV output path (default: streaming_report.csv)",
    )
    parser.add_argument(
        "--spotify-only",
        action="store_true",
        help="Only check Spotify, skip Apple Music",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=100,
        help="Albums per progress log (default: 100)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Populate albums table only, no API calls",
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        help="Show current stats and exit",
    )
    parser.add_argument(
        "--retry-errors",
        action="store_true",
        help="Re-check albums with status 'error'",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging",
    )
    return parser.parse_args(argv)


def main():
    load_dotenv()

    args = parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    asyncio.run(run(args))


if __name__ == "__main__":
    main()
