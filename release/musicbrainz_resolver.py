"""MusicBrainz-cache tracklist resolver.

Tier-3.5 fallback for `/api/v1/lookup`: when the Discogs cascade misses
(``artwork is None``) and the caller has opted into ``extended=true``,
``resolve_tracklist_via_musicbrainz`` looks up an ``(artist, album)`` pair
against the musicbrainz-cache PostgreSQL database and projects the matched
release's tracklist onto the Discogs track-item shape. The result lands on
the synth ``DiscogsSearchResult(release_id=0, ...)`` inline, so
Backend-Service consumes it without learning MB exists (cross-cache-identity
pivot, BS#800).

Returns ``None`` on no match, low similarity, missing ``mb_pg``, blank
input, or any DB error. ``None`` is a valid synth-side value (the BS#1185
sentinel contract treats ``tracklist`` as nullable) so partial degradation
is silent — the picker falls back to its empty-dropdown UX, which is what
it would have shown anyway.

Live MusicBrainz API is intentionally out of scope. The PG cache covers
the residual rotation rows the picker cares about; standing up a new HTTP
client + rate limiter for an estimated handful of rows isn't worth the
ops surface.
"""

from __future__ import annotations

import logging
from typing import Any

from entity.sources import PgSourceProtocol
from generated.api_models import DiscogsTrackItem

logger = logging.getLogger(__name__)

# Matches `CANONICAL_ARTIST_SIMILARITY_FLOOR` in ``lookup/orchestrator.py``.
# False-positive tracklists are worse than no tracklist — the DJ types the
# correct tracks in 10s either way, but a wrong tracklist gets unknowingly
# committed to the flowsheet.
_SIMILARITY_FLOOR: float = 0.70

# Top-1 (artist, album) candidate plus its tracklist in one round trip.
# The artist-credit and release-name % predicates both gate with pg_trgm,
# so the floor is enforced twice: first in PG (any score below the
# similarity-cutoff yields zero rows), then in Python (defensive, in case
# a future PG tunable lowers the % cutoff below our floor). Ordering by
# medium position, then track position, keeps multi-disc releases in
# play order.
_MB_TRACKLIST_FOR_ALBUM_SQL = """\
WITH candidate AS (
    SELECT r.id AS release_id,
           ac.name AS release_artist,
           r.name AS release_title,
           similarity(lower(r.name), lower($2)) AS album_score,
           similarity(lower(ac.name), lower($1)) AS artist_score
    FROM mb_release r
    JOIN mb_artist_credit ac ON ac.id = r.artist_credit
    WHERE lower(r.name) % lower($2)
      AND lower(ac.name) % lower($1)
    ORDER BY album_score DESC, artist_score DESC
    LIMIT 1
)
SELECT c.release_id,
       c.album_score,
       c.artist_score,
       m.position AS medium_position,
       t.position AS position,
       t.name AS title,
       t.length AS length_ms
FROM candidate c
JOIN mb_medium m ON m.release = c.release_id
JOIN mb_track t ON t.medium = m.id
ORDER BY m.position, t.position
"""


def _is_blank(value: str | None) -> bool:
    return value is None or not value.strip()


def _format_duration_ms(length_ms: int | None) -> str | None:
    """Format MB's integer ms length as ``M:SS``.

    Matches the free-text duration Discogs returns on its tracklist items
    (e.g. ``"4:05"``). MB stores ``NULL`` length when a release medium
    didn't include duration metadata — pass through as ``None``.
    """
    if length_ms is None:
        return None
    total_seconds = max(0, int(length_ms)) // 1000
    minutes, seconds = divmod(total_seconds, 60)
    return f"{minutes}:{seconds:02d}"


async def resolve_tracklist_via_musicbrainz(
    artist: str | None,
    album: str | None,
    *,
    mb_pg: PgSourceProtocol | None,
) -> list[DiscogsTrackItem] | None:
    """Look up ``(artist, album)`` in musicbrainz-cache; return its tracklist.

    Returns ``None`` when any of the following hold:
    - ``mb_pg`` is ``None`` (cache not configured)
    - ``artist`` or ``album`` is blank
    - PG returns zero rows (no candidate cleared the trigram cutoff)
    - the winning candidate's artist or album similarity is below
      ``_SIMILARITY_FLOOR``
    - the PG query raises (logged at WARN; never re-raised)
    """
    if mb_pg is None:
        return None
    if _is_blank(artist) or _is_blank(album):
        return None

    try:
        rows: list[dict[str, Any]] = await mb_pg.fetchall(
            _MB_TRACKLIST_FOR_ALBUM_SQL, artist, album
        )
    except Exception as e:
        logger.warning(
            "MusicBrainz tracklist resolver query failed for (%r, %r): %s",
            artist,
            album,
            e,
        )
        return None

    if not rows:
        return None

    first = rows[0]
    album_score = float(first.get("album_score") or 0.0)
    artist_score = float(first.get("artist_score") or 0.0)
    if album_score < _SIMILARITY_FLOOR or artist_score < _SIMILARITY_FLOOR:
        logger.info(
            "MB tracklist candidate for (%r, %r) below floor: artist=%.2f album=%.2f",
            artist,
            album,
            artist_score,
            album_score,
        )
        return None

    return [
        DiscogsTrackItem(
            position=str(row.get("position", "")),
            title=row.get("title") or "",
            duration=_format_duration_ms(row.get("length_ms")),
            artists=[],
        )
        for row in rows
    ]
