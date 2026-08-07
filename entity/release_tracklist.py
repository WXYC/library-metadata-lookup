"""Batched discogs-cache tracklist read for LML#1138's derived ``tracks[]``.

The ``kind: single_artist`` arm of ``POST /api/v1/identity/bulk-resolve-libraries``
derives per-track identity **at read time** — no store, no matcher, no backfill
(BS#801 decision D13, option (c)): given a resolved Discogs release id, the
tracklist is a deterministic join against the same ``release_track`` /
``release_track_artist`` tables the SONG_AS_TRACK search path
(``discogs/cache_service.py``) reads today.

This module owns only the tracklist leg of that derivation. The *mapping* leg
(``legacy_release_id`` → ``discogs_release_id``) lives behind
``identity.bulk_resolve.load_single_artist_release_pins`` — see that seam for
the v1 pin-table source and the LML#842 coverage plan.

Read-only by design: these are discogs-cache-owned public tables (per the
WXYC/discogs-etl#288 schema-ownership boundary) and LML never writes them
outside the cache-service write path. PG failures PROPAGATE (no swallowing):
the bulk-resolve router owns the degrade decision — unvisited ``(False, [])``
plus the ``single_artist_track_read_fail_open`` counter — and cannot make it
if a failure is silently mapped to an empty tracklist, which would read as a
genuine "attempted, nothing there" on a pinned release.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from entity.sources import PgSource

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReleaseTracklistRow:
    """One ``release_track`` row, fanned out per extra=0 track credit.

    A track with N main-artist ``release_track_artist`` credits (a split, a
    featured co-credit) appears as N rows sharing ``(release_id, sequence)``;
    a track with no per-track credit — the ordinary case on a single-artist
    release — appears once with ``credit_artist_name = None``. The composer
    (``identity.bulk_resolve._compose_derived_track_entries``) groups by
    sequence, so one physical track always becomes one wire entry.
    """

    release_id: int
    sequence: int
    position: str | None
    title: str | None
    credit_artist_name: str | None


# ``rta.extra = 0`` mirrors the SONG_AS_TRACK read path's main-artist-credit
# constraint (see ``discogs/cache_service.py``'s ``_SEARCH_BY_TRACK_SQL``
# rationale): extra=1 rows are writer/remixer/producer credits (the LML#699
# composer surface), not per-track performer credits, and echoing them as
# ``artist_name`` would misattribute the track. ``ORDER BY`` makes both the
# per-release track order and the multi-credit join order deterministic
# independent of plan/row order.
_SELECT_TRACKLISTS_SQL = """\
SELECT rt.release_id, rt.sequence, rt.position, rt.title,
       rta.artist_name AS credit_artist_name
FROM release_track rt
LEFT JOIN release_track_artist rta
    ON rta.release_id = rt.release_id
    AND rta.track_sequence = rt.sequence
    AND rta.extra = 0
WHERE rt.release_id = ANY($1::int[])
ORDER BY rt.release_id, rt.sequence, rta.artist_name\
"""


async def get_release_tracklists_for_releases(
    pg: PgSource, release_ids: Sequence[int]
) -> list[ReleaseTracklistRow]:
    """ONE query for a whole bulk-resolve batch's pinned Discogs release ids.

    The LML#1138 hard requirement, same as the LML#1021 sibling
    (``entity.compilation_track_identity.get_compilation_track_identity_for_libraries``):
    bulk-resolve batches up to 1,000 inputs, and a per-release tracklist
    query would be exactly the N+1 the ticket forbids. The caller groups the
    flat result by ``release_id`` in memory
    (``identity.bulk_resolve.load_release_tracklists_by_release_id``).

    ``release_ids`` are **Discogs release ids** (the pin targets), never
    library ids of either space — this function has no way to detect a
    caller that gets that wrong, since all three are plain ints.

    An empty ``release_ids`` short-circuits before the round-trip (a batch
    with no pinned single-artist rows is the common case at v1's ~29% pin
    coverage). PG failures propagate — see the module docstring.
    """
    if not release_ids:
        return []
    rows = await pg.fetchall(_SELECT_TRACKLISTS_SQL, list(release_ids))
    return [ReleaseTracklistRow(**row) for row in rows]
