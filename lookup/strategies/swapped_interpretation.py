"""SWAPPED_INTERPRETATION — try "X - Y" both ways.

When the raw message has an ambiguous ``X - Y`` / ``X, Y`` / ``X. Y`` shape
and the primary search returned nothing, this strategy searches the
library for both ``X as artist, Y as title`` and ``X as title, Y as artist``
and combines the hits. The execute func's second tuple element is unused
(always ``None``); the seam stays uniform with the other strategies.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, ClassVar

from core.search import (
    Outcome,
    SearchState,
    SearchStrategyType,
    detect_ambiguous_format,
    no_results_and_ambiguous_format,
)
from library.db import LibraryDB
from library.models import LibraryItem
from services.parser import ParsedRequest

SwappedInterpretationExecute = Callable[
    [LibraryDB, str, str],
    Awaitable[tuple[list[LibraryItem], Any]],
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
        items, _meta = await self.execute(self.db, part1, part2)
        return Outcome.found(items) if items else Outcome.empty()
