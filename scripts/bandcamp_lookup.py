"""Look up Bandcamp album URLs for WXYC artists using Wikidata slugs.

For all WXYC artists with a Bandcamp slug from Wikidata, fetches the artist's
Bandcamp page and extracts album URLs. Updates streaming_availability.db with
direct Bandcamp links.

Usage:
    .venv/bin/python scripts/bandcamp_lookup.py [--dry-run] [--limit N]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sqlite3

import asyncpg
import httpx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

SQLITE_PATH = "streaming_availability.db"
WIKIDATA_URL = "postgresql://jake@localhost/wikidata"


async def fetch_bandcamp_albums(
    client: httpx.AsyncClient,
    slug: str,
    semaphore: asyncio.Semaphore,
) -> list[dict]:
    """Fetch album list from a Bandcamp artist page."""
    async with semaphore:
        await asyncio.sleep(1.0)  # be polite to Bandcamp
        url = f"https://{slug}.bandcamp.com/music"
        try:
            resp = await client.get(url, timeout=15.0, follow_redirects=True)
            if resp.status_code != 200:
                return []

            # Bandcamp omits `charset=` on most pages; force UTF-8 before
            # reading resp.text so diacritics don't mojibake.
            resp.encoding = "utf-8"

            # Extract album URLs and titles from the page
            # Bandcamp album links: <a href="/album/album-name">
            albums = []
            for match in re.finditer(
                r'<a\s+href="(/album/[^"]+)"[^>]*>.*?<p\s+class="title">([^<]+)</p>',
                resp.text,
                re.DOTALL,
            ):
                path, title = match.groups()
                albums.append(
                    {
                        "url": f"https://{slug}.bandcamp.com{path}",
                        "title": title.strip(),
                    }
                )

            # Fallback: just extract album hrefs if the title pattern doesn't match
            if not albums:
                for match in re.finditer(r'href="(/album/[^"]+)"', resp.text):
                    path = match.group(1)
                    albums.append(
                        {
                            "url": f"https://{slug}.bandcamp.com{path}",
                            "title": path.split("/")[-1].replace("-", " "),
                        }
                    )

            return albums
        except Exception:
            return []


async def main(args: argparse.Namespace) -> None:
    # Get all WXYC artists with Bandcamp slugs from Wikidata
    wd = await asyncpg.connect(WIKIDATA_URL)
    rows = await wd.fetch("""
        SELECT e.label as artist_name, bc.discogs_id as bandcamp_slug
        FROM discogs_mapping bc
        JOIN entity e ON e.qid = bc.qid
        WHERE bc.property = 'P3283'
    """)
    await wd.close()

    slug_map = {r["artist_name"].lower(): r["bandcamp_slug"] for r in rows}
    log.info(f"Wikidata Bandcamp slugs: {len(slug_map)}")

    # Match against WXYC library albums
    sdb = sqlite3.connect(SQLITE_PATH)
    albums = sdb.execute(
        "SELECT id, display_artist, display_title, bandcamp_url "
        "FROM albums WHERE is_compilation = 0 AND bandcamp_url IS NULL"
    ).fetchall()
    sdb.close()

    # Group albums by artist, filter to those with Bandcamp slugs
    from collections import defaultdict

    artist_albums: dict[str, list[tuple]] = defaultdict(list)
    for sa_id, artist, title, _ in albums:
        slug = slug_map.get(artist.lower())
        if slug:
            artist_albums[slug].append((sa_id, artist, title))

    total_artists = len(artist_albums)
    total_albums = sum(len(v) for v in artist_albums.values())
    log.info(
        f"Artists with Bandcamp slug and albums to check: {total_artists} ({total_albums} albums)"
    )

    if args.limit:
        items = list(artist_albums.items())[: args.limit]
        artist_albums = dict(items)
        log.info(f"Limited to {len(artist_albums)} artists")

    if args.dry_run:
        for slug, albs in list(artist_albums.items())[:10]:
            log.info(f"  {slug}.bandcamp.com — {len(albs)} albums ({albs[0][1]})")
        return

    # Fetch Bandcamp catalogs and match
    semaphore = asyncio.Semaphore(2)
    matched = 0
    results = []

    async with httpx.AsyncClient() as client:
        for i, (slug, albs) in enumerate(artist_albums.items()):
            bc_albums = await fetch_bandcamp_albums(client, slug, semaphore)

            if bc_albums:
                from rapidfuzz import fuzz

                for sa_id, artist, title in albs:
                    # Strip format suffixes for matching
                    clean = re.sub(r'\s+(?:\d{1,2}["""]|LP|EP|CD)$', "", title).strip()
                    best_score = 0
                    best_url = None
                    for bc in bc_albums:
                        score = fuzz.token_sort_ratio(clean.lower(), bc["title"].lower())
                        if score > best_score:
                            best_score = score
                            best_url = bc["url"]
                    if best_score >= 60 and best_url:
                        matched += 1
                        results.append(
                            {
                                "id": sa_id,
                                "artist": artist,
                                "title": title,
                                "bandcamp_url": best_url,
                                "score": best_score,
                            }
                        )

            if (i + 1) % 100 == 0 or i + 1 == total_artists:
                log.info(f"  {i + 1}/{total_artists} artists, {matched} albums matched")

    log.info(f"Total Bandcamp matches: {matched}")

    if results:
        sdb = sqlite3.connect(SQLITE_PATH)
        for r in results:
            sdb.execute(
                "UPDATE albums SET bandcamp_url = ? WHERE id = ? AND bandcamp_url IS NULL",
                (r["bandcamp_url"], r["id"]),
            )
        sdb.commit()
        total_bc = sdb.execute(
            "SELECT COUNT(*) FROM albums WHERE bandcamp_url IS NOT NULL"
        ).fetchone()[0]
        sdb.close()
        log.info(f"Updated {len(results)} albums. Total with Bandcamp URLs: {total_bc}")

    with open("bandcamp_matches.json", "w") as f:
        json.dump(results, f, indent=2)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Bandcamp lookup via Wikidata slugs")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    asyncio.run(main(args))
