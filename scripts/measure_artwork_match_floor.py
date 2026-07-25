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

Known divergences from the runtime path (these are why the measurement is
illustrative, not exhaustive):

- **``discogs_titles`` compilation-rescue overrides**. The runtime track-on-
  compilation strategy populates ``discogs_titles[item.id]`` with the long
  Discogs-canonical title before calling ``fetch_artwork_for_items``. That
  override is populated mid-request from search-pipeline state the script
  doesn't have access to — so for those rows, the script queries Discogs
  with the short library title and scores against a single album variant,
  whereas the runtime queries with the long override and scores against
  two album variants. The script's flip-rate therefore under-samples this
  cohort; spot-check compilation rows from the runtime side post-merge.

- **Reproducibility / sampling determinism**. Sampling uses SQLite's
  ``ORDER BY RANDOM()`` which has no externally-seedable PRNG. The CSV
  writes ``library_id`` per row, so spot-checks can be re-run against the
  same IDs by reading them out of an existing CSV; consecutive script
  invocations otherwise sample fresh rows each time.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import logging
import os
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import asyncpg
from dotenv import load_dotenv
from wxyc_etl.text import is_compilation_artist

from clients.streaming.matching import find_best_typed_match, score_match
from discogs.cache_service import DiscogsCacheService
from discogs.models import DiscogsSearchRequest, DiscogsSearchResult
from discogs.service import DiscogsService
from library.db import LibraryDB
from lookup.artwork import (
    COMPILATION_ARTIST_CANONICAL_FORM,
    COMPILATION_ARTIST_SEARCH_FORM,
)
from lookup.matching import is_self_titled, map_library_format_to_discogs

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SampledItem:
    """A library row, in the shape the artwork-search path consumes."""

    library_id: int
    artist: str
    title: str
    alternate_artist_name: str | None
    label: str | None
    format: str | None


@dataclass(frozen=True)
class QueryShape:
    """The exact query shape fetch_one sends to Discogs + scores against."""

    search_artist: str
    search_album: str
    artist_variants: list[str]
    title_variants: list[str]


def build_query(item: SampledItem) -> QueryShape:
    """Mirror fetch_one's query construction — the search-query mutations AND
    the score-variant lists. Must stay byte-for-byte aligned with
    ``lookup.artwork.fetch_artwork_for_items.fetch_one``; any drift makes
    the measurement compare apples to oranges.
    """
    album = item.title

    if is_self_titled(album or ""):
        album = item.artist

    artist = item.alternate_artist_name or item.artist or ""
    if is_compilation_artist(artist):
        artist = COMPILATION_ARTIST_SEARCH_FORM

    artist_variants = [artist]
    if artist == COMPILATION_ARTIST_SEARCH_FORM:
        artist_variants.append(COMPILATION_ARTIST_CANONICAL_FORM)
    album_variants = [album or ""]
    if item.title and item.title != album and not is_self_titled(item.title):
        album_variants.append(item.title)

    return QueryShape(
        search_artist=artist,
        search_album=album or "",
        artist_variants=artist_variants,
        title_variants=album_variants,
    )


async def sample_library_items(db: LibraryDB, n: int) -> list[SampledItem]:
    """Return ``n`` random library rows, including alternate_artist_name and
    label when the underlying schema carries them (mirrors ``LibraryDB``'s
    own column-introspection done in ``connect()``).
    """
    assert db._conn is not None
    cols = ["id", "artist", "title", "format"]
    if db._has_alternate_artist:
        cols.append("alternate_artist_name")
    if db._has_label:
        cols.append("label")
    cursor = await db._conn.execute(
        f"SELECT {', '.join(cols)} FROM library ORDER BY RANDOM() LIMIT ?", (n,)
    )
    rows = await cursor.fetchall()
    out: list[SampledItem] = []
    for row in rows:
        out.append(
            SampledItem(
                library_id=row["id"],
                artist=row["artist"] or "",
                title=row["title"] or "",
                alternate_artist_name=(
                    row["alternate_artist_name"] if db._has_alternate_artist else None
                ),
                label=row["label"] if db._has_label else None,
                format=row["format"],
            )
        )
    return out


def classify(
    results: Iterable[DiscogsSearchResult], query: QueryShape
) -> tuple[DiscogsSearchResult | None, DiscogsSearchResult | None, float, float, float]:
    """Return (old_pick, new_pick, top1_artist_score, top1_title_score, best_combined).

    Scores are taken against the same variant lists fetch_one uses, so the
    top1 score reflects the floor's actual verdict, not a degenerate query.
    """
    results = list(results)
    old_pick = results[0] if results else None

    top1_artist_score = 0.0
    top1_title_score = 0.0
    if old_pick is not None:
        top1_artist_score = max(
            score_match(q, old_pick.artist or "") for q in query.artist_variants
        )
        top1_title_score = max(score_match(q, old_pick.album or "") for q in query.title_variants)

    new_pick = find_best_typed_match(
        results,
        query_artist=query.artist_variants,
        query_title=query.title_variants,
        artist_fn=lambda r: r.artist,
        title_fn=lambda r: r.album,
    )

    best_combined = 0.0
    if new_pick is not None:
        a = max(score_match(q, new_pick.artist or "") for q in query.artist_variants)
        t = max(score_match(q, new_pick.album or "") for q in query.title_variants)
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
    search_failed = 0

    try:
        items = await sample_library_items(library_db, args.sample)
        logger.info("Sampled %d library items", len(items))

        with csv_path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(
                [
                    "library_id",
                    "artist_raw",
                    "title_raw",
                    "alternate_artist_name",
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

            for i, item in enumerate(items, start=1):
                query = build_query(item)
                try:
                    response = await service.search(
                        DiscogsSearchRequest(
                            album=query.search_album,
                            artist=query.search_artist,
                            label=item.label,
                            format=map_library_format_to_discogs(item.format),
                        )
                    )
                except Exception as e:
                    logger.warning("search failed for id=%s: %s", item.library_id, e)
                    search_failed += 1
                    writer.writerow(
                        [
                            item.library_id,
                            item.artist,
                            item.title,
                            item.alternate_artist_name or "",
                            query.search_artist,
                            query.search_album,
                            "",
                            "",
                            "search_failed",
                            "",
                            "",
                            "",
                        ]
                    )
                    continue

                # None = degraded Discogs call (LML#918); classify an empty set.
                results = response.results if response is not None else []
                old_pick, new_pick, a_score, t_score, best = classify(results, query)

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
                        item.library_id,
                        item.artist,
                        item.title,
                        item.alternate_artist_name or "",
                        query.search_artist,
                        query.search_album,
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
        logger.info("  search failed:       %d (excluded from rates)", search_failed)
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
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
