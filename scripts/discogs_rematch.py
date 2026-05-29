"""Re-match albums missing Discogs links by title-only and fuzzy artist search.

Pass 1: Title-only search for enriched-but-unmatched albums (discogs_artist set, discogs_release_id NULL).
Pass 2: Fuzzy artist + title search for not-enriched albums (discogs_artist NULL).
Both passes support --compilations flag to include unmatched compilations.

Usage:
    railway run -- .venv/bin/python scripts/discogs_rematch.py [OPTIONS]
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
    normalize_artist_credit,
    score_match,
    strip_format_suffix,
)

logger = logging.getLogger("discogs_rematch")

_shutdown_requested = False


def _handle_signal(signum, frame):
    global _shutdown_requested
    if _shutdown_requested:
        sys.exit(1)
    logger.info("Shutdown requested, finishing current item...")
    _shutdown_requested = True


ARTIST_THRESHOLD_RELAXED = 70.0
TITLE_THRESHOLD = 70.0


async def pass1_title_search(pool, row: dict) -> dict | None:
    """Search Discogs PG cache by title only, then verify artist.

    For enriched-but-unmatched albums where the artist was found but the title
    didn't match. Searches all releases by exact title, then scores the result
    artist against the expected artist with a relaxed threshold.
    """
    title = strip_format_suffix(row["display_title"])
    if not title:
        return None

    rows = await pool.fetch(
        """SELECT r.id, r.title, ra.artist_name
           FROM release r
           JOIN release_artist ra ON ra.release_id = r.id AND ra.extra = 0
           WHERE lower(f_unaccent(r.title)) = lower(f_unaccent($1))
           LIMIT 50""",
        title,
    )

    if not rows:
        stripped = strip_format_suffix(title)
        if stripped != title:
            rows = await pool.fetch(
                """SELECT r.id, r.title, ra.artist_name
                   FROM release r
                   JOIN release_artist ra ON ra.release_id = r.id AND ra.extra = 0
                   WHERE lower(f_unaccent(r.title)) = lower(f_unaccent($1))
                   LIMIT 50""",
                stripped,
            )

    if not rows:
        return None

    # Score each result against the expected artist (use all name variants)
    expected_artist = row.get("discogs_artist") or row["display_artist"]
    artist_variants = normalize_artist_credit(expected_artist)

    best = None
    best_score = 0.0
    for r in rows:
        for variant in artist_variants:
            artist_score = score_match(variant, r["artist_name"])
            if artist_score > best_score:
                best_score = artist_score
                best = {
                    "release_id": r["id"],
                    "title": r["title"],
                    "artist": r["artist_name"],
                    "confidence": artist_score,
                }

    return best if best and best_score >= ARTIST_THRESHOLD_RELAXED else None


async def pass2_fuzzy_artist_search(pool, row: dict) -> dict | None:
    """Fuzzy artist search via pg_trgm, then search that artist's releases.

    For not-enriched albums where artist lookup failed entirely. Uses trigram
    similarity to find the artist, then fuzzy-matches titles.
    """
    display_artist = row["display_artist"]
    variants = normalize_artist_credit(display_artist)

    # Try each artist variant against the trigram index
    matched_artist = None
    for variant in variants:
        artist_rows = await pool.fetch(
            """SELECT artist_name, similarity(artist_name, $1) AS sim
               FROM release_artist
               WHERE artist_name % $1
               GROUP BY artist_name
               ORDER BY sim DESC
               LIMIT 5""",
            variant,
        )
        if artist_rows:
            # Pick the best fuzzy match
            for ar in artist_rows:
                s = score_match(variant, ar["artist_name"])
                if s >= ARTIST_THRESHOLD_RELAXED:
                    matched_artist = ar["artist_name"]
                    break
        if matched_artist:
            break

    if not matched_artist:
        return None

    # Search releases by that artist
    title = strip_format_suffix(row["display_title"])
    release_rows = await pool.fetch(
        """SELECT r.id, r.title, ra.artist_name
           FROM release r
           JOIN release_artist ra ON ra.release_id = r.id AND ra.extra = 0
           WHERE ra.artist_name = $1
           LIMIT 200""",
        matched_artist,
    )

    best = None
    best_score = 0.0
    for r in release_rows:
        title_score = score_match(title, r["title"])
        if title_score > best_score:
            best_score = title_score
            best = {
                "release_id": r["id"],
                "title": r["title"],
                "artist": r["artist_name"],
                "confidence": title_score,
            }

    return best if best and best_score >= TITLE_THRESHOLD else None


async def _run_pass(
    pass_fn,
    pool,
    rows: list[dict],
    db: aiosqlite.Connection,
    dry_run: bool,
    pass_name: str,
) -> int:
    """Run a pass function over a list of rows. Returns match count."""
    found = 0
    for i, row in enumerate(rows, 1):
        if _shutdown_requested:
            break
        result = await pass_fn(pool, row)
        if result:
            found += 1
            if not dry_run:
                await db.execute(
                    """UPDATE albums SET discogs_release_id = ?,
                       discogs_artist = ?, discogs_title = ?,
                       discogs_status = 'found'
                       WHERE id = ?""",
                    (result["release_id"], result["artist"], result["title"], row["id"]),
                )
            logger.debug(
                "%s: %s - %s → %s (%s, %.0f%%)",
                pass_name,
                row["display_artist"],
                row["display_title"],
                result["title"],
                result["artist"],
                result["confidence"],
            )
        if i % 500 == 0:
            if not dry_run:
                await db.commit()
            logger.info(
                "  %s: %d / %d | found: %d (%.0f%%)",
                pass_name,
                i,
                len(rows),
                found,
                found / i * 100,
            )
    if not dry_run:
        await db.commit()
    return found


async def run(args) -> None:
    db = await aiosqlite.connect(args.db_path)
    db.row_factory = aiosqlite.Row

    discogs_url = os.environ.get("DATABASE_URL_DISCOGS")
    if not discogs_url:
        logger.error("DATABASE_URL_DISCOGS required")
        await db.close()
        sys.exit(1)

    import asyncpg

    pool = await asyncpg.create_pool(discogs_url, min_size=2, max_size=5)

    try:
        comp_filter = "" if args.compilations else "AND is_compilation = 0"

        # Pass 1: enriched-but-unmatched (discogs_artist set, no release_id)
        if args.run_pass in ("1", "all"):
            cursor = await db.execute(
                f"""SELECT id, display_artist, display_title, discogs_artist
                    FROM albums
                    WHERE discogs_release_id IS NULL
                      AND discogs_artist IS NOT NULL
                      {comp_filter}
                    ORDER BY id LIMIT ?""",
                (args.limit,),
            )
            rows = [dict(r) for r in await cursor.fetchall()]
            logger.info("Pass 1 (title-only): %d albums to check", len(rows))
            found = await _run_pass(pass1_title_search, pool, rows, db, args.dry_run, "Pass 1")
            logger.info("Pass 1 complete: %d Discogs matches", found)

        # Pass 2: not-enriched (no discogs_artist)
        if args.run_pass in ("2", "all") and not _shutdown_requested:
            cursor = await db.execute(
                f"""SELECT id, display_artist, display_title
                    FROM albums
                    WHERE discogs_release_id IS NULL
                      AND discogs_artist IS NULL
                      AND discogs_status != 'skipped'
                      {comp_filter}
                    ORDER BY id LIMIT ?""",
                (args.limit,),
            )
            rows = [dict(r) for r in await cursor.fetchall()]
            logger.info("Pass 2 (fuzzy artist): %d albums to check", len(rows))
            found = await _run_pass(
                pass2_fuzzy_artist_search, pool, rows, db, args.dry_run, "Pass 2"
            )
            logger.info("Pass 2 complete: %d Discogs matches", found)

        # Summary
        cursor = await db.execute(
            "SELECT COUNT(*) FROM albums WHERE discogs_release_id IS NOT NULL"
        )
        row = await cursor.fetchone()
        total_matched = row[0] if row else 0
        logger.info("Total albums with Discogs match: %d", total_matched)

    finally:
        await pool.close()
        await db.close()


def main():
    load_dotenv()
    parser = ArgumentParser(description="Re-match albums missing Discogs links")
    parser.add_argument("--db-path", default="streaming_availability.db")
    parser.add_argument(
        "--pass",
        dest="run_pass",
        choices=["1", "2", "all"],
        default="all",
    )
    parser.add_argument("--compilations", action="store_true", help="Include compilations")
    parser.add_argument("--limit", type=int, default=100000)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    asyncio.run(run(args))


if __name__ == "__main__":
    main()
