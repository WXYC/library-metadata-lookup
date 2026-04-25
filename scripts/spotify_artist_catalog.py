"""Pull full Spotify discography for not-on-streaming artists and fuzzy-match titles.

Resolves Spotify artist IDs via Wikidata (Discogs artist ID → QID → P1902),
then calls GET /v1/artists/{id}/albums to get the full catalog and fuzzy-matches
against album titles in streaming_availability.db.

Usage:
    .venv/bin/python scripts/spotify_artist_catalog.py [--dry-run] [--limit N]
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
import asyncpg
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
WIKIDATA_URL = "postgresql://jake@localhost/wikidata"
DISCOGS_URL = "postgresql://jake@localhost/discogs"
SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_API_BASE = "https://api.spotify.com/v1"


class SpotifyClient:
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

    async def get_artist_albums(self, client: httpx.AsyncClient, artist_id: str) -> list[dict]:
        """Get all albums for a Spotify artist."""
        async with self._semaphore:
            token = await self._get_token(client)
            albums = []
            url = f"{SPOTIFY_API_BASE}/artists/{artist_id}/albums"
            params = {"limit": 50, "include_groups": "album,single,compilation"}

            while url:
                resp = await client.get(
                    url,
                    headers={"Authorization": f"Bearer {token}"},
                    params=params,
                    timeout=10.0,
                )
                if resp.status_code == 401:
                    self._token = None
                    token = await self._get_token(client)
                    continue
                if resp.status_code == 429:
                    retry = int(resp.headers.get("Retry-After", "5"))
                    log.warning(f"Rate limited, waiting {retry}s")
                    await asyncio.sleep(retry)
                    continue
                if resp.status_code != 200:
                    break
                data = resp.json()
                albums.extend(data.get("items", []))
                url = data.get("next")
                params = {}  # next URL includes params
            return albums


def normalize_title(title: str) -> str:
    t = re.sub(r'\s+(?:\d{1,2}["""\u201c\u201d]|LP|EP|CD)$', "", title).strip()
    t = re.sub(r"\s*\[[^\]]*\]$", "", t).strip()
    t = re.sub(r"\s+b/w\s+.*$", "", t, flags=re.I).strip()
    t = re.sub(r"\s+cd-?\d*$", "", t, flags=re.I).strip()
    return t


async def main(args: argparse.Namespace) -> None:
    # Load not-on-streaming albums
    async with aiosqlite.connect(SQLITE_PATH) as db:
        rows = await db.execute_fetchall(
            "SELECT id, display_artist, display_title, discogs_artist "
            "FROM albums WHERE is_compilation = 0 "
            "AND spotify_status != 'found' AND deezer_status != 'found' AND apple_status != 'found'"
        )
    albums = [(r[0], r[1], r[2], r[3]) for r in rows]
    if args.limit:
        albums = albums[: args.limit]
    log.info(f"Not-on-streaming albums: {len(albums)}")

    # Build artist → Spotify ID mapping via Discogs → Wikidata
    discogs_conn = await asyncpg.connect(DISCOGS_URL)
    wikidata_conn = await asyncpg.connect(WIKIDATA_URL)

    # Get all Discogs artist IDs that have Spotify IDs
    wd_rows = await wikidata_conn.fetch("""
        SELECT dm_discogs.discogs_id as discogs_artist_id, dm_spotify.discogs_id as spotify_artist_id
        FROM discogs_mapping dm_discogs
        JOIN discogs_mapping dm_spotify ON dm_discogs.qid = dm_spotify.qid AND dm_spotify.property = 'P1902'
        WHERE dm_discogs.property = 'P1953'
    """)
    discogs_to_spotify = {r["discogs_artist_id"]: r["spotify_artist_id"] for r in wd_rows}
    log.info(f"Wikidata: {len(discogs_to_spotify)} Discogs→Spotify artist mappings")
    await wikidata_conn.close()

    # For each not-on-streaming album, find the Discogs artist ID
    artist_names = list({a[1] for a in albums} | {a[3] for a in albums if a[3]})
    log.info(f"Distinct artists to resolve: {len(artist_names)}")

    artist_spotify_ids: dict[str, str] = {}  # display_artist → spotify_artist_id
    for name in artist_names:
        if not name:
            continue
        row = await discogs_conn.fetchrow(
            "SELECT DISTINCT artist_id FROM release_artist WHERE lower(artist_name) = lower($1) AND extra = 0 LIMIT 1",
            name,
        )
        if row and str(row["artist_id"]) in discogs_to_spotify:
            artist_spotify_ids[name] = discogs_to_spotify[str(row["artist_id"])]

    await discogs_conn.close()
    log.info(f"Resolved {len(artist_spotify_ids)} artists to Spotify IDs")

    # Group albums by artist Spotify ID
    artist_albums: dict[str, list[tuple]] = {}  # spotify_id → [(sa_id, artist, title)]
    for sa_id, artist, title, discogs_artist in albums:
        spotify_id = artist_spotify_ids.get(artist) or artist_spotify_ids.get(discogs_artist or "")
        if spotify_id:
            artist_albums.setdefault(spotify_id, []).append((sa_id, artist, title))

    albums_with_id = sum(len(v) for v in artist_albums.values())
    log.info(f"Albums with Spotify artist ID: {albums_with_id} across {len(artist_albums)} artists")

    if args.dry_run:
        log.info("Dry run — showing first 10 artists:")
        for sid, albs in list(artist_albums.items())[:10]:
            log.info(f"  Spotify:{sid} — {len(albs)} albums: {albs[0][1]}")
        return

    # Pull catalogs and match
    spotify = SpotifyClient()
    found = 0
    results = []

    async with httpx.AsyncClient() as client:
        for i, (spotify_id, albs) in enumerate(artist_albums.items()):
            try:
                catalog = await spotify.get_artist_albums(client, spotify_id)
            except Exception as e:
                log.warning(f"Failed to fetch {spotify_id}: {e}")
                continue

            for sa_id, artist, title in albs:
                clean = normalize_title(title)
                best_score = 0
                best_url = None
                for album in catalog:
                    score = fuzz.token_sort_ratio(clean.lower(), album["name"].lower())
                    if score > best_score:
                        best_score = score
                        best_url = album["external_urls"].get("spotify")
                        best_name = album["name"]

                if best_score >= 75 and best_url:
                    found += 1
                    results.append(
                        {
                            "id": sa_id,
                            "artist": artist,
                            "title": title,
                            "spotify_url": best_url,
                            "matched_title": best_name,
                            "score": best_score,
                        }
                    )

            if (i + 1) % 100 == 0 or i + 1 == len(artist_albums):
                log.info(f"  {i + 1}/{len(artist_albums)} artists, {found} albums matched")

    log.info(f"Total matched: {found}")

    with open("spotify_catalog_matches.json", "w") as f:
        json.dump(results, f, indent=2)

    if results:
        async with aiosqlite.connect(SQLITE_PATH) as db:
            for r in results:
                await db.execute(
                    "UPDATE albums SET spotify_status = 'found', spotify_url = ? "
                    "WHERE id = ? AND spotify_status != 'found'",
                    (r["spotify_url"], r["id"]),
                )
            await db.commit()
            log.info(f"Updated {len(results)} albums in database")

        async with aiosqlite.connect(SQLITE_PATH) as db:
            row = await db.execute_fetchall(
                "SELECT "
                "SUM(CASE WHEN spotify_status='found' OR deezer_status='found' OR apple_status='found' THEN 1 ELSE 0 END), "
                "SUM(CASE WHEN spotify_status!='found' AND deezer_status!='found' AND apple_status!='found' THEN 1 ELSE 0 END) "
                "FROM albums WHERE is_compilation = 0"
            )
            log.info(f"On streaming: {row[0][0]:,} | Not found: {row[0][1]:,}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Spotify artist catalog pull")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    asyncio.run(main(args))
