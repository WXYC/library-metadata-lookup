"""Lookup orchestrator: the core search logic extracted from request-o-matic.

This module contains the perform_lookup() function that orchestrates the full
search pipeline: artist correction -> album resolution -> search strategies ->
track validation -> artwork fetch -> metadata enrichment -> context message.

Post-decomposition (LML#722) this module is the pipeline *spine* only: a
mutable :class:`LookupState` threaded through named ``_step_*`` functions, plus
the spine-scoped helpers (``resolve_albums_for_track``,
``build_context_message``, ``_identity_to_reconciled``,
``_resolve_identities``). Every concern the steps delegate to lives in its own
``lookup/`` module.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from functools import partial

import sentry_sdk
from wxyc_etl.text import to_match_form as normalize_for_comparison
from wxyc_fastapi.observability import (
    RequestTelemetry,
)

from clients.bandcamp import BandcampClient
from clients.streaming.apple_music import AppleMusicClient
from clients.streaming.spotify import SpotifyClient
from config.settings import get_settings
from core.event_loop_lag import get_event_loop_lag_ms
from core.exceptions import BreakerOpenError
from core.search import (
    SEARCH_TYPE_NONE,
    execute_search_pipeline,
    get_search_type_from_state,
)
from discogs.cache_service import DiscogsCacheService
from discogs.lookup import lookup_releases_by_track
from discogs.models import (
    DiscogsSearchResult,
)
from discogs.ratelimit import is_discogs_low_priority
from discogs.service import DiscogsService
from entity.library_release_override import get_library_release_overrides
from entity.sources import PgSource, PgSourceProtocol
from entity.store import EntityStore, Identity
from generated.api_models import (
    DegradedReason,
    ReconciledIdentity,
    TrackMatchHint,
)
from library.db import LibraryDB
from library.models import LibraryItem
from lookup.admission import evaluate_admission_shed
from lookup.artwork import fetch_artwork_for_items
from lookup.candidate_memo import TrackCandidateMemo
from lookup.enrichment import enrich_artwork_results
from lookup.external_search import (
    build_external_catalog_item,
    search_external_fallback,
)
from lookup.location_union import (
    ResolvedLocation,
    build_location_result_items,
    primary_library_ids_from_results,
    resolve_track_shelf_locations,
    should_run_location_union,
)
from lookup.matching import (
    WAVE_A_SEARCH_LIMIT,
    library_artist_for,
    limit_results,
)
from lookup.models import LookupRequest, LookupResponse, LookupResultItem
from lookup.release_resolution import (
    ResolvedRelease,
)
from lookup.spine_deadline import (
    SpineDeadline,
    SpineDeadlineExceededError,
    project_spine_deadline_telemetry,
    run_within_spine_deadline,
)
from lookup.strategies import build_strategies
from lookup.strategies.artist_plus_album import search_library_with_fallback
from lookup.strategies.library_miss import _library_miss_discogs_search
from lookup.strategies.song_as_artist import search_song_as_artist
from lookup.strategies.song_as_track import search_song_as_track
from lookup.strategies.swapped_interpretation import search_with_alternative_interpretation
from lookup.strategies.track_on_compilation import search_compilations_for_track
from lookup.tail_deadline import should_shed_tail
from lookup.validation import apply_track_validation_cascade
from services.parser import MessageType, ParsedRequest

logger = logging.getLogger(__name__)


async def resolve_albums_for_track(
    parsed: ParsedRequest,
    discogs_service: DiscogsService | None = None,
    db: LibraryDB | None = None,
    memo: TrackCandidateMemo | None = None,
) -> tuple[list[str], bool]:
    """Resolve album names for a track if not provided.

    Searches Discogs for ALL releases containing the track, not just the first one.

    When ``db`` is supplied, tracklist validation is gated library-first (LML#866):
    the (corrected-or-typed) query artist must have at least one library row or the
    O(N) rate-limited Discogs tracklist fetches are skipped — a non-library
    artist+track request does one Discogs search and zero tracklist fetches, which
    is behavior-preserving because these album names feed only ARTIST_PLUS_ALBUM,
    whose album-fed branch re-filters every hit to the query artist.

    When ``memo`` is supplied (LML#867), the artist-filtered track search's raw
    candidates (and verdicts) are recorded under ``(song, artist)`` so the
    downstream ``TRACK_ON_COMPILATION`` strategy can reuse them as its Wave A seed
    instead of re-issuing the same search.

    Returns:
        Tuple of (list of album names, song_not_found_flag)
    """
    album_is_missing = not parsed.album
    album_is_artist = (
        parsed.album
        and parsed.artist
        and normalize_for_comparison(parsed.album).strip()
        == normalize_for_comparison(parsed.artist).strip()
    )

    if parsed.song and parsed.artist and (album_is_missing or album_is_artist):
        if album_is_artist:
            logger.info(f"Album '{parsed.album}' appears to be artist name, looking up albums")
        try:
            releases = await lookup_releases_by_track(
                parsed.song,
                parsed.artist,
                # Search at the shared Wave A page size (LML#867): the carry-through
                # reuses this search as TRACK_ON_COMPILATION's Wave A, and reuse is
                # only non-narrowing when step 2 searched at least as wide as a fresh
                # Wave A would. A smaller page would make the seed a subset the
                # consumer must re-search.
                limit=WAVE_A_SEARCH_LIMIT,
                service=discogs_service,
                db=db,
                library_artist=library_artist_for(parsed),
                memo=memo,
            )
            if releases:
                albums = []
                artist_normalized = normalize_for_comparison(parsed.artist)
                for release_artist, album in releases:
                    if normalize_for_comparison(release_artist).startswith(artist_normalized):
                        if album not in albums:
                            albums.append(album)
                if albums:
                    logger.info(f"Found {len(albums)} albums for song '{parsed.song}': {albums}")
                    return albums, False
            logger.info(f"Could not find albums for song '{parsed.song}'")
            return [], True
        except Exception as e:
            logger.warning(f"Track lookup failed: {e}")
            return [], True
    return [parsed.album] if parsed.album else [], False


def build_context_message(
    parsed: ParsedRequest,
    found_on_compilation: bool,
    song_not_found: bool,
    has_results: bool = True,
) -> str | None:
    """Build context message based on search results."""
    if found_on_compilation:
        return f'Found "{parsed.song}" by {parsed.artist} on:'

    if song_not_found and has_results:
        if parsed.song and parsed.album:
            return (
                f'"{parsed.album}" not found in the library, '
                f"but here are other albums by {parsed.artist}:"
            )
        elif parsed.song:
            return (
                f'"{parsed.song}" is not on any album in the library, '
                f"but here are some albums by {parsed.artist}:"
            )
    elif song_not_found and not has_results:
        if parsed.song and parsed.artist:
            return f'"{parsed.song}" by {parsed.artist} not found in library.'

    return None


def _identity_to_reconciled(identity: Identity) -> ReconciledIdentity:
    """Convert an EntityStore Identity dataclass to the shared ReconciledIdentity schema."""
    return ReconciledIdentity(
        discogs_artist_id=identity.discogs_artist_id,
        musicbrainz_artist_id=identity.musicbrainz_artist_id,
        wikidata_qid=identity.wikidata_qid,
        spotify_artist_id=identity.spotify_artist_id,
        apple_music_artist_id=identity.apple_music_artist_id,
        bandcamp_id=identity.bandcamp_id,
    )


async def _resolve_identities(
    artist_names: list[str], entity_store: EntityStore
) -> dict[str, ReconciledIdentity]:
    """Look up reconciled identities for unique artist names.

    Returns a dict keyed by the artist name. Names not found in the entity
    store are omitted, so callers should treat a missing key as "no identity."
    Lookups across the unique names run concurrently.
    """
    unique = list({name for name in artist_names if name})
    if not unique:
        return {}

    identities = await asyncio.gather(
        *(entity_store.get_identity(name) for name in unique),
        return_exceptions=True,
    )

    result: dict[str, ReconciledIdentity] = {}
    for name, identity in zip(unique, identities, strict=True):
        if isinstance(identity, BaseException):
            logger.warning("EntityStore.get_identity failed for %r: %s", name, identity)
            continue
        if identity is not None:
            result[name] = _identity_to_reconciled(identity)
    return result


@dataclass
class LookupState:
    """Mutable per-request state for one ``perform_lookup`` pass.

    Created empty at the top of ``perform_lookup`` and mutated in place by the
    ``_step_*`` functions, in spine order. Each field's docstring names the
    steps that touch it; each step function's docstring declares the fields it
    READS and WRITES. Cross-step mutable accumulators that shape the response
    live on this state; step-derived pipeline products (the ``ParsedRequest``,
    ``albums_for_search``, the built ``result_items``) ride as step
    parameters/returns because each is produced once by one step and consumed
    read-mostly downstream. The raw ``SearchState`` step 3 builds never leaves
    ``_step_search_pipeline`` — ``artist_fallback_results``,
    ``matched_via_by_id``, and ``timed_out`` are copied onto this state before
    the step returns, so every later step reads them here.
    """

    library_results: list[LibraryItem] = field(default_factory=list)
    """Library catalog rows for the response. Written by the search pipeline
    (step 3) and narrowed/promoted by track validation (step 3b); step 3c sets
    each row's ``on_streaming`` in place; read by the library-miss gate (3a),
    artwork fetch (4), the trace projection, the context message (5), identity
    resolution (6), and the response build."""

    items_with_artwork: list[tuple[LibraryItem, DiscogsSearchResult | None]] = field(
        default_factory=list
    )
    """``(library item, Discogs artwork match)`` pairs for the response. Built
    by artwork fetch (step 4) from ``library_results`` — or synthesized
    directly by the library-miss probe (step 3a), which bypasses steps 3b and 4
    — then rebound to the enriched pairs by enrichment (4b). Read by the trace
    projection (results_count), the context message (5, via ``has_results``),
    and identity resolution (6, which collects artist names from the
    synthesized id=0 pairs); when non-empty it takes precedence over
    ``library_results`` in the response build."""

    song_not_found: bool = False
    """True while the requested song/album is unconfirmed. Seeded by album
    resolution (steps 1+2), updated by the search pipeline (3), cleared by the
    library-miss probe (3a) on a synthesized hit and by track validation (3b)
    on a confirmed or promoted match; read by the context message (5) and the
    response."""

    found_on_compilation: bool = False
    """True when TRACK_ON_COMPILATION surfaced the track on a compilation.
    Written by the search pipeline (step 3); routes track validation (3b),
    threads into artwork fetch (4) and enrichment (4b), and shapes the context
    message (5); reported on the response
    (``LookupResponse.found_on_compilation``)."""

    discogs_titles: dict[int, ResolvedRelease] = field(default_factory=dict)
    """Library item id → resolved Discogs release, carried on the strategy seam
    so artwork fetch binds ``discogs_url`` by id. Written by the search
    pipeline (step 3), extended by the A4 cached-track promotion (3b, LML#629),
    read by artwork fetch (4)."""

    search_type: str = SEARCH_TYPE_NONE
    """Which strategy resolved the request (telemetry + response). Written by
    the search pipeline (step 3); read by the trace projection and the
    response build."""

    library_miss_outcome: str | None = None
    """LML#583 outcome marker (``library_miss_discogs_match`` /
    ``library_miss_no_discogs_match``) for the Sentry trace projection. Written
    by the library-miss probe (step 3a)."""

    corrected_artist: str | None = None
    """Fuzzy artist-spelling correction from the library vocabulary, reported
    on the response. Written by the prepare step (steps 1+2); the correction
    itself rides on ``parsed.library_artist`` (the LML#626 two-channel seam),
    never on ``parsed.artist``."""

    artist_fallback_results: list[LibraryItem] = field(default_factory=list)
    """Artist-only fallback results saved before TRACK_ON_COMPILATION replaced
    them with compilation results. Written by the search pipeline (step 3);
    read by track validation (3b) to validate the artist's own album against
    Discogs tracklists on the compilation branch."""

    unranked_fallback_candidates: list[LibraryItem] = field(default_factory=list)
    """LML#808: ``search_state.results`` pre-``limit_results`` truncation
    (unranked rowid order on the artist-only branch), distinct from
    ``artist_fallback_results`` above. Written by the search pipeline (step
    3); track validation (3b) widens to this full set instead of the
    truncated ``library_results`` when ``song_not_found`` still holds and no
    compilation was found — now safe since validation's fan-out is bounded."""

    matched_via_by_id: dict[int, list[TrackMatchHint]] = field(default_factory=dict)
    """Per-library-id track-match provenance (catalog-track-search §5.1).
    Written by the search pipeline (step 3); read by the response build to set
    each ``LookupResultItem.matched_via``."""

    timed_out: bool = False
    """True when the LML#370 hard cap fired and the search pipeline was
    abandoned. Written by the search pipeline (step 3); read by the response
    build (``LookupResponse.timeout``) and the degraded-response build."""

    @property
    def result_count(self) -> int:
        """Row count for the response; ``items_with_artwork`` takes precedence when non-empty."""
        if self.items_with_artwork:
            return len(self.items_with_artwork)
        return len(self.library_results)

    @property
    def has_results(self) -> bool:
        """True when the response will carry any result rows (see ``result_count``)."""
        return self.result_count > 0


@dataclass(frozen=True)
class LookupServices:
    """Internal read-only bundle of service handles + request-scoped config.

    Exists purely to keep the ``_step_*`` function signatures short; it is not
    exported and is not part of any wire contract. ``perform_lookup``'s public
    signature stays the sole entry point — the bundle is built from it, once
    per request.
    """

    db: LibraryDB
    """Library SQLite handle (search, artist correction, streaming flags)."""

    discogs_service: DiscogsService | None
    """Discogs API/cache facade; ``None`` degrades every Discogs-touching step."""

    telemetry: RequestTelemetry
    """Per-request step timings + API-call counters."""

    entity_store: EntityStore | None
    """Identity store for step-6 artist identity resolution."""

    discogs_cache: DiscogsCacheService | None
    """Discogs PG cache for enrichment + the external-cache fallback (step 7)."""

    mb_pg: PgSourceProtocol | None
    """MusicBrainz cache PG source (enrichment + external-cache fallback)."""

    apple_music: AppleMusicClient | None
    """Apple Music client for enrichment's streaming-URL assignment."""

    spotify: SpotifyClient | None
    """Spotify client for enrichment's streaming-URL assignment."""

    bandcamp: BandcampClient | None
    """Bandcamp client for enrichment's streaming-URL assignment."""

    discogs_cache_pg: PgSource | None
    """Discogs-cache PG source: the #632 row-less resolution cache threaded
    into the Discogs-aware strategies, plus the streaming-URL cache."""

    # --- Request-scoped config (rides with the handles to keep signatures short) ---

    caller_budget_ms: int | None
    """Caller-imposed search budget forwarded to the strategy pipeline (step 3)."""

    spine_deadline: SpineDeadline
    """LML#865 spine-level deadline built once at ``perform_lookup`` entry. Bounds
    step 2's ``resolve_albums_for_track`` Discogs pass (via
    ``run_within_spine_deadline``) and re-bases step 3's clock to the spine start
    (via ``execute_search_pipeline(pipeline_start_offset_ms=...)``) so the hard
    cap + caller budget cover ``step2 + step3`` together."""

    allow_release_resolution_fallback: bool
    """LML#652/#671 bulk kill switch: ``False`` on /lookup/bulk suppresses every
    per-row Discogs surfacing path (the row-less producers, the step-3a
    library-miss probe, and step 4's lazy artwork fallback)."""

    extended: bool
    """Extended-payload opt-in for enrichment (4b) and the trace projection.
    The generated LookupRequest models it as ``bool | None`` (api.yaml gives it
    ``default: false`` but leaves it out of the required set, so callers can
    omit it); coerced once at bundle build."""

    warm_cache: bool
    """Fire-and-forget bio-cache warm opt-in for enrichment (4b) and the trace
    projection. Same ``bool | None`` wire shape as ``extended``; coerced once
    at bundle build."""

    include_external_caches: bool
    """Step-7 mojibake-fallback opt-in. Already a plain ``bool`` on the LML
    LookupRequest subclass (``lookup/models.py``) — a straight copy, no
    coercion."""


async def _step_prepare_request(
    request: LookupRequest,
    state: LookupState,
    services: LookupServices,
) -> tuple[ParsedRequest, list[str], TrackCandidateMemo]:
    """Steps 1+2 — prepare: build the ParsedRequest, correct artist spelling, resolve albums.

    Artist correction (``db.find_similar_artist``) runs BEFORE album resolution
    (``resolve_albums_for_track``) when the request carries an artist — the
    serialization is load-bearing for the LML#866 library-first validation gate,
    which reads the corrected ``parsed.library_artist``. A correction never
    overwrites ``parsed.artist`` — see the LML#626 two-channel seam comment inline.

    READS: nothing from ``LookupState``.
    WRITES: ``corrected_artist``, ``song_not_found``.

    Returns ``(parsed, albums_for_search, candidate_memo)``: the ParsedRequest
    threads through every later step; ``albums_for_search`` feeds only the search
    pipeline (step 3); the ``candidate_memo`` (LML#867) carries step-2's
    artist-filtered track candidates so the Discogs-aware strategies (step 3)
    reuse them instead of re-searching. All three ride as return values rather
    than state — each is produced once here and consumed read-mostly downstream.
    """
    # Request-scoped candidate carry-through (LML#867). Created fresh per request;
    # never shared across requests. Populated by ``resolve_albums_for_track`` below
    # and read by ``TRACK_ON_COMPILATION`` in step 3.
    candidate_memo = TrackCandidateMemo()
    # Build a ParsedRequest from the LookupRequest for compatibility with
    # search functions that expect ParsedRequest
    parsed = ParsedRequest(
        song=request.song,
        album=request.album,
        artist=request.artist,
        is_request=True,
        message_type=MessageType.REQUEST,
        # LookupRequest.raw_message is str | None (optional when structured
        # fields are supplied), but ParsedRequest.raw_message is a non-optional
        # str whose "no raw text" sentinel is "". Coerce None here so a
        # structured-only request doesn't fail Pydantic validation and surface
        # as a 500 (WXYC/library-metadata-lookup#783). The pipeline drives off
        # artist/album/song regardless; "" is inert for the free-text legs.
        raw_message=request.raw_message or "",
    )

    if parsed.artist:
        with services.telemetry.track_step("album_lookup"):
            if parsed.song and not parsed.album:
                services.telemetry.record_api_call("discogs")
            # Resolve the fuzzy library-artist correction FIRST (a cheap, cached
            # local-SQLite lookup) so album resolution can gate its Discogs
            # tracklist validation on library membership of the CORRECTED artist
            # (LML#866). Serializing (vs the former concurrent gather) costs a
            # sub-millisecond local read and is required for the gate to use the
            # same library-artist notion step 3 does. Because the correction now
            # completes before step 2 begins, there is no concurrent sibling task
            # to orphan when the LML#865 spine deadline trips.
            corrected = await services.db.find_similar_artist(parsed.artist)
            if corrected:
                state.corrected_artist = corrected
                # Two-channel seam (WXYC/library-metadata-lookup#626): do NOT
                # overwrite ``parsed.artist``. The typed value must keep flowing to
                # every Discogs-facing path (the three Discogs-aware strategies'
                # probes, ``validate_release_for_track``, and the library-miss
                # Discogs probe), or a *distinct* non-library artist one edit from a
                # library name gets snapped into the library's vocabulary on Discogs.
                # Thread the correction on ``library_artist``, read only by the
                # library-side legs (``db.search`` queries + ``artist_matches_item``
                # match-backs).
                parsed.library_artist = corrected
            # LML#865: bound step 2's Discogs pass by the spine deadline. On a
            # trip, run_within_spine_deadline raises SpineDeadlineExceededError,
            # which perform_lookup catches to return the honest timed-out response.
            # The LML#867 candidate memo threads in so TRACK_ON_COMPILATION can
            # reuse step 2's artist-filtered search instead of re-issuing it.
            albums_for_search, song_not_found = await run_within_spine_deadline(
                resolve_albums_for_track(
                    parsed, services.discogs_service, db=services.db, memo=candidate_memo
                ),
                services.spine_deadline,
                step="album_lookup",
            )
    else:
        with services.telemetry.track_step("album_lookup"):
            # LML#865: same spine-deadline bound on the artist-less branch.
            albums_for_search, song_not_found = await run_within_spine_deadline(
                resolve_albums_for_track(
                    parsed, services.discogs_service, db=services.db, memo=candidate_memo
                ),
                services.spine_deadline,
                step="album_lookup",
            )

    state.song_not_found = song_not_found
    return parsed, albums_for_search, candidate_memo


async def _step_search_pipeline(
    parsed: ParsedRequest,
    albums_for_search: list[str],
    candidate_memo: TrackCandidateMemo,
    state: LookupState,
    services: LookupServices,
    location_union_task: asyncio.Task[list[ResolvedLocation]] | None = None,
) -> None:
    """Step 3 — execute the search strategy pipeline.

    READS: ``song_not_found`` (pipeline input, seeded by steps 1+2).
    WRITES: ``library_results``, ``song_not_found``, ``found_on_compilation``,
    ``discogs_titles``, ``search_type``, ``artist_fallback_results``,
    ``matched_via_by_id``, ``timed_out``.

    ``candidate_memo`` (LML#867) is the step-2 artist-filtered track candidate
    carry-through; it is bound into the ``TRACK_ON_COMPILATION`` execute partial
    so the strategy reuses step 2's Wave A search instead of re-issuing it.

    ``location_union_task`` (LML#1026) is the concurrent recall-index probe,
    present exactly when the location union is active. It is bound into the
    ``TRACK_ON_COMPILATION`` execute partial, which treats the index as the
    single source for comp-track resolution: an index hit makes the strategy
    contribute nothing (the fold appends the shelf locations, ranked-first as
    the Case-B primary) and an index miss suppresses the live-Discogs
    comp-discovery probes -- see ``search_compilations_for_track``'s
    docstring for the full tri-state. ``None`` (kill switch off /
    low-priority caller / no song) preserves the legacy live pass
    byte-identically.

    The raw ``SearchState`` this step builds never escapes it: every field a
    later step needs is copied onto ``LookupState`` here.
    """
    with services.telemetry.track_step("library_search"):
        # Strategies hold their own ``db`` handle (no per-call db arg on the
        # runner post-#399). The discogs_service is captured via ``partial`` on
        # the strategies that need it; ARTIST_PLUS_ALBUM is the only
        # library-only strategy (SWAPPED_INTERPRETATION now cross-references the
        # non-artist token as a track via Discogs — LML#622).
        # ``discogs_cache_pg`` threads the #632 row-less resolution cache into the
        # three Discogs-aware strategies' A1 carry-through (LML#628). The library
        # channel and ARTIST_PLUS_ALBUM / SONG_AS_ARTIST never touch it.
        # LML#652: the per-request bulk kill switch (``False`` on /lookup/bulk)
        # threads into the four Discogs-aware row-less producers here via the
        # partials, and into the fifth (the A4 cached-track safety net) at its
        # own Step-3b call site below — exactly as it already reaches
        # ``fetch_artwork_for_items`` (Step 4) for #604's lazy fallback. So no
        # row-less item surfaces, nor any carry-through resolve or #632 cache
        # write, on the backfill path.
        strategies = build_strategies(
            services.db,
            search_library_func=search_library_with_fallback,
            search_alternative_func=partial(
                search_with_alternative_interpretation,
                discogs_service=services.discogs_service,
                pg=services.discogs_cache_pg,
                allow_release_resolution_fallback=services.allow_release_resolution_fallback,
            ),
            search_compilations_func=partial(
                search_compilations_for_track,
                discogs_service=services.discogs_service,
                pg=services.discogs_cache_pg,
                allow_release_resolution_fallback=services.allow_release_resolution_fallback,
                candidate_memo=candidate_memo,
                location_union_task=location_union_task,
            ),
            search_song_as_artist_func=partial(
                search_song_as_artist,
                discogs_service=services.discogs_service,
                allow_release_resolution_fallback=services.allow_release_resolution_fallback,
            ),
            search_song_as_track_func=partial(
                search_song_as_track,
                discogs_service=services.discogs_service,
                pg=services.discogs_cache_pg,
                allow_release_resolution_fallback=services.allow_release_resolution_fallback,
            ),
        )

        search_state = await execute_search_pipeline(
            parsed=parsed,
            raw_message=parsed.raw_message,
            strategies=strategies,
            albums_for_search=albums_for_search,
            song_not_found=state.song_not_found,
            caller_budget_ms=services.caller_budget_ms,
            # LML#865: charge step 2's already-spent spine time against step 3's
            # hard cap + caller budget so the deadline covers the whole spine.
            pipeline_start_offset_ms=services.spine_deadline.elapsed_ms(),
        )

        state.library_results = limit_results(search_state.results)
        state.song_not_found = search_state.song_not_found
        state.found_on_compilation = search_state.found_on_compilation
        state.discogs_titles = search_state.discogs_titles
        state.search_type = get_search_type_from_state(search_state)
        state.artist_fallback_results = search_state.artist_fallback_results
        state.matched_via_by_id = search_state.matched_via_by_id
        state.timed_out = search_state.timed_out
        # LML#808: preserve the full, pre-truncation candidate set so track
        # validation (3b) can widen past the ``limit_results`` cut instead of
        # only ever seeing the top ``MAX_SEARCH_RESULTS`` rows by rowid.
        # ``_step_validate_tracks`` decides whether widening actually applies.
        state.unranked_fallback_candidates = search_state.results

        if state.found_on_compilation:
            services.telemetry.record_api_call("discogs")


def _task_resolved_with_renderable_locations(
    location_union_task: asyncio.Task[list[ResolvedLocation]] | None,
) -> bool:
    """Non-blocking peek: did the union probe already resolve with at least
    one renderable shelf location?

    Used by step 3a's gate (LML#1026): on an index hit the strategy returns
    nothing, leaving ``library_results`` empty — exactly the state that used
    to mean "the library has nothing, ask Discogs directly". The peek keeps
    step 3a from firing a live Discogs search and synthesizing a rowless
    id-0 row that would outrank the folded shelf location as primary. A
    pending, cancelled, or failed task reads ``False`` (step 3a proceeds —
    the safe fallback), and on the only path where this matters the strategy
    has already awaited the task, so "hit" is always observable by now.
    """
    if location_union_task is None or not location_union_task.done():
        return False
    if location_union_task.cancelled() or location_union_task.exception() is not None:
        return False
    return any(location.shelf_item is not None for location in location_union_task.result())


async def _step_library_miss_probe(
    parsed: ParsedRequest,
    state: LookupState,
    services: LookupServices,
    location_union_task: asyncio.Task[list[ResolvedLocation]] | None = None,
) -> None:
    """Step 3a — library-miss Discogs search (LML#583).

    READS: ``library_results`` (the emptiness gate).
    WRITES: ``items_with_artwork``, ``song_not_found``, ``library_miss_outcome``.

    Ordering invariant (enforced here and at step 4's emptiness guard): on a
    hit this step writes ``items_with_artwork`` directly and clears
    ``song_not_found``, deliberately bypassing track validation (3b) and the
    artwork fetch (4). The bypass holds because this step only fires when
    ``library_results`` is empty — so 3b's and 4's non-empty gates never run on
    this path and cannot overwrite the synthesized pair.
    """
    # When the entire search pipeline returned no library results AND the request
    # carries both artist and album, probe Discogs directly. A confident match
    # (80/80-floor via find_best_typed_match) is synthesized into a
    # LibraryItem(id=0) + DiscogsSearchResult pair and handed directly to Step
    # 4b (enrich_artwork_results) — bypassing fetch_artwork_for_items (which
    # would re-search and risk a second floor mis-selection) and bypassing Step
    # 3b track validation (the synthesized item is already 80-floored on
    # (artist, album) jointly; routing it through an unfloored top-1 re-search
    # has non-zero probability of flipping to a different release).
    #
    # Gated on ``allow_release_resolution_fallback`` (the bulk kill switch, LML#671):
    # /lookup/bulk passes ``False`` so the 35k-album backfill never pays a per-row
    # Discogs ``search()`` here, nor surfaces a row-less ``LibraryItem(id=0)``.
    # An album-only backfill item (artist+album, no song) hits *this* producer —
    # not the five song-bearing LML#652 producers — so gating it is what makes the
    # "no per-row Discogs surfacing/cost on bulk" guarantee true for the real drain.
    # /lookup keeps the default (``True``), so #583 fires there exactly as before.
    # LML#1026: an index hit means the fold will supply in-library shelf rows
    # for this track — synthesizing a rowless id=0 "not in your library" item
    # here would both spend the live Discogs call the hit path is meant to
    # avoid and outrank the shelf location as primary (_build_result_items
    # ranks items_with_artwork first).
    if _task_resolved_with_renderable_locations(location_union_task):
        return
    if (
        not state.library_results
        and services.allow_release_resolution_fallback
        and parsed.artist
        and parsed.album
        and parsed.album.strip()
    ):
        with services.telemetry.track_step("library_miss_discogs_search"):
            miss_match = await _library_miss_discogs_search(parsed, services.discogs_service)
        if miss_match is not None:
            synthesized_lib_item, discogs_result = miss_match
            state.items_with_artwork = [(synthesized_lib_item, discogs_result)]
            # A synthesized Discogs match resolves the request — clear song_not_found so
            # build_context_message and LookupResponse.song_not_found reflect reality.
            state.song_not_found = False
            state.library_miss_outcome = "library_miss_discogs_match"
            services.telemetry.record_api_call("discogs")
        elif services.discogs_service is not None:
            # Discogs was available and searched but found no confident match.
            state.library_miss_outcome = "library_miss_no_discogs_match"


async def _step_validate_tracks(
    parsed: ParsedRequest,
    state: LookupState,
    services: LookupServices,
) -> None:
    """Step 3b — validate results against Discogs track data.

    READS: ``library_results``, ``found_on_compilation``, ``song_not_found``,
    ``discogs_titles``, ``artist_fallback_results``, ``unranked_fallback_candidates``.
    WRITES: ``library_results``, ``song_not_found``, ``discogs_titles``.

    Sequences the gate (should step 3b run at all, timed as "track_validation")
    and delegates the promotion/narrowing policy — per-result validation, the
    A4 cached-track promotion, the song-as-album-title promotion, and the
    compilation artist-fallback merge — to
    ``lookup.validation.apply_track_validation_cascade``.

    Synthesized id=0 items from Step 3a are excluded: library_results is empty
    on that path, so the gate below never fires. The filter on real_results is
    a defensive guard for any future path that might add id=0 items to
    library_results.

    LML#808: while unconfirmed (``song_not_found`` holds, no compilation
    found), ``real_results`` widens to ``unranked_fallback_candidates`` instead
    of the truncated ``library_results``, so a high-rowid album can still
    reach ``filter_results_by_track_validation`` — safe now that its fan-out
    is bounded. A confirmed direct match skips the widen; no benefit to it.
    """
    real_results = [r for r in state.library_results if r.id != 0]
    if state.song_not_found and not state.found_on_compilation:
        widened = [r for r in state.unranked_fallback_candidates if r.id != 0]
        if len(widened) > len(real_results):
            real_results = widened
    should_validate = bool(real_results and parsed.song and parsed.artist) and (
        not state.found_on_compilation or bool(state.artist_fallback_results)
    )
    if not should_validate:
        return
    with services.telemetry.track_step("track_validation"):
        result = await apply_track_validation_cascade(
            real_results=real_results,
            library_results=state.library_results,
            found_on_compilation=state.found_on_compilation,
            song_not_found=state.song_not_found,
            discogs_titles=state.discogs_titles,
            artist_fallback_results=state.artist_fallback_results,
            song=parsed.song,
            artist=parsed.artist,
            match_artist=library_artist_for(parsed),
            db=services.db,
            discogs_service=services.discogs_service,
            allow_release_resolution_fallback=services.allow_release_resolution_fallback,
        )
    state.library_results = result.library_results
    state.song_not_found = result.song_not_found
    state.discogs_titles = result.discogs_titles


async def _step_populate_streaming_status(
    state: LookupState,
    services: LookupServices,
) -> None:
    """Step 3c — populate per-row streaming availability flags.

    READS: ``library_results`` (sets each item's ``on_streaming`` in place).
    WRITES: no ``LookupState`` field is rebound.
    """
    if state.library_results and getattr(services.db, "_has_streaming_links", None) is True:
        # Time only the awaited DB call; the sync fan-out below is not I/O and
        # would otherwise inflate the streaming_status leg on large result sets.
        with services.telemetry.track_step("streaming_status"):
            streaming_status = await services.db.get_streaming_status(
                [r.id for r in state.library_results]
            )
        for result in state.library_results:
            result.on_streaming = streaming_status.get(result.id, False)


async def _prefetch_release_overrides(
    library_results: list[LibraryItem],
    discogs_cache_pg: PgSource | None,
    *,
    allow_release_resolution_fallback: bool = True,
) -> dict[int, int]:
    """Flag-gated prefetch of verified library-release overrides (LML#850).

    Reads the ``lml_library_release_override`` flag once. When off (the default),
    returns an empty map with **zero** PG work, so /lookup resolution is the
    pre-override fuzzy pick byte-for-byte. When on, issues a single query for the
    request's real (``id > 0``) library ids — a row-less synthesized id (0) is
    never a card-catalog id and can carry no pin, so it is excluded from the id
    list. Best-effort: ``get_library_release_overrides`` swallows PG errors and
    returns ``{}``, so a transient outage falls through to the fuzzy pick.

    ``allow_release_resolution_fallback`` is the bulk kill switch: ``/lookup/bulk``
    passes ``False`` (like the #604/#628/#632 fallbacks and the #573 streaming
    warm) so the 35k-album drain never fans this per-item PG query out across the
    shared discogs-cache pool. On bulk the prefetch is skipped entirely; the flag
    is scoped to the live ``/lookup`` path.
    """
    if not allow_release_resolution_fallback:
        return {}
    if not get_settings().lml_library_release_override:
        return {}
    if discogs_cache_pg is None:
        return {}
    library_ids = [item.id for item in library_results if item.id > 0]
    if not library_ids:
        return {}
    return await get_library_release_overrides(discogs_cache_pg, library_ids)


async def _step_fetch_artwork(
    parsed: ParsedRequest,
    state: LookupState,
    services: LookupServices,
) -> None:
    """Step 4 — fetch artwork for the library results.

    READS: ``library_results``, ``discogs_titles``, ``found_on_compilation``.
    WRITES: ``items_with_artwork`` (builds the ``(item, artwork)`` pairs every
    later step consumes). The flag-gated LML#850 prefetch feeds
    ``release_overrides`` to ``fetch_artwork_for_items`` so a hand-verified pin
    binds instead of the fuzzy pick.

    Ordering invariant (LML#583): the ``if library_results`` emptiness guard is
    load-bearing — when Step 3a synthesized ``items_with_artwork``,
    ``library_results`` is empty by 3a's own gate, so this step must not run
    and overwrite the synthesized pair (re-searching here would re-open the
    floor mis-selection risk 3a's bypass exists to avoid).
    """
    with services.telemetry.track_step("artwork_fetch"):
        if state.library_results:
            # LML#850: a hand-verified pin overrides the fuzzy release pick. The
            # prefetch is one query (flag-gated, off by default, and skipped on
            # the bulk drain) — an override hit then short-circuits the Discogs
            # search in fetch_one, so a hit reduces per-request work.
            override_map = await _prefetch_release_overrides(
                state.library_results,
                services.discogs_cache_pg,
                allow_release_resolution_fallback=services.allow_release_resolution_fallback,
            )
            for _ in state.library_results:
                services.telemetry.record_api_call("discogs")
            state.items_with_artwork = await fetch_artwork_for_items(
                state.library_results,
                services.discogs_service,
                state.discogs_titles,
                song=parsed.song,
                album=parsed.album,
                allow_release_resolution_fallback=services.allow_release_resolution_fallback,
                found_on_compilation=state.found_on_compilation,
                release_overrides=override_map,
            )


async def _step_enrich_metadata(
    parsed: ParsedRequest,
    state: LookupState,
    services: LookupServices,
) -> None:
    """Step 4b — enrich with release year, artist details, streaming links.

    READS: ``items_with_artwork``, ``found_on_compilation``; plus
    ``services.extended`` and ``services.warm_cache``.
    WRITES: ``items_with_artwork`` (rebound to the enriched pairs).

    Ordering invariant: runs strictly after Step 4 built ``items_with_artwork``
    — and after Step 3a, whose synthesized pair bypasses the artwork fetch and
    lands here directly — because enrichment extends the pairs those steps
    produced.
    """
    with services.telemetry.track_step("metadata_enrichment"):
        if state.items_with_artwork:
            state.items_with_artwork = await enrich_artwork_results(
                state.items_with_artwork,
                services.discogs_service,
                song=parsed.song,
                album=parsed.album,
                artist=parsed.artist,
                library_db=services.db,
                extended=services.extended,
                warm_cache=services.warm_cache,
                discogs_cache=services.discogs_cache,
                mb_pg=services.mb_pg,
                apple_music=services.apple_music,
                spotify=services.spotify,
                bandcamp=services.bandcamp,
                entity_store=services.entity_store,
                discogs_cache_pg=services.discogs_cache_pg,
                found_on_compilation=state.found_on_compilation,
                spine_deadline=services.spine_deadline,
            )


def _step_project_trace_attrs(
    state: LookupState,
    services: LookupServices,
) -> None:
    """Project request flags and result-quality signals onto the Sentry transaction.

    Runs between steps 4b and 5; not a numbered pipeline step (observability
    only — it must never affect the response).

    READS: ``result_count`` (via ``items_with_artwork``/``library_results``),
    ``search_type``, ``library_miss_outcome``; plus ``services.extended`` and
    ``services.warm_cache``.
    WRITES: nothing.
    """
    # Project the request-side flags and result-quality signals onto the
    # active Sentry transaction so the trace can be filtered by request mode
    # (lml.lookup.extended, lml.lookup.warm_cache) and by match outcome
    # (lookup.results_count, lookup.match_type — LML#158). Mirrors the
    # cache_stats projection pattern (LML#213).
    try:
        scope = sentry_sdk.get_current_scope()
        if scope.transaction is not None:
            scope.transaction.set_data("lml.lookup.extended", services.extended)
            scope.transaction.set_data("lml.lookup.warm_cache", services.warm_cache)
            scope.transaction.set_data("lookup.results_count", state.result_count)
            scope.transaction.set_data("lookup.match_type", state.search_type)
            if state.library_miss_outcome is not None:
                scope.transaction.set_data("lookup.outcome", state.library_miss_outcome)
    except Exception:
        # Observability must not break the request path.
        pass


async def _step_resolve_result_identities(
    state: LookupState,
    services: LookupServices,
) -> dict[str, ReconciledIdentity]:
    """Step 6 — resolve external identifiers for each result's artist.

    READS: ``library_results``, ``items_with_artwork``.
    WRITES: nothing.

    Returns identities keyed by artist name (missing key = "no identity");
    consumed only by the response build, so the mapping rides as a return
    value rather than state.
    """
    # Collect artist names from library_results (normal path) and from
    # items_with_artwork (synthesized Step 3a path, where library_results is empty
    # but the synthesized LibraryItem carries a real artist name worth resolving).
    identities_by_artist: dict[str, ReconciledIdentity] = {}
    _identity_artist_names = [item.artist for item in state.library_results if item.artist] + [
        item.artist for item, _ in state.items_with_artwork if item.id == 0 and item.artist
    ]
    if services.entity_store is not None and _identity_artist_names:
        with services.telemetry.track_step("identity_resolution"):
            identities_by_artist = await _resolve_identities(
                _identity_artist_names, services.entity_store
            )
    return identities_by_artist


def _build_result_items(
    state: LookupState,
    identities_by_artist: dict[str, ReconciledIdentity],
) -> list[LookupResultItem]:
    """Build the response items (convert internal models to API contract models).

    READS: ``items_with_artwork`` (takes precedence when non-empty — the
    canonical rule statement is ``LookupState.result_count``),
    ``library_results``, ``matched_via_by_id``.
    WRITES: nothing.
    """

    def _identity_for(item: LibraryItem) -> ReconciledIdentity | None:
        if not item.artist:
            return None
        return identities_by_artist.get(item.artist)

    result_items = []
    if state.items_with_artwork:
        for item, artwork in state.items_with_artwork:
            # Synthesized items (id=0, from Step 3a) have no library call-number
            # components; build the "(external)" sentinel that Backend-Service
            # already understands (same contract as the Step 7
            # include_external_caches path — one construction site for both,
            # lookup/external_search.py:build_external_catalog_item).
            catalog_item = (
                build_external_catalog_item(artist=item.artist, title=item.title)
                if item.id == 0
                else item.to_catalog_item()
            )
            result_items.append(
                LookupResultItem(
                    library_item=catalog_item,
                    artwork=artwork.to_match_result() if artwork else None,
                    reconciled_identity=_identity_for(item),
                    # Synthesized items (id=0) carry no track-title-provenance hint;
                    # do not look up key 0 in matched_via_by_id to prevent accidental
                    # collision with any future strategy that might write to that key.
                    matched_via=None if item.id == 0 else state.matched_via_by_id.get(item.id),
                )
            )
    elif state.library_results:
        for item in state.library_results:
            result_items.append(
                LookupResultItem(
                    library_item=item.to_catalog_item(),
                    reconciled_identity=_identity_for(item),
                    matched_via=state.matched_via_by_id.get(item.id),
                )
            )
    return result_items


async def _step_external_cache_fallback(
    parsed: ParsedRequest,
    result_items: list[LookupResultItem],
    services: LookupServices,
) -> str | None:
    """Step 7 — external-cache fallback (Phase 1.5 + 1.7 mojibake recovery).

    READS/WRITES: no ``LookupState`` fields — operates on the built
    ``result_items``, appending synthetic external rows on a fallback hit;
    gated on ``services.include_external_caches``. The artist>album>song
    dispatch precedence and per-branch candidate shaping live in
    ``lookup.external_search.search_external_fallback``; this step sequences
    the call (gate + telemetry) and builds the response rows from its result.

    Returns the ``external_source`` provenance value for the response
    (``"library"`` when the pipeline already produced results, the fallback
    source name on a hit, else ``None``).
    """
    external_source: str | None = "library" if result_items else None
    if not result_items and services.include_external_caches:
        with services.telemetry.track_step("external_cache_fallback"):
            candidates, source = await search_external_fallback(
                parsed,
                discogs_cache=services.discogs_cache,
                mb_pg=services.mb_pg,
            )

        if candidates:
            external_source = source
            for candidate in candidates:
                result_items.append(
                    LookupResultItem(
                        library_item=build_external_catalog_item(
                            artist=candidate["artist"], title=candidate["title"]
                        ),
                    )
                )

    return external_source


async def perform_lookup(
    request: LookupRequest,
    db: LibraryDB,
    discogs_service: DiscogsService | None,
    telemetry: RequestTelemetry,
    *,
    entity_store: EntityStore | None = None,
    discogs_cache: DiscogsCacheService | None = None,
    mb_pg: PgSourceProtocol | None = None,
    apple_music: AppleMusicClient | None = None,
    spotify: SpotifyClient | None = None,
    bandcamp: BandcampClient | None = None,
    discogs_cache_pg: PgSource | None = None,
    caller_budget_ms: int | None = None,
    allow_release_resolution_fallback: bool = True,
) -> LookupResponse:
    """Orchestrate the full lookup pipeline.

    The body is a spine of named ``_step_*`` calls sharing one mutable
    :class:`LookupState`; each step's docstring declares the state fields it
    READS and WRITES, and the ordering invariants are documented at the steps
    that enforce them. Step numbering follows the pipeline's historical labels
    (kept stable because tests and ``docs/architecture.md`` cross-reference
    them):

    - Steps 1+2. ``_step_prepare_request`` — artist correction + album resolution
    - Step 3.    ``_step_search_pipeline`` — search strategy pipeline
    - Step 3a.   ``_step_library_miss_probe`` — library-miss Discogs search (LML#583)
    - Step 3b.   ``_step_validate_tracks`` — Discogs tracklist validation
    - Step 3c.   ``_step_populate_streaming_status`` — per-row streaming flags
    - Step 4.    ``_step_fetch_artwork`` — artwork fetch
    - Step 4b.   ``_step_enrich_metadata`` — metadata enrichment
    - (observability) ``_step_project_trace_attrs`` — Sentry trace projection
    - Step 6.    ``_step_resolve_result_identities`` — artist identity resolution
    - (response) ``_build_result_items`` — API-model conversion
    - Step 7.    ``_step_external_cache_fallback`` — external-cache fallback
    - (location fold) ``build_location_result_items`` + ``_reconcile_post_fold_signals``
      — fold the location-union's other shelf locations into ``results`` and
      recompute ``search_type``/``found_on_compilation``/``song_not_found``/
      ``context_message`` (``build_context_message``, Step 5's old call site)/
      ``external_source`` from the final, post-fold list
    """
    # LML#865: derive the spine deadline once at admission. It bounds step 2's
    # Discogs pass and re-bases step 3's clock so the hard cap + caller budget
    # cover the whole spine, not just step 3.
    spine_deadline = SpineDeadline.from_caller_budget(caller_budget_ms)
    services = LookupServices(
        db=db,
        discogs_service=discogs_service,
        telemetry=telemetry,
        entity_store=entity_store,
        discogs_cache=discogs_cache,
        mb_pg=mb_pg,
        apple_music=apple_music,
        spotify=spotify,
        bandcamp=bandcamp,
        discogs_cache_pg=discogs_cache_pg,
        caller_budget_ms=caller_budget_ms,
        spine_deadline=spine_deadline,
        allow_release_resolution_fallback=allow_release_resolution_fallback,
        extended=bool(request.extended),
        warm_cache=bool(request.warm_cache),
        include_external_caches=request.include_external_caches,
    )
    state = LookupState()

    # LML#865: step 2's ``resolve_albums_for_track`` Discogs pass is bounded by
    # the spine deadline inside ``_step_prepare_request``. On a trip we return the
    # honest empty timed-out response rather than grinding step 3 (which the
    # offset would short-circuit anyway) and the enrichment tail.
    try:
        parsed, albums_for_search, candidate_memo = await _step_prepare_request(
            request, state, services
        )
    except SpineDeadlineExceededError as exc:
        project_spine_deadline_telemetry(exc)
        logger.info(
            "Spine deadline exceeded during %s (%s) after %.1fms; returning empty timed-out "
            "response for %r",
            exc.step,
            exc.limit,
            exc.elapsed_ms,
            request.raw_message,
        )
        return _build_timed_out_response(state)

    # Launch the recall-index location-union probe as a concurrent task
    # alongside the search pipeline below (asyncio task, not serial) — gated
    # on song-present + the server-side kill switch + the caller not being
    # low-priority (D4: BS enrichment, the CDC firehose, and backfills are
    # excluded here, not inside `should_run_location_union`, which has no
    # such check). No per-request opt-in — this is the default posture for
    # every interactive, song-bearing lookup. It depends only on `parsed`,
    # not on the pipeline's outcome, so it races step 3 for free. Awaited at
    # the append site after the whole spine resolves (see below), not here —
    # though step 3's TRACK_ON_COMPILATION also consults the same task
    # (LML#1026: the index is authoritative for comp-track resolution, so the
    # strategy skips its live comp-discovery pass whenever this task exists;
    # awaiting a task twice is safe). Every early return in this function
    # threads the task itself into `_build_degraded_response`, which bounds
    # its own await, so nothing here is ever left dangling.
    # The `discogs_cache_pg is not None` leg matters post-LML#1026: with no PG
    # source the probe can only ever degrade to [], and a task that exists but
    # cannot consult the index would read as an authoritative "index miss" to
    # TRACK_ON_COMPILATION — wrongly suppressing the legacy live pass. "Union
    # unavailable" (no source) must stay distinct from "index says no" (a real
    # miss), so no task is created at all.
    location_union_task = (
        asyncio.create_task(
            resolve_track_shelf_locations(parsed, services.discogs_cache_pg, services.db)
        )
        if (
            services.discogs_cache_pg is not None
            and should_run_location_union(request)
            and not is_discogs_low_priority()
        )
        else None
    )
    if location_union_task is not None:
        # GC hygiene: post-LML#1026 the probe RAISES on degradation, and an
        # unexpected exception escaping perform_lookup after this point would
        # leave the failed task un-awaited — asyncio would then log "Task
        # exception was never retrieved" with the probe traceback at GC time,
        # polluting triage during exactly the incident windows the degraded
        # counter exists to make legible. The done-callback retrieves the
        # exception unconditionally (idempotent; every await site still sees
        # it re-raised).
        location_union_task.add_done_callback(_retrieve_union_probe_exception)

    await _step_search_pipeline(
        parsed, albums_for_search, candidate_memo, state, services, location_union_task
    )

    # LML#930 PR2 admission shed: proactively shed the tail for low-priority
    # traffic under sustained event-loop pressure — rationale in admission.py.
    admission_reason = evaluate_admission_shed(
        low_priority=is_discogs_low_priority(), lag_ms=get_event_loop_lag_ms()
    )
    if admission_reason is not None:  # ENFORCE only; always None in shadow mode
        return await _build_degraded_response(
            state,
            parsed,
            degraded_reason=admission_reason,
            location_union_task=location_union_task,
        )

    # LML#755 R2-2 backstop. The runner (`core/search.py`) already catches a
    # saturation-breaker shed *inside* the search pipeline and degrades to
    # cache-only. This try is defense in depth for the remaining tail steps
    # (library-miss probe, track validation, streaming status, artwork,
    # enrichment, identity resolution) — a shed raised from any of them must
    # degrade to whatever the library + already-cached legs produced, NOT 500.
    # A shed is "couldn't ask", never a fatal error. We keep the library
    # results found so far and skip the live-dependent enrichment tail.
    #
    # LML#1118 widened this from `DiscogsBreakerOpenError` to the shared
    # `BreakerOpenError` base: this boundary's own contract — "a shed from ANY
    # tail step degrades the same way" — was never Discogs-specific, just
    # typed that way because Discogs was the only breaker that existed yet.
    # `enrich_metadata` already threads a Bandcamp client through this tail
    # (`item.py`), which today self-catches its own shed before reaching
    # here (`lookup/enrichment/bandcamp_probe.py`); the widening is defense
    # in depth against that changing, per #1118's #755-flood rationale.
    #
    # LML#930 layers a second, orthogonal shed onto the same tail: the caller
    # *deadline*. Between each tail step we check the spine deadline; if the
    # caller's budget (or the universal hard cap) is exhausted, we stop grinding
    # live-Discogs/Apple enrichment past the window the caller can use and return
    # the search results already found, marked degraded(deadline_exceeded).
    # Headerless requests read the 25s hard-cap remaining, so their tail is
    # unshed — same posture as the empty-state cutoff. The two sheds compose:
    # saturation is "couldn't ask", deadline is "ran out of time to ask".
    tail_steps: tuple[tuple[str, Callable[[], Awaitable[None]]], ...] = (
        (
            "library_miss_probe",
            partial(_step_library_miss_probe, parsed, state, services, location_union_task),
        ),
        ("validate_tracks", partial(_step_validate_tracks, parsed, state, services)),
        ("populate_streaming_status", partial(_step_populate_streaming_status, state, services)),
        ("fetch_artwork", partial(_step_fetch_artwork, parsed, state, services)),
        ("enrich_metadata", partial(_step_enrich_metadata, parsed, state, services)),
    )
    try:
        for step_name, run_step in tail_steps:
            reason = should_shed_tail(services.spine_deadline, step_name, request.raw_message)
            if reason is not None:
                return await _build_degraded_response(
                    state, parsed, degraded_reason=reason, location_union_task=location_union_task
                )
            await run_step()
    except BreakerOpenError as exc:
        logger.info(
            "%s shed the enrichment tail; returning cache-only lookup for %r",
            type(exc).__name__,
            request.raw_message,
        )
        return await _build_degraded_response(
            state,
            parsed,
            degraded_reason=DegradedReason.upstream_unavailable,
            location_union_task=location_union_task,
        )

    _step_project_trace_attrs(state, services)

    # LML#930: identity resolution is the last live-Discogs/EntityStore tail leg —
    # shed it too if the caller deadline is exhausted by now (the returned
    # degraded response binds each row with reconciled_identity=None, exactly as
    # the breaker-shed path already does).
    identity_reason = should_shed_tail(
        services.spine_deadline, "resolve_identities", request.raw_message
    )
    if identity_reason is not None:
        return await _build_degraded_response(
            state,
            parsed,
            degraded_reason=identity_reason,
            location_union_task=location_union_task,
        )
    try:
        identities_by_artist = await _step_resolve_result_identities(state, services)
    # LML#1118: widened alongside the tail-steps catch above, same reason
    # (`_resolve_identities` swallows per-name exceptions via
    # `asyncio.gather(..., return_exceptions=True)`, so this leg is defense
    # in depth, not a normally-reachable path).
    except BreakerOpenError as exc:
        logger.info(
            "%s shed identity resolution; returning cache-only lookup for %r",
            type(exc).__name__,
            request.raw_message,
        )
        return await _build_degraded_response(
            state,
            parsed,
            degraded_reason=DegradedReason.upstream_unavailable,
            location_union_task=location_union_task,
        )

    result_items = _build_result_items(state, identities_by_artist)
    external_source = await _step_external_cache_fallback(parsed, result_items, services)

    # Fold the location-union's other shelf locations into `results` (the
    # transparent-fold design -- no separate response field). Awaited plainly
    # here (not bounded): by now the whole tail (steps 3a-4b, enrichment) has
    # had time to run, so the probe is effectively always done. Dedup keys off
    # the ids already in `result_items`, which at this point includes both the
    # primary matches AND any external-cache-fallback synthetic rows.
    folded_locations = await _await_location_union(location_union_task)
    result_items, folded_location_count, index_confirmed_existing = _fold_locations_into_results(
        result_items, folded_locations, state
    )

    (
        song_not_found,
        found_on_compilation,
        search_type,
        context,
        external_source,
    ) = _reconcile_post_fold_signals(
        parsed,
        state,
        folded_location_count=folded_location_count,
        index_confirmed_existing=index_confirmed_existing,
        external_source=external_source,
    )
    _project_post_fold_trace_attrs(result_items, search_type)

    return LookupResponse(
        results=result_items,
        search_type=search_type,
        song_not_found=song_not_found,
        found_on_compilation=found_on_compilation,
        context_message=context,
        corrected_artist=state.corrected_artist,
        external_source=external_source,
        timeout=state.timed_out,
        degraded=False,
    )


def _build_timed_out_response(state: LookupState) -> LookupResponse:
    """Build the honest empty timed-out response after a spine-deadline trip (LML#865).

    Returned when step 2's Discogs pass blew the spine deadline before step 3 ran.
    The request has spent its budget without confirming a match, so we surface the
    determinable answer — "not found, ran out of time" — instead of grinding:
    empty ``results``, ``song_not_found=True``, ``timeout=True``, and the
    none/empty ``search_type`` sentinel. ``corrected_artist`` is preserved from
    whatever the prepare step had written before it tripped (``None`` when the
    trip preempted the artist-correction gather).
    """
    return LookupResponse(
        results=[],
        search_type=SEARCH_TYPE_NONE,
        song_not_found=True,
        found_on_compilation=False,
        context_message=None,
        corrected_artist=state.corrected_artist,
        external_source=None,
        timeout=True,
    )


_DEGRADED_LOCATION_UNION_AWAIT_TIMEOUT_S = 0.25
"""Bound on a degraded/shed-path await of the location-union task. A shed
path is exactly where blocking on a stalled recall-index probe is
counterproductive — the whole point of shedding is to return fast. Kept short
(single indexed btree probe territory) rather than tuned against the tail's
own budget, since a degraded return is trying to get OUT from under a slow
tail, not adopt another one."""


def _retrieve_union_probe_exception(location_union_task: asyncio.Task) -> None:
    """Done-callback for the union probe task: mark any failure retrieved.

    ``Task.exception()`` on a done task flags the exception as observed, so
    asyncio never emits "Task exception was never retrieved" for a probe
    that failed on a request path that then blew up before any await site
    consumed it. Retrieval is idempotent — the strategy's consult and the
    fold's await still see the exception re-raised as normal.
    """
    if location_union_task.cancelled():
        return
    exc = location_union_task.exception()
    if exc is not None:
        logger.debug("location-union probe task failed (retrieved for GC hygiene): %r", exc)


def _fold_locations_into_results(
    result_items: list[LookupResultItem],
    folded_locations: list[ResolvedLocation],
    state: LookupState,
) -> tuple[list[LookupResultItem], int, bool]:
    """Fold the resolved shelf locations into the built result list.

    Returns ``(combined_items, folded_location_count,
    index_confirmed_existing)`` -- the latter two feed
    :func:`_reconcile_post_fold_signals`.

    Positioning (LML#1026): when the pre-fold spine confirmed the track
    somewhere (``state.song_not_found`` is False), the folded locations are
    APPENDED after the primaries -- the Case-A contract ("the primary always
    precedes them"). When it did NOT (``song_not_found`` True -- Case B's
    empty spine, or the A-prime shape where the artist's own albums matched
    but none carries the track), the locations are PREPENDED: the response is
    about to read found-on-compilation with a "Found ... on:" context, and
    the rows that actually carry the track must lead it, not trail albums
    that don't. Pre-#1026 the legacy live pass produced the same ordering by
    *replacing* prior results with the comp hit (stashing them for
    validation); the fold reproduces the ranking without the replace.

    ``index_confirmed_existing`` is True when a resolved index row's
    ``library_id`` is already present in the built response (the fold then
    dedups it away, e.g. the LML#717 shape where the comp row arrived via
    the album-title leg of the artist search) -- proof the track is on a row
    the caller is already receiving, which the reconciliation must reflect
    even though nothing new was appended.
    """
    primary_ids = primary_library_ids_from_results(result_items)
    location_items = build_location_result_items(folded_locations, primary_ids)
    index_confirmed_existing = any(
        location.row.library_id in primary_ids for location in folded_locations
    )
    if location_items and state.song_not_found:
        combined = location_items + result_items
    else:
        combined = result_items + location_items
    return combined, len(location_items), index_confirmed_existing


async def _await_location_union(
    location_union_task: asyncio.Task[list[ResolvedLocation]] | None,
) -> list[ResolvedLocation]:
    """Unbounded await for the happy-path append site.

    By the time the happy path gets here the task has had the whole tail
    (steps 3a-4b, enrichment) to finish, so this rarely actually waits.
    Post-LML#1026 :func:`resolve_track_shelf_locations` deliberately RAISES
    on degradation (probe failure/timeout, rank/join error) so the strategy
    can distinguish "union unavailable" from an authoritative miss; this
    fold site's policy for the same failure is simply "no locations" -- and
    it must never surface as a `/lookup` 500.
    """
    if location_union_task is None:
        return []
    try:
        return await location_union_task
    except Exception:
        logger.exception("location-union probe failed at the happy-path append site")
        return []


async def _await_location_union_bounded(
    location_union_task: asyncio.Task[list[ResolvedLocation]] | None,
) -> list[ResolvedLocation]:
    """Bounded await for a degraded/shed early return (~250ms).

    On a timeout, ``asyncio.wait_for`` cancels the underlying task and this
    degrades to "no locations" rather than let a stuck PG pool acquire hold
    up an already-degraded response. Contrast :func:`_await_location_union`,
    the happy path's plain (unbounded) await.
    """
    if location_union_task is None:
        return []
    try:
        return await asyncio.wait_for(
            location_union_task, timeout=_DEGRADED_LOCATION_UNION_AWAIT_TIMEOUT_S
        )
    except TimeoutError:
        logger.info("location-union probe still pending at a degraded shed; degrading to none")
        return []
    except Exception:
        logger.exception("location-union probe failed during a degraded-path await")
        return []


def _reconcile_post_fold_signals(
    parsed: ParsedRequest,
    state: LookupState,
    *,
    folded_location_count: int,
    index_confirmed_existing: bool = False,
    external_source: str | None,
) -> tuple[bool, bool, str, str | None, str | None]:
    """Recompute every state-derived response signal from whether the
    location union folded anything into the final result list, not from the
    pre-fold ``LookupState`` alone — so ``results``, ``found_on_compilation``,
    ``song_not_found``, ``search_type``, ``context_message``, and
    ``external_source`` always agree about what the caller actually received.

    Returns ``(song_not_found, found_on_compilation, search_type,
    context_message, external_source)``. A **no-op** when
    ``folded_location_count`` is 0 AND ``index_confirmed_existing`` is False
    — every request the location union didn't touch (kill switch off,
    low-priority caller, no song, no recall-index hit) gets byte-identical
    output to before this reconciliation existed; the ``context_message``
    branch below reproduces the old Step 5 call site exactly (same three
    inputs, same function), just moved to run after the fold instead of
    before it. ``index_confirmed_existing`` (LML#1026) covers the fold's
    dedup blind spot: when every resolved index row was already present in
    the response (e.g. the comp row arrived via the artist search's
    album-title leg — the LML#717 shape), nothing new is appended but the
    index has still proven the track is on a returned row, so the response
    must read found-on-compilation, not not-found.

    Keyed on the explicit ``folded_location_count``, never on ``matched_via``
    presence in ``result_items`` — the primary track-match strategies
    (SONG_AS_TRACK, TRACK_ON_COMPILATION) already stamp ``matched_via`` on
    PRIMARY rows (``lookup/strategies/track_release_matching.py``), so
    scanning ``result_items`` for the hint cannot distinguish a folded shelf
    location from a primary track match.

    Callers that don't need the full bundle — the degraded/shed early
    returns, which keep ``context_message=None`` and ``external_source=None``
    unconditionally, per the pre-existing degraded contract — read only the
    ``song_not_found``/``found_on_compilation`` pair and discard the rest.
    """
    if folded_location_count == 0 and not index_confirmed_existing:
        context_message = build_context_message(
            parsed,
            state.found_on_compilation,
            state.song_not_found,
            has_results=state.has_results,
        )
        return (
            state.song_not_found,
            state.found_on_compilation,
            state.search_type,
            context_message,
            external_source,
        )

    # A response the location union contributed to always reads as
    # found-on-compilation: at least one row in `results` is a
    # compilation/soundtrack shelf location, whether or not the artist's own
    # release also matched (Case A) or not (Case B).
    song_not_found = False
    found_on_compilation = True
    search_type = "compilation"
    context_message = build_context_message(
        parsed, found_on_compilation, song_not_found, has_results=True
    )
    resolved_external_source = (
        external_source if external_source in ("discogs", "musicbrainz") else "library"
    )
    return (
        song_not_found,
        found_on_compilation,
        search_type,
        context_message,
        resolved_external_source,
    )


def _project_post_fold_trace_attrs(result_items: list[LookupResultItem], search_type: str) -> None:
    """Re-project ``lookup.results_count``/``lookup.match_type`` after the
    location-union fold, overwriting whatever ``_step_project_trace_attrs``
    projected pre-fold.

    That call reads ``state.result_count``, which derives from
    ``state.items_with_artwork``/``state.library_results`` — the folded
    locations never enter ``LookupState``, so its projection is stale exactly
    when this matters. Observability must not break the request path; the
    failure mode mirrors ``_step_project_trace_attrs``.
    """
    try:
        scope = sentry_sdk.get_current_scope()
        if scope.transaction is not None:
            scope.transaction.set_data("lookup.results_count", len(result_items))
            scope.transaction.set_data("lookup.match_type", search_type)
    except Exception:
        # Observability must not break the request path.
        pass


async def _build_degraded_response(
    state: LookupState,
    parsed: ParsedRequest,
    *,
    degraded_reason: DegradedReason,
    location_union_task: asyncio.Task[list[ResolvedLocation]] | None,
) -> LookupResponse:
    """Build a degraded ``LookupResponse`` after the tail was shed (LML#755 / LML#930).

    Returns the library rows accumulated so far with **no** live-Discogs
    enrichment (artwork/streaming/identity resolution shed) and no context
    message. Identities are empty (identity resolution itself can shed), so
    ``_build_result_items`` binds each row with ``reconciled_identity=None``.
    ``timeout`` is left as the search pipeline reported it. This degrades the
    hot path to fast, partial metadata instead of a 500.

    ``degraded_reason`` names *why* the tail was shed — ``upstream_unavailable``
    for a Discogs saturation-breaker shed (LML#755), ``deadline_exceeded`` for a
    caller-deadline shed (LML#930 PR1), ``cache_only`` for a load-adaptive
    admission shed of low-priority work under sustained loop lag (LML#930 PR2) —
    and is surfaced both on the wire (``degraded=True`` + ``degraded_reason``)
    and as the filterable ``lml.degraded_reason`` Sentry tag so the canary
    (wxyc-canary#82) and LML#931 can slice degraded-mode rate by cause without
    decoding response bodies.

    The location union's other shelf locations are folded in here too — a
    Discogs-breaker/admission/deadline shed is exactly when a DJ most needs
    the physical-shelf backups, and they cost no live enrichment (PG-only,
    already resolved or resolving concurrently). The await is *bounded*
    (:func:`_await_location_union_bounded`, ~250ms): a shed path is exactly
    where blocking on a stalled probe would be counterproductive. The
    ``found_on_compilation``/``song_not_found``/``search_type`` signals are
    recomputed the same way the happy path does
    (:func:`_reconcile_post_fold_signals`) — without this, a Case-B shed would
    emit location rows while still claiming
    ``song_not_found=True``/``found_on_compilation=False`` with a stale
    ``search_type``, the exact desync the happy-path reconciliation exists to
    eliminate. ``context_message`` and
    ``external_source`` stay ``None`` unconditionally either way, matching the
    pre-existing degraded contract.
    """
    try:
        sentry_sdk.set_tag("lml.degraded_reason", degraded_reason.value)
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("Failed to project degraded_reason onto Sentry transaction: %s", e)
    result_items = _build_result_items(state, {})
    folded_locations = await _await_location_union_bounded(location_union_task)
    result_items, folded_location_count, index_confirmed_existing = _fold_locations_into_results(
        result_items, folded_locations, state
    )

    song_not_found, found_on_compilation, search_type, _context_message, _external_source = (
        _reconcile_post_fold_signals(
            parsed,
            state,
            folded_location_count=folded_location_count,
            index_confirmed_existing=index_confirmed_existing,
            external_source=None,
        )
    )

    return LookupResponse(
        results=result_items,
        search_type=search_type,
        song_not_found=song_not_found,
        found_on_compilation=found_on_compilation,
        context_message=None,
        corrected_artist=state.corrected_artist,
        external_source=None,
        timeout=state.timed_out,
        degraded=True,
        degraded_reason=degraded_reason,
    )
