"""Re-search Spotify and Apple Music for albums where the artist is confirmed on Spotify
but the album wasn't found — likely due to title truncation or normalization mismatches.

Generates multiple search queries per album using both library and Discogs metadata,
then fuzzy-matches against results.

Usage:
    uv run python -m scripts.revalidate_misses [--dry-run] [--limit N] [--db-path PATH]
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from argparse import ArgumentParser
from datetime import UTC

import aiosqlite
from dotenv import load_dotenv

from clients.streaming.matching import (
    find_best_match,
    strip_format_suffix,
)
from clients.streaming.spotify import SpotifyClient

logger = logging.getLogger(__name__)

_SPOTIFY_ARTIST = lambda r: r.get("artists", [{}])[0].get("name", "")  # noqa: E731
_SPOTIFY_TITLE = lambda r: r.get("name", "")  # noqa: E731
_SPOTIFY_URL = lambda r: r.get("external_urls", {}).get("spotify", "")  # noqa: E731
_SPOTIFY_ID = lambda r: r.get("id", "")  # noqa: E731


def build_search_queries(row: dict) -> list[tuple[str, str]]:
    """Build deduplicated (artist, title) search queries for an album.

    Uses both library display info and Discogs metadata to generate multiple
    query variants. Strips format suffixes and deduplicates.
    """
    display_artist = row["display_artist"]
    display_title = strip_format_suffix(row["display_title"])
    discogs_artist = row.get("discogs_artist") or ""
    discogs_title = strip_format_suffix(row.get("discogs_title") or "")

    seen: set[tuple[str, str]] = set()
    queries: list[tuple[str, str]] = []

    def _add(artist: str, title: str) -> None:
        key = (artist.lower().strip(), title.lower().strip())
        if key not in seen and key[0] and key[1]:
            seen.add(key)
            queries.append((artist, title))

    # Primary: display info
    _add(display_artist, display_title)

    # Variant: Discogs title with display artist
    if discogs_title:
        _add(display_artist, discogs_title)

    # Variant: Discogs artist with display title
    if discogs_artist:
        _add(discogs_artist, display_title)

    # Variant: full Discogs info
    if discogs_artist and discogs_title:
        _add(discogs_artist, discogs_title)

    return queries


async def run(args) -> None:
    db = await aiosqlite.connect(args.db_path)
    db.row_factory = aiosqlite.Row

    cursor = await db.execute(
        """SELECT id, display_artist, display_title, discogs_artist, discogs_title
           FROM albums
           WHERE artist_on_spotify = 1
             AND spotify_status = 'not_found'
             AND deezer_status = 'not_found'
             AND bandcamp_url IS NULL
           ORDER BY id
           LIMIT ?""",
        (args.limit,),
    )
    rows = [dict(r) for r in await cursor.fetchall()]
    logger.info("Loaded %d albums to revalidate", len(rows))

    if not rows:
        await db.close()
        return

    client_id = os.environ.get("SPOTIFY_CLIENT_ID")
    client_secret = os.environ.get("SPOTIFY_CLIENT_SECRET")
    if not client_id or not client_secret:
        logger.error("SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET required")
        await db.close()
        sys.exit(1)

    spotify = SpotifyClient(client_id, client_secret)
    found = 0
    still_missing = 0

    try:
        for i, row in enumerate(rows, 1):
            album_id = row["id"]
            queries = build_search_queries(row)
            best_match = None

            for artist, title in queries:
                results = await spotify.search_album(artist, title)
                match = find_best_match(
                    results,
                    artist,
                    title,
                    artist_fn=_SPOTIFY_ARTIST,
                    title_fn=_SPOTIFY_TITLE,
                    url_fn=_SPOTIFY_URL,
                    id_fn=_SPOTIFY_ID,
                )
                if match and (best_match is None or match["confidence"] > best_match["confidence"]):
                    best_match = match

            if best_match:
                found += 1
                logger.info(
                    "✅ [%d/%d] %s - %s → %s (%.0f%% via %s)",
                    i,
                    len(rows),
                    row["display_artist"],
                    row["display_title"],
                    best_match["matched_title"],
                    best_match["confidence"],
                    best_match["url"],
                )
                if not args.dry_run:
                    from datetime import datetime

                    now_str = datetime.now(UTC).isoformat()
                    await db.execute(
                        """UPDATE albums SET
                           spotify_status = 'found', spotify_url = ?, spotify_id = ?,
                           spotify_confidence = ?, spotify_matched_artist = ?,
                           spotify_matched_title = ?, spotify_checked_at = ?
                           WHERE id = ?""",
                        (
                            best_match["url"],
                            best_match.get("id"),
                            best_match["confidence"],
                            best_match["matched_artist"],
                            best_match["matched_title"],
                            now_str,
                            album_id,
                        ),
                    )
                    await db.commit()
            else:
                still_missing += 1
                logger.debug(
                    "❌ [%d/%d] %s - %s (tried %d queries)",
                    i,
                    len(rows),
                    row["display_artist"],
                    row["display_title"],
                    len(queries),
                )

            if i % 50 == 0:
                logger.info(
                    "Progress: %d/%d | found: %d | still missing: %d",
                    i,
                    len(rows),
                    found,
                    still_missing,
                )
    finally:
        await spotify.close()
        await db.close()

    logger.info("Done: %d found, %d still missing out of %d", found, still_missing, len(rows))


def main():
    load_dotenv()
    parser = ArgumentParser(description="Re-search Spotify for artist-on-Spotify misses")
    parser.add_argument("--db-path", default="streaming_availability.db")
    parser.add_argument("--limit", type=int, default=10000)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
