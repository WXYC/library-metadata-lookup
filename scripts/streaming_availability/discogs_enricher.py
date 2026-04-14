"""Enrich library albums with canonical Discogs artist/title names."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from scripts.streaming_availability.matching import (
    is_acceptable_match,
    score_match,
    strip_discogs_suffix,
)

if TYPE_CHECKING:
    import asyncpg

logger = logging.getLogger(__name__)

# SQL to load the artist mapping from the entity store.
# Returns all reconciled artists with a known Discogs artist ID.
_ENTITY_STORE_QUERY = """
    SELECT library_name, discogs_artist_id
    FROM entity.identity
    WHERE reconciliation_status = 'reconciled'
      AND discogs_artist_id IS NOT NULL
"""

# Release lookup by artist ID on wxyc schema (direct, no fuzzy matching needed)
_WXYC_RELEASE_BY_ARTIST_ID_QUERY = """
    SELECT DISTINCT ON (r.id)
        r.id as release_id, r.title, ra.artist_name, r.artwork_url
    FROM wxyc.release r
    JOIN wxyc.release_artist ra ON ra.release_id = r.id AND ra.extra = 0
    WHERE ra.artist_id = $1
    ORDER BY r.id
    LIMIT 200
"""

# Fallback: release lookup by artist ID on full public schema
_PUBLIC_RELEASE_BY_ARTIST_ID_QUERY = """
    SELECT DISTINCT ON (r.id)
        r.id as release_id, r.title, ra.artist_name, r.artwork_url
    FROM release r
    JOIN release_artist ra ON ra.release_id = r.id AND ra.extra = 0
    WHERE ra.artist_id = $1
    ORDER BY r.id
    LIMIT 200
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


async def load_entity_store_mapping(pool: asyncpg.Pool) -> dict[str, int]:
    """Load artist mapping from the entity store.

    Queries ``entity.identity`` for all reconciled artists with a known
    Discogs artist ID. Returns a dict mapping library_name to discogs_artist_id.

    If the entity schema does not exist or the query fails, returns an empty dict
    so the pipeline can fall back to name-based lookups.
    """
    t0 = time.monotonic()
    try:
        rows = await pool.fetch(_ENTITY_STORE_QUERY)
        mapping = {
            row["library_name"]: row["discogs_artist_id"]
            for row in rows
            if row["discogs_artist_id"] is not None
        }
        elapsed = time.monotonic() - t0
        logger.info(
            "Entity store: loaded %d artist mappings in %.2fs",
            len(mapping),
            elapsed,
        )
        return mapping
    except Exception:
        logger.warning("Entity store query failed, falling back to name-based lookups")
        return {}


async def enrich_album(
    pool: asyncpg.Pool,
    artist: str,
    title: str,
    *,
    discogs_artist_id: int | None = None,
) -> dict | None:
    """Search the Discogs PG cache for canonical artist/title.

    When ``discogs_artist_id`` is provided (from the entity store), uses direct
    ID-based release lookups which are faster and more accurate than name matching.
    When absent, falls back to name-based release lookups.

    Tries wxyc schema first, falls back to public schema if no match found.

    Returns a dict with 'artist_name', 'title', 'release_id' if found, else None.
    """
    try:
        if discogs_artist_id is not None:
            # Direct lookup by artist ID (fast, precise)
            rows = await pool.fetch(_WXYC_RELEASE_BY_ARTIST_ID_QUERY, discogs_artist_id)
            results = [dict(row) for row in rows]
            match = pick_best_match(artist, title, results)
            if match:
                return match

            rows = await pool.fetch(_PUBLIC_RELEASE_BY_ARTIST_ID_QUERY, discogs_artist_id)
            results = [dict(row) for row in rows]
            return pick_best_match(artist, title, results)
        else:
            # Name-based lookup (fallback for unreconciled artists)
            rows = await pool.fetch(_WXYC_RELEASE_QUERY, artist)
            results = [dict(row) for row in rows]
            match = pick_best_match(artist, title, results)
            if match:
                return match

            rows = await pool.fetch(_PUBLIC_RELEASE_QUERY, artist)
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
