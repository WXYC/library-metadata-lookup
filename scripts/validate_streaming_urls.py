"""Validate streaming URLs in streaming_availability.db against actual service metadata.

Checks URLs for all services:
- Spotify: oEmbed API (title comparison)
- Bandcamp: fetch page title (artist/album comparison)
- Apple Music: oEmbed API
- Deezer: public API (/album/{id})
- Tidal: HEAD request (URL resolves check)

Removes false positives from the database.

Usage:
    .venv/bin/python scripts/validate_streaming_urls.py [--dry-run] [--services spotify bandcamp]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re

import aiosqlite
import httpx
from rapidfuzz import fuzz

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

SQLITE_PATH = "streaming_availability.db"


def _normalize_title(title: str) -> str:
    """Strip format suffixes, annotations, and common noise from a title for comparison."""
    t = title.strip()
    # Strip format suffixes: 12", 7", LP, EP, CD
    t = re.sub(r'\s+(?:\d{1,2}["""\u201c\u201d]|LP|EP|CD)$', "", t)
    # Strip [single], [EP], [local], etc.
    t = re.sub(r"\s*\[[^\]]*\]$", "", t)
    # Strip (promotional cd ep), (Remixes), (deluxe), etc.
    t = re.sub(
        r"\s*\([^)]*(?:promo|single|remix|deluxe|edition|remaster|ep|cd)\w*\)", "", t, flags=re.I
    )
    # Strip trailing " b/w ..." (B-side notation)
    t = re.sub(r"\s+b/w\s+.*$", "", t, flags=re.I)
    # Strip "cd-5", "cd-single"
    t = re.sub(r"\s+cd-?\d*$", "", t, flags=re.I)
    return t.strip()


def _is_title_match(expected_title: str, expected_artist: str, actual_text: str) -> bool:
    """Check if actual_text matches the expected title or artist.

    Returns True if any of:
    - Normalized title fuzzy score >= 50
    - Expected artist name appears in actual text
    - Expected title (cleaned) appears as substring of actual text
    """
    actual = actual_text.lower()
    clean_expected = _normalize_title(expected_title).lower()
    clean_actual = _normalize_title(actual_text).lower()

    # Direct fuzzy comparison on cleaned titles
    title_score = fuzz.token_sort_ratio(clean_expected, clean_actual)

    # Also try against the raw actual (oEmbed often returns just the album name)
    title_score = max(title_score, fuzz.token_sort_ratio(clean_expected, actual))

    # If "artist - album" format, extract album part
    if " - " in actual_text:
        album_part = actual_text.split(" - ", 1)[1]
        title_score = max(title_score, fuzz.token_sort_ratio(clean_expected, album_part.lower()))

    # Substring check: "drukqs" in "Drukqs" or "Coma" in "Coma"
    if len(clean_expected) >= 4 and clean_expected in actual:
        return True

    artist_in_text = expected_artist.lower() in actual
    return title_score >= 50 or artist_in_text


async def validate_spotify(
    client: httpx.AsyncClient, url: str, expected_artist: str, expected_title: str
) -> tuple[bool | None, str]:
    """Returns (True, title) if valid, (False, title) if wrong match, (None, reason) if inconclusive."""
    try:
        resp = await client.get(f"https://open.spotify.com/oembed?url={url}", timeout=10.0)
        if resp.status_code >= 500 or resp.status_code == 429:
            return None, f"HTTP {resp.status_code}"  # inconclusive
        if resp.status_code == 404:
            return False, "HTTP 404 — URL does not exist"
        if resp.status_code != 200:
            return None, f"HTTP {resp.status_code}"
        actual = resp.json().get("title", "")
        return _is_title_match(expected_title, expected_artist, actual), actual
    except (httpx.TimeoutException, httpx.ConnectError):
        return None, "timeout"
    except Exception as e:
        return None, str(e)


async def validate_bandcamp(
    client: httpx.AsyncClient, url: str, expected_artist: str, expected_title: str
) -> tuple[bool | None, str]:
    try:
        resp = await client.get(url, timeout=10.0, follow_redirects=True)
        if resp.status_code >= 500 or resp.status_code == 429:
            return None, f"HTTP {resp.status_code}"
        if resp.status_code == 404:
            return False, "HTTP 404"
        if resp.status_code != 200:
            return None, f"HTTP {resp.status_code}"
        match = re.search(r"<title>(.*?)</title>", resp.text, re.IGNORECASE | re.DOTALL)
        if not match:
            return None, "no title tag"
        actual = match.group(1).strip()
        return _is_title_match(expected_title, expected_artist, actual), actual
    except (httpx.TimeoutException, httpx.ConnectError):
        return None, "timeout"
    except Exception as e:
        return None, str(e)


async def validate_apple_music(
    client: httpx.AsyncClient, url: str, expected_artist: str, expected_title: str
) -> tuple[bool | None, str]:
    try:
        resp = await client.get(
            f"https://music.apple.com/oembed?url={url}&format=json", timeout=10.0
        )
        if resp.status_code == 200:
            actual = resp.json().get("title", "") or resp.json().get("name", "")
            return _is_title_match(expected_title, expected_artist, actual), actual
        resp2 = await client.head(url, timeout=10.0, follow_redirects=True)
        if resp2.status_code == 200:
            return True, "URL resolves"
        if resp2.status_code == 404:
            return False, "HTTP 404"
        return None, f"HEAD {resp2.status_code}"
    except (httpx.TimeoutException, httpx.ConnectError):
        return None, "timeout"
    except Exception as e:
        return None, str(e)


async def validate_deezer(
    client: httpx.AsyncClient, url: str, expected_artist: str, expected_title: str
) -> tuple[bool | None, str]:
    try:
        match = re.search(r"/album/(\d+)", url)
        if not match:
            return False, "no album ID in URL"
        resp = await client.get(f"https://api.deezer.com/album/{match.group(1)}", timeout=10.0)
        if resp.status_code >= 500 or resp.status_code == 429:
            return None, f"HTTP {resp.status_code}"
        if resp.status_code != 200:
            return None, f"HTTP {resp.status_code}"
        data = resp.json()
        if data.get("error"):
            return False, data["error"].get("message", "API error")
        actual = f"{data.get('artist', {}).get('name', '')} - {data.get('title', '')}"
        return _is_title_match(expected_title, expected_artist, actual), actual
    except (httpx.TimeoutException, httpx.ConnectError):
        return None, "timeout"
    except Exception as e:
        return None, str(e)


async def validate_tidal(
    client: httpx.AsyncClient, url: str, expected_artist: str, expected_title: str
) -> tuple[bool | None, str]:
    try:
        resp = await client.head(url, timeout=10.0, follow_redirects=True)
        if resp.status_code == 200:
            return True, "URL resolves"
        if resp.status_code == 404:
            return False, "HTTP 404"
        return None, f"HTTP {resp.status_code}"
    except (httpx.TimeoutException, httpx.ConnectError):
        return None, "timeout"
    except Exception as e:
        return None, str(e)


# (validator_fn, max_concurrency, delay_seconds)
SERVICE_CONFIG = {
    "spotify": (validate_spotify, 10, 0.1),
    "deezer": (validate_deezer, 5, 0.2),
    "apple_music": (validate_apple_music, 5, 0.2),
    "bandcamp": (validate_bandcamp, 2, 1.0),
    "tidal": (validate_tidal, 3, 0.5),
}

SERVICE_DB_MAP = {
    "spotify": ("spotify_status", "spotify_url"),
    "apple_music": ("apple_status", "apple_url"),
    "deezer": ("deezer_status", "deezer_url"),
    "bandcamp": (None, "bandcamp_url"),
    "tidal": (None, "tidal_url"),
}

SERVICE_QUERIES = {
    "spotify": "SELECT id, display_artist, display_title, spotify_url FROM albums WHERE spotify_url IS NOT NULL",
    "bandcamp": "SELECT id, display_artist, display_title, bandcamp_url FROM albums WHERE bandcamp_url IS NOT NULL",
    "apple_music": "SELECT id, display_artist, display_title, apple_url FROM albums WHERE apple_url IS NOT NULL",
    "deezer": "SELECT id, display_artist, display_title, deezer_url FROM albums WHERE deezer_url IS NOT NULL",
    "tidal": "SELECT id, display_artist, display_title, tidal_url FROM albums WHERE tidal_url IS NOT NULL",
}


async def main(args: argparse.Namespace) -> None:
    queries = SERVICE_QUERIES
    if args.services:
        queries = {k: v for k, v in queries.items() if k in args.services}

    all_rows: dict[str, list[tuple]] = {}
    async with aiosqlite.connect(SQLITE_PATH) as db:
        for service, query in queries.items():
            rows = await db.execute_fetchall(query)
            all_rows[service] = [(r[0], r[1], r[2], r[3]) for r in rows]

    total_urls = sum(len(rows) for rows in all_rows.values())
    log.info(f"URLs to validate: {total_urls}")
    for service, rows in all_rows.items():
        if rows:
            log.info(f"  {service}: {len(rows)}")

    stats: dict[str, dict[str, int]] = {}
    invalid_entries: list[dict] = []
    _invalid_lock = asyncio.Lock()

    async def validate_service(
        client: httpx.AsyncClient, service: str, rows: list[tuple]
    ) -> dict[str, int]:
        validator, concurrency, delay = SERVICE_CONFIG[service]
        semaphore = asyncio.Semaphore(concurrency)
        valid = 0
        invalid = 0
        skipped = 0
        log.info(
            f"Validating {len(rows)} {service} URLs (concurrency={concurrency}, delay={delay}s)..."
        )

        batch_size = max(concurrency * 5, 50)
        for batch_start in range(0, len(rows), batch_size):
            batch = rows[batch_start : batch_start + batch_size]

            async def _check(sa_id, artist, title, url, _sem=semaphore, _delay=delay):
                async with _sem:
                    await asyncio.sleep(_delay)
                    return sa_id, artist, title, url, await validator(client, url, artist, title)

            results = await asyncio.gather(
                *[_check(sa_id, a, t, u) for sa_id, a, t, u in batch],
                return_exceptions=True,
            )

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
                    async with _invalid_lock:
                        invalid_entries.append(
                            {
                                "id": sa_id,
                                "artist": artist,
                                "title": title,
                                "service": service,
                                "url": url,
                                "actual": actual,
                            }
                        )
                    if invalid <= 15:
                        log.warning(f"  FALSE POSITIVE [{service}]: [{artist}] {title} => {actual}")

            done = min(batch_start + batch_size, len(rows))
            if done % 500 < batch_size or done == len(rows):
                log.info(
                    f"  [{service}] {done}/{len(rows)} checked: "
                    f"{valid} valid, {invalid} invalid, {skipped} skipped"
                )

        total_checked = valid + invalid
        rate = invalid / total_checked * 100 if total_checked else 0
        result_stats = {"valid": valid, "invalid": invalid, "skipped": skipped, "total": len(rows)}
        log.info(
            f"  {service} done: {valid} valid, {invalid} invalid, {skipped} skipped "
            f"({rate:.1f}% false positive of checked)"
        )
        return result_stats

    async with httpx.AsyncClient() as client:
        # Run all services concurrently
        service_tasks = {}
        for service in SERVICE_CONFIG:
            rows = all_rows.get(service, [])
            if rows:
                service_tasks[service] = validate_service(client, service, rows)

        if service_tasks:
            results = await asyncio.gather(*service_tasks.values())
            for service, result in zip(service_tasks.keys(), results, strict=True):
                stats[service] = result

    log.info("Validation summary:")
    total_valid = total_invalid = total_skipped = 0
    for service, s in stats.items():
        log.info(
            f"  {service}: {s['valid']} valid, {s['invalid']} invalid, "
            f"{s['skipped']} skipped (of {s['total']})"
        )
        total_valid += s["valid"]
        total_invalid += s["invalid"]
        total_skipped += s["skipped"]
    log.info(f"Total: {total_valid} valid, {total_invalid} invalid, {total_skipped} skipped")

    with open("kagi_false_positives.json", "w") as f:
        json.dump(invalid_entries, f, indent=2)
    log.info("Saved to kagi_false_positives.json")

    if not args.dry_run and invalid_entries:
        async with aiosqlite.connect(SQLITE_PATH) as db:
            removed = 0
            for entry in invalid_entries:
                service = entry["service"]
                db_map = SERVICE_DB_MAP.get(service)
                if not db_map:
                    continue
                status_col, url_col = db_map
                if status_col:
                    await db.execute(
                        f"UPDATE albums SET {status_col} = 'not_found', {url_col} = NULL "
                        f"WHERE id = ? AND {url_col} = ?",
                        (entry["id"], entry["url"]),
                    )
                else:
                    await db.execute(
                        f"UPDATE albums SET {url_col} = NULL WHERE id = ? AND {url_col} = ?",
                        (entry["id"], entry["url"]),
                    )
                removed += 1
            await db.commit()
            log.info(f"Removed {removed} false positive URLs from database")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Validate streaming URLs")
    parser.add_argument("--dry-run", action="store_true", help="Don't modify database")
    parser.add_argument(
        "--services",
        nargs="+",
        choices=list(SERVICE_CONFIG.keys()),
        help="Only validate these services",
    )
    args = parser.parse_args()
    asyncio.run(main(args))
