"""Search Bandcamp for artists not on any streaming service.

Uses Bandcamp's autocomplete API to find artist pages for albums
that have no streaming links and no Bandcamp URL from Wikidata.

Usage:
    .venv/bin/python scripts/bandcamp_search.py [--dry-run] [--limit N]
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sqlite3

import httpx
from rapidfuzz import fuzz

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

SQLITE_PATH = "streaming_availability.db"
BANDCAMP_API = "https://bandcamp.com/api/fuzzysearch/2/app_autocomplete"


async def search_bandcamp(
    client: httpx.AsyncClient,
    artist: str,
    semaphore: asyncio.Semaphore,
) -> str | None:
    """Search Bandcamp for an artist. Returns artist page URL or None."""
    async with semaphore:
        await asyncio.sleep(0.5)  # polite rate limiting
        try:
            resp = await client.get(
                BANDCAMP_API,
                params={"q": artist, "param": "b"},
                timeout=10.0,
            )
            if resp.status_code != 200:
                return None

            data = resp.json()
            for result in data.get("results", []):
                if result.get("type") != "b":  # band results only
                    continue
                bc_name = result.get("name", "")
                score = fuzz.token_sort_ratio(artist.lower(), bc_name.lower())
                if score >= 75:
                    url = result.get("url", "")
                    if url and "bandcamp.com" in url:
                        return url
            return None
        except Exception:
            return None


async def main(args: argparse.Namespace) -> None:
    conn = sqlite3.connect(SQLITE_PATH)

    # Get not-on-streaming artists without Bandcamp URLs
    rows = conn.execute("""
        SELECT DISTINCT display_artist FROM albums
        WHERE is_compilation = 0
          AND spotify_status != 'found' AND deezer_status != 'found' AND apple_status != 'found'
          AND bandcamp_url IS NULL
    """).fetchall()
    conn.close()

    artists = [r[0] for r in rows]
    if args.limit:
        artists = artists[: args.limit]

    log.info(f"Artists to search on Bandcamp: {len(artists)}")

    if args.dry_run:
        for a in artists[:10]:
            log.info(f"  {a}")
        return

    semaphore = asyncio.Semaphore(2)
    found = 0
    results: dict[str, str] = {}  # artist → bandcamp_url

    async with httpx.AsyncClient() as client:
        for i, artist in enumerate(artists):
            url = await search_bandcamp(client, artist, semaphore)
            if url:
                found += 1
                results[artist] = url

            if (i + 1) % 200 == 0 or i + 1 == len(artists):
                log.info(f"  {i + 1}/{len(artists)} searched, {found} found")

    log.info(f"Total found: {found} / {len(artists)}")

    if results:
        conn = sqlite3.connect(SQLITE_PATH)
        updated = 0
        for artist, url in results.items():
            conn.execute(
                "UPDATE albums SET bandcamp_url = ? WHERE display_artist = ? AND bandcamp_url IS NULL AND is_compilation = 0",
                (url, artist),
            )
            updated += conn.total_changes
        conn.commit()
        total_bc = conn.execute(
            "SELECT COUNT(*) FROM albums WHERE bandcamp_url IS NOT NULL"
        ).fetchone()[0]
        conn.close()
        log.info(f"Updated albums for {len(results)} artists. Total with Bandcamp: {total_bc}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Search Bandcamp for not-on-streaming artists")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    asyncio.run(main(args))
