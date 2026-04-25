"""Revalidate Spotify URLs using the Spotify Web API.

Checks all Spotify URLs in streaming_availability.db by fetching album/track
metadata directly from the Spotify API and comparing against expected titles.
Removes false positives where the URL points to the wrong album.

Usage:
    .venv/bin/python scripts/revalidate_spotify.py [--dry-run] [--limit N]
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import os
import re

import aiosqlite
import httpx
from dotenv import load_dotenv
from rapidfuzz import fuzz

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

SQLITE_PATH = "streaming_availability.db"
SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_API_BASE = "https://api.spotify.com/v1"


class SpotifyValidator:
    def __init__(self) -> None:
        self._client_id = os.environ["SPOTIFY_CLIENT_ID"]
        self._client_secret = os.environ["SPOTIFY_CLIENT_SECRET"]
        self._token: str | None = None
        self._semaphore = asyncio.Semaphore(3)

    async def _get_token(self, client: httpx.AsyncClient) -> str:
        if self._token:
            return self._token
        creds = base64.b64encode(f"{self._client_id}:{self._client_secret}".encode()).decode()
        resp = await client.post(
            SPOTIFY_TOKEN_URL,
            headers={"Authorization": f"Basic {creds}"},
            data={"grant_type": "client_credentials"},
        )
        resp.raise_for_status()
        self._token = resp.json()["access_token"]
        return self._token

    async def validate_url(
        self, client: httpx.AsyncClient, url: str, expected_artist: str, expected_title: str
    ) -> tuple:
        """Returns (is_valid, actual_title) or (None, reason) for errors."""
        match = re.search(r"/(album|track|artist)/([A-Za-z0-9]+)", url)
        if not match:
            return None, f"cannot parse URL: {url}"

        resource_type, resource_id = match.groups()
        if resource_type == "artist":
            return True, "artist URL (skip)"

        async with self._semaphore:
            try:
                token = await self._get_token(client)
                resp = await client.get(
                    f"{SPOTIFY_API_BASE}/{resource_type}s/{resource_id}",
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=10.0,
                )
                if resp.status_code == 401:
                    self._token = None
                    token = await self._get_token(client)
                    resp = await client.get(
                        f"{SPOTIFY_API_BASE}/{resource_type}s/{resource_id}",
                        headers={"Authorization": f"Bearer {token}"},
                        timeout=10.0,
                    )
                if resp.status_code == 429:
                    retry_after = int(resp.headers.get("Retry-After", "5"))
                    await asyncio.sleep(retry_after)
                    return None, f"rate limited ({retry_after}s)"
                if resp.status_code == 404:
                    return False, "404 not found"
                if resp.status_code >= 500:
                    return None, f"HTTP {resp.status_code}"
                if resp.status_code != 200:
                    return None, f"HTTP {resp.status_code}"

                data = resp.json()
                actual_name = data.get("name", "")
                actual_artists = ", ".join(a["name"] for a in data.get("artists", []))
                actual = f"{actual_artists} - {actual_name}"

                # Normalize expected title
                clean = re.sub(
                    r'\s+(?:\d{1,2}["""\u201c\u201d]|LP|EP|CD)$', "", expected_title
                ).strip()
                clean = re.sub(r"\s*\[[^\]]*\]$", "", clean).strip()
                clean = re.sub(
                    r"\s*\([^)]*(?:promo|single|remix|deluxe|edition|remaster|ep|cd)\w*\)",
                    "",
                    clean,
                    flags=re.I,
                ).strip()
                clean = re.sub(r"\s+b/w\s+.*$", "", clean, flags=re.I).strip()
                clean = re.sub(r"\s+cd-?\d*$", "", clean, flags=re.I).strip()

                title_score = fuzz.token_sort_ratio(clean.lower(), actual_name.lower())
                artist_in = expected_artist.lower() in actual_artists.lower()
                title_in = len(clean) >= 4 and clean.lower() in actual_name.lower()

                is_valid = title_score >= 50 or artist_in or title_in
                return is_valid, actual

            except (httpx.TimeoutException, httpx.ConnectError):
                return None, "timeout"
            except Exception as e:
                return None, str(e)


async def main(args: argparse.Namespace) -> None:
    async with aiosqlite.connect(SQLITE_PATH) as db:
        rows = await db.execute_fetchall(
            "SELECT id, display_artist, display_title, spotify_url "
            "FROM albums WHERE spotify_url IS NOT NULL "
            "AND spotify_revalidated_at IS NULL"
        )

    albums = [(r[0], r[1], r[2], r[3]) for r in rows]
    if args.limit:
        albums = albums[: args.limit]

    log.info(f"Spotify URLs to validate: {len(albums):,}")

    validator = SpotifyValidator()
    valid = 0
    invalid = 0
    skipped = 0
    false_positives = []

    async with httpx.AsyncClient() as client:
        batch_size = 3  # match semaphore to avoid bursts
        for batch_start in range(0, len(albums), batch_size):
            batch = albums[batch_start : batch_start + batch_size]
            await asyncio.sleep(1.0)  # 1s between batches

            async def _check(sa_id, artist, title, url):
                return (
                    sa_id,
                    artist,
                    title,
                    url,
                    await validator.validate_url(client, url, artist, title),
                )

            results = await asyncio.gather(
                *[_check(sa_id, a, t, u) for sa_id, a, t, u in batch],
                return_exceptions=True,
            )

            # Mark all processed albums so we don't re-check
            async with aiosqlite.connect(SQLITE_PATH) as mark_db:
                for sa_id, _, _, _ in batch:
                    await mark_db.execute(
                        "UPDATE albums SET spotify_revalidated_at = datetime('now') WHERE id = ?",
                        (sa_id,),
                    )
                await mark_db.commit()

            for result in results:
                if isinstance(result, Exception):
                    skipped += 1
                    continue
                sa_id, artist, title, url, (is_valid, actual) = result
                if is_valid is None:
                    skipped += 1
                elif is_valid:
                    valid += 1
                else:
                    invalid += 1
                    false_positives.append(
                        {
                            "id": sa_id,
                            "artist": artist,
                            "title": title,
                            "url": url,
                            "actual": actual,
                        }
                    )
                    if invalid <= 20:
                        log.warning(f"  FALSE POSITIVE: [{artist}] {title} => {actual}")

            done = min(batch_start + batch_size, len(albums))
            if done % 500 == 0 or done == len(albums):
                log.info(
                    f"  {done:,}/{len(albums):,}: {valid} valid, {invalid} invalid, {skipped} skipped"
                )

    checked = valid + invalid
    rate = invalid / checked * 100 if checked else 0
    log.info(
        f"Result: {valid} valid, {invalid} invalid, {skipped} skipped ({rate:.1f}% false positive)"
    )

    with open("spotify_revalidation_false_positives.json", "w") as f:
        json.dump(false_positives, f, indent=2)

    if not args.dry_run and false_positives:
        async with aiosqlite.connect(SQLITE_PATH) as db:
            for fp in false_positives:
                await db.execute(
                    "UPDATE albums SET spotify_status = 'not_found', spotify_url = NULL WHERE id = ? AND spotify_url = ?",
                    (fp["id"], fp["url"]),
                )
            await db.commit()
            log.info(f"Removed {len(false_positives)} false positive URLs")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Revalidate Spotify URLs via Spotify API")
    parser.add_argument("--dry-run", action="store_true", help="Don't modify database")
    parser.add_argument("--limit", type=int, help="Limit to first N albums")
    args = parser.parse_args()
    asyncio.run(main(args))
