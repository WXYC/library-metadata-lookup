"""Unified Bandcamp pipeline: discover slugs, then match albums.

Phase 1 (Search): Uses Bandcamp autocomplete API to discover artist slugs
    for artists missing Bandcamp data. Writes slug to albums.bandcamp_slug.

Phase 2 (Lookup): For artists with known slugs (from Phase 1 or Wikidata),
    fetches the artist's Bandcamp catalog and fuzzy-matches album titles.
    Writes album-level URLs to albums.bandcamp_url.

When both phases run together (the default), they operate concurrently:
search discoveries are passed to lookup via an asyncio.Queue so album
matching begins immediately as slugs are found.

Usage:
    python -m scripts.bandcamp_pipeline [--phase {search,lookup,both}]
        [--include-streaming] [--artist-fallback] [--dry-run] [--limit N]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from collections import defaultdict

from rapidfuzz import fuzz

from clients.bandcamp import BandcampClient, extract_slug
from clients.streaming.matching import score_match
from scripts.streaming_availability.results_db import ResultsDB

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

MATCH_THRESHOLD_ARTIST = 75
MATCH_THRESHOLD_ALBUM = 70


async def process_batched(
    items: list,
    process_fn,
    batch_size: int = 3,
) -> list:
    """Process items in batches using asyncio.gather.

    Exceptions in individual items are captured (not raised), so one failure
    doesn't abort the batch.
    """
    results: list = []
    for i in range(0, len(items), batch_size):
        batch = items[i : i + batch_size]
        batch_results = await asyncio.gather(
            *[process_fn(item) for item in batch],
            return_exceptions=True,
        )
        results.extend(batch_results)
    return results


async def migrate_existing_bandcamp_urls(db: ResultsDB) -> None:
    """One-time migration: extract slugs from existing bandcamp_url values.

    - Album-level URLs (/album/...): extract slug, keep URL intact.
    - Artist-level URLs: extract slug, clear URL so Phase 2 can do album matching.
    """
    assert db._db is not None

    # Step 1: Album-level URLs -- extract slug, keep URL
    cursor = await db._db.execute(
        "SELECT id, bandcamp_url FROM albums "
        "WHERE bandcamp_url LIKE '%/album/%' AND bandcamp_slug IS NULL"
    )
    album_level = list(await cursor.fetchall())
    for row in album_level:
        slug = extract_slug(row["bandcamp_url"])
        if slug:
            await db._db.execute(
                "UPDATE albums SET bandcamp_slug = ? WHERE id = ?",
                (slug, row["id"]),
            )

    # Step 2: Artist-level URLs -- extract slug, clear URL
    cursor = await db._db.execute(
        "SELECT id, bandcamp_url FROM albums "
        "WHERE bandcamp_url IS NOT NULL AND bandcamp_url NOT LIKE '%/album/%' "
        "AND bandcamp_slug IS NULL"
    )
    artist_level = list(await cursor.fetchall())
    for row in artist_level:
        slug = extract_slug(row["bandcamp_url"])
        if slug:
            await db._db.execute(
                "UPDATE albums SET bandcamp_slug = ?, bandcamp_url = NULL WHERE id = ?",
                (slug, row["id"]),
            )

    await db._db.commit()

    if album_level or artist_level:
        log.info(
            f"Migrated existing Bandcamp URLs: "
            f"{len(album_level)} album-level (kept), "
            f"{len(artist_level)} artist-level (cleared for re-matching)"
        )


async def phase_search(
    client: BandcampClient,
    db: ResultsDB,
    *,
    include_streaming: bool = False,
    limit: int | None = None,
    queue: asyncio.Queue | None = None,
) -> dict[str, str]:
    """Phase 1: Discover Bandcamp slugs via autocomplete search.

    Args:
        client: BandcampClient for API calls.
        db: ResultsDB instance.
        include_streaming: If True, search ALL artists (not just not-on-streaming).
        limit: Maximum number of artists to search.
        queue: Optional queue to push discovered slugs for concurrent lookup.

    Returns:
        Dict mapping display_artist -> discovered slug.
    """
    rows = await db.get_artists_without_bandcamp_slug(
        not_on_streaming_only=not include_streaming,
        limit=limit,
    )
    artists = [r["display_artist"] for r in rows]

    if not artists:
        log.info("No artists to search on Bandcamp")
        return {}

    log.info(f"Searching Bandcamp for {len(artists)} artists")

    discovered: dict[str, str] = {}
    searched = 0

    async def search_one(artist: str) -> tuple[str, str | None]:
        results = await client.search_artist(artist)
        for r in results:
            score = fuzz.token_sort_ratio(artist.lower(), r["name"].lower())
            if score >= MATCH_THRESHOLD_ARTIST:
                return (artist, r["slug"])
        return (artist, None)

    batch_results = await process_batched(artists, search_one, batch_size=3)

    for result in batch_results:
        if isinstance(result, Exception):
            log.warning(f"Search error: {result}")
            continue

        artist, slug = result
        searched += 1
        assert db._db is not None

        if slug:
            discovered[artist] = slug
            await db._db.execute(
                "UPDATE albums SET bandcamp_slug = ? "
                "WHERE display_artist = ? AND bandcamp_slug IS NULL AND is_compilation = 0",
                (slug, artist),
            )
            if queue is not None:
                await queue.put(slug)
        else:
            await db._db.execute(
                "UPDATE albums SET bandcamp_slug = '' "
                "WHERE display_artist = ? AND bandcamp_slug IS NULL AND is_compilation = 0",
                (artist,),
            )
        await db._db.commit()

        if searched % 100 == 0:
            log.info(f"  {searched}/{len(artists)} searched, {len(discovered)} found")

    log.info(f"Search complete: {len(discovered)} slugs found from {searched} artists")
    return discovered


async def phase_lookup(
    client: BandcampClient,
    db: ResultsDB,
    *,
    artist_fallback: bool = False,
    limit: int | None = None,
    slug: str | None = None,
) -> list[dict]:
    """Phase 2: Album-level matching using known slugs.

    Args:
        client: BandcampClient for page scraping.
        db: ResultsDB instance.
        artist_fallback: If True, write artist-level URL when no album match.
        limit: Maximum number of distinct slugs to process.
        slug: If provided, only process albums with this exact slug. The
            concurrent consumer passes the freshly discovered slug so a queue
            event fetches one catalog instead of re-scanning every pending
            catalog (#125).

    Returns:
        List of match result dicts for bandcamp_matches.json.
    """
    rows = await db.get_pending_bandcamp_lookup(slug=slug)

    if not rows:
        log.info("No albums pending Bandcamp lookup")
        return []

    # Group by slug to avoid duplicate fetches
    slug_albums: dict[str, list] = defaultdict(list)
    for row in rows:
        slug_albums[row["bandcamp_slug"]].append(row)

    slugs_to_process = list(slug_albums.keys())
    if limit is not None:
        slugs_to_process = slugs_to_process[:limit]

    log.info(
        f"Looking up {len(slugs_to_process)} Bandcamp catalogs "
        f"({sum(len(slug_albums[s]) for s in slugs_to_process)} albums)"
    )

    results: list[dict] = []
    processed = 0

    for slug in slugs_to_process:
        catalog = await client.fetch_artist_catalog(slug)
        albums = slug_albums[slug]

        if catalog:
            for album_row in albums:
                title = album_row["display_title"]
                best_score = 0.0
                best_url = None

                for bc in catalog:
                    s = score_match(title, bc["title"])
                    if s > best_score:
                        best_score = s
                        best_url = bc["url"]

                if best_score >= MATCH_THRESHOLD_ALBUM and best_url:
                    await db.update_bandcamp_url(album_row["id"], best_url)
                    results.append(
                        {
                            "id": album_row["id"],
                            "artist": album_row["display_artist"],
                            "title": title,
                            "bandcamp_url": best_url,
                            "score": best_score,
                        }
                    )
                elif artist_fallback:
                    fallback_url = f"https://{slug}.bandcamp.com"
                    await db.update_bandcamp_url(album_row["id"], fallback_url)
        elif artist_fallback:
            for album_row in albums:
                fallback_url = f"https://{slug}.bandcamp.com"
                await db.update_bandcamp_url(album_row["id"], fallback_url)

        processed += 1
        if processed % 100 == 0:
            log.info(f"  {processed}/{len(slugs_to_process)} catalogs, {len(results)} matches")

    log.info(f"Lookup complete: {len(results)} album matches from {processed} catalogs")
    return results


async def run_concurrent(
    client: BandcampClient,
    db: ResultsDB,
    *,
    include_streaming: bool = False,
    artist_fallback: bool = False,
    limit: int | None = None,
) -> list[dict]:
    """Run search and lookup concurrently via producer-consumer queue.

    Search discovers slugs and pushes them to a queue.
    Lookup consumes slugs from the queue and does album-level matching.
    Pre-existing slugs (from Wikidata or previous runs) are also processed.
    """
    queue: asyncio.Queue[str | None] = asyncio.Queue()
    all_results: list[dict] = []

    async def search_producer():
        try:
            await phase_search(
                client, db, include_streaming=include_streaming, limit=limit, queue=queue
            )
        except Exception:
            log.exception("Search phase error")
        finally:
            await queue.put(None)  # sentinel to signal done

    async def lookup_consumer():
        # First, process any pre-existing slugs
        existing_results = await phase_lookup(client, db, artist_fallback=artist_fallback)
        all_results.extend(existing_results)

        # Then consume newly discovered slugs from the queue
        while True:
            slug = await queue.get()
            if slug is None:
                break

            # Process only the newly discovered slug's albums (#125): without
            # the slug filter, phase_lookup re-scanned the entire pending set
            # on every queue event, re-scraping every still-pending catalog.
            new_results = await phase_lookup(client, db, artist_fallback=artist_fallback, slug=slug)
            all_results.extend(new_results)

    await asyncio.gather(search_producer(), lookup_consumer())
    return all_results


async def load_wikidata_slugs(db: ResultsDB) -> int:
    """Load Bandcamp slugs from Wikidata and write to bandcamp_slug column.

    Requires DATABASE_URL_WIKIDATA environment variable.
    Returns count of albums updated.
    """
    wikidata_url = os.environ.get("DATABASE_URL_WIKIDATA")
    if not wikidata_url:
        log.warning("DATABASE_URL_WIKIDATA not set, skipping Wikidata slug loading")
        return 0

    import asyncpg

    conn = await asyncpg.connect(wikidata_url)
    try:
        rows = await conn.fetch("""
            SELECT e.label as artist_name, bc.discogs_id as bandcamp_slug
            FROM discogs_mapping bc
            JOIN entity e ON e.qid = bc.qid
            WHERE bc.property = 'P3283'
        """)
    finally:
        await conn.close()

    slug_map = {r["artist_name"].lower(): r["bandcamp_slug"] for r in rows}
    log.info(f"Loaded {len(slug_map)} Bandcamp slugs from Wikidata")

    assert db._db is not None
    updated = 0
    for artist_lower, slug in slug_map.items():
        cursor = await db._db.execute(
            "UPDATE albums SET bandcamp_slug = ? "
            "WHERE LOWER(display_artist) = ? AND bandcamp_slug IS NULL AND is_compilation = 0",
            (slug, artist_lower),
        )
        updated += cursor.rowcount
    await db._db.commit()

    log.info(f"Applied {updated} Wikidata slugs to albums")
    return updated


async def dry_run(db: ResultsDB, *, include_streaming: bool = False) -> None:
    """Report what the pipeline would do without making any changes."""
    assert db._db is not None

    # Count albums with existing bandcamp_url
    cursor = await db._db.execute(
        "SELECT COUNT(*) FROM albums WHERE bandcamp_url LIKE '%/album/%' AND bandcamp_slug IS NULL"
    )
    row = await cursor.fetchone()
    album_level = row[0] if row else 0

    cursor = await db._db.execute(
        "SELECT COUNT(*) FROM albums "
        "WHERE bandcamp_url IS NOT NULL AND bandcamp_url NOT LIKE '%/album/%' "
        "AND bandcamp_slug IS NULL"
    )
    row = await cursor.fetchone()
    artist_level = row[0] if row else 0

    if album_level or artist_level:
        log.info(
            f"Migration would process: "
            f"{album_level} album-level URLs (keep), "
            f"{artist_level} artist-level URLs (clear for re-matching)"
        )

    # Count artists to search
    rows = await db.get_artists_without_bandcamp_slug(
        not_on_streaming_only=not include_streaming,
    )
    log.info(f"Search phase would query {len(rows)} artists")
    for r in rows[:10]:
        log.info(f"  {r['display_artist']}")
    if len(rows) > 10:
        log.info(f"  ... and {len(rows) - 10} more")

    # Count albums pending lookup
    pending = await db.get_pending_bandcamp_lookup()
    if pending:
        slugs = {r["bandcamp_slug"] for r in pending}
        log.info(f"Lookup phase would process {len(pending)} albums across {len(slugs)} slugs")
    else:
        log.info("No albums currently pending Bandcamp lookup")


async def main(args: argparse.Namespace) -> None:
    db = ResultsDB(args.db_path)
    await db.connect()

    try:
        if args.dry_run:
            await dry_run(db, include_streaming=args.include_streaming)
            return

        # One-time migration of existing bandcamp_url values
        await migrate_existing_bandcamp_urls(db)

        # Load Wikidata slugs if available
        await load_wikidata_slugs(db)

        client = BandcampClient()
        try:
            if args.phase == "both":
                results = await run_concurrent(
                    client,
                    db,
                    include_streaming=args.include_streaming,
                    artist_fallback=args.artist_fallback,
                    limit=args.limit,
                )
            elif args.phase == "search":
                slugs = await phase_search(
                    client,
                    db,
                    include_streaming=args.include_streaming,
                    limit=args.limit,
                )
                with open("bandcamp_slugs.json", "w") as f:
                    json.dump(slugs, f, indent=2)
                log.info(f"Saved {len(slugs)} slugs to bandcamp_slugs.json")
                return
            elif args.phase == "lookup":
                results = await phase_lookup(
                    client,
                    db,
                    artist_fallback=args.artist_fallback,
                    limit=args.limit,
                )
        finally:
            await client.close()

        if results:
            with open("bandcamp_matches.json", "w") as f:
                json.dump(results, f, indent=2)
            log.info(f"Saved {len(results)} matches to bandcamp_matches.json")
    finally:
        await db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Unified Bandcamp pipeline: discover slugs + match albums"
    )
    parser.add_argument(
        "--phase",
        choices=["search", "lookup", "both"],
        default="both",
        help="Which phase to run (default: both)",
    )
    parser.add_argument(
        "--include-streaming",
        action="store_true",
        help="Search ALL artists, not just not-on-streaming",
    )
    parser.add_argument(
        "--artist-fallback",
        action="store_true",
        help="Write artist-level URL when no album match found",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, help="Max artists/slugs to process")
    parser.add_argument(
        "--db-path",
        default="streaming_availability.db",
        help="Path to streaming_availability.db",
    )
    args = parser.parse_args()
    asyncio.run(main(args))
