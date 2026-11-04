"""Shape the pinned-tracklists response (LML#1503).

Pure composition over already-loaded maps: the route does the I/O. Unlike
``identity.bulk_resolve._compose_derived_track_entries`` this needs no resolved
artist name and never drops a track for lacking one.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from pydantic import ConfigDict, field_validator

from entity.release_tracklist import ReleaseTracklistRow
from generated.api_models import (
    PinnedTrack,
    PinnedTracklist,
    PinnedTracklistsRequest,
    PinnedTracklistsResult,
)

# ``library.legacy_release_id`` (and the override table's ``library_id``) is a
# Postgres INTEGER. An id outside this range cannot be bound as ``int4[]``:
# asyncpg raises a client-side ``DataError`` (an ``InterfaceError``), which the
# route's transient-503 handler would report as a retryable outage forever.
INT4_MIN = -(2**31)
INT4_MAX = 2**31 - 1


class StrictPinnedTracklistsRequest(PinnedTracklistsRequest):
    """The generated request model, tightened to what api.yaml documents.

    The generated ``list[int]`` validates in Pydantic's lax mode, which coerces
    ``"5"``, ``5.0`` and ``true`` into ints, while the contract says a
    non-integer id is a 422. Strict mode rejects all three; the validator
    bounds each id to the INTEGER column's range so an out-of-range id is a
    422 here rather than a permanent 503 at query time. A subclass, so
    ``generated/api_models.py`` stays untouched.
    """

    model_config = ConfigDict(strict=True)

    @field_validator("legacy_release_ids")
    @classmethod
    def _ids_fit_int4(cls, ids: list[int]) -> list[int]:
        for legacy_id in ids:
            if not INT4_MIN <= legacy_id <= INT4_MAX:
                raise ValueError(f"legacy_release_id {legacy_id} is outside the int4 range")
        return ids


def group_tracks(rows: Sequence[ReleaseTracklistRow]) -> list[PinnedTrack]:
    """One ``PinnedTrack`` per sequence: credit-fanned rows share a sequence."""
    by_sequence: dict[int, list[ReleaseTracklistRow]] = {}
    for row in rows:
        by_sequence.setdefault(row.sequence, []).append(row)
    tracks = []
    for sequence, group in sorted(by_sequence.items()):
        names = sorted({n for r in group if (n := (r.credit_artist_name or "").strip())})
        tracks.append(
            PinnedTrack(
                sequence=sequence,
                position=group[0].position or None,
                title=group[0].title or "",
                artist_credits=names,
            )
        )
    return tracks


def build_pinned_tracklists_results(
    requested_ids: Sequence[int],
    pins: Mapping[int, tuple[int, str]],
    tracklists: Mapping[int, Sequence[ReleaseTracklistRow]],
) -> list[PinnedTracklistsResult]:
    """One result per requested id in request order; repeats share their pin.

    Key membership in ``tracklists`` is the discogs-cache presence verdict.
    """
    results = []
    for legacy_id in requested_ids:
        pin = pins.get(legacy_id)
        if pin is None:
            results.append(PinnedTracklistsResult(legacy_release_id=legacy_id, pin=None))
            continue
        release_id, source = pin
        in_cache = release_id in tracklists
        results.append(
            PinnedTracklistsResult(
                legacy_release_id=legacy_id,
                pin=PinnedTracklist(
                    discogs_release_id=release_id,
                    source=source,
                    in_cache=in_cache,
                    tracks=group_tracks(tracklists[release_id]) if in_cache else [],
                ),
            )
        )
    return results
