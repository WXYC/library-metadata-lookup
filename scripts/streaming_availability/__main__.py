"""Analyze streaming availability for the WXYC library catalog.

Searches Deezer (pre-filter), Spotify, and Apple Music to identify albums
NOT on streaming platforms.

Usage:
    uv run python -m scripts.streaming_availability [OPTIONS]
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
from scripts.streaming_availability.deezer_client import DeezerClient
from scripts.streaming_availability.discogs_enricher import (
    build_artist_mapping,
    check_wxyc_schema,
    enrich_album,
)
from scripts.streaming_availability.matching import (
    is_acceptable_match,
    score_match,
    strip_format_suffix,
)
from scripts.streaming_availability.report import generate_csv_report, generate_summary
from scripts.streaming_availability.results_db import ResultsDB
from scripts.streaming_availability.spotify_client import SpotifyClient

logger = logging.getLogger("streaming_availability")

_shutdown_requested = False


def _handle_signal(signum, frame):
    global _shutdown_requested
    if _shutdown_requested:
        logger.warning("Force quit requested, exiting immediately")
        sys.exit(1)
    logger.info("Shutdown requested, finishing current batch...")
    _shutdown_requested = True


# ---------------------------------------------------------------------------
# Processing functions
# ---------------------------------------------------------------------------


async def _process_spotify(
    results_db: ResultsDB,
    spotify: SpotifyClient,
    batch_size: int,
    retry_errors: bool = False,
) -> None:
    """Process pending albums against Spotify."""
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
            artist = row["discogs_artist"] or row["display_artist"]
            title = strip_format_suffix(row["discogs_title"] or row["display_title"])
            is_single = bool(row["is_single"])

            try:
                # Album search first
                results = await spotify.search_album(artist, title)
                best = _find_best_spotify_match(artist, title, results) if results else None

                # For singles: fall back to track search if album search missed
                if not best and is_single:
                    track_results = await spotify.search_track(artist, title)
                    best = (
                        _find_best_track_match(artist, title, track_results)
                        if track_results
                        else None
                    )

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
                hit_rate = found / processed * 100 if processed else 0
                logger.info(
                    "  Spotify: %d / %d (%.1f%%) | found: %d (%.0f%%) | not found: %d | errors: %d",
                    processed,
                    total_pending,
                    pct,
                    found,
                    hit_rate,
                    not_found,
                    errors,
                )

    hit_rate = found / processed * 100 if processed else 0
    logger.info(
        "Spotify complete: %d processed | found: %d (%.0f%%) | not found: %d | errors: %d",
        processed,
        found,
        hit_rate,
        not_found,
        errors,
    )


async def _process_apple_music(
    results_db: ResultsDB,
    apple: AppleMusicClient,
    batch_size: int,
    check_all: bool = False,
) -> None:
    """Process albums against Apple Music."""

    async def _get_rows(limit: int) -> list:
        if check_all:
            return await results_db.get_pending("apple", limit=limit)
        return await results_db.get_spotify_misses_pending_apple(limit=limit)

    rows = await _get_rows(limit=1)
    if not rows:
        logger.info("No albums to check on Apple Music")
        return

    all_pending = await _get_rows(limit=100_000)
    total_pending = len(all_pending)
    label = "all pending" if check_all else "Spotify misses"
    logger.info("Apple Music: %d albums to check (%s)", total_pending, label)

    processed = 0
    found = 0
    not_found = 0
    errors = 0

    while not _shutdown_requested:
        rows = await _get_rows(limit=batch_size)
        if not rows:
            break

        for row in rows:
            if _shutdown_requested:
                break

            album_id = row["id"]
            artist = row["discogs_artist"] or row["display_artist"]
            title = strip_format_suffix(row["discogs_title"] or row["display_title"])

            try:
                results = await apple.search_album(artist, title)
                best = _find_best_apple_match(artist, title, results) if results else None
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
                hit_rate = found / processed * 100 if processed else 0
                logger.info(
                    "  Apple Music: %d / %d (%.1f%%) | found: %d (%.0f%%) | not found: %d | errors: %d",
                    processed,
                    total_pending,
                    pct,
                    found,
                    hit_rate,
                    not_found,
                    errors,
                )

    hit_rate = found / processed * 100 if processed else 0
    logger.info(
        "Apple Music complete: %d processed | found: %d (%.0f%%) | not found: %d | errors: %d",
        processed,
        found,
        hit_rate,
        not_found,
        errors,
    )


async def _process_deezer(
    results_db: ResultsDB,
    deezer: DeezerClient,
    batch_size: int,
) -> None:
    """Pre-filter albums through Deezer to identify digitally distributed releases."""
    rows = await results_db.get_pending("deezer", limit=1)
    if not rows:
        logger.info("No albums to check on Deezer")
        return

    all_pending = await results_db.get_pending("deezer", limit=100_000)
    total_pending = len(all_pending)
    logger.info("Deezer pre-filter: %d albums to check", total_pending)

    processed = 0
    found = 0
    not_found = 0
    errors = 0

    while not _shutdown_requested:
        rows = await results_db.get_pending("deezer", limit=batch_size)
        if not rows:
            break

        for row in rows:
            if _shutdown_requested:
                break

            album_id = row["id"]
            artist = row["discogs_artist"] or row["display_artist"]
            title = strip_format_suffix(row["discogs_title"] or row["display_title"])
            is_single = bool(row["is_single"])

            try:
                results = await deezer.search_album(artist, title)
                best = _find_best_deezer_match(artist, title, results) if results else None

                if not best and is_single:
                    track_results = await deezer.search_track(artist, title)
                    best = (
                        _find_best_deezer_track_match(artist, title, track_results)
                        if track_results
                        else None
                    )

                if best:
                    await results_db.update_result(
                        album_id,
                        "deezer",
                        "found",
                        url=best["url"],
                        confidence=best["confidence"],
                        matched_artist=best["matched_artist"],
                        matched_title=best["matched_title"],
                    )
                    found += 1
                else:
                    await results_db.update_result(album_id, "deezer", "not_found")
                    not_found += 1
            except Exception:
                logger.exception("Error processing %s - %s", artist, title)
                await results_db.update_result(album_id, "deezer", "error")
                errors += 1

            processed += 1
            if processed % batch_size == 0:
                pct = processed / total_pending * 100
                hit_rate = found / processed * 100 if processed else 0
                logger.info(
                    "  Deezer: %d / %d (%.1f%%) | found: %d (%.0f%%) | not found: %d | errors: %d",
                    processed,
                    total_pending,
                    pct,
                    found,
                    hit_rate,
                    not_found,
                    errors,
                )

    hit_rate = found / processed * 100 if processed else 0
    logger.info(
        "Deezer complete: %d processed | found: %d (%.0f%%) | not found: %d | errors: %d",
        processed,
        found,
        hit_rate,
        not_found,
        errors,
    )


async def _process_discogs_enrichment(
    results_db: ResultsDB,
    pool: object,
    batch_size: int,
    artist_mapping: dict[str, str] | None = None,
) -> None:
    """Enrich all pending albums with Discogs canonical names."""
    rows = await results_db.get_pending_discogs(limit=1)
    if not rows:
        logger.info("No albums to enrich from Discogs")
        return

    all_pending = await results_db.get_pending_discogs(limit=100_000)
    total_pending = len(all_pending)
    mapped = len(artist_mapping) if artist_mapping else 0
    logger.info(
        "Discogs enrichment: %d albums to process (%d artist mappings)", total_pending, mapped
    )

    processed = 0
    found = 0
    not_found = 0
    errors = 0

    while not _shutdown_requested:
        rows = await results_db.get_pending_discogs(limit=batch_size)
        if not rows:
            break

        for row in rows:
            if _shutdown_requested:
                break

            album_id = row["id"]
            artist = row["display_artist"]
            title = strip_format_suffix(row["display_title"])

            try:
                match = await enrich_album(pool, artist, title, artist_mapping=artist_mapping)
                if match:
                    await results_db.update_discogs_result(
                        album_id,
                        "found",
                        artist=match["artist_name"],
                        title=match["title"],
                        release_id=match["release_id"],
                    )
                    found += 1
                else:
                    await results_db.update_discogs_result(album_id, "not_found")
                    not_found += 1
            except Exception:
                logger.exception("Discogs enrichment error: %s - %s", artist, title)
                await results_db.update_discogs_result(album_id, "error")
                errors += 1

            processed += 1
            if processed % batch_size == 0:
                pct = processed / total_pending * 100
                hit_rate = found / processed * 100 if processed else 0
                logger.info(
                    "  Discogs: %d / %d (%.1f%%) | found: %d (%.0f%%) | not found: %d | errors: %d",
                    processed,
                    total_pending,
                    pct,
                    found,
                    hit_rate,
                    not_found,
                    errors,
                )

    hit_rate = found / processed * 100 if processed else 0
    logger.info(
        "Discogs enrichment complete: %d processed | found: %d (%.0f%%) | not found: %d | errors: %d",
        processed,
        found,
        hit_rate,
        not_found,
        errors,
    )


# ---------------------------------------------------------------------------
# Match scoring helpers
# ---------------------------------------------------------------------------


def _find_best_spotify_match(artist: str, title: str, results: list[dict]) -> dict | None:
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


def _find_best_track_match(artist: str, title: str, results: list[dict]) -> dict | None:
    best: dict | None = None
    best_score = 0.0
    for track in results:
        spotify_artist = track.get("artists", [{}])[0].get("name", "")
        spotify_title = track.get("name", "")
        artist_score = score_match(artist, spotify_artist)
        title_score = score_match(title, spotify_title)
        if not is_acceptable_match(artist_score, title_score):
            continue
        combined = (artist_score + title_score) / 2
        if combined > best_score:
            best_score = combined
            best = {
                "url": track.get("external_urls", {}).get("spotify", ""),
                "id": track.get("id", ""),
                "confidence": combined,
                "matched_artist": spotify_artist,
                "matched_title": spotify_title,
            }
    return best


def _find_best_apple_match(artist: str, title: str, results: list[dict]) -> dict | None:
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


def _find_best_deezer_match(artist: str, title: str, results: list[dict]) -> dict | None:
    best: dict | None = None
    best_score = 0.0
    for album in results:
        deezer_artist = album.get("artist", {}).get("name", "")
        deezer_title = album.get("title", "")
        artist_score = score_match(artist, deezer_artist)
        title_score = score_match(title, deezer_title)
        if not is_acceptable_match(artist_score, title_score):
            continue
        combined = (artist_score + title_score) / 2
        if combined > best_score:
            best_score = combined
            best = {
                "url": album.get("link", ""),
                "confidence": combined,
                "matched_artist": deezer_artist,
                "matched_title": deezer_title,
            }
    return best


def _find_best_deezer_track_match(artist: str, title: str, results: list[dict]) -> dict | None:
    best: dict | None = None
    best_score = 0.0
    for track in results:
        deezer_artist = track.get("artist", {}).get("name", "")
        deezer_title = track.get("title", "")
        artist_score = score_match(artist, deezer_artist)
        title_score = score_match(title, deezer_title)
        if not is_acceptable_match(artist_score, title_score):
            continue
        combined = (artist_score + title_score) / 2
        if combined > best_score:
            best_score = combined
            best = {
                "url": track.get("link", ""),
                "confidence": combined,
                "matched_artist": deezer_artist,
                "matched_title": deezer_title,
            }
    return best


async def _skip_compilations(results_db: ResultsDB) -> int:
    db = results_db._db
    assert db is not None
    cursor = await db.execute("""UPDATE albums SET
           spotify_status = 'skipped', apple_status = 'skipped',
           discogs_status = 'skipped', deezer_status = 'skipped'
           WHERE is_compilation = 1
           AND (spotify_status = 'pending' OR apple_status = 'pending'
                OR discogs_status = 'pending' OR deezer_status = 'pending')""")
    await db.commit()
    return cursor.rowcount


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


async def run(args: argparse.Namespace) -> None:
    results_db = ResultsDB(args.results_db)
    await results_db.connect()

    try:
        if args.stats:
            summary = await generate_summary(results_db)
            print(summary)
            return

        library_path = args.library_db
        if not Path(library_path).is_file():
            logger.error("library.db not found at %s", library_path)
            sys.exit(1)

        logger.info("Reading library from %s...", library_path)
        albums = await deduplicate_library(library_path)
        singles = sum(1 for a in albums if a.is_single)
        compilations = sum(1 for a in albums if a.is_compilation)
        logger.info(
            "Deduplicated: %d unique albums (%d singles, %d compilations)",
            len(albums),
            singles,
            compilations,
        )

        inserted = await results_db.insert_albums(albums)
        logger.info("Inserted %d new albums into results DB (duplicates ignored)", inserted)

        skipped = await _skip_compilations(results_db)
        if skipped:
            logger.info("Marked %d compilations as skipped", skipped)

        if args.dry_run:
            logger.info("Dry run complete. Use --stats to see current state.")
            return

        # Build pipeline: Discogs → Deezer → Spotify
        # Each stage takes an album_id, reads row from DB, processes, writes result,
        # and returns the album_id for the next stage (or None to filter).

        from scripts.streaming_availability.pipeline import Pipeline, Stage

        discogs_pool = None
        artist_mapping: dict[str, str] = {}
        discogs_url = os.environ.get("DATABASE_URL_DISCOGS")
        if discogs_url:
            import asyncpg

            async def _init_conn(conn: asyncpg.Connection) -> None:
                await conn.execute("SET pg_trgm.similarity_threshold = 0.4")

            discogs_pool = await asyncpg.create_pool(
                discogs_url, min_size=2, max_size=10, init=_init_conn
            )
            use_wxyc = await check_wxyc_schema(discogs_pool)
            if use_wxyc:
                unique_artists = sorted({a.display_artist for a in albums if not a.is_compilation})
                logger.info("Building artist mapping for %d artists...", len(unique_artists))
                artist_mapping = await build_artist_mapping(discogs_pool, unique_artists)
                logger.info(
                    "Artist mapping: %d of %d artists have Discogs name variants",
                    len(artist_mapping),
                    len(unique_artists),
                )
            else:
                logger.info("wxyc schema not found, using exact match only")
        else:
            logger.warning("DATABASE_URL_DISCOGS not set, skipping Discogs enrichment")

        deezer = DeezerClient()
        spotify: SpotifyClient | None = None
        if not args.apple_only:
            client_id = os.environ.get("SPOTIFY_CLIENT_ID")
            client_secret = os.environ.get("SPOTIFY_CLIENT_SECRET")
            if client_id and client_secret:
                spotify = SpotifyClient(client_id, client_secret)
            else:
                logger.error("SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET required in .env")

        # Counters for progress
        discogs_found = 0
        discogs_total = 0
        deezer_found = 0
        deezer_total = 0
        spotify_found = 0
        spotify_total = 0

        async def discogs_stage_fn(album_id: int) -> int | None:
            nonlocal discogs_found, discogs_total
            row = await results_db._db.execute("SELECT * FROM albums WHERE id = ?", (album_id,))
            row = await row.fetchone()
            if not row or row["discogs_status"] != "pending":
                return album_id  # already done, pass through

            artist = row["display_artist"]
            title = strip_format_suffix(row["display_title"])
            if discogs_pool:
                match = await enrich_album(
                    discogs_pool, artist, title, artist_mapping=artist_mapping
                )
                if match:
                    await results_db.update_discogs_result(
                        album_id,
                        "found",
                        artist=match["artist_name"],
                        title=match["title"],
                        release_id=match["release_id"],
                    )
                    discogs_found += 1
                else:
                    await results_db.update_discogs_result(album_id, "not_found")
            else:
                await results_db.update_discogs_result(album_id, "not_found")

            discogs_total += 1
            if discogs_total % 500 == 0:
                rate = discogs_found / discogs_total * 100
                logger.info("  Discogs: %d done (%.0f%% found)", discogs_total, rate)
            return album_id

        async def deezer_stage_fn(album_id: int) -> int | None:
            nonlocal deezer_found, deezer_total
            row = await results_db._db.execute("SELECT * FROM albums WHERE id = ?", (album_id,))
            row = await row.fetchone()
            if not row or row["deezer_status"] != "pending":
                # Already checked — pass through if it was found, filter if not
                if row and row["deezer_status"] == "found":
                    return album_id
                return None

            artist = row["discogs_artist"] or row["display_artist"]
            title = strip_format_suffix(row["discogs_title"] or row["display_title"])
            is_single = bool(row["is_single"])

            results = await deezer.search_album(artist, title)
            best = _find_best_deezer_match(artist, title, results) if results else None
            if not best and is_single:
                track_results = await deezer.search_track(artist, title)
                best = (
                    _find_best_deezer_track_match(artist, title, track_results)
                    if track_results
                    else None
                )

            deezer_total += 1
            if best:
                await results_db.update_result(
                    album_id,
                    "deezer",
                    "found",
                    url=best["url"],
                    confidence=best["confidence"],
                    matched_artist=best["matched_artist"],
                    matched_title=best["matched_title"],
                )
                deezer_found += 1
                if deezer_total % 500 == 0:
                    rate = deezer_found / deezer_total * 100
                    logger.info("  Deezer: %d done (%.0f%% found)", deezer_total, rate)
                return album_id  # pass to Spotify
            else:
                await results_db.update_result(album_id, "deezer", "not_found")
                # Also mark as not_found on Spotify (not digitally distributed)
                await results_db.update_result(album_id, "spotify", "not_found")
                if deezer_total % 500 == 0:
                    rate = deezer_found / deezer_total * 100
                    logger.info("  Deezer: %d done (%.0f%% found)", deezer_total, rate)
                return None  # filter from Spotify

        async def spotify_stage_fn(album_id: int) -> int | None:
            nonlocal spotify_found, spotify_total
            if not spotify:
                return None

            row = await results_db._db.execute("SELECT * FROM albums WHERE id = ?", (album_id,))
            row = await row.fetchone()
            if not row or row["spotify_status"] != "pending":
                return album_id

            artist = row["discogs_artist"] or row["display_artist"]
            title = strip_format_suffix(row["discogs_title"] or row["display_title"])
            is_single = bool(row["is_single"])

            results = await spotify.search_album(artist, title)
            best = _find_best_spotify_match(artist, title, results) if results else None
            if not best and is_single:
                track_results = await spotify.search_track(artist, title)
                best = (
                    _find_best_track_match(artist, title, track_results) if track_results else None
                )

            spotify_total += 1
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
                spotify_found += 1
            else:
                await results_db.update_result(album_id, "spotify", "not_found")

            if spotify_total % 100 == 0:
                rate = spotify_found / spotify_total * 100
                logger.info("  Spotify: %d done (%.0f%% found)", spotify_total, rate)
            return album_id

        # Get all non-compilation, non-skipped album IDs
        all_rows = await results_db.get_pending("discogs", limit=100_000)
        # Also include already-enriched albums that still need Deezer/Spotify
        deezer_pending = await results_db.get_pending("deezer", limit=100_000)
        spotify_pending = await results_db.get_pending("spotify", limit=100_000)

        # Combine all unique album IDs that need any processing
        album_ids = sorted(
            {row["id"] for row in all_rows}
            | {row["id"] for row in deezer_pending}
            | {row["id"] for row in spotify_pending}
        )
        logger.info("Starting pipeline: %d albums (Discogs → Deezer → Spotify)", len(album_ids))

        try:
            pipeline = Pipeline(batch_size=args.batch_size)
            pipeline.add_stage(Stage("discogs", discogs_stage_fn))
            pipeline.add_stage(Stage("deezer", deezer_stage_fn))
            if spotify:
                pipeline.add_stage(Stage("spotify", spotify_stage_fn))
            await pipeline.run(album_ids)
        finally:
            await deezer.close()
            if spotify:
                await spotify.close()
            if discogs_pool:
                await discogs_pool.close()

        # Log final stats
        if discogs_total:
            logger.info(
                "Discogs: %d found / %d total (%.0f%%)",
                discogs_found,
                discogs_total,
                discogs_found / discogs_total * 100,
            )
        if deezer_total:
            logger.info(
                "Deezer: %d found / %d total (%.0f%%)",
                deezer_found,
                deezer_total,
                deezer_found / deezer_total * 100,
            )
        if spotify_total:
            logger.info(
                "Spotify: %d found / %d total (%.0f%%)",
                spotify_found,
                spotify_total,
                spotify_found / spotify_total * 100,
            )

        # Apple Music (still sequential, runs after pipeline)
        if not args.spotify_only and not _shutdown_requested:
            apple = AppleMusicClient()
            try:
                await _process_apple_music(
                    results_db, apple, args.batch_size, check_all=args.apple_only
                )
            finally:
                await apple.close()

        # Report
        logger.info("Generating CSV report: %s", args.output)
        await generate_csv_report(results_db, args.output)
        summary = await generate_summary(results_db)
        print(summary)

    finally:
        await results_db.close()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Analyze streaming availability for the WXYC library catalog.",
    )
    parser.add_argument("--library-db", default="library.db")
    parser.add_argument("--results-db", default="streaming_availability.db")
    parser.add_argument("--output", default="streaming_report.csv")
    parser.add_argument("--spotify-only", action="store_true")
    parser.add_argument("--apple-only", action="store_true")
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--stats", action="store_true")
    parser.add_argument("--retry-errors", action="store_true")
    parser.add_argument("--verbose", action="store_true")
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
    logging.getLogger("httpx").setLevel(logging.WARNING)

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    asyncio.run(run(args))


if __name__ == "__main__":
    main()
