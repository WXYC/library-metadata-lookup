"""Search for streaming links using Kagi FastGPT API.

Searches for not-on-streaming albums across Spotify, Apple Music, Bandcamp,
Tidal, and Deezer. Also finds Discogs matches for albums missing them.

Usage:
    .venv/bin/python scripts/kagi_streaming_search.py [--dry-run] [--limit N] [--concurrency N]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re

import aiosqlite
import httpx
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

KAGI_API_KEY = os.environ["KAGI_API_KEY"]
KAGI_FASTGPT_URL = "https://kagi.com/api/v0/fastgpt"
SQLITE_PATH = "streaming_availability.db"

# Streaming service URL patterns
STREAMING_PATTERNS = {
    "spotify": re.compile(r"https?://open\.spotify\.com/(?:album|track)/\w+"),
    "apple_music": re.compile(r"https?://music\.apple\.com/.+/album/.+"),
    "bandcamp": re.compile(r"https?://[\w-]+\.bandcamp\.com/(?:album|track)/.+"),
    "tidal": re.compile(r"https?://(?:listen\.)?tidal\.com/(?:album|browse/album)/\d+"),
    "youtube_music": re.compile(r"https?://music\.youtube\.com/playlist\?list=.+"),
    "deezer": re.compile(r"https?://www\.deezer\.com/.+/album/\d+"),
    "soundcloud": re.compile(r"https?://soundcloud\.com/[\w-]+/[\w-]+"),
}

DISCOGS_PATTERN = re.compile(r"https?://www\.discogs\.com/(?:release|master)/\d+")


async def search_kagi(
    client: httpx.AsyncClient,
    artist: str,
    title: str,
    semaphore: asyncio.Semaphore,
) -> tuple[dict[str, str], float]:
    """Search for streaming links via Kagi FastGPT. Returns (links_dict, balance)."""
    query = (
        f'Is the album "{title}" by {artist} available on any streaming service '
        f"(Spotify, Apple Music, Bandcamp, Tidal, Deezer)? "
        f"List all streaming URLs you find. If not found on any, say NOT FOUND."
    )
    async with semaphore:
        await asyncio.sleep(2.0)  # stagger requests to avoid Kagi rate limits
        resp = await client.post(
            KAGI_FASTGPT_URL,
            headers={
                "Authorization": f"Bot {KAGI_API_KEY}",
                "Content-Type": "application/json",
            },
            json={"query": query},
            timeout=60.0,
        )
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", "5"))
            log.warning(f"Rate limited, waiting {retry_after}s")
            await asyncio.sleep(retry_after)
            return await search_kagi(client, artist, title, semaphore)
        resp.raise_for_status()
        data = resp.json()
        if data.get("error"):
            error_msg = data["error"][0].get("msg", "Unknown error")
            raise RuntimeError(f"Kagi API error: {error_msg}")

        balance = data["meta"].get("api_balance", 0.0)
        output = data["data"]["output"]
        references = data["data"].get("references", [])

        # Extract URLs from both the output text and references
        links: dict[str, str] = {}
        all_urls = re.findall(r"https?://[^\s\)>\]]+", output)
        for ref in references:
            if ref.get("url"):
                all_urls.append(ref["url"])

        for url in all_urls:
            url = url.rstrip(".,;")
            for service, pattern in STREAMING_PATTERNS.items():
                if service not in links and pattern.match(url):
                    links[service] = url
            if "discogs" not in links and DISCOGS_PATTERN.match(url):
                links["discogs"] = url

        return links, balance


async def main(args: argparse.Namespace) -> None:
    async with aiosqlite.connect(SQLITE_PATH) as db:
        db.row_factory = aiosqlite.Row
        rows = await db.execute_fetchall(
            "SELECT id, display_artist, display_title, discogs_release_id "
            "FROM albums "
            "WHERE is_compilation = 0 "
            "  AND spotify_status != 'found' "
            "  AND deezer_status != 'found' "
            "  AND apple_status != 'found' "
            "  AND kagi_checked_at IS NULL "
            "ORDER BY id"
        )

    albums = list(rows)
    if args.limit:
        albums = albums[: args.limit]

    log.info(f"Albums to search: {len(albums):,}")
    cost_estimate = len(albums) * 0.015
    log.info(
        f"Estimated cost: ~${cost_estimate:.2f} ({len(albums)} queries @ ~$0.015/query via FastGPT)"
    )

    if args.dry_run:
        log.info("Dry run — showing first 10 albums:")
        for a in albums[:10]:
            log.info(f"  [{a['display_artist']}] {a['display_title']}")
        return

    semaphore = asyncio.Semaphore(args.concurrency)
    found_streaming = 0
    found_discogs = 0
    total = len(albums)
    results_log: list[dict] = []
    balance = 0.0
    stop = False

    async def process_album(album: dict) -> dict | None:
        """Search a single album. Returns result dict or None."""
        artist = album["display_artist"]
        title = album["display_title"]
        clean_title = re.sub(r'\s+(?:\d{1,2}["""\u201c\u201d]|LP|EP|CD)$', "", title).strip()

        try:
            links, bal = await search_kagi(client, artist, clean_title, semaphore)
        except Exception as e:
            if "billing" in str(e).lower() or "balance" in str(e).lower():
                raise
            return None

        if links:
            return {
                "id": album["id"],
                "artist": artist,
                "title": title,
                "links": links,
                "has_discogs": album["discogs_release_id"] is not None,
                "balance": bal,
            }
        return None

    async with httpx.AsyncClient() as client:
        # Process in concurrent batches
        batch_size = args.concurrency
        for batch_start in range(0, total, batch_size):
            batch = albums[batch_start : batch_start + batch_size]
            tasks = [process_album(album) for album in batch]

            try:
                batch_results = await asyncio.gather(*tasks, return_exceptions=True)
            except Exception:
                break

            # Mark all processed albums with kagi_checked_at
            async with aiosqlite.connect(SQLITE_PATH) as db:
                for album in batch:
                    await db.execute(
                        "UPDATE albums SET kagi_checked_at = datetime('now') WHERE id = ?",
                        (album["id"],),
                    )
                await db.commit()

            for result in batch_results:
                if isinstance(result, Exception):
                    if "billing" in str(result).lower() or "balance" in str(result).lower():
                        log.error("Out of credits — stopping")
                        stop = True
                        break
                    continue
                if result is None:
                    continue

                results_log.append(result)
                balance = result["balance"]
                any_streaming = any(
                    k in result["links"]
                    for k in (
                        "spotify",
                        "apple_music",
                        "bandcamp",
                        "tidal",
                        "deezer",
                        "youtube_music",
                        "soundcloud",
                    )
                )
                if any_streaming:
                    found_streaming += 1
                if "discogs" in result["links"] and not result["has_discogs"]:
                    found_discogs += 1

            if stop:
                break

            done = min(batch_start + batch_size, total)
            if done % 50 < batch_size or done == total:
                log.info(
                    f"  {done:,}/{total:,} | streaming: {found_streaming} | "
                    f"discogs: {found_discogs} | balance: ${balance:.2f}"
                )

    # Save all results
    output_path = "kagi_search_results.json"
    with open(output_path, "w") as f:
        json.dump(results_log, f, indent=2)
    log.info(f"Saved {len(results_log)} results to {output_path}")

    # Update streaming_availability.db
    if results_log:
        async with aiosqlite.connect(SQLITE_PATH) as db:
            updated = 0
            for entry in results_log:
                links = entry["links"]
                sa_id = entry["id"]

                if "spotify" in links:
                    await db.execute(
                        "UPDATE albums SET spotify_status = 'found', spotify_url = ? "
                        "WHERE id = ? AND spotify_status != 'found'",
                        (links["spotify"], sa_id),
                    )
                if "apple_music" in links:
                    await db.execute(
                        "UPDATE albums SET apple_status = 'found', apple_url = ? "
                        "WHERE id = ? AND apple_status != 'found'",
                        (links["apple_music"], sa_id),
                    )
                if "deezer" in links:
                    await db.execute(
                        "UPDATE albums SET deezer_status = 'found', deezer_url = ? "
                        "WHERE id = ? AND deezer_status != 'found'",
                        (links["deezer"], sa_id),
                    )
                if "bandcamp" in links:
                    await db.execute(
                        "UPDATE albums SET bandcamp_url = ? WHERE id = ?",
                        (links["bandcamp"], sa_id),
                    )
                if "tidal" in links:
                    await db.execute(
                        "UPDATE albums SET tidal_url = ? WHERE id = ?",
                        (links["tidal"], sa_id),
                    )
                if "youtube_music" in links:
                    await db.execute(
                        "UPDATE albums SET youtube_music_url = ? WHERE id = ?",
                        (links["youtube_music"], sa_id),
                    )
                if "soundcloud" in links:
                    await db.execute(
                        "UPDATE albums SET soundcloud_url = ? WHERE id = ?",
                        (links["soundcloud"], sa_id),
                    )
                updated += 1
                if updated % 500 == 0:
                    await db.commit()
            await db.commit()
            log.info(f"Updated {updated} albums in database")

        # Report new totals
        async with aiosqlite.connect(SQLITE_PATH) as db:
            row = await db.execute_fetchall(
                "SELECT "
                "SUM(CASE WHEN spotify_status='found' OR deezer_status='found' OR apple_status='found' THEN 1 ELSE 0 END), "
                "SUM(CASE WHEN spotify_status!='found' AND deezer_status!='found' AND apple_status!='found' THEN 1 ELSE 0 END), "
                "COUNT(*) "
                "FROM albums WHERE is_compilation = 0"
            )
            on_streaming, not_found, total_albums = row[0]
            log.info(
                f"On streaming: {on_streaming:,} | Not found: {not_found:,} | Total: {total_albums:,}"
            )

    log.info(f"Found streaming: {found_streaming} | Found Discogs: {found_discogs}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Kagi streaming search")
    parser.add_argument("--dry-run", action="store_true", help="Show queries without searching")
    parser.add_argument("--limit", type=int, help="Limit to first N albums")
    parser.add_argument("--concurrency", type=int, default=3, help="Max concurrent requests")
    args = parser.parse_args()
    asyncio.run(main(args))
