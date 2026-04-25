"""Match Discogs-unmatched albums against local MusicBrainz release groups.

For albums without a Discogs release ID, searches MusicBrainz for matching
artist + release group title. Provides MBIDs for future enrichment.

Usage:
    .venv/bin/python scripts/musicbrainz_matching.py [--dry-run] [--limit N]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sqlite3

import asyncpg
from rapidfuzz import fuzz

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

SQLITE_PATH = "streaming_availability.db"
MB_URL = "postgresql://jake@localhost/musicbrainz"


async def main(args: argparse.Namespace) -> None:
    # Load unmatched albums
    conn_sa = sqlite3.connect(SQLITE_PATH)
    rows = conn_sa.execute(
        "SELECT id, display_artist, display_title FROM albums "
        "WHERE is_compilation = 0 AND discogs_release_id IS NULL"
    ).fetchall()
    conn_sa.close()

    if args.limit:
        rows = rows[: args.limit]
    log.info(f"Unmatched albums: {len(rows)}")

    mb = await asyncpg.connect(MB_URL)

    # Phase 1: Exact artist name match → get release groups → fuzzy title
    found = 0
    results = []
    total = len(rows)

    for i, (sa_id, artist, title) in enumerate(rows):
        # Find artist by exact lowercase name
        mb_artist = await mb.fetchrow(
            "SELECT id, name FROM mb_artist WHERE lower(name) = lower($1) LIMIT 1",
            artist,
        )
        if not mb_artist:
            # Try alias
            mb_artist = await mb.fetchrow(
                "SELECT a.id, a.name FROM mb_artist a "
                "JOIN mb_artist_alias aa ON aa.artist = a.id "
                "WHERE lower(aa.name) = lower($1) LIMIT 1",
                artist,
            )

        if not mb_artist:
            if (i + 1) % 2000 == 0:
                log.info(f"  {i + 1}/{total} processed, {found} found")
            continue

        # Get all release groups for this artist
        rg_rows = await mb.fetch(
            "SELECT DISTINCT rg.id, rg.name "
            "FROM mb_release_group rg "
            "JOIN mb_artist_credit_name acn ON acn.artist_credit = rg.artist_credit "
            "WHERE acn.artist = $1",
            mb_artist["id"],
        )

        # Fuzzy match title
        best_score = 0
        best_rg = None
        clean_title = title.strip()
        for rg in rg_rows:
            score = fuzz.token_sort_ratio(clean_title.lower(), rg["name"].lower())
            if score > best_score:
                best_score = score
                best_rg = rg

        if best_rg and best_score >= 75:
            found += 1
            results.append(
                {
                    "sa_id": sa_id,
                    "artist": artist,
                    "title": title,
                    "mb_artist_id": mb_artist["id"],
                    "mb_artist_name": mb_artist["name"],
                    "mb_release_group_id": best_rg["id"],
                    "mb_release_group_name": best_rg["name"],
                    "score": best_score,
                }
            )
            if found <= 20:
                log.info(
                    f"  [{artist}] {title} => [{mb_artist['name']}] {best_rg['name']} (score:{best_score})"
                )

        if (i + 1) % 2000 == 0 or i + 1 == total:
            log.info(f"  {i + 1}/{total} processed, {found} found")

    await mb.close()
    log.info(f"Total matched: {found} / {total} ({found / total * 100:.1f}%)")

    output = "musicbrainz_matches.json"
    with open(output, "w") as f:
        json.dump(results, f, indent=2)
    log.info(f"Saved {len(results)} to {output}")

    if not args.dry_run and results:
        log.info("MusicBrainz matches are informational (no streaming URLs). No DB update needed.")
        log.info(f"Results saved to {output} for future enrichment.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Match against MusicBrainz")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    asyncio.run(main(args))
