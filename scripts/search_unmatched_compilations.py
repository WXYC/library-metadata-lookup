"""Search unmatched compilations by title against Discogs cache, then streaming APIs.

Phase 1: Discogs PG cache title search (exact + fuzzy) → gets release_id + tracklist
Phase 2: Deezer/Spotify album search for Discogs misses → gets streaming URLs

Usage:
    railway run -- .venv/bin/python -m scripts.search_unmatched_compilations [OPTIONS]
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from argparse import ArgumentParser

import aiosqlite
from dotenv import load_dotenv

from clients.streaming.matching import (
    find_best_match,
    normalize_album_title,
    score_match,
    strip_format_suffix,
)
from scripts.track_streaming.compilation_search import build_compilation_query

logger = logging.getLogger("search_compilations")

_shutdown_requested = False


def _handle_signal(signum, frame):
    global _shutdown_requested
    if _shutdown_requested:
        sys.exit(1)
    logger.info("Shutdown requested, finishing current item...")
    _shutdown_requested = True


# Deezer/Spotify accessors for album search
_DEEZER_ARTIST = lambda r: r.get("artist", {}).get("name", "")  # noqa: E731
_DEEZER_TITLE = lambda r: r.get("title", "")  # noqa: E731
_DEEZER_URL = lambda r: r.get("link", "")  # noqa: E731
_SPOTIFY_ARTIST = lambda r: r.get("artists", [{}])[0].get("name", "")  # noqa: E731
_SPOTIFY_TITLE = lambda r: r.get("name", "")  # noqa: E731
_SPOTIFY_URL = lambda r: r.get("external_urls", {}).get("spotify", "")  # noqa: E731
_SPOTIFY_ID = lambda r: r.get("id", "")  # noqa: E731


async def search_discogs_by_title(pool, title: str) -> dict | None:
    """Search Discogs PG cache by title. Returns best match or None."""
    normalized = normalize_album_title(title)
    if not normalized:
        return None

    # Exact title match
    rows = await pool.fetch(
        """SELECT r.id, r.title, ra.artist_name
           FROM release r
           JOIN release_artist ra ON ra.release_id = r.id AND ra.extra = 0
           WHERE lower(f_unaccent(r.title)) = lower(f_unaccent($1))
           LIMIT 20""",
        title,
    )
    if not rows:
        # Try with stripped format suffix
        stripped = strip_format_suffix(title)
        if stripped != title:
            rows = await pool.fetch(
                """SELECT r.id, r.title, ra.artist_name
                   FROM release r
                   JOIN release_artist ra ON ra.release_id = r.id AND ra.extra = 0
                   WHERE lower(f_unaccent(r.title)) = lower(f_unaccent($1))
                   LIMIT 20""",
                stripped,
            )

    if not rows:
        return None

    # Score by title similarity (should be high for exact matches)
    best = None
    best_score = 0.0
    for row in rows:
        title_score = score_match(title, row["title"])
        if title_score > best_score:
            best_score = title_score
            best = {
                "release_id": row["id"],
                "title": row["title"],
                "artist": row["artist_name"],
                "confidence": title_score,
            }

    return best if best and best_score >= 70 else None


async def run(args) -> None:
    db = await aiosqlite.connect(args.db_path)
    db.row_factory = aiosqlite.Row

    cursor = await db.execute(
        """SELECT id, display_artist, display_title, discogs_release_id
           FROM albums
           WHERE is_compilation = 1
             AND spotify_status = 'skipped'
             AND id NOT IN (SELECT DISTINCT album_id FROM track_results)
           ORDER BY id
           LIMIT ?""",
        (args.limit,),
    )
    rows = [dict(r) for r in await cursor.fetchall()]
    logger.info("Loaded %d unmatched compilations", len(rows))

    if not rows:
        await db.close()
        return

    # Phase 1: Discogs title search
    discogs_found = 0
    discogs_misses = []
    discogs_url = os.environ.get("DATABASE_URL_DISCOGS")

    if discogs_url:
        import asyncpg

        pool = await asyncpg.create_pool(discogs_url, min_size=2, max_size=5)
        logger.info("Phase 1: Searching Discogs cache by title...")

        try:
            for i, row in enumerate(rows, 1):
                if _shutdown_requested:
                    break

                _, search_title = build_compilation_query(
                    row["display_artist"], row["display_title"]
                )
                match = await search_discogs_by_title(pool, search_title)

                if match:
                    discogs_found += 1
                    if not args.dry_run:
                        await db.execute(
                            """UPDATE albums SET discogs_release_id = ?,
                               discogs_artist = ?, discogs_title = ?,
                               discogs_status = 'found'
                               WHERE id = ?""",
                            (
                                match["release_id"],
                                match["artist"],
                                match["title"],
                                row["id"],
                            ),
                        )
                    logger.debug(
                        "Discogs: %s → %s (%s, %.0f%%)",
                        row["display_title"],
                        match["title"],
                        match["artist"],
                        match["confidence"],
                    )
                else:
                    discogs_misses.append(row)

                if i % 200 == 0:
                    if not args.dry_run:
                        await db.commit()
                    logger.info(
                        "  Discogs: %d / %d | found: %d | miss: %d",
                        i,
                        len(rows),
                        discogs_found,
                        len(discogs_misses),
                    )
        finally:
            if not args.dry_run:
                await db.commit()
            await pool.close()

        logger.info(
            "Phase 1 complete: %d Discogs matches, %d misses",
            discogs_found,
            len(discogs_misses),
        )
    else:
        logger.warning("DATABASE_URL_DISCOGS not set, skipping Discogs search")
        discogs_misses = rows

    if _shutdown_requested or args.discogs_only:
        await db.close()
        return

    # Phase 2: Streaming API search for Discogs misses
    logger.info("Phase 2: Searching streaming APIs for %d Discogs misses...", len(discogs_misses))

    from clients.streaming.deezer import DeezerClient

    deezer = DeezerClient()
    spotify = None

    client_id = os.environ.get("SPOTIFY_CLIENT_ID")
    client_secret = os.environ.get("SPOTIFY_CLIENT_SECRET")
    if client_id and client_secret:
        from clients.streaming.spotify import SpotifyClient

        spotify = SpotifyClient(client_id, client_secret)

    streaming_found = 0
    streaming_miss = 0

    try:
        for i, row in enumerate(discogs_misses, 1):
            if _shutdown_requested:
                break

            search_artist, search_title = build_compilation_query(
                row["display_artist"], row["display_title"]
            )
            found = False

            # Deezer album search
            try:
                results = await deezer.search_album(
                    search_artist or "", strip_format_suffix(search_title)
                )
                match = find_best_match(
                    results,
                    search_artist or "Various",
                    search_title,
                    artist_fn=_DEEZER_ARTIST,
                    title_fn=_DEEZER_TITLE,
                    url_fn=_DEEZER_URL,
                )
                # For compilations, relax artist check — just need title match
                if not match and results:
                    for r in results:
                        title_score = score_match(search_title, _DEEZER_TITLE(r))
                        if title_score >= 80:
                            match = {
                                "url": _DEEZER_URL(r),
                                "confidence": title_score,
                                "matched_title": _DEEZER_TITLE(r),
                            }
                            break
                if match:
                    found = True
                    if not args.dry_run:
                        await db.execute(
                            "UPDATE albums SET deezer_status = 'found', deezer_url = ?, deezer_confidence = ? WHERE id = ?",
                            (match["url"], match["confidence"], row["id"]),
                        )
            except Exception:
                logger.warning("Deezer error for %s", row["display_title"])

            # Spotify album search
            if spotify:
                try:
                    results = await spotify.search_album(
                        search_artist or "Various Artists",
                        strip_format_suffix(search_title),
                    )
                    match = find_best_match(
                        results,
                        search_artist or "Various",
                        search_title,
                        artist_fn=_SPOTIFY_ARTIST,
                        title_fn=_SPOTIFY_TITLE,
                        url_fn=_SPOTIFY_URL,
                        id_fn=_SPOTIFY_ID,
                    )
                    if not match and results:
                        for r in results:
                            title_score = score_match(search_title, _SPOTIFY_TITLE(r))
                            if title_score >= 80:
                                match = {
                                    "url": _SPOTIFY_URL(r),
                                    "id": _SPOTIFY_ID(r),
                                    "confidence": title_score,
                                    "matched_title": _SPOTIFY_TITLE(r),
                                }
                                break
                    if match:
                        found = True
                        if not args.dry_run:
                            await db.execute(
                                """UPDATE albums SET spotify_status = 'found',
                                   spotify_url = ?, spotify_id = ?, spotify_confidence = ?
                                   WHERE id = ?""",
                                (
                                    match["url"],
                                    match.get("id"),
                                    match["confidence"],
                                    row["id"],
                                ),
                            )
                except Exception:
                    logger.warning("Spotify error for %s", row["display_title"])

            if found:
                streaming_found += 1
            else:
                streaming_miss += 1

            if i % 100 == 0:
                if not args.dry_run:
                    await db.commit()
                hit_rate = streaming_found / i * 100
                logger.info(
                    "  Streaming: %d / %d | found: %d (%.0f%%) | miss: %d",
                    i,
                    len(discogs_misses),
                    streaming_found,
                    hit_rate,
                    streaming_miss,
                )
    finally:
        await deezer.close()
        if spotify:
            await spotify.close()
        if not args.dry_run:
            await db.commit()

    logger.info(
        "Phase 2 complete: %d streaming matches, %d misses",
        streaming_found,
        streaming_miss,
    )
    logger.info(
        "Total: %d Discogs + %d streaming = %d found out of %d",
        discogs_found,
        streaming_found,
        discogs_found + streaming_found,
        len(rows),
    )

    await db.close()


def main():
    load_dotenv()
    parser = ArgumentParser(description="Search unmatched compilations by title")
    parser.add_argument("--db-path", default="streaming_availability.db")
    parser.add_argument("--limit", type=int, default=100000)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--discogs-only", action="store_true", help="Skip streaming API search")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    asyncio.run(run(args))


if __name__ == "__main__":
    main()
