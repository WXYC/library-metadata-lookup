"""LLM canonicalization of unmatched non-compilation album titles.

Sends artist+title pairs for albums without a Discogs match to Claude Haiku
for canonicalization, then matches the corrected names against the Discogs
PostgreSQL database using exact and trigram fuzzy matching.

Usage:
    .venv/bin/python scripts/canonicalize_albums.py [--dry-run] [--limit N] [--concurrency N]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import time

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
API_KEY = os.environ["ANTHROPIC_API_KEY"]
MODEL = "claude-haiku-4-5-20251001"

SYSTEM_PROMPT = """You are a music metadata expert. Given a list of artist + album title pairs from a college radio station library catalog, return the canonical artist name and album title as they would appear on Discogs or streaming services.

Common patterns to fix:
- Fix artist name spelling/formatting: "Bjork" → "Björk", "MF Doom" → "MF DOOM"
- Fix "Last, First" to "First Last": "Coltrane, John" → "John Coltrane"
- Strip format annotations from titles: "Gangstarr Foundation Sampler 12\"" → "Gangstarr Foundation Sampler"
- Strip parenthetical annotations: "Jazz Actuel (3 cd box)" → "Jazz Actuel"
- Fix volume formatting: "Vol." vs "Volume" vs "Vol"
- Fix typos and misspellings in both artist and title
- Normalize "self-titled" albums to use the artist name as the title
- Strip label names prepended to titles: "Warp - Artificial Intelligence" → "Artificial Intelligence"
- If the entry is clearly not a real album (just a catalog ID, radio station promo, test pressing, etc.), return "SKIP" for both artist and title

Return ONLY a JSON array of objects with "original_artist", "original_title", "canonical_artist", "canonical_title" keys. No explanation."""

USER_TEMPLATE = """Canonicalize these album entries:

{entries_json}"""


async def call_haiku(
    client: object,
    entries: list[dict[str, str]],
    semaphore: asyncio.Semaphore,
) -> list[dict]:
    """Send a batch of artist+title pairs to Claude Haiku for canonicalization."""
    import httpx

    assert isinstance(client, httpx.AsyncClient)

    entries_json = json.dumps(entries, indent=2)
    async with semaphore:
        for attempt in range(3):
            try:
                resp = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key": API_KEY,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json={
                        "model": MODEL,
                        "max_tokens": 4096,
                        "system": SYSTEM_PROMPT,
                        "messages": [
                            {
                                "role": "user",
                                "content": USER_TEMPLATE.format(entries_json=entries_json),
                            }
                        ],
                    },
                    timeout=60.0,
                )
                resp.raise_for_status()
                data = resp.json()
                text = data["content"][0]["text"]

                # Parse JSON from response (handle markdown code blocks)
                text = text.strip()
                if text.startswith("```"):
                    text = re.sub(r"^```(?:json)?\n?", "", text)
                    text = re.sub(r"\n?```$", "", text)

                return json.loads(text)
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 529 and attempt < 2:
                    wait = 2 ** (attempt + 1)
                    log.warning(f"API overloaded, retrying in {wait}s...")
                    await asyncio.sleep(wait)
                    continue
                raise
            except Exception:
                if attempt < 2:
                    await asyncio.sleep(2)
                    continue
                raise
    return []


def strip_discogs_suffix(name: str) -> str:
    """Remove Discogs disambiguation suffixes like (2), (22) from names."""
    return re.sub(r"\s+\(\d+\)$", "", name)


async def main(args: argparse.Namespace) -> None:
    t0 = time.monotonic()

    # Load unmatched albums from SQLite
    async with aiosqlite.connect(SQLITE_PATH) as db:
        db.row_factory = aiosqlite.Row
        rows = await db.execute_fetchall(
            "SELECT id, display_artist, display_title "
            "FROM albums WHERE is_compilation = 0 AND discogs_release_id IS NULL"
        )
    albums = [
        {"id": r["id"], "artist": r["display_artist"], "title": r["display_title"]} for r in rows
    ]

    if args.limit:
        albums = albums[: args.limit]

    log.info(f"Loaded {len(albums):,} unmatched non-compilation albums")

    # --- Phase 1: LLM Canonicalization ---

    batch_size = 40
    batches: list[list[dict]] = []
    for i in range(0, len(albums), batch_size):
        batches.append(albums[i : i + batch_size])

    log.info(f"Split into {len(batches)} batches of ~{batch_size} for LLM canonicalization")

    semaphore = asyncio.Semaphore(args.concurrency)
    # Map (original_artist, original_title) -> (canonical_artist, canonical_title)
    canonicalized: dict[tuple[str, str], tuple[str, str]] = {}
    skipped = 0
    errors = 0
    processed = 0

    import httpx

    async with httpx.AsyncClient() as client:

        async def process_batch(batch: list[dict]) -> None:
            nonlocal processed, skipped, errors
            entries = [{"artist": d["artist"], "title": d["title"]} for d in batch]
            try:
                results = await call_haiku(client, entries, semaphore)
                for r in results:
                    orig_artist = r.get("original_artist", "")
                    orig_title = r.get("original_title", "")
                    canon_artist = r.get("canonical_artist", "")
                    canon_title = r.get("canonical_title", "")
                    if (
                        canon_artist == "SKIP"
                        or canon_title == "SKIP"
                        or not canon_artist
                        or not canon_title
                    ):
                        skipped += 1
                    else:
                        canonicalized[(orig_artist, orig_title)] = (canon_artist, canon_title)
            except Exception as e:
                log.warning(f"Batch failed: {e}")
                errors += len(batch)

            processed += len(batch)
            if processed % 200 == 0 or processed == len(albums):
                log.info(
                    f"LLM progress: {processed:,}/{len(albums):,} processed, "
                    f"{len(canonicalized):,} canonicalized, {skipped} skipped, {errors} errors"
                )

        tasks = [process_batch(batch) for batch in batches]
        await asyncio.gather(*tasks)

    elapsed_llm = time.monotonic() - t0
    log.info(
        f"LLM canonicalization complete in {elapsed_llm:.1f}s: "
        f"{len(canonicalized):,} canonicalized, {skipped} skipped, {errors} errors"
    )

    # Save canonicalized names
    canon_path = "canonicalized_album_names.json"
    canon_data = [
        {
            "original_artist": k[0],
            "original_title": k[1],
            "canonical_artist": v[0],
            "canonical_title": v[1],
        }
        for k, v in canonicalized.items()
    ]
    with open(canon_path, "w") as f:
        json.dump(canon_data, f, indent=2)
    log.info(f"Saved {len(canon_data):,} canonicalized names to {canon_path}")

    if args.dry_run:
        # Show samples of changes
        changed = [(k, v) for k, v in canonicalized.items() if k[0] != v[0] or k[1] != v[1]]
        log.info(f"Changed entries: {len(changed):,} / {len(canonicalized):,}")
        for (orig_a, orig_t), (canon_a, canon_t) in changed[:20]:
            parts = []
            if orig_a != canon_a:
                parts.append(f"artist: {orig_a!r} => {canon_a!r}")
            if orig_t != canon_t:
                parts.append(f"title: {orig_t!r} => {canon_t!r}")
            print(f"  {', '.join(parts)}")
        return

    # --- Phase 2: Match canonicalized names against Discogs PG ---

    t1 = time.monotonic()
    conn = await asyncpg.connect(DISCOGS_URL)
    try:
        all_matches: list[dict] = []
        matched_ids: set[int] = set()

        # Build lookup from album id -> canonical names
        album_canon: dict[
            int, tuple[str, str, str, str]
        ] = {}  # id -> (orig_artist, orig_title, canon_artist, canon_title)
        for album in albums:
            key = (album["artist"], album["title"])
            if key in canonicalized:
                ca, ct = canonicalized[key]
                album_canon[album["id"]] = (album["artist"], album["title"], ca, ct)

        log.info(f"Albums with canonicalized names for matching: {len(album_canon):,}")

        # Phase 2a: Exact title match using btree index on lower(title)
        exact_count = 0
        total = len(album_canon)
        for i, (album_id, (orig_artist, orig_title, canon_artist, canon_title)) in enumerate(
            album_canon.items()
        ):
            hits = await conn.fetch(
                """
                SELECT release_id, title, discogs_artist as artist_name
                FROM wxyc_release_match
                WHERE norm_title = lower($1)
                """,
                canon_title,
            )
            if hits:
                # Score artist match using rapidfuzz
                best = max(
                    hits,
                    key=lambda h: fuzz.token_sort_ratio(
                        canon_artist.lower(),
                        strip_discogs_suffix(h["artist_name"]).lower(),
                    ),
                )
                artist_score = fuzz.token_sort_ratio(
                    canon_artist.lower(),
                    strip_discogs_suffix(best["artist_name"]).lower(),
                )
                title_score = fuzz.token_sort_ratio(
                    canon_title.lower(),
                    best["title"].lower(),
                )
                if artist_score >= 70 and title_score >= 80:
                    all_matches.append(
                        {
                            "album_id": album_id,
                            "original_artist": orig_artist,
                            "original_title": orig_title,
                            "canonical_artist": canon_artist,
                            "canonical_title": canon_title,
                            "discogs_release_id": best["release_id"],
                            "discogs_artist": best["artist_name"],
                            "discogs_title": best["title"],
                            "artist_score": artist_score,
                            "title_score": title_score,
                            "method": "llm_exact",
                        }
                    )
                    matched_ids.add(album_id)
                    exact_count += 1

            if (i + 1) % 2000 == 0 or i + 1 == total:
                log.info(f"Exact match: {i + 1:,}/{total:,} processed, {exact_count:,} matched")

        log.info(f"Phase 2a (exact): {exact_count:,} matches")

        # Phase 2b: Trigram fuzzy match for remaining
        await conn.execute("SET pg_trgm.similarity_threshold = 0.3")
        trgm_count = 0
        remaining = {aid: v for aid, v in album_canon.items() if aid not in matched_ids}
        total_remaining = len(remaining)

        for i, (album_id, (orig_artist, orig_title, canon_artist, canon_title)) in enumerate(
            remaining.items()
        ):
            rows = await conn.fetch(
                """
                SELECT release_id, title, discogs_artist as artist_name,
                       similarity(norm_title, lower($1)) as sim
                FROM wxyc_release_match
                WHERE norm_title % lower($1)
                ORDER BY sim DESC
                LIMIT 10
                """,
                canon_title,
            )

            if rows:
                # Score combined artist + title match
                best = None
                best_combined = 0.0
                for row in rows:
                    artist_score = fuzz.token_sort_ratio(
                        canon_artist.lower(),
                        strip_discogs_suffix(row["artist_name"]).lower(),
                    )
                    title_score = fuzz.token_sort_ratio(
                        canon_title.lower(),
                        row["title"].lower(),
                    )
                    combined = (artist_score + title_score) / 2
                    if artist_score >= 70 and title_score >= 80 and combined > best_combined:
                        best_combined = combined
                        best = row
                        best_artist_score = artist_score
                        best_title_score = title_score

                if best is not None:
                    all_matches.append(
                        {
                            "album_id": album_id,
                            "original_artist": orig_artist,
                            "original_title": orig_title,
                            "canonical_artist": canon_artist,
                            "canonical_title": canon_title,
                            "discogs_release_id": best["release_id"],
                            "discogs_artist": best["artist_name"],
                            "discogs_title": best["title"],
                            "artist_score": best_artist_score,
                            "title_score": best_title_score,
                            "method": "llm_trigram",
                        }
                    )
                    matched_ids.add(album_id)
                    trgm_count += 1

            if (i + 1) % 2000 == 0 or i + 1 == total_remaining:
                log.info(
                    f"Trigram match: {i + 1:,}/{total_remaining:,} processed, {trgm_count:,} matched"
                )

        elapsed_pg = time.monotonic() - t1
        log.info(f"Phase 2b (trigram): {trgm_count:,} matches in {elapsed_pg:.1f}s")
        log.info(
            f"Total LLM-assisted matches: {len(all_matches):,} / {len(albums):,} "
            f"({len(all_matches) / len(albums) * 100:.1f}%)"
        )

        # Save matches
        matches_path = "album_llm_matches.json"
        with open(matches_path, "w") as f:
            json.dump(all_matches, f, indent=2)
        log.info(f"Saved {len(all_matches):,} matches to {matches_path}")

        # Update streaming_availability.db
        async with aiosqlite.connect(SQLITE_PATH) as db:
            updated = 0
            for match in all_matches:
                await db.execute(
                    "UPDATE albums SET discogs_release_id = ?, discogs_artist = ?, "
                    "discogs_title = ?, discogs_status = 'found' "
                    "WHERE id = ? AND discogs_release_id IS NULL",
                    (
                        match["discogs_release_id"],
                        match["discogs_artist"],
                        match["discogs_title"],
                        match["album_id"],
                    ),
                )
                updated += 1
                if updated % 1000 == 0:
                    await db.commit()
                    log.info(f"  SQLite updated {updated:,}/{len(all_matches):,}")
            await db.commit()
            log.info(f"Updated {updated:,} albums in streaming_availability.db")

        # Report new totals
        async with aiosqlite.connect(SQLITE_PATH) as db:
            rows = await db.execute_fetchall(
                "SELECT "
                "SUM(CASE WHEN discogs_release_id IS NOT NULL THEN 1 ELSE 0 END), "
                "SUM(CASE WHEN discogs_release_id IS NULL THEN 1 ELSE 0 END), "
                "COUNT(*) "
                "FROM albums WHERE is_compilation = 0"
            )
            has_discogs, no_discogs, total_albums = rows[0]
            log.info(
                f"Discogs coverage: {has_discogs:,}/{total_albums:,} "
                f"({has_discogs / total_albums * 100:.1f}%) -- "
                f"{no_discogs:,} still unmatched"
            )

    finally:
        await conn.close()

    elapsed_total = time.monotonic() - t0
    log.info(f"Total elapsed: {elapsed_total:.1f}s")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="LLM canonicalization and Discogs matching for unmatched albums"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Only canonicalize, don't match or update"
    )
    parser.add_argument("--limit", type=int, help="Limit to first N albums")
    parser.add_argument("--concurrency", type=int, default=20, help="Max concurrent LLM API calls")
    args = parser.parse_args()
    asyncio.run(main(args))
