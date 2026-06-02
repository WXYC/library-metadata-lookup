"""Measure the LML#478 80/80-floor's flip rate on a sample of library items.

Replays the ``fetch_artwork_for_items`` Discogs search for N random library
items and computes what the new ``find_best_typed_match`` floor would pick
versus today's unconditional ``response.results[0]``. Reports the flip
rates (to-None, to-different) and writes a per-row CSV for spot-checking.

The pre-merge acceptance criterion in the LML#478 plan is ≤ 5% flip-to-None.
Above that, escalate before merging.

Usage:
    DISCOGS_TOKEN=... DATABASE_URL_DISCOGS=postgresql://... \\
        python scripts/measure_artwork_match_floor.py [--sample 1000] [--csv /tmp/lml-478-floor.csv]

The script touches the Discogs cache + API exactly like the runtime path. To
keep API load low and the measurement repeatable, point ``DATABASE_URL_DISCOGS``
at a cache that's already warmed for the sampled items (the production cache
qualifies).
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import logging
import os
import random
import sys
from collections.abc import Iterable
from pathlib import Path

import asyncpg
from dotenv import load_dotenv
from wxyc_etl.text import is_compilation_artist

from clients.streaming.matching import find_best_typed_match, score_match
from discogs.cache_service import DiscogsCacheService
from discogs.models import DiscogsSearchRequest, DiscogsSearchResult
from discogs.service import DiscogsService
from library.db import LibraryDB
from lookup.orchestrator import (
    is_self_titled,
    map_library_format_to_discogs,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def mutate(artist: str, title: str) -> tuple[str, str]:
    """Mirror the artist/album mutations fetch_one applies before the search."""
    album = title
    if is_self_titled(album or ""):
        album = artist
    if is_compilation_artist(artist):
        artist = "Various"
    return artist, album


async def sample_library_items(
    db: LibraryDB, n: int
) -> list[tuple[int, str, str, str | None, str | None]]:
    """Return ``n`` random rows from the library as (id, artist, title, label, format)."""
    assert db._conn is not None
    has_label = db._has_label
    cols = "id, artist, title, format" + (", label" if has_label else "")
    cursor = await db._conn.execute(f"SELECT {cols} FROM library ORDER BY RANDOM() LIMIT ?", (n,))
    rows = await cursor.fetchall()
    out = []
    for row in rows:
        out.append(
            (
                row["id"],
                row["artist"] or "",
                row["title"] or "",
                row["label"] if has_label else None,
                row["format"],
            )
        )
    return out


def classify(
    results: Iterable[DiscogsSearchResult], query_artist: str, query_album: str
) -> tuple[DiscogsSearchResult | None, DiscogsSearchResult | None, float, float, float]:
    """Return (old_pick, new_pick, top1_artist_score, top1_title_score, best_combined)."""
    results = list(results)
    old_pick = results[0] if results else None

    top1_artist_score = 0.0
    top1_title_score = 0.0
    if old_pick is not None:
        top1_artist_score = score_match(query_artist, old_pick.artist or "")
        top1_title_score = score_match(query_album, old_pick.album or "")

    new_pick = find_best_typed_match(
        results,
        query_artist=query_artist,
        query_title=query_album,
        artist_fn=lambda r: r.artist,
        title_fn=lambda r: r.album,
    )

    best_combined = 0.0
    if new_pick is not None:
        a = score_match(query_artist, new_pick.artist or "")
        t = score_match(query_album, new_pick.album or "")
        best_combined = (a + t) / 2

    return old_pick, new_pick, top1_artist_score, top1_title_score, best_combined


async def run(args: argparse.Namespace) -> None:
    discogs_token = os.environ.get("DISCOGS_TOKEN")
    if not discogs_token:
        logger.error("DISCOGS_TOKEN required")
        sys.exit(1)
    discogs_url = os.environ.get("DATABASE_URL_DISCOGS")
    if not discogs_url:
        logger.error("DATABASE_URL_DISCOGS required (Discogs cache)")
        sys.exit(1)

    library_db = LibraryDB()
    await library_db.connect()

    pool = await asyncpg.create_pool(discogs_url, min_size=2, max_size=5)
    cache = DiscogsCacheService(pool)
    service = DiscogsService(token=discogs_token, cache_service=cache)

    csv_path = Path(args.csv)

    flipped_to_none = 0
    flipped_to_different = 0
    same_pick = 0
    no_results = 0

    try:
        items = await sample_library_items(library_db, args.sample)
        logger.info("Sampled %d library items", len(items))

        with csv_path.open("w", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(
                [
                    "library_id",
                    "artist_raw",
                    "title_raw",
                    "artist_query",
                    "album_query",
                    "old_release_id",
                    "new_release_id",
                    "outcome",
                    "top1_artist_score",
                    "top1_title_score",
                    "best_combined_score",
                ]
            )

            for i, (lid, artist_raw, title_raw, label, fmt) in enumerate(items, start=1):
                artist_q, album_q = mutate(artist_raw, title_raw)
                try:
                    response = await service.search(
                        DiscogsSearchRequest(
                            album=album_q,
                            artist=artist_q,
                            label=label,
                            format=map_library_format_to_discogs(fmt),
                        )
                    )
                except Exception as e:
                    logger.warning("search failed for id=%s: %s", lid, e)
                    continue

                old_pick, new_pick, a_score, t_score, best = classify(
                    response.results, artist_q, album_q
                )

                if old_pick is None:
                    no_results += 1
                    outcome = "no_results"
                elif new_pick is None:
                    flipped_to_none += 1
                    outcome = "flipped_to_none"
                elif new_pick.release_id != old_pick.release_id:
                    flipped_to_different += 1
                    outcome = "flipped_to_different"
                else:
                    same_pick += 1
                    outcome = "same"

                writer.writerow(
                    [
                        lid,
                        artist_raw,
                        title_raw,
                        artist_q,
                        album_q,
                        old_pick.release_id if old_pick else "",
                        new_pick.release_id if new_pick else "",
                        outcome,
                        f"{a_score:.1f}",
                        f"{t_score:.1f}",
                        f"{best:.1f}",
                    ]
                )

                if i % 50 == 0:
                    logger.info(
                        "  %d/%d | same=%d flip_none=%d flip_diff=%d no_results=%d",
                        i,
                        len(items),
                        same_pick,
                        flipped_to_none,
                        flipped_to_different,
                        no_results,
                    )

        total_with_results = same_pick + flipped_to_none + flipped_to_different
        denom = max(total_with_results, 1)
        logger.info("---")
        logger.info("Sample N=%d (with Discogs results: %d)", len(items), total_with_results)
        logger.info("  same pick:           %d (%.1f%%)", same_pick, 100 * same_pick / denom)
        logger.info(
            "  flipped to None:     %d (%.1f%%)  ← acceptance bar: ≤ 5%%",
            flipped_to_none,
            100 * flipped_to_none / denom,
        )
        logger.info(
            "  flipped to diff id:  %d (%.1f%%)",
            flipped_to_different,
            100 * flipped_to_different / denom,
        )
        logger.info("  no results:          %d (excluded from rates)", no_results)
        logger.info("CSV: %s", csv_path)
    finally:
        await service.close()
        await pool.close()
        await library_db.close()


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample", type=int, default=1000, help="Number of items to sample")
    parser.add_argument(
        "--csv",
        type=str,
        default="/tmp/lml-478-floor.csv",
        help="Output CSV path",
    )
    parser.add_argument("--seed", type=int, default=None, help="Random seed (optional)")
    args = parser.parse_args()
    if args.seed is not None:
        random.seed(args.seed)
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
