"""Shape the pinned-tracklists response (LML#1503).

Pure composition over already-loaded maps: the route does the I/O. Unlike
``identity.bulk_resolve._compose_derived_track_entries`` this needs no resolved
artist name and never drops a track for lacking one.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from entity.release_tracklist import ReleaseTracklistRow
from generated.api_models import PinnedTrack, PinnedTracklist, PinnedTracklistsResult


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
