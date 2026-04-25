"""Find Discogs release matches for catalog albums missing discogs_release_id.

Uses exact title match against the full Discogs release table (19M releases)
with fuzzy artist name scoring in Python. Catches disambiguation suffixes
("3-D" → "3-D (4)"), "The" prefix mismatches, diacritics, and punctuation.

Usage:
    .venv/bin/python scripts/enrich_discogs_matches.py [--dry-run] [--min-score 70]
"""

from __future__ import annotations

import argparse
import asyncio
import logging

import aiosqlite
import asyncpg
from rapidfuzz import fuzz

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

DISCOGS_URL = "postgresql://jake@localhost/discogs"
SQLITE_PATH = "streaming_availability.db"


async def main(args: argparse.Namespace) -> None:
    # Load albums without Discogs match
    async with aiosqlite.connect(SQLITE_PATH) as db:
        db.row_factory = aiosqlite.Row
        rows = await db.execute_fetchall(
            "SELECT id, display_artist, display_title FROM albums "
            "WHERE is_compilation = 0 AND discogs_release_id IS NULL"
        )
    albums = [(r["id"], r["display_artist"], r["display_title"]) for r in rows]
    log.info(f"Albums without Discogs match: {len(albums):,}")

    conn = await asyncpg.connect(DISCOGS_URL)
    try:
        matches: list[
            tuple[int, int, str, str, float]
        ] = []  # (sa_id, release_id, artist, title, score)
        total = len(albums)

        for i, (sa_id, artist, title) in enumerate(albums):
            # Exact title match using btree index on lower(title)
            hits = await conn.fetch(
                """
                SELECT r.id, r.title, ra.artist_name
                FROM release r
                JOIN release_artist ra ON ra.release_id = r.id AND ra.extra = 0
                WHERE lower(r.title) = lower($1)
                """,
                title,
            )

            if hits:
                best = max(
                    hits,
                    key=lambda h: fuzz.token_sort_ratio(artist.lower(), h["artist_name"].lower()),
                )
                score = fuzz.token_sort_ratio(artist.lower(), best["artist_name"].lower())
                if score >= args.min_score:
                    matches.append((sa_id, best["id"], best["artist_name"], best["title"], score))

            if (i + 1) % 5000 == 0 or i + 1 == total:
                log.info(f"  {i + 1:,}/{total:,} processed, {len(matches):,} found")

        log.info(f"Total matches: {len(matches):,} / {total:,} ({len(matches) / total * 100:.1f}%)")

        if args.dry_run:
            log.info("Dry run — not updating database")
            for sa_id, _rid, dart, dtitle, score in matches[:30]:
                orig = next(a for a in albums if a[0] == sa_id)
                log.info(f"  [{orig[1]}] {orig[2]} => [{dart}] {dtitle} (score: {score:.0f})")
            return

        # Update streaming_availability.db
        async with aiosqlite.connect(SQLITE_PATH) as db:
            updated = 0
            for sa_id, release_id, discogs_artist, discogs_title, _score in matches:
                await db.execute(
                    "UPDATE albums SET discogs_release_id = ?, discogs_artist = ?, "
                    "discogs_title = ?, discogs_status = 'found' "
                    "WHERE id = ? AND discogs_release_id IS NULL",
                    (release_id, discogs_artist, discogs_title, sa_id),
                )
                updated += 1
                if updated % 1000 == 0:
                    await db.commit()
                    log.info(f"  Updated {updated:,}/{len(matches):,}")
            await db.commit()
            log.info(f"Updated {updated:,} albums with Discogs release IDs")

        # Report new totals
        async with aiosqlite.connect(SQLITE_PATH) as db:
            row = await db.execute_fetchall(
                "SELECT "
                "SUM(CASE WHEN discogs_release_id IS NOT NULL THEN 1 ELSE 0 END), "
                "SUM(CASE WHEN discogs_release_id IS NULL THEN 1 ELSE 0 END), "
                "COUNT(*) "
                "FROM albums WHERE is_compilation = 0"
            )
            has_discogs, no_discogs, total_albums = row[0]
            log.info(
                f"Discogs coverage: {has_discogs:,}/{total_albums:,} "
                f"({has_discogs / total_albums * 100:.1f}%) — "
                f"{no_discogs:,} still unmatched"
            )

    finally:
        await conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Enrich albums with Discogs release matches")
    parser.add_argument("--dry-run", action="store_true", help="Show matches without updating")
    parser.add_argument(
        "--min-score", type=int, default=70, help="Minimum artist fuzzy score (default: 70)"
    )
    args = parser.parse_args()
    asyncio.run(main(args))
