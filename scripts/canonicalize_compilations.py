"""LLM canonicalization of unmatched VA compilation titles.

Sends unmatched compilation titles to Claude Haiku for canonicalization,
then matches the corrected titles against Discogs VA releases.

Usage:
    .venv/bin/python scripts/canonicalize_compilations.py [--dry-run] [--limit N] [--concurrency N]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re

import asyncpg
import httpx
from rapidfuzz import fuzz

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

DISCOGS_URL = "postgresql://jake@localhost/discogs"
API_KEY = os.environ["ANTHROPIC_API_KEY"]
MODEL = "claude-haiku-4-5-20251001"

SYSTEM_PROMPT = """You are a music metadata expert. Given a list of compilation album titles from a college radio station library catalog, return the canonical title as it would appear on Discogs or streaming services.

Common patterns to fix:
- Strip label/series name prefixes: "Sugar Hill - The Great Rap Hits" → "The Great Rap Hits"
- Strip format annotations: "Gangstarr Foundation Sampler 12\"" → "Gangstarr Foundation Sampler"
- Strip parenthetical annotations: "Jazz Actuel (3 cd box)" → "Jazz Actuel"
- Fix "Soundtrack" suffix: "Friday Soundtrack" → "Friday (Music From The Motion Picture)" or just "Friday"
- Fix volume formatting: "Vol." vs "Volume" vs "Vol"
- Remove radio station call letters: "WFMU- 5 cds" → skip (not a real album)
- Fix typos and misspellings
- If the title is clearly not a real album (just a catalog ID, radio station promo, etc.), return "SKIP"

Return ONLY a JSON array of objects with "original" and "canonical" keys. No explanation."""

USER_TEMPLATE = """Canonicalize these compilation album titles:

{titles_json}"""


async def call_haiku(client: httpx.AsyncClient, titles: list[str]) -> list[dict]:
    """Send a batch of titles to Claude Haiku for canonicalization."""
    titles_json = json.dumps(titles, indent=2)
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
                {"role": "user", "content": USER_TEMPLATE.format(titles_json=titles_json)}
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


async def main(args: argparse.Namespace) -> None:
    with open("compilation_unmatched.json") as f:
        unmatched = json.load(f)

    if args.limit:
        unmatched = unmatched[: args.limit]

    log.info(f"Loaded {len(unmatched):,} unmatched compilations")

    # Batch into groups of 40
    batch_size = 40
    batches = []
    for i in range(0, len(unmatched), batch_size):
        batch = unmatched[i : i + batch_size]
        batches.append(batch)

    log.info(f"Split into {len(batches)} batches of ~{batch_size}")

    # Process with concurrency
    semaphore = asyncio.Semaphore(args.concurrency)
    canonicalized: dict[str, str] = {}  # original → canonical
    total_cost = 0.0
    processed = 0
    skipped = 0

    async with httpx.AsyncClient() as client:

        async def process_batch(batch: list[dict]) -> None:
            nonlocal processed, skipped, total_cost
            titles = [d["title"] for d in batch]
            async with semaphore:
                try:
                    results = await call_haiku(client, titles)
                    for r in results:
                        orig = r.get("original", "")
                        canon = r.get("canonical", "")
                        if canon == "SKIP" or not canon:
                            skipped += 1
                        else:
                            canonicalized[orig] = canon
                except Exception as e:
                    log.warning(f"Batch failed: {e}")

                processed += len(batch)
                if processed % 200 == 0 or processed == len(unmatched):
                    log.info(
                        f"Processed {processed}/{len(unmatched)}, canonicalized {len(canonicalized)}, skipped {skipped}"
                    )

        tasks = [process_batch(batch) for batch in batches]
        await asyncio.gather(*tasks)

    log.info(f"Canonicalized {len(canonicalized):,} titles, skipped {skipped}")

    # Save canonicalized names
    canon_path = "compilation_canonicalized.json"
    with open(canon_path, "w") as f:
        json.dump(canonicalized, f, indent=2)
    log.info(f"Saved to {canon_path}")

    if args.dry_run:
        # Show samples
        for orig, canon in list(canonicalized.items())[:20]:
            changed = " *" if orig != canon else ""
            print(f"  {orig} => {canon}{changed}")
        return

    # Match canonicalized titles against Discogs
    conn = await asyncpg.connect(DISCOGS_URL)
    try:
        va_releases = await conn.fetch("SELECT id, title FROM va_release")
        title_map: dict[str, list[tuple[int, str]]] = {}
        for row in va_releases:
            title_map.setdefault(row["title"].lower(), []).append((row["id"], row["title"]))
        log.info(f"Loaded {len(va_releases):,} VA releases for matching")

        # Phase 1: Exact match on canonicalized titles
        exact_matched = 0
        all_matches = []
        matched_ids = set()

        for d in unmatched:
            canon = canonicalized.get(d["title"])
            if not canon:
                continue
            hits = title_map.get(canon.lower(), [])
            if hits:
                all_matches.append(
                    {
                        "comp_id": d["id"],
                        "original_title": d["title"],
                        "canonical_title": canon,
                        "discogs_release_id": hits[0][0],
                        "discogs_title": hits[0][1],
                        "confidence": 95.0,
                        "method": "llm_exact",
                    }
                )
                matched_ids.add(d["id"])
                exact_matched += 1

        log.info(f"LLM exact match: {exact_matched}")

        # Phase 2: Trigram match on canonicalized titles
        await conn.execute("SET pg_trgm.similarity_threshold = 0.3")
        trgm_matched = 0

        for d in unmatched:
            if d["id"] in matched_ids:
                continue
            canon = canonicalized.get(d["title"])
            if not canon:
                continue

            rows = await conn.fetch(
                """
                SELECT id, title, similarity(norm_title, lower($1)) as sim
                FROM va_release
                WHERE norm_title % lower($1)
                ORDER BY sim DESC LIMIT 5
                """,
                canon,
            )
            if rows:
                best = max(
                    rows, key=lambda r: fuzz.token_sort_ratio(canon.lower(), r["title"].lower())
                )
                score = fuzz.token_sort_ratio(canon.lower(), best["title"].lower())
                if score >= 80:
                    all_matches.append(
                        {
                            "comp_id": d["id"],
                            "original_title": d["title"],
                            "canonical_title": canon,
                            "discogs_release_id": best["id"],
                            "discogs_title": best["title"],
                            "confidence": score,
                            "method": "llm_trigram",
                        }
                    )
                    matched_ids.add(d["id"])
                    trgm_matched += 1

            if (exact_matched + trgm_matched) % 100 == 0:
                log.info(f"LLM trigram: {trgm_matched} (total {exact_matched + trgm_matched})")

        log.info(f"LLM trigram match: {trgm_matched}")
        log.info(f"Total LLM matches: {exact_matched + trgm_matched} / {len(unmatched)}")

        # Save matches
        output_path = "compilation_llm_matches.json"
        with open(output_path, "w") as f:
            json.dump(all_matches, f, indent=2)
        log.info(f"Saved {len(all_matches)} matches to {output_path}")

    finally:
        await conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="LLM canonicalization of compilation titles")
    parser.add_argument("--dry-run", action="store_true", help="Only canonicalize, don't match")
    parser.add_argument("--limit", type=int, help="Limit to first N titles")
    parser.add_argument("--concurrency", type=int, default=20, help="Max concurrent API calls")
    args = parser.parse_args()
    asyncio.run(main(args))
