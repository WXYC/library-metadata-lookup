"""Discogs PostgreSQL cache queries for track-level artist resolution.

Queries the discogs-cache database to find which artist performed a specific
track on a compilation release, using the release_track_artist table.
"""

from __future__ import annotations

import asyncio
import logging
import os

import httpx
from aiolimiter import AsyncLimiter

logger = logging.getLogger(__name__)

# Discogs API rate limiter: 25 req/min (conservative, each album uses ~3 calls)
_api_limiter = AsyncLimiter(25, 60)
_DISCOGS_BASE = "https://api.discogs.com"


def _title_variants(title: str) -> list[str]:
    """Generate title variants for matching.

    Produces multiple forms to handle mismatches between flowsheet and Discogs:
    - Full title as-is
    - Subtitle stripped (after ':', ';', ' - ')
    - "v/a: " or "v/a " prefix stripped
    - Common Discogs prefixes added: "Various - <title>", "Various Artists - <title>"
    """
    import re

    variants = [title]

    # Strip subtitles
    for sep in [":", ";", " - "]:
        if sep in title:
            short = title.split(sep, 1)[0].strip()
            if short and len(short) >= 3 and short not in variants:
                variants.append(short)

    # Strip "v/a:" or "v/a " prefix from flowsheet title
    stripped = re.sub(
        r"^(?:v/?a\.?|various(?:\s+artists?)?)\s*[:\s]\s*", "", title, flags=re.IGNORECASE
    ).strip()
    if stripped and stripped != title and stripped not in variants:
        variants.append(stripped)

    # Try with Discogs-style prefixes (Discogs often stores "Various - Title")
    base = variants[0]
    for prefix in ["Various - ", "Various Artists - "]:
        prefixed = prefix + base
        if prefixed not in variants:
            variants.append(prefixed)

    return variants


async def resolve_track_artist_exact(pool, song_title: str, release_title: str) -> list[dict]:
    """Find track artists by exact match on song title and prefix match on release title.

    Uses case-insensitive, diacritics-insensitive matching via f_unaccent().
    Tries multiple release title variants:
    1. Exact match on full title
    2. Exact match on title with subtitle stripped (after ':', ';', ' - ')
    3. Prefix match (Discogs title starts with flowsheet title or vice versa)

    This handles common mismatches between flowsheet and Discogs titles:
    - Flowsheet has subtitle, Discogs doesn't: "Vintage Palmwine: Highlife..." vs "Vintage Palmwine"
    - Discogs has "Various - " prefix: "Various - Bougouni Yaalali CD (...)"
    - Discogs has label/catno suffix: "Radio Myanmar (Burma) [Sublime Frequencies: SF044]"

    Args:
        pool: asyncpg connection pool.
        song_title: Track title from the flowsheet.
        release_title: Album title from the flowsheet.

    Returns:
        List of dicts with keys: artist_name, track_title, release_title, sim.
    """
    # Strategy A: exact match on full title and subtitle-stripped variants
    # Uses lower(title) on track (matches idx_release_track_title_lower btree)
    # and lower(f_unaccent(title)) on release (matches idx_release_title_lower btree)
    exact_query = """
        SELECT DISTINCT rta.artist_name, rt.title as track_title,
               r.title as release_title, 1.0 as sim
        FROM release_track rt
        JOIN release r ON r.id = rt.release_id
        JOIN release_track_artist rta ON rta.release_id = rt.release_id
                                     AND rta.track_sequence = rt.sequence
                                     AND rta.extra = 0
        WHERE lower(rt.title) = lower($1)
          AND lower(f_unaccent(r.title)) = lower(f_unaccent($2))
        LIMIT 10
    """
    for variant in _title_variants(release_title):
        rows = await pool.fetch(exact_query, song_title, variant)
        if rows:
            return [dict(row) for row in rows]

    return []


async def resolve_track_artist_fuzzy(pool, song_title: str, release_title: str) -> list[dict]:
    """Find track artists by fuzzy match on song + release title.

    Uses pg_trgm similarity for typo-tolerant matching.

    Args:
        pool: asyncpg connection pool.
        song_title: Track title from the flowsheet.
        release_title: Album title from the flowsheet.

    Returns:
        List of dicts with keys: artist_name, track_title, release_title,
        song_sim, release_sim.
    """
    query = """
        SELECT * FROM (
            SELECT DISTINCT rta.artist_name, rt.title as track_title,
                   r.title as release_title,
                   similarity(lower(f_unaccent(rt.title)), lower(f_unaccent($1))) as song_sim,
                   similarity(lower(f_unaccent(r.title)), lower(f_unaccent($2))) as release_sim
            FROM release_track rt
            JOIN release r ON r.id = rt.release_id
            JOIN release_track_artist rta ON rta.release_id = rt.release_id
                                         AND rta.track_sequence = rt.sequence
                                         AND rta.extra = 0
            WHERE lower(f_unaccent(rt.title)) % lower(f_unaccent($1))
              AND lower(f_unaccent(r.title)) % lower(f_unaccent($2))
        ) sub
        ORDER BY song_sim + release_sim DESC
        LIMIT 10
    """
    rows = await pool.fetch(query, song_title, release_title)
    return [dict(row) for row in rows]


async def resolve_track_artist_song_only(pool, song_title: str) -> list[dict]:
    """Find track artists by song title only, filtered to compilation releases.

    Used when the flowsheet entry has no release title. Restricts to releases
    where the primary artist is a Various Artists variant.

    Args:
        pool: asyncpg connection pool.
        song_title: Track title from the flowsheet.

    Returns:
        List of dicts with keys: artist_name, track_title, release_title.
    """
    query = """
        SELECT DISTINCT rta.artist_name, rt.title as track_title,
               r.title as release_title
        FROM release_track rt
        JOIN release r ON r.id = rt.release_id
        JOIN release_artist ra ON ra.release_id = r.id AND ra.extra = 0
        JOIN release_track_artist rta ON rta.release_id = rt.release_id
                                     AND rta.track_sequence = rt.sequence
                                     AND rta.extra = 0
        WHERE lower(f_unaccent(rt.title)) % lower(f_unaccent($1))
          AND lower(ra.artist_name) LIKE '%various%'
        LIMIT 10
    """
    rows = await pool.fetch(query, song_title)
    return [dict(row) for row in rows]


async def collect_track_artists_for_release(pool, release_title: str) -> list[dict]:
    """Collect all track artist credits for a compilation release.

    Matches the release by title (exact, case-insensitive) and returns
    all (artist_name, track_title) pairs from release_track_artist,
    ordered by track sequence.

    Args:
        pool: asyncpg connection pool.
        release_title: Album title to look up.

    Returns:
        List of dicts with keys: artist_name, track_title, sequence.
    """
    query = """
        SELECT rta.artist_name, rt.title as track_title, rt.sequence
        FROM release r
        JOIN release_artist ra ON ra.release_id = r.id AND ra.extra = 0
        JOIN release_track rt ON rt.release_id = r.id
        JOIN release_track_artist rta ON rta.release_id = rt.release_id
                                     AND rta.track_sequence = rt.sequence
                                     AND rta.extra = 0
        WHERE lower(f_unaccent(r.title)) = lower(f_unaccent($1))
          AND lower(ra.artist_name) LIKE '%various%'
        ORDER BY rt.sequence
    """
    for variant in _title_variants(release_title):
        rows = await pool.fetch(query, variant)
        if rows:
            return [dict(row) for row in rows]
    return []


# ── Discogs API fallback ──


async def _api_request(
    client: httpx.AsyncClient, path: str, params: dict | None = None
) -> dict | None:
    """Make a rate-limited request to the Discogs API with 429 backoff."""
    for attempt in range(4):
        await _api_limiter.acquire()
        try:
            response = await client.get(f"{_DISCOGS_BASE}{path}", params=params)
            if response.status_code == 429:
                delay = max(int(response.headers.get("Retry-After", "10")), 2 ** (attempt + 1))
                logger.warning(f"Rate limited, waiting {delay}s (attempt {attempt + 1})")
                await asyncio.sleep(delay)
                continue
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError:
            return None
        except Exception as e:
            logger.warning(f"Discogs API request failed: {e}")
            return None
    return None


async def api_search_release(client: httpx.AsyncClient, title: str) -> int | None:
    """Search the Discogs API for a compilation release by title.

    Tries at most 2 title variants (full title, then subtitle-stripped) to stay
    within rate limits. Returns the release_id of the best match, or None.
    """
    variants = _title_variants(title)[:2]  # limit to 2 variants for API calls
    for variant in variants:
        data = await _api_request(
            client,
            "/database/search",
            {
                "q": variant,
                "type": "release",
                "per_page": 5,
            },
        )
        if not data or not data.get("results"):
            continue

        for result in data["results"]:
            # Prefer results that look like compilations
            formats = result.get("format", [])
            if "Compilation" in formats or "various" in result.get("title", "").lower():
                return result["id"]

        # Fall back to first result
        return data["results"][0]["id"]

    return None


async def api_get_release_tracks(client: httpx.AsyncClient, release_id: int) -> list[dict]:
    """Fetch a release's tracklist with per-track artist credits from the Discogs API.

    Returns list of dicts with keys: artist_name, track_title.
    """
    data = await _api_request(client, f"/releases/{release_id}")
    if not data:
        return []

    results = []
    for track in data.get("tracklist", []):
        title = track.get("title", "")
        artists = track.get("artists", [])
        if artists:
            for artist in artists:
                name = artist.get("name", "")
                if name:
                    results.append({"artist_name": name, "track_title": title})
        # Tracks without per-track artists are credited to the release artist (skip for compilations)

    return results


def create_discogs_api_client() -> httpx.AsyncClient:
    """Create an httpx client configured for the Discogs API."""
    token = os.environ.get("DISCOGS_TOKEN", "")
    return httpx.AsyncClient(
        headers={
            "Authorization": f"Discogs token={token}",
            "User-Agent": "WXYCLibraryMetadataLookup/1.0",
        },
        timeout=15.0,
    )
