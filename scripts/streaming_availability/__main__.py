"""Analyze streaming availability for the WXYC library catalog.

Searches Deezer (pre-filter), Spotify, and Apple Music to identify albums
NOT on streaming platforms.

Usage:
    uv run python -m scripts.streaming_availability [OPTIONS]
"""

from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import asyncpg
import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

from dotenv import load_dotenv

from clients.streaming.apple_music import AppleMusicClient
from clients.streaming.deezer import DeezerClient
from clients.streaming.matching import (
    find_best_match,
    strip_discogs_suffix,
    strip_format_suffix,
)
from clients.streaming.spotify import SpotifyClient
from scripts.streaming_availability.dedup import deduplicate_library
from scripts.streaming_availability.discogs_enricher import (
    check_wxyc_schema,
    enrich_album,
    load_entity_store_mapping,
)
from scripts.streaming_availability.report import generate_csv_report, generate_summary
from scripts.streaming_availability.results_db import ResultsDB

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
    pool: asyncpg.Pool,
    batch_size: int,
    entity_mapping: dict[str, int] | None = None,
) -> None:
    """Enrich all pending albums with Discogs canonical names."""
    rows = await results_db.get_pending_discogs(limit=1)
    if not rows:
        logger.info("No albums to enrich from Discogs")
        return

    all_pending = await results_db.get_pending_discogs(limit=100_000)
    total_pending = len(all_pending)
    mapped = len(entity_mapping) if entity_mapping else 0
    logger.info(
        "Discogs enrichment: %d albums to process (%d entity store mappings)",
        total_pending,
        mapped,
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
                artist_id = entity_mapping.get(artist) if entity_mapping else None
                match = await enrich_album(pool, artist, title, discogs_artist_id=artist_id)
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


# ---------------------------------------------------------------------------
# Service-specific field extractors for find_best_match
# ---------------------------------------------------------------------------

_SPOTIFY_ARTIST = lambda r: r.get("artists", [{}])[0].get("name", "")  # noqa: E731
_SPOTIFY_TITLE = lambda r: r.get("name", "")  # noqa: E731
_SPOTIFY_URL = lambda r: r.get("external_urls", {}).get("spotify", "")  # noqa: E731
_SPOTIFY_ID = lambda r: r.get("id", "")  # noqa: E731

_APPLE_ARTIST = lambda r: r.get("artistName", "")  # noqa: E731
_APPLE_TITLE = lambda r: r.get("collectionName", "")  # noqa: E731
_APPLE_URL = lambda r: r.get("collectionViewUrl", "")  # noqa: E731

_DEEZER_ARTIST = lambda r: r.get("artist", {}).get("name", "")  # noqa: E731
_DEEZER_TITLE = lambda r: r.get("title", "")  # noqa: E731
_DEEZER_URL = lambda r: r.get("link", "")  # noqa: E731


def _find_best_spotify_match(artist: str, title: str, results: list[dict]) -> dict | None:
    return find_best_match(
        results,
        artist,
        title,
        artist_fn=_SPOTIFY_ARTIST,
        title_fn=_SPOTIFY_TITLE,
        url_fn=_SPOTIFY_URL,
        id_fn=_SPOTIFY_ID,
    )


def _find_best_track_match(artist: str, title: str, results: list[dict]) -> dict | None:
    return find_best_match(
        results,
        artist,
        title,
        artist_fn=_SPOTIFY_ARTIST,
        title_fn=_SPOTIFY_TITLE,
        url_fn=_SPOTIFY_URL,
        id_fn=_SPOTIFY_ID,
    )


def _find_best_apple_match(artist: str, title: str, results: list[dict]) -> dict | None:
    return find_best_match(
        results,
        artist,
        title,
        artist_fn=_APPLE_ARTIST,
        title_fn=_APPLE_TITLE,
        url_fn=_APPLE_URL,
    )


def _find_best_deezer_match(artist: str, title: str, results: list[dict]) -> dict | None:
    return find_best_match(
        results,
        artist,
        title,
        artist_fn=_DEEZER_ARTIST,
        title_fn=_DEEZER_TITLE,
        url_fn=_DEEZER_URL,
    )


def _find_best_deezer_track_match(artist: str, title: str, results: list[dict]) -> dict | None:
    return find_best_match(
        results,
        artist,
        title,
        artist_fn=_DEEZER_ARTIST,
        title_fn=_DEEZER_TITLE,
        url_fn=_DEEZER_URL,
    )


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

        # Retry misses: reset not_found to pending for another pass
        if args.retry_misses:
            db = results_db._db
            assert db is not None
            async with results_db._write_lock:
                cursor = await db.execute("""UPDATE albums SET deezer_status = 'pending',
                       deezer_url = NULL, deezer_confidence = NULL,
                       deezer_matched_artist = NULL, deezer_matched_title = NULL,
                       deezer_checked_at = NULL
                       WHERE deezer_status = 'not_found'""")
                dz_reset = cursor.rowcount
                # Reset Spotify only for albums that were auto-skipped due to Deezer miss
                cursor = await db.execute("""UPDATE albums SET spotify_status = 'pending',
                       spotify_url = NULL, spotify_id = NULL, spotify_confidence = NULL,
                       spotify_matched_artist = NULL, spotify_matched_title = NULL,
                       spotify_checked_at = NULL
                       WHERE spotify_status = 'not_found'
                       AND deezer_status = 'pending'""")
                sp_reset = cursor.rowcount
                await db.commit()
            logger.info(
                "Retry misses: reset %d Deezer, %d Spotify not_found to pending", dz_reset, sp_reset
            )

        # Build pipeline: Discogs → Deezer → Spotify
        # Each stage takes an album_id, reads row from DB, processes, writes result,
        # and returns the album_id for the next stage (or None to filter).

        from scripts.streaming_availability.pipeline import Pipeline, Stage

        discogs_pool = None
        entity_mapping: dict[str, int] = {}
        discogs_url = os.environ.get("DATABASE_URL_DISCOGS")
        if discogs_url:
            import asyncpg

            discogs_pool = await asyncpg.create_pool(discogs_url, min_size=2, max_size=10)
            use_wxyc = await check_wxyc_schema(discogs_pool)
            if use_wxyc:
                entity_mapping = await load_entity_store_mapping(discogs_pool)
                unique_artists = sorted({a.display_artist for a in albums if not a.is_compilation})
                mapped = sum(1 for a in unique_artists if a in entity_mapping)
                logger.info(
                    "Entity store: %d of %d artists have Discogs artist IDs",
                    mapped,
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

        async def discogs_stage_fn(item: dict) -> dict | None:
            nonlocal discogs_found, discogs_total
            album_id = item["id"]

            if item.get("discogs_done"):
                return item  # already enriched, pass through

            artist = item["display_artist"]
            title = strip_format_suffix(item["display_title"])
            if discogs_pool:
                artist_id = entity_mapping.get(artist)
                match = await enrich_album(discogs_pool, artist, title, discogs_artist_id=artist_id)
                if match:
                    await results_db.update_discogs_result(
                        album_id,
                        "found",
                        artist=match["artist_name"],
                        title=match["title"],
                        release_id=match["release_id"],
                    )
                    item["search_artist"] = match["artist_name"]
                    item["search_title"] = match["title"]
                    discogs_found += 1
                else:
                    await results_db.update_discogs_result(album_id, "not_found")
            else:
                await results_db.update_discogs_result(album_id, "not_found")

            discogs_total += 1
            if discogs_total % 500 == 0:
                rate = discogs_found / discogs_total * 100
                logger.info("  Discogs: %d done (%.0f%% found)", discogs_total, rate)
            return item

        async def deezer_stage_fn(item: dict) -> dict | None:
            nonlocal deezer_found, deezer_total
            album_id = item["id"]

            if item.get("deezer_done"):
                return item if item.get("deezer_found") else None

            raw_artist = item.get("search_artist") or item["display_artist"]
            raw_title = item.get("search_title") or item["display_title"]
            artist = strip_discogs_suffix(raw_artist)
            title = strip_format_suffix(raw_title)
            is_single = item.get("is_single", False)

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
                # Pass Deezer-matched names to Spotify for better matching
                item["deezer_artist"] = best["matched_artist"]
                item["deezer_title"] = best["matched_title"]
                if deezer_total % 500 == 0:
                    rate = deezer_found / deezer_total * 100
                    logger.info("  Deezer: %d done (%.0f%% found)", deezer_total, rate)
                return item  # pass to Spotify
            else:
                await results_db.update_result(album_id, "deezer", "not_found")
                # Also mark as not_found on Spotify (not digitally distributed)
                await results_db.update_result(album_id, "spotify", "not_found")
                if deezer_total % 500 == 0:
                    rate = deezer_found / deezer_total * 100
                    logger.info("  Deezer: %d done (%.0f%% found)", deezer_total, rate)
                return None  # filter from Spotify

        async def spotify_stage_fn(item: dict) -> dict | None:
            nonlocal spotify_found, spotify_total
            if not spotify:
                return None

            album_id = item["id"]
            if item.get("spotify_done"):
                return item

            # Prefer Deezer-matched names (closest to what streaming services use),
            # then Discogs canonical names, then library names
            raw_artist = (
                item.get("deezer_artist") or item.get("search_artist") or item["display_artist"]
            )
            raw_title = (
                item.get("deezer_title") or item.get("search_title") or item["display_title"]
            )
            artist = strip_discogs_suffix(raw_artist)
            title = strip_format_suffix(raw_title)
            is_single = item.get("is_single", False)

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
            return item

        # Build pipeline input: dicts with album data.
        # Albums enter at the earliest stage they still need.
        # Discogs-pending → start at Discogs stage
        # Deezer-pending (Discogs done) → start at Deezer stage (skip Discogs)
        # Spotify-pending (Deezer found) → start at Spotify stage (skip both)
        # Albums only reach Spotify if Deezer found them (filtering happens in stage).
        all_rows = await results_db.get_pending("discogs", limit=100_000)
        deezer_pending = await results_db.get_pending("deezer", limit=100_000)
        spotify_deezer_hits = await results_db.get_deezer_hits_pending_spotify(limit=100_000)

        seen: dict[int, dict] = {}
        for row in all_rows:
            seen[row["id"]] = {
                "id": row["id"],
                "display_artist": row["display_artist"],
                "display_title": row["display_title"],
                "is_single": bool(row["is_single"]),
            }
        for row in deezer_pending:
            if row["id"] not in seen:
                seen[row["id"]] = {
                    "id": row["id"],
                    "display_artist": row["display_artist"],
                    "display_title": row["display_title"],
                    "is_single": bool(row["is_single"]),
                    "discogs_done": True,
                    "search_artist": row["discogs_artist"],
                    "search_title": row["discogs_title"],
                }
        for row in spotify_deezer_hits:
            if row["id"] not in seen:
                seen[row["id"]] = {
                    "id": row["id"],
                    "display_artist": row["display_artist"],
                    "display_title": row["display_title"],
                    "is_single": bool(row["is_single"]),
                    "discogs_done": True,
                    "deezer_done": True,
                    "deezer_found": True,
                    "search_artist": row["discogs_artist"],
                    "search_title": row["discogs_title"],
                    "deezer_artist": row["deezer_matched_artist"],
                    "deezer_title": row["deezer_matched_title"],
                }

        pipeline_items = [seen[k] for k in sorted(seen)]
        logger.info(
            "Starting pipeline: %d albums (Discogs → Deezer → Spotify)", len(pipeline_items)
        )

        try:
            pipeline = Pipeline(batch_size=args.batch_size)
            pipeline.add_stage(Stage("discogs", discogs_stage_fn))
            pipeline.add_stage(Stage("deezer", deezer_stage_fn))
            if spotify:
                pipeline.add_stage(Stage("spotify", spotify_stage_fn))
            await pipeline.run(pipeline_items)
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
            am_team = os.environ.get("APPLE_MUSIC_TEAM_ID")
            am_key = os.environ.get("APPLE_MUSIC_KEY_ID")
            am_priv = os.environ.get("APPLE_MUSIC_PRIVATE_KEY")
            if not (am_team and am_key and am_priv):
                logger.warning(
                    "APPLE_MUSIC_TEAM_ID/KEY_ID/PRIVATE_KEY not all set — "
                    "skipping Apple Music phase"
                )
            else:
                apple = AppleMusicClient(team_id=am_team, key_id=am_key, private_key=am_priv)
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
    parser.add_argument(
        "--retry-misses",
        action="store_true",
        help="Reset not_found results on Deezer/Spotify and retry with improved matching",
    )
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
