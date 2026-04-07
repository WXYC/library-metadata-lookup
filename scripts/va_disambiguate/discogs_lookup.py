"""Discogs PostgreSQL cache queries for track-level artist resolution.

Queries the discogs-cache database to find which artist performed a specific
track on a compilation release, using the release_track_artist table.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


async def resolve_track_artist_exact(
    pool, song_title: str, release_title: str
) -> list[dict]:
    """Find track artists by exact match on song + release title.

    Uses case-insensitive, diacritics-insensitive matching via f_unaccent().

    Args:
        pool: asyncpg connection pool.
        song_title: Track title from the flowsheet.
        release_title: Album title from the flowsheet.

    Returns:
        List of dicts with keys: artist_name, track_title, release_title, sim.
    """
    query = """
        SELECT DISTINCT rta.artist_name, rt.title as track_title,
               r.title as release_title, 1.0 as sim
        FROM release_track rt
        JOIN release r ON r.id = rt.release_id
        JOIN release_track_artist rta ON rta.release_id = rt.release_id
                                     AND rta.track_sequence = rt.sequence
        WHERE lower(f_unaccent(rt.title)) = lower(f_unaccent($1))
          AND lower(f_unaccent(r.title)) = lower(f_unaccent($2))
        LIMIT 10
    """
    rows = await pool.fetch(query, song_title, release_title)
    return [dict(row) for row in rows]


async def resolve_track_artist_fuzzy(
    pool, song_title: str, release_title: str
) -> list[dict]:
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
            WHERE lower(f_unaccent(rt.title)) % lower(f_unaccent($1))
              AND lower(f_unaccent(r.title)) % lower(f_unaccent($2))
        ) sub
        ORDER BY song_sim + release_sim DESC
        LIMIT 10
    """
    rows = await pool.fetch(query, song_title, release_title)
    return [dict(row) for row in rows]


async def resolve_track_artist_song_only(
    pool, song_title: str
) -> list[dict]:
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
        WHERE lower(f_unaccent(rt.title)) % lower(f_unaccent($1))
          AND lower(ra.artist_name) LIKE '%various%'
        LIMIT 10
    """
    rows = await pool.fetch(query, song_title)
    return [dict(row) for row in rows]


async def collect_track_artists_for_release(
    pool, release_title: str
) -> list[dict]:
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
        WHERE lower(f_unaccent(r.title)) = lower(f_unaccent($1))
          AND lower(ra.artist_name) LIKE '%various%'
        ORDER BY rt.sequence
    """
    rows = await pool.fetch(query, release_title)
    return [dict(row) for row in rows]
