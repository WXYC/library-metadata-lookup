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
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, ClassVar, Protocol

import sentry_sdk

from generated.api_models import TrackMatchHint
from library.models import LibraryItem
from services.parser import ParsedRequest

if TYPE_CHECKING:
    # Type-only import: ``core`` referencing ``lookup`` at runtime would invert the
    # layering and risk an import cycle. ``release_resolution`` is a leaf today, but
    # keeping this reference annotation-only (quoted below) makes the boundary
    # structural, not just a convention defended by a comment.
    from lookup.release_resolution import ResolvedRelease

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


def resolve_positive_int_env(env_var: str, default: int) -> int:
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


SEARCH_API_CALL_CAP_FIRED_STAT_KEY = "search_api_call_cap_fired"
"""Per-request cache-stats key incremented on every LML#543 ``_chunked_gather``
cap-fire. A counter for batch-aggregate PostHog/Sentry telemetry — the runner
uses a *separate* per-task ContextVar (:data:`_cap_fire_count_var` below) for
control-flow propagation so concurrent bulk items don't race through the
shared cache_stats dict."""


_cap_fire_count_var: ContextVar[list[int] | None] = ContextVar("lml_cap_fire_count", default=None)
"""Per-:func:`execute_search_pipeline` cap-fire counter for runner control flow,
isolated from sibling pipeline calls via ``ContextVar.set`` on each entry
(mutates only the current task's context copy). The shared cache_stats counter
keyed by :data:`SEARCH_API_CALL_CAP_FIRED_STAT_KEY` covers batch-aggregate
PostHog/Sentry telemetry; this var covers control flow — two channels by
design. Stored as a single-element ``list[int]`` (mutable box) so the writer
can ``counter[0] += 1`` without re-binding the var, which would lose the
parent reference the runner holds. ``default=None`` so an unset read in a
warm-path or direct-``_chunked_gather`` unit test is structurally a no-op."""


def _record_cap_fire_for_runner() -> None:
    """Bump the per-pipeline cap-fire counter if a runner is active; no-op otherwise."""
    counter = _cap_fire_count_var.get()
    if counter is None:
        return
    counter[0] += 1


def resolve_search_budget_ms() -> int:
    """Return the active search budget in ms, honoring ``LML_SEARCH_BUDGET_MS``.

    See :data:`DEFAULT_SEARCH_BUDGET_MS` for the contract.
    """
    return resolve_positive_int_env(SEARCH_BUDGET_ENV_VAR, DEFAULT_SEARCH_BUDGET_MS)


def resolve_search_hard_timeout_ms() -> int:
    """Return the hard timeout ceiling in ms, honoring ``LML_SEARCH_HARD_TIMEOUT_MS``.

    Unlike :func:`resolve_effective_search_budget_ms`, this resolver is
    env-var-only; callers cannot override the hard cap via HTTP header.
    The hard cap is a safety floor, not a per-request budget — letting
    callers raise it would defeat its purpose. See
    :data:`DEFAULT_SEARCH_HARD_TIMEOUT_MS` for the contract.
    """
    return resolve_positive_int_env(SEARCH_HARD_TIMEOUT_ENV_VAR, DEFAULT_SEARCH_HARD_TIMEOUT_MS)


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

    discogs_titles: "dict[int, ResolvedRelease]" = field(default_factory=dict)
    """Map of library item ID to the resolved Discogs release (for artwork lookup).

    Widened from ``dict[int, str]`` (bare album title) to carry the full
    :class:`~lookup.release_resolution.ResolvedRelease` — release id/url,
    compilation flag, and the album title the seam used to carry. Internal to
    LML; never on the wire.
    """

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

    Populated by SONG_AS_TRACK, and by SWAPPED_INTERPRETATION when its narrowing
    cross-references a track (LML#622) — both run through the shared
    ``_match_track_releases_to_library`` kernel. perform_lookup() reads this when
    building LookupResultItem.matched_via so the API contract carries the
    track→release linkage back to the caller.
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


@dataclass(frozen=True, slots=True)
class Outcome:
    """A strategy's complete declared effect on :class:`SearchState`.

    Returned (not applied) by :meth:`Strategy.attempt` — the runner applies it
    via :func:`_apply`. This is what makes cancellation safety structural: a
    cancelled ``attempt()`` never returns an ``Outcome``, so no commit happens,
    period. The await-then-commit *discipline* from step 1 (#391) becomes a
    *type-enforced* contract here.

    The five named constructors below encode the four real effect-shapes the
    pre-#399 strategy wrappers expressed with heterogeneous ``(list, Any)``
    tuples, plus an explicit no-op:

        - :meth:`empty`            -- nothing to apply
        - :meth:`found`            -- direct match (the 90% case)
        - :meth:`artist_fallback`  -- fell through to artist-only / artist+song
        - :meth:`compilation`      -- TRACK_ON_COMPILATION's full signal
        - :meth:`track_match`      -- SONG_AS_TRACK's per-id hint set

    Field-level documentation lives on :func:`_apply`, which is the single
    write site that consumes these fields.
    """

    items: list[LibraryItem]
    """Library items the strategy produced. Empty list means no-op for results.

    ``Outcome.artist_fallback([])`` is the exception: empty items but
    ``song_not_found_after=True`` — ARTIST_PLUS_ALBUM's "no library hit but
    flag the downstream cascade" signal.
    """

    song_not_found_after: bool | None = None
    """Desired ``state.song_not_found`` value after :func:`_apply`.

    ``None`` means "leave alone". The flag flips for two opposite reasons:
    ``True`` when fallback was used (downstream TRACK_ON_COMPILATION needs to
    know the song wasn't directly matched), ``False`` when the strategy
    actually matched the song (clears any prior fallback signal).
    """

    found_on_compilation_after: bool = False
    """When True, :func:`_apply` sets ``state.found_on_compilation = True``."""

    preserve_prior_results_as_fallback: bool = False
    """The TRACK_ON_COMPILATION stash rule.

    When True AND ``state.results`` AND ``state.song_not_found``, the
    pre-update ``state.results`` is copied to ``state.artist_fallback_results``
    so ``perform_lookup`` can validate the artist fallback against Discogs
    tracklists and merge any confirmed matches back into the final results
    after the compilation hit replaces them.
    """

    discogs_titles: "dict[int, ResolvedRelease] | None" = None
    """Per-id resolved Discogs releases (for artwork lookup). ``None`` = no-op."""

    matched_via_by_id: dict[int, list[TrackMatchHint]] | None = None
    """Per-id track-match hints (catalog-track-search §5.1). ``None`` = no-op."""

    @classmethod
    def empty(cls) -> "Outcome":
        """Strategy ran but produced nothing — no writes to apply."""
        return cls(items=[])

    @classmethod
    def found(cls, items: list[LibraryItem]) -> "Outcome":
        """Direct match where the strategy can vouch for the song.

        Clears ``state.song_not_found`` because the strategy explicitly
        confirmed the song (SWAPPED_INTERPRETATION matched the swapped
        interpretation, SONG_AS_ARTIST cross-referenced Discogs releases,
        etc.). Use this for the 90% case where success means "the song
        was matched."

        For ARTIST_PLUS_ALBUM's direct path — where the artist's album was
        found by name but the song's presence on the album wasn't verified —
        use :meth:`album_match` instead, which leaves ``song_not_found``
        alone so a prior album-resolution signal can still drive
        TRACK_ON_COMPILATION downstream.
        """
        return cls(items=items, song_not_found_after=False)

    @classmethod
    def album_match(cls, items: list[LibraryItem]) -> "Outcome":
        """ARTIST_PLUS_ALBUM's direct path: items found, but no claim about the song.

        ARTIST_PLUS_ALBUM is the only strategy that doesn't independently
        confirm the song — it matches by artist+album text, not by tracklist
        cross-reference. When ``resolve_albums_for_track`` already set
        ``song_not_found=True`` (Discogs returned only VA releases for the
        track), and ARTIST_PLUS_ALBUM then finds the artist's album by name,
        we want to *preserve* that ``song_not_found=True`` signal so
        TRACK_ON_COMPILATION still runs to surface the compilation match.
        ``perform_lookup``'s post-pipeline track_validation step then merges
        the artist's album back in via the artist_fallback_results stash.

        See ``test_compilation_search_when_song_not_found_from_album_resolution``
        and the wider trace in the Adonis / "No Way Back" / Trax 20th case.
        """
        return cls(items=items)

    @classmethod
    def artist_fallback(cls, items: list[LibraryItem]) -> "Outcome":
        """ARTIST_PLUS_ALBUM's fallback path: results came from artist-only
        or artist+song search rather than artist+album.

        ``song_not_found_after=True`` because the song title wasn't directly
        matched. Downstream TRACK_ON_COMPILATION keys on the flag.
        Works with ``items=[]`` too — the flag-only "no library hit but
        downstream cascade should know" signal.
        """
        return cls(items=items, song_not_found_after=True)

    @classmethod
    def compilation(
        cls, items: list[LibraryItem], *, discogs_titles: "dict[int, ResolvedRelease]"
    ) -> "Outcome":
        """TRACK_ON_COMPILATION's full signal.

        Five writes packaged together:
            - new items
            - ``found_on_compilation = True``
            - ``song_not_found = False`` (the song was matched, just on a
              compilation rather than the artist's own release)
            - ``discogs_titles`` for artwork lookup
            - ``preserve_prior_results_as_fallback = True`` so any artist
              fallback that ran first is stashed for post-pipeline validation
        """
        return cls(
            items=items,
            song_not_found_after=False,
            found_on_compilation_after=True,
            preserve_prior_results_as_fallback=True,
            discogs_titles=discogs_titles,
        )

    @classmethod
    def track_match(
        cls,
        items: list[LibraryItem],
        *,
        matched_via_by_id: dict[int, list[TrackMatchHint]],
        discogs_titles: "dict[int, ResolvedRelease] | None" = None,
    ) -> "Outcome":
        """Track-driven match with per-id provenance (SONG_AS_TRACK; also
        SWAPPED_INTERPRETATION's narrowing, LML#622).

        The library row(s) were surfaced by cross-referencing a track against
        Discogs's tracklist index. Each row carries a TrackMatchHint pointing
        back to the release that surfaced it — the API contract uses this for
        the ``matched_via`` field per catalog-track-search §5.1.

        ``discogs_titles`` is the row-less carry-through seam (LML#628): when the
        track resolves to a release with **no** WXYC catalog row, the kernel
        surfaces a single ``LibraryItem(id=0)`` here and carries the validated
        :class:`ResolvedRelease` on ``discogs_titles`` (``{0: ...}``) so
        ``fetch_artwork_for_items`` binds it. Empty / ``None`` on the in-library
        path, which keeps ``matched_via_by_id`` as its only side-channel.
        """
        return cls(
            items=items,
            song_not_found_after=False,
            matched_via_by_id=matched_via_by_id,
            discogs_titles=discogs_titles,
        )


def _apply(state: SearchState, outcome: Outcome) -> None:
    """Apply an :class:`Outcome` to :class:`SearchState`.

    The single write site for strategy-driven SearchState mutations. The runner
    calls this exactly once per strategy attempt (after ``asyncio.wait_for``
    returns successfully); no per-strategy branching needed.

    Two ordering invariants worth pinning:

    1. **Items-empty short-circuit applies to most writes, but not to
       ``song_not_found_after``.** ARTIST_PLUS_ALBUM can return ``([], True)``
       when artist+song falls through to artist-only and the artist isn't in
       the library at all — the flag still needs to propagate so downstream
       TRACK_ON_COMPILATION's condition (``state.song_not_found and ...``)
       evaluates correctly.

    2. **The stash check reads prior ``state.song_not_found`` before the
       update.** TRACK_ON_COMPILATION clears ``song_not_found`` to False as
       part of its outcome (the song WAS matched, just on a compilation), but
       the stash predicate ``state.results and state.song_not_found`` must
       evaluate against the value as it stood when the strategy started. A
       naive single-pass write order would never stash; this function does
       stash first, then writes.
    """
    if not outcome.items:
        # No items → only the flag-only signal (ARTIST_PLUS_ALBUM ``([], True)``
        # case) can propagate. Skip results / found_on_compilation / titles /
        # hints to avoid clobbering prior strategy state with a no-op.
        if outcome.song_not_found_after is not None:
            state.song_not_found = outcome.song_not_found_after
        return

    # Stash check must run BEFORE song_not_found_after is written, because
    # the predicate reads the prior value.
    if outcome.preserve_prior_results_as_fallback and state.results and state.song_not_found:
        state.artist_fallback_results = list(state.results)

    state.results = outcome.items
    if outcome.song_not_found_after is not None:
        state.song_not_found = outcome.song_not_found_after
    if outcome.found_on_compilation_after:
        state.found_on_compilation = True
    if outcome.discogs_titles is not None:
        state.discogs_titles = outcome.discogs_titles
    if outcome.matched_via_by_id is not None:
        state.matched_via_by_id = outcome.matched_via_by_id


class Strategy(Protocol):
    """The runner's strategy seam — concrete implementations live in ``lookup/strategies/``.

    Two methods plus a name:

        - ``name`` (a ``ClassVar``) — telemetry identifier appended to
          ``state.strategies_tried`` before ``attempt`` runs.
        - ``should_attempt(parsed, state, raw_message) -> bool`` — predicate
          gating whether this iteration runs ``attempt``. Pure; cheap; reads
          only the inputs.
        - ``attempt(parsed, state, raw_message) -> Outcome`` — the async body.
          Returns an :class:`Outcome`; **never** writes to ``state`` directly.
          This is what makes cancellation safety structural — see
          :class:`Outcome` for the contract.
    """

    name: ClassVar[SearchStrategyType]
    """Telemetry identifier appended to ``state.strategies_tried``."""

    def should_attempt(
        self, parsed: ParsedRequest, state: SearchState, raw_message: str
    ) -> bool: ...

    async def attempt(
        self, parsed: ParsedRequest, state: SearchState, raw_message: str
    ) -> Outcome: ...


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
# Pipeline runner
# =============================================================================
#
# Step-1 (#391) made the runner generic — no ``strategy.name`` branch — by
# wrapping each execute func in a closure that owned the commit logic.
# Step-2 (#399) deepens it further: the seam now carries a uniform
# :class:`Outcome` value type, and the runner has exactly one
# :func:`_apply` write site to ``SearchState``. The per-strategy commit
# logic moves into the strategy classes themselves
# (``lookup/strategies/``); the runner names no strategy and writes no
# state field individually.


async def execute_search_pipeline(
    parsed: ParsedRequest,
    raw_message: str,
    strategies: list[Strategy],
    albums_for_search: list[str] | None = None,
    song_not_found: bool = False,
    caller_budget_ms: int | None = None,
) -> SearchState:
    """Execute strategies in array order until results found.

    Dispatch is **generic**: the runner calls ``strategy.attempt`` without
    naming any strategy and applies the returned :class:`Outcome` via
    :func:`_apply`. The cross-cutting budget / hard-cap / caller-budget
    machinery and the ``state.timed_out`` projection are unchanged from
    pre-#399.

    Cancellation safety is **structural**: ``attempt()`` returns ``Outcome``
    and cannot mutate ``state`` directly, so a cancelled ``attempt()`` is a
    no-op against state by construction. The step-1 await-then-commit
    *discipline* is replaced by a type-enforced contract.

    Args:
        parsed: The parsed request with artist/song/album.
        raw_message: Original request message (for ambiguous format detection).
        strategies: List of search strategies to try, in execution order.
            Each strategy holds its own dependencies (``db``,
            ``discogs_service``, etc.) as instance fields.
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
    # Per-invocation cap-fire counter for LML#543 propagation. Lives on a
    # ContextVar (not the shared cache_stats dict) so concurrent bulk items
    # don't race. See :data:`_cap_fire_count_var` for the rationale.
    cap_fire_counter: list[int] = [0]
    cap_fire_token = _cap_fire_count_var.set(cap_fire_counter)
    try:
        return await _run_strategy_pipeline(
            state,
            strategies,
            parsed,
            raw_message,
            caller_budget_ms,
            budget_ms,
            hard_cap_ms,
            cap_fire_counter,
        )
    finally:
        _cap_fire_count_var.reset(cap_fire_token)


async def _run_strategy_pipeline(
    state: SearchState,
    strategies: list[Strategy],
    parsed: ParsedRequest,
    raw_message: str,
    caller_budget_ms: int | None,
    budget_ms: int,
    hard_cap_ms: int,
    cap_fire_counter: list[int],
) -> SearchState:
    """Inner loop of :func:`execute_search_pipeline`.

    Extracted purely so the outer function can wrap the per-task ContextVar
    set/reset in a try/finally without indenting the entire cascade. Callers
    use the outer function exclusively.
    """
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

        # Check if strategy should run.
        if not strategy.should_attempt(parsed, state, raw_message):
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
        # Outcome-returning contract (attempt cannot mutate state) makes a
        # cancelled attempt a structural no-op against SearchState — no need
        # for an await-then-commit discipline inside the strategy body.
        strategy_cap_fire_baseline = cap_fire_counter[0]
        try:
            outcome = await asyncio.wait_for(
                strategy.attempt(parsed, state, raw_message),
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

        # The one write site: every per-strategy ``SearchState`` mutation
        # happens here, driven by the outcome's flags. See :func:`_apply`.
        _apply(state, outcome)

        # LML#543 cap-fire propagation. ``_chunked_gather`` bumps the per-task
        # ``cap_fire_counter`` when it bails between chunks. We short-circuit
        # the cascade exactly when the strategy fired the cap AND the natural-
        # completion gate below would NOT have stopped us — so the artist-
        # fallback shape (results + song_not_found) propagates, but a confirmed
        # song match doesn't. See ``LML_SEARCH_MAX_API_CALLS`` in
        # ``docs/env-vars.md`` for the full rationale.
        if cap_fire_counter[0] > strategy_cap_fire_baseline and not (
            state.results and not state.song_not_found
        ):
            state.timed_out = True
            break

        # Stop when results are populated AND we're not still in the
        # song-not-found cascade. The pre-#391 ``strategy.name !=
        # TRACK_ON_COMPILATION`` clause collapsed away in step 1: every
        # downstream strategy already gates on ``not state.results`` in its
        # condition, so the name-check never changed the outcome. With the
        # Outcome seam in place, the check simplifies to a pure state read —
        # ``outcome.items`` would imply ``state.results`` here, so dropping
        # the ``produced`` boolean from step 1 doesn't change semantics.
        if state.results and not state.song_not_found:
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
