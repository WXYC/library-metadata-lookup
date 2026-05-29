"""Search strategy pattern for request handling.

This module provides a declarative way to define and execute search strategies.
Each strategy has explicit trigger conditions and can be easily tested in isolation.

Strategies are executed in array order until results are found.
"""

import asyncio
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import sentry_sdk

from generated.api_models import TrackMatchHint
from library.db import LibraryDB
from library.models import LibraryItem
from services.parser import ParsedRequest

logger = logging.getLogger(__name__)

DEFAULT_SEARCH_BUDGET_MS = 4000
"""Wall-clock budget for ``execute_search_pipeline``, in milliseconds.

Leaves ~1s headroom under Backend-Service's current 5s LML runtime timeout
(BS#873). Once WXYC/Backend-Service#876 (single coordinator) lands and the
BS timeout normalizes, this value should move; in the meantime,
``LML_SEARCH_BUDGET_MS`` overrides without a deploy.
"""

SEARCH_BUDGET_ENV_VAR = "LML_SEARCH_BUDGET_MS"

DEFAULT_SEARCH_HARD_TIMEOUT_MS = 25000
"""Hard ceiling on ``execute_search_pipeline`` wall time, in milliseconds.

Unlike :data:`DEFAULT_SEARCH_BUDGET_MS` (which only short-circuits when
``state.results`` is non-empty — by design, so the all-empty cascade keeps
grinding for a better answer), this cap fires *regardless* of results to
bound cascade-exhaustion tail latency. 25 s leaves ~5 s headroom under
Backend-Service's 30 s ``AbortController`` (BS#873). The 2026-05-24
outliers (414 s server-side span on a /lookup BS had already abandoned at
30 s) are the receipt for needing this.

``LML_SEARCH_HARD_TIMEOUT_MS`` overrides without a deploy. To effectively
disable, set the cap well above the request timeout (e.g. 600000).
"""

SEARCH_HARD_TIMEOUT_ENV_VAR = "LML_SEARCH_HARD_TIMEOUT_MS"

TRANSPORT_OVERHEAD_MS = 200
"""Approximate round-trip overhead between LML and a caller (Backend-Service).

When a caller advertises its own remaining budget via ``X-Caller-Budget-Ms``
(A8 / LML#345), LML uses ``caller_budget - TRANSPORT_OVERHEAD_MS`` as the
effective pipeline budget so the response can hit the wire and reach the
caller before the caller's own timeout fires. 200 ms covers a typical
loopback-to-Railway-edge round-trip with margin; if measurements ever show
a tighter bound, narrow this constant rather than the caller-side header.
"""


def _resolve_positive_int_env(env_var: str, default: int) -> int:
    """Read a positive integer from ``env_var``, falling back to ``default`` with a WARN.

    Shared by :func:`resolve_search_budget_ms` and
    :func:`resolve_search_hard_timeout_ms`. Operator typos must not 500 every
    /lookup, so unparseable, zero, and negative values all fall back. Zero
    is treated as a misconfiguration alongside negatives: with the value
    plumbed into ``elapsed_ms > threshold`` checks, ``threshold=0`` fires
    on the first iteration after any await — almost certainly not what the
    operator intended. To effectively disable a gate, set the value far
    above the request timeout (e.g. ``600000``).

    Read per-call (not at import) so tests can monkeypatch the env var
    without reaching into module state.
    """
    raw = os.environ.get(env_var)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("%s=%r is not an integer; falling back to %d", env_var, raw, default)
        return default
    if value <= 0:
        logger.warning("%s=%d is not positive; falling back to %d", env_var, value, default)
        return default
    return value


def resolve_search_budget_ms() -> int:
    """Return the active search budget in ms, honoring ``LML_SEARCH_BUDGET_MS``.

    See :data:`DEFAULT_SEARCH_BUDGET_MS` for the contract.
    """
    return _resolve_positive_int_env(SEARCH_BUDGET_ENV_VAR, DEFAULT_SEARCH_BUDGET_MS)


def resolve_search_hard_timeout_ms() -> int:
    """Return the hard timeout ceiling in ms, honoring ``LML_SEARCH_HARD_TIMEOUT_MS``.

    Unlike :func:`resolve_effective_search_budget_ms`, this resolver is
    env-var-only; callers cannot override the hard cap via HTTP header.
    The hard cap is a safety floor, not a per-request budget — letting
    callers raise it would defeat its purpose. See
    :data:`DEFAULT_SEARCH_HARD_TIMEOUT_MS` for the contract.
    """
    return _resolve_positive_int_env(SEARCH_HARD_TIMEOUT_ENV_VAR, DEFAULT_SEARCH_HARD_TIMEOUT_MS)


def resolve_effective_search_budget_ms(caller_budget_ms: int | None) -> int:
    """Return the effective pipeline budget given an optional caller-supplied budget.

    Combines the env-var/default budget (:func:`resolve_search_budget_ms`) with
    the caller's ``X-Caller-Budget-Ms`` header (A8 / LML#345). The contract:

    - Caller header absent or non-positive → return the env-var budget (the A3
      contract is unchanged for callers that don't opt in).
    - Caller header below ``TRANSPORT_OVERHEAD_MS`` → treat as misconfiguration
      (effective budget would be ≤ 0 and the gate would short-circuit the first
      iteration). Fall back to the env-var budget.
    - Caller header otherwise → effective = min(caller − overhead, env). The
      ``min`` clamps so callers cannot claim more time than the operator has
      authorized via the env var.

    The transport-overhead subtraction is a one-way courtesy: LML stops slightly
    before the caller would, so the response can hit the wire in time. See
    :data:`TRANSPORT_OVERHEAD_MS` for the rationale.
    """
    env_budget = resolve_search_budget_ms()
    if caller_budget_ms is None or caller_budget_ms <= 0:
        return env_budget
    caller_effective = caller_budget_ms - TRANSPORT_OVERHEAD_MS
    if caller_effective <= 0:
        # A caller asking for a budget below the transport overhead is
        # effectively asking for zero or negative. The pipeline can't run
        # meaningfully with a non-positive budget; fall back to env so the
        # request stays alive rather than short-circuiting at strategy 1.
        logger.warning(
            "caller_budget_ms=%d is below TRANSPORT_OVERHEAD_MS=%d; falling back to env budget %d",
            caller_budget_ms,
            TRANSPORT_OVERHEAD_MS,
            env_budget,
        )
        return env_budget
    return min(caller_effective, env_budget)


def _log_search_budget_exceeded(
    *, elapsed_ms: float, skipped: list["SearchStrategyType"], budget_ms: int
) -> None:
    """Project a budget breach onto the active Sentry transaction.

    Mirrors ``_log_album_title_fallback`` / ``_log_resolver_pre_pass`` in
    ``lookup/orchestrator``: structured INFO log line plus two ``set_data``
    keys on the active transaction so trace explorer can filter on
    ``search_budget_exceeded:true`` without re-pulling Railway logs.

    No-op when there is no active transaction. Any SDK error is swallowed so
    observability cannot break /lookup.
    """
    payload = {
        "elapsed_ms": round(elapsed_ms, 2),
        "budget_ms": budget_ms,
        "skipped": [s.value for s in skipped],
    }
    logger.info("search_budget_exceeded %s", payload)
    try:
        transaction = sentry_sdk.get_current_scope().transaction
        if transaction is None:
            return
        transaction.set_data("search_budget_exceeded", True)
        transaction.set_data("search_strategies_skipped", payload["skipped"])
    except Exception as e:
        logger.warning("Failed to project search_budget_exceeded onto Sentry transaction: %s", e)


def _log_hard_cap_fired(
    *, elapsed_ms: float, skipped: list["SearchStrategyType"], hard_cap_ms: int
) -> None:
    """Project a hard-cap breach onto the active Sentry transaction (LML#370).

    Sibling of :func:`_log_search_budget_exceeded`. The hard cap fires when
    the cascade has spent more wall time than the safety floor allows,
    regardless of ``state.results``. ``hard_cap_fired:true`` on the
    trace lets Sentry trace explorer filter cap-firing requests without
    re-pulling Railway logs.

    No-op when there is no active transaction. Any SDK error is swallowed
    so observability cannot break /lookup.
    """
    payload = {
        "elapsed_ms": round(elapsed_ms, 2),
        "hard_cap_ms": hard_cap_ms,
        "skipped": [s.value for s in skipped],
    }
    logger.info("hard_cap_fired %s", payload)
    try:
        transaction = sentry_sdk.get_current_scope().transaction
        if transaction is None:
            return
        transaction.set_data("hard_cap_fired", True)
        transaction.set_data("hard_cap_skipped_strategies", payload["skipped"])
        transaction.set_data("hard_cap_elapsed_ms", payload["elapsed_ms"])
    except Exception as e:
        logger.warning("Failed to project hard_cap_fired onto Sentry transaction: %s", e)


def detect_ambiguous_format(raw_message: str) -> tuple[str, str] | None:
    """Detect if message has ambiguous 'X - Y', 'X, Y', or 'X. Y' format.

    These formats are ambiguous because they could be interpreted as either:
    - Artist: X, Title: Y
    - Title: X, Artist: Y

    Args:
        raw_message: The original request message

    Returns:
        Tuple of (part1, part2) if ambiguous format detected, None otherwise.
    """
    # Check for "X - Y" pattern with various spacing around dash
    # Matches: "X - Y", "X- Y", "X -Y" (requires at least one space to avoid "hip-hop")
    dash_match = re.search(r"(.+?)\s*-\s+(.+)|(.+?)\s+-\s*(.+)", raw_message)
    if dash_match:
        # Groups 1,2 for "X- Y" pattern, groups 3,4 for "X -Y" pattern
        if dash_match.group(1) and dash_match.group(2):
            part1, part2 = dash_match.group(1).strip(), dash_match.group(2).strip()
        else:
            part1, part2 = dash_match.group(3).strip(), dash_match.group(4).strip()
        if part1 and part2:
            return (part1, part2)

    # Check for "X, Y" pattern (comma separator)
    if "," in raw_message:
        parts = raw_message.split(",", 1)
        if len(parts) == 2 and parts[0].strip() and parts[1].strip():
            return (parts[0].strip(), parts[1].strip())

    # Check for "X. Y" pattern (period followed by space)
    if ". " in raw_message:
        parts = raw_message.split(". ", 1)
        if len(parts) == 2 and parts[0].strip() and parts[1].strip():
            return (parts[0].strip(), parts[1].strip())

    return None


class SearchStrategyType(StrEnum):
    """Descriptive names for each search strategy.

    These names are used in telemetry to track which strategy succeeded.
    """

    ARTIST_PLUS_ALBUM = "artist_plus_album"
    """Search by artist + album/song name."""

    ARTIST_ONLY = "artist_only"
    """Fallback to just artist name when album/song search fails."""

    SWAPPED_INTERPRETATION = "swapped_interpretation"
    """Try "X - Y", "X, Y", or "X. Y" format as both artist/title orderings."""

    TRACK_ON_COMPILATION = "track_on_compilation"
    """Find song on compilation albums via Discogs cross-reference."""

    SONG_AS_ARTIST = "song_as_artist"
    """Fallback: try parsed song as artist when no results and no artist parsed."""

    SONG_AS_TRACK = "song_as_track"
    """Fallback: cross-reference the parsed song against Discogs and match releases
    back to the WXYC library. Fires for song-only inputs after SONG_AS_ARTIST returns
    empty. Load-bearing for catalog-track-search (plan §4.2)."""

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

    artist_fallback_results: list[LibraryItem] = field(default_factory=list)
    """Artist-only fallback results saved before TRACK_ON_COMPILATION replaces them.

    When compilation search replaces the previous artist-only results, the originals
    are preserved here so perform_lookup can validate them against Discogs tracklists
    and merge any confirmed matches (e.g., the artist's own album containing the track)
    back into the final results.
    """

    matched_via_by_id: dict[int, list[TrackMatchHint]] = field(default_factory=dict)
    """Per-library-id provenance for track-driven matches (catalog-track-search §5.1).

    Populated by SONG_AS_TRACK when a track-title cross-reference surfaces a library
    row. perform_lookup() reads this when building LookupResultItem.matched_via so
    the API contract carries the track→release linkage back to the caller.
    """

    timed_out: bool = False
    """True when the LML#370 hard cap fired and the pipeline was abandoned.

    Projected into ``LookupResponse.timeout`` so callers can distinguish
    "no match" (empty ``results``, ``timed_out: False``) from "ran out of
    time" (``results`` may be empty, ``timed_out: True``).
    """


# Type aliases for strategy functions
ConditionFunc = Callable[[ParsedRequest, SearchState, str], bool]
"""Function that returns True if a strategy should be executed.

Args:
    parsed: The parsed request
    state: Current search state
    raw_message: Original request message
"""

ExecuteFunc = Callable[..., Awaitable[tuple[list[LibraryItem], Any]]]
"""Async function executed by a strategy's ``run`` wrapper.

Returns a tuple whose second element shape varies per strategy:

    - ``ARTIST_PLUS_ALBUM``: ``bool`` (fallback_used)
    - ``SWAPPED_INTERPRETATION``: ``None``
    - ``TRACK_ON_COMPILATION``: ``dict[int, str]`` (discogs_titles)
    - ``SONG_AS_ARTIST``: ``None``
    - ``SONG_AS_TRACK``: ``dict[int, list[TrackMatchHint]]`` (matched_via_by_id)

The heterogeneous shape is the artifact step 2 (#399) replaces with a uniform
``Outcome`` value type. In step 1 it lives behind each strategy's ``run``
wrapper so the central dispatch loop never sees it.
"""

RunFunc = Callable[[LibraryDB, ParsedRequest, "SearchState", str], Awaitable[bool]]
"""Async strategy body invoked by ``execute_search_pipeline``.

Contract:
    1. Do **all** awaits before any write to ``SearchState``. The runner wraps
       ``run`` in ``asyncio.wait_for`` (the LML#370 hard cap); a cancellation
       between the last await and the final state mutation would leave the
       state half-updated. Await-then-commit makes a cancelled ``run`` a
       structural no-op.
    2. Return ``True`` if this strategy populated ``state.results``; ``False``
       otherwise. The runner combines the return with the current
       ``state.results`` / ``state.song_not_found`` to decide whether to
       break the cascade.
"""


@dataclass(frozen=True)
class _Strategy:
    """Strategy seam consumed by :func:`execute_search_pipeline`.

    Built by :func:`build_strategies` from an injected ``ExecuteFunc`` plus the
    per-strategy condition predicate; each strategy's ``run`` closure owns the
    commit logic that used to live in the runner's ``if/elif`` arm.

    Underscore-prefixed because callers should not construct these directly —
    they're an implementation detail of the runner's seam. Tests that need a
    bespoke strategy (e.g. ``TestPerStrategyWaitFor``) bypass
    ``build_strategies`` and construct ``_Strategy`` directly, which is the
    one accepted external use.
    """

    name: SearchStrategyType
    """Strategy identifier for telemetry (``strategies_tried`` log)."""

    condition: ConditionFunc
    """Predicate gating whether ``run`` is called this iteration."""

    run: RunFunc
    """Async body — see :data:`RunFunc` for the cancellation contract."""


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


def no_results_and_song_but_no_artist_track_fallback(
    parsed: ParsedRequest, state: SearchState, raw_message: str
) -> bool:
    """Condition: SONG_AS_ARTIST already ran and produced nothing.

    Fires after the song-as-artist interpretation has failed, then treats the
    song as a *track* and cross-references against Discogs to find releases
    that contain it. Strictly downstream of SONG_AS_ARTIST so the
    parser-misread-artist case keeps first-crack ordering.
    """
    return (
        not state.results
        and bool(parsed.song)
        and not parsed.artist
        and SearchStrategyType.SONG_AS_ARTIST in state.strategies_tried
    )


# =============================================================================
# Strategy Registry
# =============================================================================


def build_strategies(
    search_library_func: ExecuteFunc,
    search_alternative_func: ExecuteFunc,
    search_compilations_func: ExecuteFunc,
    search_song_as_artist_func: ExecuteFunc | None = None,
    search_song_as_track_func: ExecuteFunc | None = None,
) -> list[_Strategy]:
    """Build the list of search strategies with injected execute functions.

    Wraps each ``ExecuteFunc`` into a :class:`_Strategy` whose ``run`` closure
    owns the per-strategy commit to :class:`SearchState`. The five wrappers
    encode the contract that used to live in the runner's ``if/elif`` arms:
    each one awaits first, then commits, then returns the ``produced`` bool.

    Args:
        search_library_func: Function implementing ARTIST_PLUS_ALBUM search.
            Returns ``(list, fallback_used)``; the wrapper writes
            ``state.results`` when non-empty and ``state.song_not_found``
            when the fallback flag fires.
        search_alternative_func: Function implementing SWAPPED_INTERPRETATION
            search. The wrapper extracts ``(part1, part2)`` from the raw
            message via :func:`detect_ambiguous_format`.
        search_compilations_func: Function implementing TRACK_ON_COMPILATION
            search. The wrapper stashes the prior artist-fallback results
            into ``state.artist_fallback_results`` before replacing
            ``state.results``, so perform_lookup can validate them against
            Discogs tracklists afterward.
        search_song_as_artist_func: Function implementing SONG_AS_ARTIST search.
            Optional; when ``None``, the strategy is omitted from the list.
        search_song_as_track_func: Function implementing SONG_AS_TRACK search
            (catalog-track-search §4.2). Fires strictly after SONG_AS_ARTIST.
            Optional; when ``None``, the strategy is omitted.

    Returns:
        List of ``_Strategy`` objects in execution order. The runner consumes
        them generically — see :class:`_Strategy` for the seam contract.
    """

    async def run_artist_plus_album(
        db: LibraryDB,
        parsed: ParsedRequest,
        state: SearchState,
        raw_message: str,  # noqa: ARG001
    ) -> bool:
        results, fallback_used = await search_library_func(db, parsed, state.albums_for_search)
        # Commit only after the await returns — cancellation between here and
        # the wait_for raise is a structural no-op because no state was written.
        if results:
            state.results = results
        if fallback_used:
            state.song_not_found = True
        return bool(results)

    async def run_swapped_interpretation(
        db: LibraryDB,
        parsed: ParsedRequest,  # noqa: ARG001
        state: SearchState,
        raw_message: str,
    ) -> bool:
        parts = detect_ambiguous_format(raw_message)
        if parts is None:
            # Defensive — the SWAPPED condition already guarantees parts is non-None
            # when this runs. Kept so a future condition change can't silently
            # crash here.
            return False
        part1, part2 = parts
        results, _meta = await search_alternative_func(db, part1, part2)
        if results:
            state.results = results
            state.song_not_found = False
        return bool(results)

    async def run_track_on_compilation(
        db: LibraryDB,
        parsed: ParsedRequest,
        state: SearchState,
        raw_message: str,  # noqa: ARG001
    ) -> bool:
        results, discogs_titles = await search_compilations_func(db, parsed)
        if not results:
            return False
        # Save artist-fallback results before replacing — perform_lookup will
        # validate them against Discogs tracklists and merge any confirmed
        # matches back into the final results. The condition for this stash
        # (state.results AND state.song_not_found) was previously inlined in
        # the runner's TRACK_ON_COMPILATION arm.
        if state.results and state.song_not_found:
            state.artist_fallback_results = list(state.results)
        state.results = results
        state.found_on_compilation = True
        state.song_not_found = False
        state.discogs_titles = discogs_titles
        return True

    async def run_song_as_artist(
        db: LibraryDB,
        parsed: ParsedRequest,
        state: SearchState,
        raw_message: str,  # noqa: ARG001
    ) -> bool:
        assert search_song_as_artist_func is not None  # type narrowing — see closure capture
        results, _meta = await search_song_as_artist_func(db, parsed.song)
        if results:
            state.results = results
            state.song_not_found = False
        return bool(results)

    async def run_song_as_track(
        db: LibraryDB,
        parsed: ParsedRequest,
        state: SearchState,
        raw_message: str,  # noqa: ARG001
    ) -> bool:
        assert search_song_as_track_func is not None
        results, matched_via_by_id = await search_song_as_track_func(db, parsed.song)
        if results:
            state.results = results
            state.song_not_found = False
            state.matched_via_by_id = matched_via_by_id
        return bool(results)

    strategies: list[_Strategy] = [
        _Strategy(
            name=SearchStrategyType.ARTIST_PLUS_ALBUM,
            condition=has_artist_or_album_or_song,
            run=run_artist_plus_album,
        ),
        _Strategy(
            name=SearchStrategyType.SWAPPED_INTERPRETATION,
            condition=no_results_and_ambiguous_format,
            run=run_swapped_interpretation,
        ),
        _Strategy(
            name=SearchStrategyType.TRACK_ON_COMPILATION,
            condition=song_not_found_with_artist_and_song,
            run=run_track_on_compilation,
        ),
    ]

    if search_song_as_artist_func is not None:
        strategies.append(
            _Strategy(
                name=SearchStrategyType.SONG_AS_ARTIST,
                condition=no_results_and_song_but_no_artist,
                run=run_song_as_artist,
            )
        )

    # SONG_AS_TRACK must come AFTER SONG_AS_ARTIST — its condition checks that
    # SONG_AS_ARTIST already ran and produced nothing. Ordering is array
    # position; the condition does the runtime cross-check.
    if search_song_as_track_func is not None:
        strategies.append(
            _Strategy(
                name=SearchStrategyType.SONG_AS_TRACK,
                condition=no_results_and_song_but_no_artist_track_fallback,
                run=run_song_as_track,
            )
        )

    return strategies


async def execute_search_pipeline(
    parsed: ParsedRequest,
    db: LibraryDB,
    raw_message: str,
    strategies: list[_Strategy],
    albums_for_search: list[str] | None = None,
    song_not_found: bool = False,
    caller_budget_ms: int | None = None,
) -> SearchState:
    """Execute strategies in array order until results found.

    Dispatch is **generic**: the runner calls ``strategy.run`` without naming
    any strategy. Each ``run`` is a closure built by :func:`build_strategies`
    that owns the per-strategy commit to ``SearchState`` (see :data:`RunFunc`
    for the cancellation contract). The cross-cutting budget / hard-cap /
    caller-budget machinery and the ``state.timed_out`` projection are
    unchanged from pre-#391.

    Args:
        parsed: The parsed request with artist/song/album.
        db: Library database for searches.
        raw_message: Original request message (for ambiguous format detection).
        strategies: List of search strategies to try, in execution order.
        albums_for_search: Optional list of album names from Discogs lookup.
        song_not_found: Whether album resolution already determined the song
            wasn't found.
        caller_budget_ms: Optional X-Caller-Budget-Ms header value
            (LML#345). When set, also gates the empty-results tail
            short-circuit (LML#347).

    Returns:
        SearchState with results and metadata about the search.
    """
    state = SearchState(
        results=[],
        strategies_tried=[],
        albums_for_search=albums_for_search or [],
        song_not_found=song_not_found,
    )

    budget_ms = resolve_effective_search_budget_ms(caller_budget_ms)
    hard_cap_ms = resolve_search_hard_timeout_ms()
    # Project the caller-budget value when present so Sentry trace explorer can
    # split header-driven vs env-driven cutoffs (A8 / LML#345). The
    # effective-budget computation already clamps to env, so we record the raw
    # header value the caller sent — that's the diagnostic that matters
    # (mismatched expectations between caller and LML).
    if caller_budget_ms is not None:
        try:
            transaction = sentry_sdk.get_current_scope().transaction
            if transaction is not None:
                transaction.set_data("lml.caller_budget_ms", caller_budget_ms)
        except Exception as e:
            logger.warning("Failed to project lml.caller_budget_ms onto Sentry transaction: %s", e)
    start = time.monotonic()

    for idx, strategy in enumerate(strategies):
        elapsed_ms = (time.monotonic() - start) * 1000

        # Hard cap (LML#370). Fires regardless of state.results — the safety
        # floor that bounds cascade-exhaustion tail latency. Layered ABOVE
        # the soft-budget gate so that when both would fire, we record the
        # cap (the more severe condition) and skip the budget telemetry.
        if elapsed_ms > hard_cap_ms:
            state.timed_out = True
            _log_hard_cap_fired(
                elapsed_ms=elapsed_ms,
                skipped=[s.name for s in strategies[idx:]],
                hard_cap_ms=hard_cap_ms,
            )
            break

        # Soft-budget gate (LML#340). Two clauses, both load-bearing:
        #   1. elapsed_ms > budget — we've spent more time than the caller is
        #      willing to wait.
        #   2. state.results is non-empty — we have *something* to return.
        # When the second clause is false we keep grinding: the user gets
        # "nothing" either way, and the next strategy might surface results.
        # The hard cap above catches the keep-grinding case when it grinds
        # too long.
        if elapsed_ms > budget_ms and state.results:
            _log_search_budget_exceeded(
                elapsed_ms=elapsed_ms,
                skipped=[s.name for s in strategies[idx:]],
                budget_ms=budget_ms,
            )
            break

        # Caller-budget gate (LML#347 — Epic A no-results tail follow-up).
        # An explicit X-Caller-Budget-Ms header is the caller saying "I will
        # discard whatever arrives after my deadline." LML continuing past
        # that deadline is wasted Discogs quota for an answer the caller
        # already abandoned — see WXYC/library-metadata-lookup#337's
        # Rita Villa / Fly Girlz tail (20+ s with empty results).
        # Distinct from the env safety branch above: this gate fires when
        # `state.results` is empty AND the caller opted in. Without a
        # header, the safety branch above keeps the keep-grinding contract
        # intact for warm-cache / write-path callers.
        if caller_budget_ms is not None and elapsed_ms > budget_ms and not state.results:
            state.timed_out = True
            _log_search_budget_exceeded(
                elapsed_ms=elapsed_ms,
                skipped=[s.name for s in strategies[idx:]],
                budget_ms=budget_ms,
            )
            break

        # Check if strategy should run
        if not strategy.condition(parsed, state, raw_message):
            continue

        state.strategies_tried.append(strategy.name)

        # Per-strategy ceiling. Caps the *currently-running* strategy at the
        # remaining hard-cap budget, so a single slow Discogs cascade can't
        # blow the loop-level gate (which only fires between strategies).
        # The 0.01s floor avoids a degenerate wait_for(timeout=0) when
        # elapsed_ms has just crossed hard_cap_ms due to scheduling jitter —
        # the wait_for will TimeoutError immediately and the outer except
        # records the strategy as timed out, same end state as a real cap fire.
        remaining_budget_seconds = max(0.01, (hard_cap_ms - elapsed_ms) / 1000)

        # Single try/except around the strategy invocation so the per-strategy
        # wait_for ceiling fires uniformly. CancelledError → TimeoutError raised
        # by wait_for propagates into in-flight asyncio.gather() probes inside
        # the strategy, freeing the Discogs semaphore on cap-fire. The
        # RunFunc contract (await-then-commit) makes a cancelled run a
        # structural no-op against SearchState.
        try:
            produced = await asyncio.wait_for(
                strategy.run(db, parsed, state, raw_message),
                timeout=remaining_budget_seconds,
            )
        except TimeoutError:
            state.timed_out = True
            _log_hard_cap_fired(
                elapsed_ms=(time.monotonic() - start) * 1000,
                skipped=[s.name for s in strategies[idx + 1 :]],
                hard_cap_ms=hard_cap_ms,
            )
            break

        # Stop when this strategy populated results AND we're not still in the
        # song-not-found cascade. The pre-#391 ``strategy.name !=
        # TRACK_ON_COMPILATION`` clause collapses away: every downstream
        # strategy already gates on ``not state.results`` in its condition, so
        # the name-check never changed the outcome. ``produced`` separates
        # "this strategy added results" from "results carried over from a
        # previous strategy" — keeps the contract legible even though
        # ``state.results`` would also be True in the well-behaved case.
        if produced and state.results and not state.song_not_found:
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
    elif last_strategy == SearchStrategyType.SONG_AS_TRACK:
        # api.yaml v1.3.0 SearchType has no `song_as_track` value. Precise
        # provenance lives on matched_via.source per plan §5.1; from the
        # caller's lens, track-driven matches are compilation-class.
        return "compilation"

    return "none"
