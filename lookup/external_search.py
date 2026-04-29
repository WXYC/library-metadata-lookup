"""External cache fallback for artist / album / track lookup.

When the WXYC library catalog returns no results, callers that opt in via
``LookupRequest.include_external_caches`` fall back to fuzzy search against
the discogs-cache PostgreSQL DB and, on miss, the musicbrainz-cache PG DB.

Phase 1.5 covered artist skeletons; Phase 1.7 broadens the fallback so
RELEASE_TITLE / SONG_TITLE skeletons (which dominate the lossy-mojibake
bucket — 631 of the 815 unmatched rows) also get a fair shot.

Each branch returns a sequence of result dicts plus the ``ExternalSource``
that produced them. The orchestrator wraps these into synthetic
``LookupResultItem`` rows so the matcher's existing scoring code applies
unchanged.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from discogs.cache_service import CacheUnavailableError, DiscogsCacheService
from scripts.entity_resolution.sources import PgSourceProtocol

logger = logging.getLogger(__name__)

ExternalSource = Literal["discogs", "musicbrainz"]

# Trigram similarity over mb_artist.name UNION mb_artist_alias.name. Aliases
# carry alternate spellings and ASCII transliterations of accented names
# (e.g. 'Csillagrablok' -> 'Csillagrablók'); skipping them undercuts recall
# on the lossy-mojibake matcher. The canonical mb_artist.name is returned
# even when the trigram hit was against an alias row, so downstream skeleton
# scoring operates on the canonical form.
#
# Both sides of the `%` predicate are wrapped in `lower(f_unaccent(...))` for
# symmetry with the discogs leg (`discogs/cache_service.py:search_artists_by_name`).
# The lossy-mojibake matcher feeds in diacritic-stripped skeletons (`Bjork`,
# `Sigur Ros`); without f_unaccent on the column side, those skeletons would
# miss MB rows whose canonical name carries diacritics (`Björk`, `Sigur Rós`),
# undercutting recall. See WXYC/library-metadata-lookup#194.
#
# Index dependency: this query benefits from expression-trigram indexes on
# `lower(f_unaccent(mb_artist.name))` and `lower(f_unaccent(mb_artist_alias.name))`.
# Without expression indexes, the `%` predicate falls back to a sequential scan.
# Coordinate with WXYC/musicbrainz-cache to ship matching index DDL.
_MB_ARTIST_FUZZY_SQL = """\
SELECT id, name, max(score) AS score FROM (
    SELECT a.gid AS id, a.name,
           similarity(lower(f_unaccent(a.name)), lower(f_unaccent($1))) AS score
    FROM mb_artist a
    WHERE lower(f_unaccent(a.name)) % lower(f_unaccent($1))
    UNION ALL
    SELECT a.gid AS id, a.name,
           similarity(lower(f_unaccent(aa.name)), lower(f_unaccent($1))) AS score
    FROM mb_artist a
    JOIN mb_artist_alias aa ON aa.artist = a.id
    WHERE lower(f_unaccent(aa.name)) % lower(f_unaccent($1))
) sub
GROUP BY id, name
ORDER BY score DESC
LIMIT $2\
"""

# Trigram similarity on mb_release.name with the canonical artist credit
# pulled from mb_artist_credit. Returns the full credit string (e.g.
# "Lupe Fiasco feat. Skylar Grey") rather than picking a single artist —
# the matcher's skeleton scoring uses this as artist context.
_MB_RELEASE_FUZZY_SQL = """\
SELECT id, title, artist, max(score) AS score FROM (
    SELECT r.id AS id, r.name AS title, ac.name AS artist,
           similarity(lower(r.name), lower($1)) AS score
    FROM mb_release r
    JOIN mb_artist_credit ac ON ac.id = r.artist_credit
    WHERE lower(r.name) % lower($1)
) sub
GROUP BY id, title, artist
ORDER BY score DESC
LIMIT $2\
"""

# Trigram similarity on mb_recording.name. Recordings are MB's track-level
# entity; mb_track is per-medium-position and produces duplicates for the
# same recording on multiple releases.
_MB_RECORDING_FUZZY_SQL = """\
SELECT id, title, artist, max(score) AS score FROM (
    SELECT rec.id AS id, rec.name AS title, ac.name AS artist,
           similarity(lower(rec.name), lower($1)) AS score
    FROM mb_recording rec
    JOIN mb_artist_credit ac ON ac.id = rec.artist_credit
    WHERE lower(rec.name) % lower($1)
) sub
GROUP BY id, title, artist
ORDER BY score DESC
LIMIT $2\
"""


def _is_blank(value: str | None) -> bool:
    return value is None or not value.strip()


async def search_external_artists(
    skeleton: str,
    *,
    discogs_cache: DiscogsCacheService | None,
    mb_pg: PgSourceProtocol | None,
    limit: int = 5,
) -> tuple[list[dict[str, Any]], ExternalSource | None]:
    """Fuzzy-match ``skeleton`` against external artist caches.

    Tries discogs-cache first (broadest coverage), then falls through to
    musicbrainz-cache on miss. Each cache is independently optional —
    callers wire ``None`` when the corresponding DSN isn't configured.

    Returns a list of ``{"id", "name"}`` dicts and the source that produced
    them, or ``([], None)`` if neither cache returned a hit. Errors against
    one cache do not block the next.
    """
    if _is_blank(skeleton):
        return [], None

    if discogs_cache is not None:
        try:
            rows = await discogs_cache.search_artists_by_name(skeleton, limit=limit)
            if rows:
                logger.info(
                    "External fallback: discogs cache returned %d artists for %r",
                    len(rows),
                    skeleton,
                )
                return [{"id": r["id"], "name": r["name"]} for r in rows], "discogs"
        except CacheUnavailableError as e:
            logger.warning("Discogs cache unavailable for external fallback: %s", e)
        except Exception as e:  # defensive: any unexpected error shouldn't block MB
            logger.warning("Discogs external-fallback query failed: %s", e)

    if mb_pg is not None:
        try:
            rows = await mb_pg.fetchall(_MB_ARTIST_FUZZY_SQL, skeleton, limit)
            if rows:
                logger.info(
                    "External fallback: musicbrainz cache returned %d artists for %r",
                    len(rows),
                    skeleton,
                )
                return [{"id": r["id"], "name": r["name"]} for r in rows], "musicbrainz"
        except Exception as e:
            logger.warning("MusicBrainz external-fallback query failed: %s", e)

    return [], None


async def search_external_albums(
    skeleton: str,
    *,
    discogs_cache: DiscogsCacheService | None,
    mb_pg: PgSourceProtocol | None,
    limit: int = 5,
) -> tuple[list[dict[str, Any]], ExternalSource | None]:
    """Fuzzy-match a release-title ``skeleton`` against external caches.

    Returns ``[{"id", "artist", "title"}, ...]`` plus the source — the
    ``artist`` is the release's primary credit pulled from the same query,
    so the matcher's skeleton scoring has an artist context even for a
    bare RELEASE_TITLE input.
    """
    if _is_blank(skeleton):
        return [], None

    if discogs_cache is not None:
        try:
            rows = await discogs_cache.search_releases_by_title(skeleton, limit=limit)
            if rows:
                logger.info(
                    "External fallback: discogs cache returned %d releases for %r",
                    len(rows),
                    skeleton,
                )
                return (
                    [{"id": r["id"], "artist": r["artist"], "title": r["title"]} for r in rows],
                    "discogs",
                )
        except CacheUnavailableError as e:
            logger.warning("Discogs cache unavailable for release fallback: %s", e)
        except Exception as e:
            logger.warning("Discogs release fallback query failed: %s", e)

    if mb_pg is not None:
        try:
            rows = await mb_pg.fetchall(_MB_RELEASE_FUZZY_SQL, skeleton, limit)
            if rows:
                logger.info(
                    "External fallback: musicbrainz cache returned %d releases for %r",
                    len(rows),
                    skeleton,
                )
                return (
                    [{"id": r["id"], "artist": r["artist"], "title": r["title"]} for r in rows],
                    "musicbrainz",
                )
        except Exception as e:
            logger.warning("MusicBrainz release fallback query failed: %s", e)

    return [], None


async def search_external_tracks(
    skeleton: str,
    *,
    discogs_cache: DiscogsCacheService | None,
    mb_pg: PgSourceProtocol | None,
    limit: int = 5,
) -> tuple[list[dict[str, Any]], ExternalSource | None]:
    """Fuzzy-match a track-title ``skeleton`` against external caches.

    The lossy bucket is 54% song titles (443 of 815) and the WXYC library
    has no song-level FTS, so this branch is the highest-recall use case
    for the Phase 1.7 widening.
    """
    if _is_blank(skeleton):
        return [], None

    if discogs_cache is not None:
        try:
            rows = await discogs_cache.search_tracks_by_title(skeleton, limit=limit)
            if rows:
                logger.info(
                    "External fallback: discogs cache returned %d tracks for %r",
                    len(rows),
                    skeleton,
                )
                return (
                    [{"id": r["id"], "artist": r["artist"], "title": r["title"]} for r in rows],
                    "discogs",
                )
        except CacheUnavailableError as e:
            logger.warning("Discogs cache unavailable for track fallback: %s", e)
        except Exception as e:
            logger.warning("Discogs track fallback query failed: %s", e)

    if mb_pg is not None:
        try:
            rows = await mb_pg.fetchall(_MB_RECORDING_FUZZY_SQL, skeleton, limit)
            if rows:
                logger.info(
                    "External fallback: musicbrainz cache returned %d recordings for %r",
                    len(rows),
                    skeleton,
                )
                return (
                    [{"id": r["id"], "artist": r["artist"], "title": r["title"]} for r in rows],
                    "musicbrainz",
                )
        except Exception as e:
            logger.warning("MusicBrainz recording fallback query failed: %s", e)

    return [], None
