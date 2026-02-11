"""Search strategy pattern for request handling.

This module provides a declarative way to define and execute search strategies.
Each strategy has explicit trigger conditions and can be easily tested in isolation.

Strategies are executed in array order until results are found.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from core.matching import detect_ambiguous_format
from library.db import LibraryDB
from library.models import LibraryItem
from services.parser import ParsedRequest


class SearchStrategyType(StrEnum):
    """Descriptive names for each search strategy.

    These names are used in telemetry to track which strategy succeeded.
    """

    ARTIST_PLUS_ALBUM = "artist_plus_album"
    """Search by artist + album/song name."""

    ARTIST_ONLY = "artist_only"
    """Fallback to just artist name when album/song search fails."""

    SWAPPED_INTERPRETATION = "swapped_interpretation"
    """Try "X - Y" format as both artist/title orderings."""

    TRACK_ON_COMPILATION = "track_on_compilation"
    """Find song on compilation albums via Discogs cross-reference."""

    SONG_AS_ARTIST = "song_as_artist"
    """Fallback: try parsed song as artist when no results and no artist parsed."""

    KEYWORD_MATCH = "keyword_match"
    """Significant word extraction search."""


@dataclass
class SearchState:
    """Tracks state across strategy execution.

    This state is passed to each strategy's condition function to allow
    strategies to make decisions based on previous results.
    """

    results: list[LibraryItem] = field(default_factory=list)
    """Current search results."""

    song_not_found: bool = False
    """True if the exact song/album wasn't found (fell back to artist-only)."""

    found_on_compilation: bool = False
    """True if the song was found on a compilation album."""

    strategies_tried: list[SearchStrategyType] = field(default_factory=list)
    """List of strategies that have been executed."""

    discogs_titles: dict[int, str] = field(default_factory=dict)
    """Map of library item ID to Discogs album title (for artwork lookup)."""

    albums_for_search: list[str] = field(default_factory=list)
    """Album names resolved from Discogs track lookup (may contain multiple)."""


# Type aliases for strategy functions
ConditionFunc = Callable[[ParsedRequest, SearchState, str], bool]
"""Function that returns True if a strategy should be executed.

Args:
    parsed: The parsed request
    state: Current search state
    raw_message: Original request message
"""

ExecuteFunc = Callable[..., Awaitable[tuple[list[LibraryItem], Any]]]
"""Async function that executes the search strategy.

Returns:
    Tuple of (results, metadata). Metadata varies by strategy:
    - ARTIST_PLUS_ALBUM: bool (fallback_used)
    - SWAPPED_INTERPRETATION: None
    - TRACK_ON_COMPILATION: dict (discogs_titles)
"""


@dataclass
class SearchStrategy:
    """Declarative search strategy with explicit trigger condition.

    Strategies are executed in priority order (array position).
    The first strategy that produces results wins.
    """

    name: SearchStrategyType
    """Strategy identifier for telemetry."""

    condition: ConditionFunc
    """Function that returns True if this strategy should run."""

    execute: ExecuteFunc
    """Async function that performs the search."""

    updates_song_not_found: bool = False
    """If True, the strategy's metadata (second return value) updates song_not_found."""

    updates_discogs_titles: bool = False
    """If True, the strategy's metadata contains discogs_titles to merge."""


# =============================================================================
# Strategy Conditions
# =============================================================================


def has_artist_or_album_or_song(
    parsed: ParsedRequest, state: SearchState, raw_message: str
) -> bool:
    """Condition: Has artist OR album OR song to search for."""
    return bool(parsed.artist or state.albums_for_search or parsed.song)


def no_results_and_ambiguous_format(
    parsed: ParsedRequest, state: SearchState, raw_message: str
) -> bool:
    """Condition: No results yet AND message has ambiguous X - Y format."""
    if state.results:
        return False
    return detect_ambiguous_format(raw_message) is not None


def song_not_found_with_artist_and_song(
    parsed: ParsedRequest, state: SearchState, raw_message: str
) -> bool:
    """Condition: Song not found AND we have both artist and song."""
    return state.song_not_found and bool(parsed.artist) and bool(parsed.song)


def no_results_and_song_but_no_artist(
    parsed: ParsedRequest, state: SearchState, raw_message: str
) -> bool:
    """Condition: No results AND parsed song but no artist.

    This handles cases where the AI parser misinterpreted an artist name
    as a song title (e.g., "Laid Back" parsed as song instead of artist).
    """
    return not state.results and bool(parsed.song) and not parsed.artist


# =============================================================================
# Strategy Registry
# =============================================================================


def build_strategies(
    search_library_func: ExecuteFunc,
    search_alternative_func: ExecuteFunc,
    search_compilations_func: ExecuteFunc,
    search_song_as_artist_func: ExecuteFunc | None = None,
) -> list[SearchStrategy]:
    """Build the list of search strategies with injected execute functions.

    This allows the router to inject its own implementations while keeping
    the strategy pattern logic separate.

    Args:
        search_library_func: Function implementing ARTIST_PLUS_ALBUM search
        search_alternative_func: Function implementing SWAPPED_INTERPRETATION search
        search_compilations_func: Function implementing TRACK_ON_COMPILATION search
        search_song_as_artist_func: Function implementing SONG_AS_ARTIST search

    Returns:
        List of SearchStrategy objects in execution order
    """
    strategies = [
        SearchStrategy(
            name=SearchStrategyType.ARTIST_PLUS_ALBUM,
            condition=has_artist_or_album_or_song,
            execute=search_library_func,
            updates_song_not_found=True,
        ),
        SearchStrategy(
            name=SearchStrategyType.SWAPPED_INTERPRETATION,
            condition=no_results_and_ambiguous_format,
            execute=search_alternative_func,
        ),
        SearchStrategy(
            name=SearchStrategyType.TRACK_ON_COMPILATION,
            condition=song_not_found_with_artist_and_song,
            execute=search_compilations_func,
            updates_discogs_titles=True,
        ),
    ]

    # Add SONG_AS_ARTIST if function provided
    if search_song_as_artist_func is not None:
        strategies.append(
            SearchStrategy(
                name=SearchStrategyType.SONG_AS_ARTIST,
                condition=no_results_and_song_but_no_artist,
                execute=search_song_as_artist_func,
            )
        )

    return strategies


async def execute_search_pipeline(
    parsed: ParsedRequest,
    db: LibraryDB,
    raw_message: str,
    strategies: list[SearchStrategy],
    albums_for_search: list[str] | None = None,
) -> SearchState:
    """Execute strategies in array order until results found.

    Args:
        parsed: The parsed request with artist/song/album
        db: Library database for searches
        raw_message: Original request message (for ambiguous format detection)
        strategies: List of search strategies to try
        albums_for_search: Optional list of album names from Discogs lookup

    Returns:
        SearchState with results and metadata about the search
    """
    state = SearchState(
        results=[],
        strategies_tried=[],
        albums_for_search=albums_for_search or [],
    )

    for strategy in strategies:
        # Check if strategy should run
        if not strategy.condition(parsed, state, raw_message):
            continue

        state.strategies_tried.append(strategy.name)

        # Execute the strategy
        if strategy.name == SearchStrategyType.ARTIST_PLUS_ALBUM:
            results, fallback_used = await strategy.execute(db, parsed, state.albums_for_search)
            if results:
                state.results = results
            if strategy.updates_song_not_found and fallback_used:
                state.song_not_found = True

        elif strategy.name == SearchStrategyType.SWAPPED_INTERPRETATION:
            # Parse the ambiguous format
            parts = detect_ambiguous_format(raw_message)
            if parts:
                part1, part2 = parts
                results, _ = await strategy.execute(db, part1, part2)
            else:
                results = []
            if results:
                state.results = results
                state.song_not_found = False

        elif strategy.name == SearchStrategyType.TRACK_ON_COMPILATION:
            results, discogs_titles = await strategy.execute(db, parsed)
            if results:
                state.results = results
                state.found_on_compilation = True
                state.song_not_found = False
                if strategy.updates_discogs_titles:
                    state.discogs_titles = discogs_titles

        elif strategy.name == SearchStrategyType.SONG_AS_ARTIST:
            # Try using the parsed song as an artist name
            results, _ = await strategy.execute(db, parsed.song)
            if results:
                state.results = results
                state.song_not_found = False

        # Stop if we found results (unless we're doing compilation search which can replace results)
        if state.results and strategy.name != SearchStrategyType.TRACK_ON_COMPILATION:
            # For compilation search, we continue even if we have artist-only results
            # because finding the actual song is better than just artist albums
            if not state.song_not_found:
                break

    return state


def get_search_type_from_state(state: SearchState) -> str:
    """Derive the search type string for telemetry from state.

    Args:
        state: The completed search state

    Returns:
        String describing which search type succeeded
    """
    if state.found_on_compilation:
        return "compilation"

    if not state.strategies_tried:
        return "none"

    last_strategy = state.strategies_tried[-1]

    if last_strategy == SearchStrategyType.ARTIST_PLUS_ALBUM:
        return "fallback" if state.song_not_found else "direct"
    elif last_strategy == SearchStrategyType.SWAPPED_INTERPRETATION:
        return "alternative"
    elif last_strategy == SearchStrategyType.TRACK_ON_COMPILATION:
        return "compilation"
    elif last_strategy == SearchStrategyType.SONG_AS_ARTIST:
        return "song_as_artist"

    return "none"
