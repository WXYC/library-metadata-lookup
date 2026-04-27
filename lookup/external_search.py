"""External cache fallback for artist-name lookup.

When the WXYC library catalog returns no results, callers that opt in via
``LookupRequest.include_external_caches`` get a fuzzy artist-name search
against the discogs-cache PostgreSQL DB and, on miss, the musicbrainz-cache
PostgreSQL DB. Used by the lossy-mojibake matcher
(tubafrenzy/scripts/db/recovery/lossy_mojibake_recovery.py) to recover
canonical artist names for skeletons that don't appear in the WXYC physical
catalog.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from discogs.cache_service import CacheUnavailableError, DiscogsCacheService
from scripts.entity_resolution.sources import PgSourceProtocol

logger = logging.getLogger(__name__)

ExternalSource = Literal["discogs", "musicbrainz"]

_MB_ARTIST_FUZZY_SQL = """\
SELECT gid AS id, name,
       similarity(lower(name), lower($1)) AS score
FROM mb_artist
WHERE lower(name) %% lower($1)
ORDER BY score DESC
LIMIT $2\
""".replace("%%", "%")


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
    if not skeleton or not skeleton.strip():
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
