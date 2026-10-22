"""Which lane, if any, opens the step-3a library-miss probe.

Step 3a (``_step_library_miss_probe`` in ``lookup/orchestrator.py``) probes
Discogs directly when the library has nothing that answers the typed
(artist, album). "Nothing" has two shapes, and the lane decides what a probe
hit does to the rows already in hand:

- ``LIBRARY_MISS`` (LML#583): the search pipeline returned no rows. A hit is
  the response.
- ``SERVE_BLOCKED`` (LML#1319, songless): rows came back but none can clear
  the serve floor (``fallback_rows_block_serving``). A hit is *additional*
  evidence and the rows stay; the probe runs cache-only.
"""

from enum import StrEnum

from library.models import LibraryItem
from lookup.strategies.library_miss import fallback_rows_block_serving
from services.parser import ParsedRequest


class MissProbeLane(StrEnum):
    """A step-3a lane."""

    LIBRARY_MISS = "library_miss"
    SERVE_BLOCKED = "serve_blocked_fallback"


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
    return None
