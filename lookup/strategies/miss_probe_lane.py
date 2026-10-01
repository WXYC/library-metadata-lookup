"""Which lane, if any, opens the step-3a library-miss probe.

Step 3a (``_step_library_miss_probe`` in ``lookup/orchestrator.py``) probes
Discogs directly when the library has nothing that answers the typed
(artist, album). "Nothing" has three shapes, and the lane decides both what a
probe hit does to the rows already in hand and which ``lookup.outcome`` value
the trace carries:

- ``LIBRARY_MISS`` (LML#583): the search pipeline returned no rows. A hit is
  the response. Outcomes ``library_miss_discogs_match`` /
  ``library_miss_no_discogs_match``.
- ``SERVE_BLOCKED`` (LML#1319, songless): rows came back but none can clear
  the serve floor (``fallback_rows_block_serving``). A hit is *additional*
  evidence and the rows stay; the probe runs cache-only. Outcomes
  ``serve_blocked_fallback_discogs_match`` /
  ``serve_blocked_fallback_no_discogs_match``.
- ``FLOOR_BLOCKED`` (LML#1391/#717, song-bearing): rows came back but none
  clears the typed album at all (``song_bearing_fallback_all_floor_failed``).
  A hit *replaces* them. Outcomes ``floor_blocked_fallback_discogs_match`` /
  ``floor_blocked_fallback_no_discogs_match``.

The outcome values are kept distinct per lane because existing
``lookup.outcome`` slices and runbooks key on the classic pair meaning "the
library returned nothing", which is not true on the other two lanes (LML#1319
review).
"""

from enum import StrEnum

from library.models import LibraryItem
from lookup.strategies.library_miss import (
    fallback_rows_block_serving,
    song_bearing_fallback_all_floor_failed,
)
from services.parser import ParsedRequest


class MissProbeLane(StrEnum):
    """A step-3a lane; the value is its ``lookup.outcome`` prefix."""

    LIBRARY_MISS = "library_miss"
    SERVE_BLOCKED = "serve_blocked_fallback"
    FLOOR_BLOCKED = "floor_blocked_fallback"

    def outcome(self, *, matched: bool) -> str:
        """The ``lookup.outcome`` value for this lane's probe hit or miss."""
        return f"{self.value}_discogs_match" if matched else f"{self.value}_no_discogs_match"


def miss_probe_lane(
    parsed: ParsedRequest,
    library_results: list[LibraryItem],
    search_type: str,
) -> MissProbeLane | None:
    """Return the lane ``library_results`` opens, or ``None`` when they answer the request."""
    if not library_results:
        return MissProbeLane.LIBRARY_MISS
    if fallback_rows_block_serving(parsed, library_results, search_type):
        return MissProbeLane.SERVE_BLOCKED
    if song_bearing_fallback_all_floor_failed(parsed, library_results, search_type):
        return MissProbeLane.FLOOR_BLOCKED
    return None
