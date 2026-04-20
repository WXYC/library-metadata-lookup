"""Track-level streaming availability for singles and compilations.

Extracts individual tracks, resolves them against an in-memory index of
on-streaming Discogs tracklists, then falls back to Spotify/Deezer track
search for unresolved tracks.

Usage:
    uv run python -m scripts.track_streaming [OPTIONS]
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
from argparse import ArgumentParser, Namespace
from pathlib import Path

from dotenv import load_dotenv

from scripts.streaming_availability.results_db import ResultsDB
from scripts.track_streaming.api_search import search_track_on_services
from scripts.track_streaming.local_index import LocalStreamingIndex, TrackEntry
from scripts.track_streaming.track_extractor import (
    extract_compilation_tracks,
    extract_single_tracks,
)

logger = logging.getLogger("track_streaming")

_shutdown_requested = False


def _handle_signal(signum, frame):
    global _shutdown_requested
    if _shutdown_requested:
        logger.warning("Force quit requested, exiting immediately")
        sys.exit(1)
    logger.info("Shutdown requested, finishing current batch...")
    _shutdown_requested = True


# ---------------------------------------------------------------------------
# Phase 1: Extract tracks
# ---------------------------------------------------------------------------


async def phase_extract(results_db: ResultsDB, args) -> int:
    """Extract tracks from singles and compilations into track_results table."""
    assert results_db._db is not None
    inserted = 0

    # Singles
    cursor = await results_db._db.execute(
        """SELECT id, display_artist, display_title, discogs_release_id
           FROM albums WHERE is_single = 1
           AND id NOT IN (SELECT DISTINCT album_id FROM track_results WHERE source_type = 'single')
           ORDER BY id LIMIT ?""",
        (args.limit,),
    )
    singles = [dict(row) for row in await cursor.fetchall()]
    logger.info("Singles to extract: %d", len(singles))

    # Connect to Discogs cache if available
    discogs_cache = None
    discogs_pool = None
    discogs_url = os.environ.get("DATABASE_URL_DISCOGS")
    if discogs_url:
        try:
            import asyncpg

            from discogs.cache_service import DiscogsCacheService

            discogs_pool = await asyncpg.create_pool(discogs_url, min_size=1, max_size=5)
            discogs_cache = DiscogsCacheService(discogs_pool)
            logger.info("Connected to Discogs cache")
        except Exception:
            logger.warning("Could not connect to Discogs cache, using title parsing only")

    try:
        for i, row in enumerate(singles, 1):
            if _shutdown_requested:
                break
            tracks = await extract_single_tracks(row, discogs_cache=discogs_cache)
            if tracks:
                count = await results_db.insert_tracks(tracks)
                inserted += count
            if i % 500 == 0:
                logger.info("  Singles: %d / %d extracted (%d tracks)", i, len(singles), inserted)
    finally:
        if discogs_pool:
            await discogs_pool.close()

    logger.info("Singles extraction complete: %d tracks from %d singles", inserted, len(singles))

    if _shutdown_requested:
        return inserted

    # Compilations
    comp_json_path = args.comp_json
    if not Path(comp_json_path).is_file():
        logger.warning(
            "compilation_track_artists.json not found at %s, skipping compilations", comp_json_path
        )
        return inserted

    with open(comp_json_path) as f:
        comp_data_list = json.load(f)
    comp_data_by_id = {entry["comp_id"]: entry for entry in comp_data_list}
    logger.info("Loaded %d compilations from JSON", len(comp_data_by_id))

    cursor = await results_db._db.execute(
        """SELECT id, display_artist, display_title
           FROM albums WHERE is_compilation = 1
           AND id NOT IN (SELECT DISTINCT album_id FROM track_results WHERE source_type = 'compilation')
           ORDER BY id LIMIT ?""",
        (args.limit,),
    )
    comps = [dict(row) for row in await cursor.fetchall()]
    logger.info("Compilations to extract: %d", len(comps))

    comp_inserted = 0
    for i, row in enumerate(comps, 1):
        if _shutdown_requested:
            break
        comp_data = comp_data_by_id.get(row["id"])
        tracks = extract_compilation_tracks(row, comp_data)
        if tracks:
            count = await results_db.insert_tracks(tracks)
            comp_inserted += count
        if i % 500 == 0:
            logger.info(
                "  Compilations: %d / %d extracted (%d tracks)", i, len(comps), comp_inserted
            )

    logger.info(
        "Compilation extraction complete: %d tracks from %d compilations", comp_inserted, len(comps)
    )
    inserted += comp_inserted
    return inserted


# ---------------------------------------------------------------------------
# Phase 2: Build local index
# ---------------------------------------------------------------------------


async def phase_build_index(results_db: ResultsDB) -> LocalStreamingIndex:
    """Build in-memory index from on-streaming albums with Discogs tracklists."""
    discogs_url = os.environ.get("DATABASE_URL_DISCOGS")
    if not discogs_url:
        logger.warning("DATABASE_URL_DISCOGS not set, local index will be empty")
        return LocalStreamingIndex()

    import asyncpg

    pool = await asyncpg.create_pool(discogs_url, min_size=2, max_size=10)

    try:
        # Get on-streaming album IDs with Discogs release IDs
        assert results_db._db is not None
        cursor = await results_db._db.execute(
            """SELECT id, discogs_release_id, spotify_url, deezer_url
               FROM albums
               WHERE (spotify_status = 'found' OR deezer_status = 'found'
                      OR apple_status = 'found' OR bandcamp_url IS NOT NULL)
                 AND discogs_release_id IS NOT NULL"""
        )
        albums = [dict(row) for row in await cursor.fetchall()]
        logger.info(
            "Building local index from %d on-streaming albums with Discogs data", len(albums)
        )

        # Map discogs_release_id → album info
        release_to_album = {}
        for a in albums:
            release_to_album[a["discogs_release_id"]] = a

        # Batch fetch tracklists from Discogs PG
        index = LocalStreamingIndex()
        release_ids = list(release_to_album.keys())
        batch_size = 1000

        for batch_start in range(0, len(release_ids), batch_size):
            if _shutdown_requested:
                break
            batch = release_ids[batch_start : batch_start + batch_size]
            rows = await pool.fetch(
                """SELECT rt.release_id, rt.title, rt.sequence,
                          COALESCE(rta.artist_name, ra.artist_name) as artist_name
                   FROM release_track rt
                   JOIN release_artist ra ON ra.release_id = rt.release_id AND ra.extra = 0
                   LEFT JOIN release_track_artist rta ON rta.release_id = rt.release_id
                             AND rta.track_sequence = rt.sequence
                   WHERE rt.release_id = ANY($1)""",
                batch,
            )
            for row in rows:
                album = release_to_album.get(row["release_id"])
                if not album:
                    continue
                index.add(
                    TrackEntry(
                        artist=row["artist_name"],
                        title=row["title"],
                        album_id=album["id"],
                        discogs_release_id=row["release_id"],
                        spotify_url=album.get("spotify_url"),
                        deezer_url=album.get("deezer_url"),
                    )
                )

            if (batch_start + batch_size) % 5000 == 0:
                logger.info(
                    "  Index: %d / %d releases loaded (%d tracks)",
                    min(batch_start + batch_size, len(release_ids)),
                    len(release_ids),
                    index.track_count,
                )

        logger.info(
            "Local index built: %d tracks, %d unique titles from %d releases",
            index.track_count,
            index.title_count,
            len(release_ids),
        )
        return index
    finally:
        await pool.close()


# ---------------------------------------------------------------------------
# Phase 3: Local resolution
# ---------------------------------------------------------------------------


async def phase_local_resolve(results_db: ResultsDB, index: LocalStreamingIndex) -> tuple[int, int]:
    """Resolve pending tracks against the local index. Returns (resolved, checked)."""
    resolved = 0
    checked = 0

    while not _shutdown_requested:
        pending = await results_db.get_pending_tracks(limit=500)
        if not pending:
            break

        for row in pending:
            if _shutdown_requested:
                break
            checked += 1
            match = index.lookup(row["artist"], row["title"])
            if match:
                await results_db.update_track_resolution(
                    track_id=row["id"],
                    status="local_match",
                    resolved_via="local_index",
                    resolved_album_id=match.album_id,
                    resolved_release_id=match.discogs_release_id,
                    spotify_url=match.spotify_url,
                    deezer_url=match.deezer_url,
                )
                resolved += 1
            else:
                await results_db.update_track_resolution(track_id=row["id"], status="local_miss")

        if checked % 5000 == 0 and checked > 0:
            logger.info("  Local resolve: %d checked, %d resolved", checked, resolved)

    # Mark remaining pending tracks that weren't matched locally as 'pending_api'
    # so the API phase can distinguish them. Actually, the API phase just reads
    # all pending tracks, so no status change needed.
    logger.info("Local resolution complete: %d resolved out of %d checked", resolved, checked)
    return resolved, checked


# ---------------------------------------------------------------------------
# Phase 4: API fallback
# ---------------------------------------------------------------------------


async def phase_api_search(results_db: ResultsDB, args) -> tuple[int, int]:
    """Search Spotify/Deezer for tracks not resolved locally. Returns (found, checked)."""
    spotify = None
    deezer = None

    client_id = os.environ.get("SPOTIFY_CLIENT_ID")
    client_secret = os.environ.get("SPOTIFY_CLIENT_SECRET")
    if client_id and client_secret:
        from scripts.streaming_availability.spotify_client import SpotifyClient

        spotify = SpotifyClient(client_id, client_secret)

    from scripts.streaming_availability.deezer_client import DeezerClient

    deezer = DeezerClient()

    found = 0
    checked = 0

    try:
        while not _shutdown_requested:
            tracks = await results_db.get_local_miss_tracks(limit=args.batch_size)
            if not tracks:
                break

            for row in tracks:
                if _shutdown_requested:
                    break
                checked += 1
                try:
                    result = await search_track_on_services(
                        row["artist"], row["title"], spotify, deezer
                    )
                    if result:
                        await results_db.update_track_resolution(
                            track_id=row["id"],
                            status="api_match",
                            resolved_via="spotify_track"
                            if result.get("spotify_url")
                            else "deezer_track",
                            spotify_url=result.get("spotify_url"),
                            spotify_confidence=result.get("spotify_confidence"),
                            deezer_url=result.get("deezer_url"),
                            deezer_confidence=result.get("deezer_confidence"),
                        )
                        found += 1
                    else:
                        await results_db.update_track_resolution(
                            track_id=row["id"], status="not_found"
                        )
                except Exception:
                    logger.exception("Error searching for %s - %s", row["artist"], row["title"])
                    await results_db.update_track_resolution(track_id=row["id"], status="error")

                if checked % 100 == 0:
                    hit_rate = found / checked * 100 if checked else 0
                    logger.info(
                        "  API search: %d checked | found: %d (%.0f%%)",
                        checked,
                        found,
                        hit_rate,
                    )
    finally:
        if spotify:
            await spotify.close()
        await deezer.close()

    logger.info("API search complete: %d found out of %d checked", found, checked)
    return found, checked


# ---------------------------------------------------------------------------
# Phase 5: Album-level rollup
# ---------------------------------------------------------------------------


async def phase_album_rollup(results_db: ResultsDB) -> tuple[int, int]:
    """Update album status based on track results. Returns (on_streaming, off_streaming)."""
    assert results_db._db is not None

    # Get all albums that have track results
    cursor = await results_db._db.execute("SELECT DISTINCT album_id FROM track_results")
    album_ids = [row[0] for row in await cursor.fetchall()]
    logger.info("Rolling up track results for %d albums", len(album_ids))

    on_streaming = 0
    off_streaming = 0

    for album_id in album_ids:
        if _shutdown_requested:
            break
        summary = await results_db.get_album_track_summary(album_id)
        if summary["pending"] > 0:
            continue  # Not fully resolved yet

        if summary["on_streaming"]:
            on_streaming += 1
        else:
            off_streaming += 1

    logger.info(
        "Album rollup: %d on streaming, %d off streaming, %d total",
        on_streaming,
        off_streaming,
        len(album_ids),
    )
    return on_streaming, off_streaming


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


async def run(args) -> None:
    results_db = ResultsDB(args.results_db)
    await results_db.connect()

    try:
        if args.stats:
            stats = await results_db.get_track_stats()
            print(f"\nTrack results: {stats.get('total', 0)} total")
            for status, count in sorted(stats.items()):
                if status != "total":
                    print(f"  {status}: {count}")
            return

        # Phase 1: Extract
        if args.phase in ("extract", "all"):
            extracted = await phase_extract(results_db, args)
            logger.info("Phase 1 complete: %d tracks extracted", extracted)
            if args.dry_run:
                logger.info("Dry run — stopping after extraction")
                return

        # Phase 2: Build index
        if args.phase in ("resolve", "all") and not _shutdown_requested:
            index = await phase_build_index(results_db)

            # Phase 3: Local resolution
            resolved, checked = await phase_local_resolve(results_db, index)
            logger.info("Phase 3 complete: %d / %d resolved locally", resolved, checked)

        # Phase 4: API search
        if args.phase in ("search", "all") and not _shutdown_requested:
            if not args.skip_api:
                found, checked = await phase_api_search(results_db, args)
                logger.info("Phase 4 complete: %d / %d found via API", found, checked)

        # Phase 5: Album rollup
        if args.phase in ("rollup", "all") and not _shutdown_requested:
            on, off = await phase_album_rollup(results_db)
            logger.info("Phase 5 complete: %d on streaming, %d off streaming", on, off)

        # Final stats
        stats = await results_db.get_track_stats()
        logger.info(
            "Final: %d total tracks | %s",
            stats.get("total", 0),
            " | ".join(f"{k}: {v}" for k, v in sorted(stats.items()) if k != "total"),
        )

    finally:
        await results_db.close()


def parse_args(argv: list[str] | None = None) -> Namespace:
    parser = ArgumentParser(
        description="Track-level streaming availability for singles and compilations.",
    )
    parser.add_argument("--results-db", default="streaming_availability.db")
    parser.add_argument(
        "--comp-json",
        default="compilation_track_artists.json",
        help="Path to compilation_track_artists.json",
    )
    parser.add_argument(
        "--phase",
        choices=["extract", "resolve", "search", "rollup", "all"],
        default="all",
    )
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--limit", type=int, default=100000)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--stats", action="store_true")
    parser.add_argument("--skip-api", action="store_true", help="Skip API search phase")
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
