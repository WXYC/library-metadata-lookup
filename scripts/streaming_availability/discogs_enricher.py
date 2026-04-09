"""Enrich library albums with canonical Discogs artist/title names."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from rapidfuzz import fuzz

from core.matching import strip_diacritics
from scripts.streaming_availability.matching import (
    is_acceptable_match,
    score_match,
    strip_discogs_suffix,
)

if TYPE_CHECKING:
    import asyncpg

logger = logging.getLogger(__name__)

# Artist name fuzzy lookup on the small wxyc.artist_names table (~130K rows, ~1ms)
_ARTIST_FUZZY_QUERY = """
    SELECT artist_name FROM wxyc.artist_names
    WHERE lower(f_unaccent(artist_name)) % lower(f_unaccent($1))
    LIMIT 10
"""

# Release lookup by artist name on wxyc schema (~10ms)
# Uses regexp_replace to strip Discogs disambiguation suffixes like (2), (22)
# so that searching for "Los Naturales" also matches "Los Naturales (2)".
_WXYC_RELEASE_QUERY = """
    SELECT DISTINCT ON (r.id)
        r.id as release_id, r.title, ra.artist_name, r.artwork_url
    FROM wxyc.release r
    JOIN wxyc.release_artist ra ON ra.release_id = r.id AND ra.extra = 0
    WHERE lower(regexp_replace(left(ra.artist_name, 200), '\\s+\\(\\d+\\)$', ''))
        = lower(regexp_replace($1, '\\s+\\(\\d+\\)$', ''))
    ORDER BY r.id
    LIMIT 200
"""

# Fallback: release lookup on full public schema (~13ms with btree index)
# Same disambiguation-suffix stripping as the wxyc query above.
_PUBLIC_RELEASE_QUERY = """
    SELECT DISTINCT ON (r.id)
        r.id as release_id, r.title, ra.artist_name, r.artwork_url
    FROM release r
    JOIN release_artist ra ON ra.release_id = r.id AND ra.extra = 0
    WHERE lower(regexp_replace(left(ra.artist_name, 200), '\\s+\\(\\d+\\)$', ''))
        = lower(regexp_replace($1, '\\s+\\(\\d+\\)$', ''))
    ORDER BY r.id
    LIMIT 200
"""


def pick_best_match(query_artist: str, query_title: str, results: list[dict]) -> dict | None:
    """Pick the best Discogs match using fuzzy scoring.

    Strips Discogs disambiguation suffixes like (2) before scoring.
    Returns the best result dict if it meets the acceptance threshold, else None.
    """
    best: dict | None = None
    best_score = 0.0

    for result in results:
        clean_artist = strip_discogs_suffix(result["artist_name"])
        clean_title = strip_discogs_suffix(result["title"])
        artist_score = score_match(query_artist, clean_artist)
        title_score = score_match(query_title, clean_title)

        if not is_acceptable_match(artist_score, title_score):
            continue

        combined = (artist_score + title_score) / 2
        if combined > best_score:
            best_score = combined
            best = result

    return best


def _best_artist_match(query: str, candidates: list[str]) -> str | None:
    """Pick the best fuzzy match from candidate artist names.

    Strips Discogs disambiguation suffixes before comparison.
    """
    query_normalized = strip_diacritics(query).lower()
    best_name = None
    best_score = 0.0

    for name in candidates:
        clean = strip_discogs_suffix(name)
        score = fuzz.token_sort_ratio(query_normalized, strip_diacritics(clean).lower())
        if score > best_score and score >= 80:
            best_score = score
            best_name = name

    return best_name


async def build_artist_mapping(pool: asyncpg.Pool, artists: list[str]) -> dict[str, str]:
    """Build a mapping from library artist names to Discogs canonical names.

    Uses trigram fuzzy search on the small wxyc.artist_names table (~130K rows).
    Only includes entries where the names actually differ.

    Returns dict mapping library_name -> discogs_canonical_name.
    """
    mapping: dict[str, str] = {}

    for artist in artists:
        try:
            rows = await pool.fetch(_ARTIST_FUZZY_QUERY, artist)
            candidates = [row["artist_name"] for row in rows]
            if not candidates:
                continue

            best = _best_artist_match(artist, candidates)
            if best and best.lower() != artist.lower():
                mapping[artist] = best
        except Exception:
            logger.debug("Artist mapping failed for %s", artist)

    return mapping


async def enrich_album(
    pool: asyncpg.Pool,
    artist: str,
    title: str,
    artist_mapping: dict[str, str] | None = None,
) -> dict | None:
    """Search the Discogs PG cache for canonical artist/title.

    Uses the artist_mapping to resolve the canonical Discogs artist name,
    then does exact match release lookups. Tries wxyc schema first, falls
    back to public schema if no match found.

    Returns a dict with 'artist_name', 'title', 'release_id' if found, else None.
    """
    try:
        # Use mapped name if available, otherwise use original
        search_artist = artist
        if artist_mapping and artist in artist_mapping:
            search_artist = artist_mapping[artist]

        # Try wxyc schema first
        rows = await pool.fetch(_WXYC_RELEASE_QUERY, search_artist)
        results = [dict(row) for row in rows]
        match = pick_best_match(artist, title, results)
        if match:
            return match

        # Fall back to public schema
        rows = await pool.fetch(_PUBLIC_RELEASE_QUERY, search_artist)
        results = [dict(row) for row in rows]
        return pick_best_match(artist, title, results)
    except Exception:
        logger.exception("Discogs enrichment failed for %s - %s", artist, title)
        return None


async def check_wxyc_schema(pool: asyncpg.Pool) -> bool:
    """Check if the wxyc schema exists and has data."""
    try:
        count = await pool.fetchval("SELECT COUNT(*) FROM wxyc.release")
        return count > 0
    except Exception:
        return False
