"""SWAPPED_INTERPRETATION — try "X - Y" both ways.

When the raw message has an ambiguous ``X - Y`` / ``X, Y`` / ``X. Y`` shape
and the primary search returned nothing, this strategy searches the
library for both ``X as artist, Y as title`` and ``X as title, Y as artist``
and combines the hits.

Once the artist side is identified, the execute func cross-references the
*other* part as a track against Discogs and narrows to the release that holds
it (LML#622). When that narrowing fires, the second tuple element carries a
``matched_via`` map (library-id → ``TrackMatchHint``) and ``attempt`` reports
an :meth:`Outcome.track_match`; otherwise the second element is empty and the
artist-filtered result is returned via :meth:`Outcome.found` as before.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import ClassVar

from core.search import (
    Outcome,
    SearchState,
    SearchStrategyType,
    detect_ambiguous_format,
    no_results_and_ambiguous_format,
)
from generated.api_models import TrackMatchHint
from library.db import LibraryDB
from library.models import LibraryItem
from services.parser import ParsedRequest

SwappedInterpretationExecute = Callable[
    [LibraryDB, str, str],
    Awaitable[tuple[list[LibraryItem], dict[int, list[TrackMatchHint]]]],
]


@dataclass(frozen=True)
class SwappedInterpretation:
    """Search both ``X as artist, Y as title`` and the reverse for ambiguous formats."""

    name: ClassVar[SearchStrategyType] = SearchStrategyType.SWAPPED_INTERPRETATION

    db: LibraryDB
    execute: SwappedInterpretationExecute
    """Production: ``lookup.orchestrator.search_with_alternative_interpretation``."""

    def should_attempt(self, parsed: ParsedRequest, state: SearchState, raw_message: str) -> bool:
        return no_results_and_ambiguous_format(parsed, state, raw_message)

    async def attempt(self, parsed: ParsedRequest, state: SearchState, raw_message: str) -> Outcome:
        parts = detect_ambiguous_format(raw_message)
        if parts is None:
            # Defensive — the should_attempt predicate already guarantees parts
            # is non-None when this runs. Kept so a future condition change
            # can't silently crash here.
            return Outcome.empty()
        part1, part2 = parts
        items, matched_via = await self.execute(self.db, part1, part2)
        if not items:
            return Outcome.empty()
        # ``matched_via`` is populated only when the track cross-reference
        # narrowed the result (LML#622); legacy execute funcs return ``None``.
        if matched_via:
            return Outcome.track_match(items, matched_via_by_id=matched_via)
        return Outcome.found(items)
