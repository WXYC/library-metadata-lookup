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

import asyncio
import logging
from typing import Any

import sentry_sdk

from entity.sources import PgSourceProtocol
from generated.api_models import DiscogsTrackItem

logger = logging.getLogger(__name__)


def _project_resolver_attrs(*, requested_album: str, returned_album: str | None) -> None:
    """Project requested-vs-returned album titles onto the active Sentry trace.

    LML#506 sizing: the resolver runs ``LIMIT 1`` with a lenient 0.70
    trigram floor, so on Deluxe-vs-Original sibling shapes it can return a
    non-requested album whose tracklist still partially overlaps. The
    downstream song-sanity check catches one cohort (bonus-only-track
    Deluxe leaks); this attribute exposes the population needed to size
    whether the resolver-side Option 2 follow-up (top-K candidates
    filtered by song-presence) is worth shipping.

    Silent on Sentry SDK errors; observability never breaks the resolver.
    """
    try:
        transaction = sentry_sdk.get_current_scope().transaction
        if transaction is not None:
            transaction.set_data("lookup.mb_resolver.requested_album", requested_album)
            transaction.set_data("lookup.mb_resolver.returned_album", returned_album)
    except Exception as e:
        logger.warning("Failed to project mb_resolver attrs onto Sentry transaction: %s", e)


# Matches `CANONICAL_ARTIST_SIMILARITY_FLOOR` in ``lookup/orchestrator.py``.
# Kept duplicated rather than imported to avoid a circular import with the
# orchestrator (which imports this module). Bump both together on calibration.
# False-positive tracklists are worse than no tracklist — the DJ types the
# correct tracks in 10s either way, but a wrong tracklist gets unknowingly
# committed to the flowsheet.
_SIMILARITY_FLOOR: float = 0.70

# Hard ceiling on the PG round trip. The rescue fires inside
# ``enrich_artwork_results``, AFTER the cascade's ``LML_SEARCH_HARD_TIMEOUT_MS``
# already closed; without this, a slow MB query could push the response past
# Backend-Service's 30s ``AbortController`` ceiling and surface as a CORS-less
# 502 to the dj-site picker.
_PG_TIMEOUT_S: float = 2.0

# Top-1 (artist, album) candidate plus its tracklist in one round trip. The
# ``lower(f_unaccent(...))`` wrapping matches the Phase 1.5 mojibake-recovery
# pattern in ``lookup/external_search.py``: diacritic-stripped inputs (e.g.
# "Sigur Ros" against MB's "Sigur Rós") must hit when both sides are normalized.
# Index dependency: expression-trigram indexes on ``lower(f_unaccent(name))``
# on ``mb_release.name`` and ``mb_artist_credit.name`` (mirrors the index
# requirement called out in ``external_search.py``).
_MB_TRACKLIST_FOR_ALBUM_SQL = """\
WITH candidate AS (
    SELECT r.id AS release_id,
           ac.name AS release_artist,
           r.name AS release_title,
           similarity(lower(f_unaccent(r.name)), lower(f_unaccent($2))) AS album_score,
           similarity(lower(f_unaccent(ac.name)), lower(f_unaccent($1))) AS artist_score
    FROM mb_release r
    JOIN mb_artist_credit ac ON ac.id = r.artist_credit
    WHERE lower(f_unaccent(r.name)) % lower(f_unaccent($2))
      AND lower(f_unaccent(ac.name)) % lower(f_unaccent($1))
    ORDER BY album_score DESC, artist_score DESC
    LIMIT 1
)
SELECT c.release_id,
       c.release_title,
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
        rows: list[dict[str, Any]] = await asyncio.wait_for(
            mb_pg.fetchall(_MB_TRACKLIST_FOR_ALBUM_SQL, artist, album),
            timeout=_PG_TIMEOUT_S,
        )
    except TimeoutError:
        logger.warning(
            "MusicBrainz tracklist resolver timed out after %.1fs for (%r, %r)",
            _PG_TIMEOUT_S,
            artist,
            album,
        )
        return None
    except Exception as e:
        logger.warning(
            "MusicBrainz tracklist resolver query failed for (%r, %r): %s",
            artist,
            album,
            e,
        )
        return None

    if not rows:
        _project_resolver_attrs(requested_album=album, returned_album=None)
        return None

    first = rows[0]
    album_score = float(first.get("album_score") or 0.0)
    artist_score = float(first.get("artist_score") or 0.0)
    # Capture before the floor check so the trace explorer sees both rejected
    # and accepted candidates — operator can quantify the "trigram floor
    # cleared but downstream sanity check dropped it" cohort by joining this
    # attribute with ``lookup.mb_rescue.song_sanity_rejected``.
    returned_album_title = first.get("release_title") or ""
    _project_resolver_attrs(requested_album=album, returned_album=returned_album_title)
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
