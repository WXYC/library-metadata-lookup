"""TRACK_ON_COMPILATION — cross-reference the song against Discogs compilations.

Fires when ARTIST_PLUS_ALBUM didn't find the song directly. Asks Discogs
"what compilations contain this track by this artist?" and matches those
compilation titles back against the WXYC library. The execute func's
second tuple element is the per-library-id Discogs title map used by the
artwork-fetch step.

Carries the artist-fallback **stash** rule: when prior results exist AND
``state.song_not_found`` is True, the runner preserves those prior results
into ``state.artist_fallback_results`` before replacing them with the
compilation hit. ``perform_lookup`` then validates the stash against
Discogs tracklists and merges any confirmed matches back into the final
results. Today only this strategy uses the rule; ``_apply`` honors the
``preserve_prior_results_as_fallback`` flag declaratively so adding a
second user of the same rule is a one-liner.
"""

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from functools import partial
from typing import Any, ClassVar, Protocol

import sentry_sdk
from wxyc_etl.text import is_compilation_artist
from wxyc_etl.text import to_match_form as normalize_for_comparison

from config.settings import get_settings
from core.search import (
    Outcome,
    SearchState,
    SearchStrategyType,
    song_not_found_with_artist_and_song,
)
from discogs.lookup import lookup_releases_by_track
from discogs.models import ReleaseInfo, TrackReleasesResponse
from discogs.service import DiscogsService
from entity.sources import PgSource
from library.db import STOPWORDS, LibraryDB
from library.models import LibraryItem
from lookup.artist_resolution import (
    ResolverOutcome,
    _log_resolver_pre_pass,
    resolve_canonical_artist,
)
from lookup.candidate_memo import TrackCandidateMemo, TrackCandidateSet
from lookup.concurrency import _chunked_gather
from lookup.matching import (
    _FALLBACK_ARTIST_SIMILARITY_FLOOR,
    _FETCH_LIMIT,
    MAX_SEARCH_RESULTS,
    WAVE_A_SEARCH_LIMIT,
    artist_matches_item,
    library_artist_for,
    limit_results,
)
from lookup.release_resolution import (
    ResolvedRelease,
    has_va_compilation,
    merge_wave_b_compilations,
    rank_resolved_releases,
    validate_release_for_track,
)
from lookup.rowless import (
    ROWLESS_LIBRARY_ID,
    _make_rowless_item,
    _resolve_nonlibrary_release,
)
from lookup.strategies.track_release_matching import search_album_fuzzy
from services.parser import ParsedRequest

logger = logging.getLogger(__name__)

_COMPILATION_TITLE_RATIO_FLOOR = 80
_COMPILATION_TITLE_LENGTH_RATIO_FLOOR = 0.9
"""LML#973: the compilation carve-out's ``fuzz.ratio`` floor alone can't tell
"same comp, reformatted title" from "different comp, similarly-worded title" —
e.g. "Greatest Hits Of The 50's" vs "Greatest hits of the 50s & 60s" clears
``ratio >= 80`` (87.3) even though the second title names a different,
track-less pressing. Requiring the shorter title to be at least 90% the
length of the longer catches that case (length ratio 0.83) while a genuine
reformatting — punctuation, an added "!", "Vol." vs "Vol" — stays
length-comparable and still clears both floors."""


def _compilation_title_length_ratio(a: str, b: str) -> float:
    """``min(len)/max(len)`` of two titles, in [0, 1]; 0.0 if either is empty."""
    if not a or not b:
        return 0.0
    return min(len(a), len(b)) / max(len(a), len(b))


TrackOnCompilationExecute = Callable[
    [LibraryDB, ParsedRequest],
    Awaitable[tuple[list[LibraryItem], dict[int, ResolvedRelease]]],
]


@dataclass(frozen=True)
class TrackOnCompilation:
    """Match the song against compilation tracklists via Discogs cross-reference."""

    name: ClassVar[SearchStrategyType] = SearchStrategyType.TRACK_ON_COMPILATION

    db: LibraryDB
    execute: TrackOnCompilationExecute
    """Production: ``functools.partial(search_compilations_for_track,
    discogs_service=discogs_service)``."""

    def should_attempt(self, parsed: ParsedRequest, state: SearchState, raw_message: str) -> bool:
        return song_not_found_with_artist_and_song(parsed, state, raw_message)

    async def attempt(self, parsed: ParsedRequest, state: SearchState, raw_message: str) -> Outcome:
        items, discogs_titles = await self.execute(self.db, parsed)
        if not items:
            return Outcome.empty()
        return Outcome.compilation(items, discogs_titles=discogs_titles)


_ProbeResult = TrackReleasesResponse | tuple[TrackReleasesResponse | None, str | None]
"""Heterogeneous gather result: artist-scoped probes return
``TrackReleasesResponse``; the album-title probe returns ``(response, error)``."""


class ReleaseProcessor(Protocol):
    """Per-release worker ``_make_release_processor`` builds, ``partial()``-rebindable."""

    async def __call__(
        self,
        release_info: ReleaseInfo,
        *,
        skip_self_named_album: bool = ...,
        skip_artist_match_filter: bool = ...,
    ) -> list[tuple[LibraryItem, "ResolvedRelease"]]: ...


def _log_album_title_fallback(
    *,
    album: str,
    n_candidates: int,
    surfaced_library_match: bool,
    error: str | None = None,
) -> None:
    """Emit telemetry for the album-title fallback (#319 / #237).

    Mirrors the ``_log_resolver_pre_pass`` shape. The fallback's firing
    population grew significantly when the gate changed from ``not raw_releases``
    to ``not results`` (see WXYC/library-metadata-lookup#322 review), so each
    fire is recorded both as an INFO log line and as a Sentry transaction
    ``data.album_title_fallback`` attribute. Sentry can answer "what
    percentage of /lookup calls trigger this fallback, and what's the
    surface rate?" without re-pulling Railway logs.

    No-op when there's no active Sentry transaction. Any SDK error is
    swallowed so observability never breaks /lookup.
    """
    payload: dict[str, Any] = {
        "album": album,
        "n_candidates": n_candidates,
        "surfaced_library_match": surfaced_library_match,
    }
    if error is not None:
        payload["error"] = error
        logger.warning("album_title_fallback %s", payload)
    else:
        logger.info("album_title_fallback %s", payload)
    try:
        transaction = sentry_sdk.get_current_scope().transaction
        if transaction is not None:
            transaction.set_data("album_title_fallback", payload)
    except Exception as e:
        logger.warning("Failed to project album_title_fallback onto Sentry transaction: %s", e)


async def _fetch_release_artist_variations(
    discogs_service: DiscogsService | None, release_id: int
) -> set[str]:
    """Normalized name-variation set for a release's artist (LML#971 bridge).

    Best-effort: no service, no release id, or *any* failure yields an empty set,
    so an identity-bridge lookup can never degrade the strategy (the service
    wrapper already turns a cache miss/outage into an empty set; this guards
    anything else). Variations that normalize to empty are dropped, so a
    degenerate variation (e.g. an ANV of ``"*"``) can't match a blank row.

    Note: a multi-credit release returns the variations of *every* ``extra=0``
    artist; artist-level scoping isn't possible from a bare ``release_id`` and the
    typed-artist ``validate_release_for_track`` gate downstream is the belt.
    """
    if discogs_service is None or not release_id:
        return set()
    try:
        variations = await discogs_service.get_release_artist_variations(release_id)
    except Exception as exc:  # noqa: BLE001 - best-effort enrichment
        logger.debug("identity-bridge variation lookup failed: %s", exc)
        return set()
    return {n for v in variations if (n := normalize_for_comparison(v))}


def _row_identity_in_variations(item: LibraryItem, normalized_variations: set[str]) -> bool:
    """True when the row's ``artist`` or ``alternate_artist_name`` is an EXACT
    normalized member of ``normalized_variations`` -- exact membership, not a
    prefix, so a bare variation like "Yeh" can't admit an unrelated longer row."""
    if not normalized_variations:
        return False
    return any(
        candidate and normalize_for_comparison(candidate) in normalized_variations
        for candidate in (item.artist, item.alternate_artist_name)
    )


async def _search_by_keyword(
    db: LibraryDB, lib_artist: str | None, parsed: ParsedRequest
) -> list[LibraryItem]:
    """Phase: direct keyword search, artist-filtered — a last-resort source
    used only if the Discogs probe pass finds nothing; never blocks it."""
    try:
        artist_words = re.sub(r"[^\w\s]", " ", lib_artist.lower()).split() if lib_artist else []
        song_words = re.sub(r"[^\w\s]", " ", parsed.song.lower()).split() if parsed.song else []

        sig_artist = [w for w in artist_words if len(w) > 3 and w not in STOPWORDS]
        sig_song = [w for w in song_words if len(w) > 3 and w not in STOPWORDS]

        query_words = sig_artist[:2] + sig_song[:2]
        if not query_words:
            return []

        keyword_query = " ".join(query_words)
        logger.info(f"Trying direct keyword search: '{keyword_query}'")
        keyword_results = await db.search(query=keyword_query, limit=_FETCH_LIMIT)
        if not keyword_results:
            return []

        filtered_results = []
        for item in keyword_results:
            if lib_artist and artist_matches_item(item, lib_artist):
                filtered_results.append(item)
            elif is_compilation_artist(item.artist or ""):
                filtered_results.append(item)

        if filtered_results:
            logger.info(
                f"Found {len(filtered_results)} matches via keyword search (after artist filter)"
            )
        return filtered_results
    except Exception as e:
        logger.warning(f"Keyword search failed: {e}")
        return []


async def _album_title_probe_safe(
    discogs_service: DiscogsService, album: str
) -> tuple[TrackReleasesResponse | None, str | None]:
    """Catch-and-return wrapper so a Discogs failure here doesn't take down
    the artist-scoped probes in the same gather (error logged by the caller
    via ``_log_album_title_fallback``)."""
    try:
        resp = await discogs_service.search_releases_by_album_title(album)
        return resp, None
    except Exception as exc:
        return None, str(exc)


async def _probes_from_memo_seed(
    memo_seed: TrackCandidateSet,
    discogs_service: DiscogsService,
    song_search: str,
    artist_for_probes: str | None,
    parsed: ParsedRequest,
    album_fallback_should_fire: bool,
) -> tuple[list[ReleaseInfo], TrackReleasesResponse | None, str | None]:
    """LML#867: Wave A came free from ``memo_seed`` (caller proved it's at
    least as wide as a fresh Wave A). Only Wave B still fires, and only when
    the seed lacks a true-V/A comp — what ``merge_wave_b_compilations``
    would discard anyway (same ``has_va_compilation`` predicate)."""
    wave_a_releases = list(memo_seed.releases)
    seed_has_va = has_va_compilation(wave_a_releases)

    tail_probes: list[Coroutine[Any, Any, _ProbeResult]] = []
    wave_b_index: int | None = None
    if not seed_has_va:
        wave_b_index = len(tail_probes)
        tail_probes.append(
            discogs_service.search_releases_by_track(
                song_search, artist_for_probes, artist_as_keyword=True
            )
        )
    album_probe_index: int | None = None
    if album_fallback_should_fire:
        assert parsed.album is not None  # narrow for type-checker
        album_probe_index = len(tail_probes)
        tail_probes.append(_album_title_probe_safe(discogs_service, parsed.album))

    gathered_tail = await asyncio.gather(*tail_probes) if tail_probes else []

    wave_b_releases: list[ReleaseInfo] = []
    if wave_b_index is not None:
        va_response = gathered_tail[wave_b_index]
        assert isinstance(va_response, TrackReleasesResponse)
        wave_b_releases = list(va_response.releases or [])

    album_fallback_response: TrackReleasesResponse | None = None
    album_fallback_error: str | None = None
    if album_probe_index is not None:
        probe_tuple = gathered_tail[album_probe_index]
        assert isinstance(probe_tuple, tuple)
        album_fallback_response, album_fallback_error = probe_tuple

    raw_releases = merge_wave_b_compilations(wave_a_releases, wave_b_releases)
    return raw_releases, album_fallback_response, album_fallback_error


async def _probes_fresh_gather(
    discogs_service: DiscogsService,
    song_search: str,
    artist_for_probes: str | None,
    parsed: ParsedRequest,
    album_fallback_should_fire: bool,
) -> tuple[list[ReleaseInfo], TrackReleasesResponse | None, str | None]:
    """Pre-#867 parallel gather: Wave A (artist-scoped, PG cache CTE), Wave B
    (``artist_as_keyword=True``, API-only — LML#817 finding C2), and the
    album-title probe (#339), fired together so cold-cache wall time is
    ``max(A, B, C)``."""
    probes: list[Coroutine[Any, Any, _ProbeResult]] = [
        discogs_service.search_releases_by_track(
            song_search, artist_for_probes, limit=WAVE_A_SEARCH_LIMIT
        ),
        discogs_service.search_releases_by_track(
            song_search, artist_for_probes, artist_as_keyword=True
        ),
    ]
    if album_fallback_should_fire:
        assert parsed.album is not None  # narrow for type-checker
        probes.append(_album_title_probe_safe(discogs_service, parsed.album))

    gathered = await asyncio.gather(*probes)
    # gathered[0] / gathered[1] are TrackReleasesResponse by construction
    # (the order matches the `probes` list above); narrow via assert.
    assert isinstance(gathered[0], TrackReleasesResponse)
    assert isinstance(gathered[1], TrackReleasesResponse)
    response, va_response = gathered[0], gathered[1]

    album_fallback_response: TrackReleasesResponse | None = None
    album_fallback_error: str | None = None
    if album_fallback_should_fire:
        probe_tuple = gathered[2]
        assert isinstance(probe_tuple, tuple)
        album_fallback_response, album_fallback_error = probe_tuple

    # Merge Wave B's V/A compilations into Wave A unless Wave A already
    # surfaced a true-V/A hit (WXYC#527).
    raw_releases = merge_wave_b_compilations(
        list(response.releases or []), list(va_response.releases or [])
    )
    return raw_releases, album_fallback_response, album_fallback_error


@dataclass
class _ProbeBundle:
    """Output of ``_run_discogs_probes``: the merged candidate releases plus the
    speculative album-title probe's outcome, ready for the caller to consume."""

    song_search: str
    raw_releases: list[ReleaseInfo]
    album_fallback_should_fire: bool
    album_fallback_response: TrackReleasesResponse | None
    album_fallback_error: str | None


async def _resolve_artist_for_probes(
    parsed: ParsedRequest, discogs_service: DiscogsService | None
) -> tuple[str, ResolverOutcome]:
    """Phase: resolver pre-pass. When the inbound artist string trigram-matches
    a canonical Discogs name with confidence >= the floor, use the canonical
    form for both Discogs probes. Gated by ``lml_resolve_artist_canonical`` —
    the flag controls *both* the trigram lookup and the swap, so the
    default-off path pays no PG cost. See WXYC/library-metadata-lookup#343
    Option 2."""
    assert parsed.artist is not None  # guaranteed by the caller's gate
    if not bool(get_settings().lml_resolve_artist_canonical):
        outcome = ResolverOutcome(
            original=parsed.artist or "",
            canonical=parsed.artist or "",
            score=0.0,
            swapped=False,
        )
        return parsed.artist, outcome

    cache_service = getattr(discogs_service, "cache_service", None)
    outcome = await resolve_canonical_artist(parsed.artist, cache_service=cache_service)
    _log_resolver_pre_pass(outcome, actual_swap=outcome.swapped)
    artist_for_probes = outcome.canonical if outcome.swapped else parsed.artist
    return artist_for_probes, outcome


async def _run_discogs_probes(
    parsed: ParsedRequest,
    discogs_service: DiscogsService | None,
    candidate_memo: TrackCandidateMemo | None,
) -> _ProbeBundle:
    """Phase: candidate discovery via Discogs. Resolver pre-pass
    (``_resolve_artist_for_probes``) -> Wave A/Wave B artist-scoped probes
    (reused from ``candidate_memo`` when LML#867's recall guard clears, else
    freshly gathered) -> the optional speculative album-title probe (#339),
    all in parallel so a cold cache pays ``max()`` rather than the sum.
    Two-channel seam (#626): probes use ``parsed.artist``, not the caller's
    library-side ``lib_artist``."""
    assert parsed.song is not None and parsed.artist is not None  # guaranteed by the caller's gate

    # Widen the Discogs query to include a parenthetical remix/mix/version/edit
    # tag when the raw request actually typed one, so e.g. "Song (Extended
    # Mix)" searches Discogs as the full title rather than just "Song".
    raw_lower = parsed.raw_message.lower()
    song_search = parsed.song
    remix_match = re.search(r"\((.*?(?:remix|mix|version|edit).*?)\)", raw_lower, re.IGNORECASE)
    if remix_match and parsed.song.lower() in raw_lower:
        song_search = f"{parsed.song} ({remix_match.group(1)})"
        logger.info(f"Using full track name with version info: '{song_search}'")

    artist_for_probes, outcome = await _resolve_artist_for_probes(parsed, discogs_service)

    # The album-title fallback's preconditions (`parsed.album` set, no
    # resolver swap) are decidable here, so its probe joins the same
    # `asyncio.gather` as the two artist-scoped probes (WXYC/library-
    # metadata-lookup#339).
    album_fallback_should_fire = (
        discogs_service is not None and bool(parsed.album) and not outcome.swapped
    )

    # LML#867: reuse step 2's artist-filtered candidates as Wave A's seed when
    # the memo holds this exact query. Recall guard: reuse only when the seed
    # was fetched at least as wide as a fresh Wave A would search — a
    # narrower seed can be a strict subset, so on a too-narrow seed we fall
    # through to the fresh gather below.
    memo_seed = (
        candidate_memo.get(song_search, artist_for_probes) if candidate_memo is not None else None
    )
    if memo_seed is not None and memo_seed.search_limit < WAVE_A_SEARCH_LIMIT:
        memo_seed = None

    if discogs_service and memo_seed is not None:
        raw_releases, album_fallback_response, album_fallback_error = await _probes_from_memo_seed(
            memo_seed,
            discogs_service,
            song_search,
            artist_for_probes,
            parsed,
            album_fallback_should_fire,
        )
    elif discogs_service:
        raw_releases, album_fallback_response, album_fallback_error = await _probes_fresh_gather(
            discogs_service, song_search, artist_for_probes, parsed, album_fallback_should_fire
        )
    else:
        # No injected service — fall back to lookup helper (validates all).
        tuples = await lookup_releases_by_track(song_search, parsed.artist, service=None)
        raw_releases = [
            ReleaseInfo(
                album=album,
                artist=artist,
                release_id=0,
                release_url="",
                is_compilation=is_compilation_artist(artist),
            )
            for artist, album in tuples
        ]
        album_fallback_response, album_fallback_error = None, None

    return _ProbeBundle(
        song_search=song_search,
        raw_releases=raw_releases,
        album_fallback_should_fire=album_fallback_should_fire,
        album_fallback_response=album_fallback_response,
        album_fallback_error=album_fallback_error,
    )


async def _filter_release_matches(
    matches: list[LibraryItem],
    lib_artist: str,
    release_info: ReleaseInfo,
    discogs_service: DiscogsService | None,
    *,
    strict: bool,
) -> list[LibraryItem]:
    """Artist-verification phase. ``strict=True``: a row passes on a direct
    artist-prefix match, a title-matched Various-Artists row, or — LML#971
    identity bridge, flag-gated, non-compilation only — a resolved Discogs
    name-variation. ``strict=False`` (album-title fallback, where trio rows
    won't prefix-match): a lenient similarity backstop
    (``_FALLBACK_ARTIST_SIMILARITY_FLOOR``) instead. Either way,
    ``validate_release_for_track`` downstream remains the safety belt."""
    from rapidfuzz import fuzz as _fuzz

    discogs_is_compilation = release_info.is_compilation
    release_album_lower = release_info.album.lower()

    if not strict:
        lib_artist_norm = normalize_for_comparison(lib_artist)

        def _fallback_row_acceptable(match: LibraryItem) -> bool:
            if discogs_is_compilation and is_compilation_artist(match.artist or ""):
                match_title_lower = (match.title or "").lower()
                title_score = _fuzz.ratio(release_album_lower, match_title_lower)
                length_ratio = _compilation_title_length_ratio(
                    release_album_lower, match_title_lower
                )
                return (
                    title_score >= _COMPILATION_TITLE_RATIO_FLOOR
                    and length_ratio >= _COMPILATION_TITLE_LENGTH_RATIO_FLOOR
                )
            return (
                max(
                    _fuzz.token_set_ratio(
                        lib_artist_norm, normalize_for_comparison(match.artist or "")
                    ),
                    _fuzz.token_set_ratio(
                        lib_artist_norm, normalize_for_comparison(match.alternate_artist_name or "")
                    ),
                )
                >= _FALLBACK_ARTIST_SIMILARITY_FLOOR
            )

        return [match for match in matches if _fallback_row_acceptable(match)]

    bridge_enabled = get_settings().lml_resolve_artist_variations and not discogs_is_compilation
    release_variations: set[str] | None = None
    filtered_matches = []
    for match in matches:
        if artist_matches_item(match, lib_artist):
            filtered_matches.append(match)
        elif discogs_is_compilation and is_compilation_artist(match.artist or ""):
            match_title_lower = (match.title or "").lower()
            title_score = _fuzz.ratio(release_album_lower, match_title_lower)
            length_ratio = _compilation_title_length_ratio(release_album_lower, match_title_lower)
            if (
                title_score >= _COMPILATION_TITLE_RATIO_FLOOR
                and length_ratio >= _COMPILATION_TITLE_LENGTH_RATIO_FLOOR
            ):
                filtered_matches.append(match)
            else:
                logger.debug(
                    f"Rejected '{match.title}' for '{release_info.album}' "
                    f"(title_score={title_score:.0f}, length_ratio={length_ratio:.2f})"
                )
        elif bridge_enabled:
            if release_variations is None:
                release_variations = await _fetch_release_artist_variations(
                    discogs_service, release_info.release_id
                )
            if _row_identity_in_variations(match, release_variations):
                filtered_matches.append(match)
    return filtered_matches


def _make_release_processor(
    db: LibraryDB,
    parsed: ParsedRequest,
    lib_artist: str | None,
    discogs_service: DiscogsService | None,
    song_search: str,
) -> ReleaseProcessor:
    """Build the per-release worker (library search -> artist verification ->
    tracklist validation), closed over context shared by both passes."""

    async def process_release(
        release_info: ReleaseInfo,
        *,
        skip_self_named_album: bool = True,
        skip_artist_match_filter: bool = False,
    ) -> list[tuple[LibraryItem, ResolvedRelease]]:
        """Process one Discogs release: library search, filter, validate.

        ``skip_self_named_album`` defaults True to preserve existing behavior
        for callers that arrived via artist-scoped probes. The album-title
        fallback (WXYC/library-metadata-lookup#319) passes False because the
        trio-collaboration case has ``album == artist`` by design.

        ``skip_artist_match_filter`` defaults False. When True, the
        library-side ``artist_matches_item`` prefix-filter is skipped and
        artist gating is deferred to ``validate_track_on_release``'s fuzzy
        ``rapidfuzz.token_set_ratio`` (PR #236). The fallback path passes
        True because the library row's artist string for trio/collaborative
        releases (e.g. ``"Orcutt, Bill / Shelley, Chris / Miller, Mette"``)
        won't prefix-match a user-typed ``"Orcutt Shelley Miller"``, but the
        fuzzy validator on the Discogs side does accept it.
        """
        release_album = release_info.album

        if skip_self_named_album and parsed.artist:
            album_clean = release_album.lower().replace('"', "").replace("'", "").strip()
            artist_clean = parsed.artist.lower().replace('"', "").replace("'", "").strip()
            if album_clean == artist_clean:
                logger.debug(f"Skipping '{release_album}' - appears to be artist name, not album")
                return []

        if len(release_album.strip()) < 3:
            return []

        matches = await search_album_fuzzy(db, release_album)

        # If album-only search failed for a compilation, retry with "Various"
        # to help FTS5 match entries stored as "Various Artists - ..."
        if not matches and release_info.is_compilation:
            matches = await search_album_fuzzy(db, f"Various {release_album}")

        if matches and lib_artist:
            matches = await _filter_release_matches(
                matches,
                lib_artist,
                release_info,
                discogs_service,
                strict=not skip_artist_match_filter,
            )

        if not matches:
            return []

        # Validate that the track actually exists on this release.
        # Deferred until after library matching so we only validate
        # releases we might actually return — saving API calls (LML#536).
        if discogs_service and release_info.release_id and parsed.artist:
            is_valid = await validate_release_for_track(
                discogs_service,
                release_info.release_id,
                song_search,
                parsed.artist,
                source="compilation_inline",
            )
            if not is_valid:
                logger.info(f"Skipping '{release_album}' - track/artist not validated on release")
                return []

        logger.info(
            f"Found '{parsed.song}' in library on '{matches[0].title}' "
            f"(matched from Discogs: '{release_album}')"
        )
        resolved = ResolvedRelease(
            release_id=release_info.release_id,
            release_url=release_info.release_url,
            is_compilation=bool(release_info.is_compilation),
            album_title=release_album,
        )
        return [(match, resolved) for match in matches]

    return process_release


async def _collect_release_matches(
    raw_releases: list[ReleaseInfo],
    process_release: ReleaseProcessor,
    *,
    results: list[LibraryItem],
    seen_ids: set[int],
    discogs_titles: dict[int, ResolvedRelease],
    rank_carried: bool,
) -> None:
    """Chunked dispatch + accumulate, shared by the primary Discogs-probe pass
    and the album-title fallback pass. Chunking bounds the per-request
    validate budget once the response cap is hit (LML#536). Mutates
    ``results``/``seen_ids``/``discogs_titles`` in place; the latter binds
    each match's best ``ResolvedRelease`` (LML#604 #2) — ``rank_carried`` on
    ranks title-best across releases, off keeps first-seen."""
    async for _, release_matches in _chunked_gather(
        raw_releases, process_release, MAX_SEARCH_RESULTS
    ):
        for match, resolved in release_matches:
            if match.id not in seen_ids:
                results.append(match)
                seen_ids.add(match.id)
            existing = discogs_titles.get(match.id)
            if existing is None:
                discogs_titles[match.id] = resolved
            elif rank_carried:
                discogs_titles[match.id] = rank_resolved_releases(
                    [existing, resolved], match.title
                )[0]
        if len(results) >= MAX_SEARCH_RESULTS:
            break


async def _apply_album_title_fallback(
    *,
    parsed: ParsedRequest,
    process_release: ReleaseProcessor,
    album_fallback_should_fire: bool,
    album_fallback_response: TrackReleasesResponse | None,
    album_fallback_error: str | None,
    results: list[LibraryItem],
    seen_ids: set[int],
    discogs_titles: dict[int, ResolvedRelease],
    rank_carried: bool,
) -> bool:
    """Album-title fallback (#319 + #237): consume the speculative title-only
    probe fired in ``_run_discogs_probes`` (#339), with the trio-aware kwargs
    (no self-named-album guard, no library-side artist filter). Gate is
    ``not results`` — conservative, backfilling the zero-results case rather
    than augmenting partial ones. A probe failure only logs, never raises.
    Returns True when the fallback surfaced candidates (``discogs_found_releases``).
    """
    if not album_fallback_should_fire:
        return False

    pre_fallback_results_count = len(results)
    # `album_fallback_should_fire` implies `parsed.album is not None` (see the
    # precondition in `_run_discogs_probes`); each branch below re-asserts to
    # narrow the type locally for the type-checker.
    if album_fallback_error is not None:
        assert parsed.album is not None
        _log_album_title_fallback(
            album=parsed.album,
            n_candidates=0,
            surfaced_library_match=False,
            error=album_fallback_error,
        )
        return False

    if results or album_fallback_response is None:
        return False

    assert parsed.album is not None
    fallback_releases = list(album_fallback_response.releases or [])
    if fallback_releases:
        logger.info(
            f"Album-title fallback returned {len(fallback_releases)} candidates "
            f"for '{parsed.album}'"
        )

    fallback_worker = partial(
        process_release, skip_self_named_album=False, skip_artist_match_filter=True
    )
    await _collect_release_matches(
        fallback_releases,
        fallback_worker,
        results=results,
        seen_ids=seen_ids,
        discogs_titles=discogs_titles,
        rank_carried=rank_carried,
    )
    _log_album_title_fallback(
        album=parsed.album,
        n_candidates=len(fallback_releases),
        surfaced_library_match=len(results) > pre_fallback_results_count,
    )
    return bool(fallback_releases)


async def _carry_through_nonlibrary_release(
    parsed: ParsedRequest,
    discogs_service: DiscogsService | None,
    pg: PgSource | None,
    allow_release_resolution_fallback: bool,
    results: list[LibraryItem],
    discogs_titles: dict[int, ResolvedRelease],
) -> LibraryItem | None:
    """A1 carry-through (LML#628): resolve + validate off a non-library
    release and surface it row-less. Keyed on typed ``parsed.artist`` (#626).
    Off by default; LML#652's bulk kill switch skips /lookup/bulk. Mutates
    ``discogs_titles`` in place on a hit; ``None`` otherwise."""
    if (
        results
        or not get_settings().lml_resolve_nonlibrary_release
        or not allow_release_resolution_fallback
        or not parsed.song
    ):
        return None

    resolved_nonlibrary = await _resolve_nonlibrary_release(
        discogs_service,
        pg,
        song=parsed.song,
        artist=parsed.artist or "",
        album=parsed.album,
        is_track=True,
    )
    if resolved_nonlibrary is None:
        return None

    rowless = _make_rowless_item(artist=parsed.artist or "", title=resolved_nonlibrary.album_title)
    discogs_titles[ROWLESS_LIBRARY_ID] = resolved_nonlibrary
    logger.info(
        f"TRACK_ON_COMPILATION: surfacing row-less Discogs release "
        f"{resolved_nonlibrary.release_id} ('{resolved_nonlibrary.album_title}') "
        f"— validated, not in library"
    )
    return rowless


async def search_compilations_for_track(
    db: LibraryDB,
    parsed: ParsedRequest,
    discogs_service: DiscogsService | None = None,
    *,
    pg: PgSource | None = None,
    allow_release_resolution_fallback: bool = True,
    candidate_memo: TrackCandidateMemo | None = None,
) -> tuple[list[LibraryItem], dict[int, ResolvedRelease]]:
    """Search for track on compilation albums using Discogs and library keyword search.

    The second tuple element maps each surfaced library id to the
    :class:`~lookup.release_resolution.ResolvedRelease` it matched — the widened
    ``discogs_titles`` seam consumed by the artwork-binding step.

    The body is a sequence of named phases sharing the request-scoped
    ``results``/``seen_ids``/``discogs_titles`` accumulators:
    ``_search_by_keyword`` (last-resort candidates) -> ``_run_discogs_probes``
    (Wave A/B + album-title discovery, reusing ``candidate_memo`` per
    LML#867) -> ``_make_release_processor``/``_collect_release_matches``
    (tracklist cross-reference + artist verification) ->
    ``_apply_album_title_fallback`` -> result shaping ->
    ``_carry_through_nonlibrary_release`` (LML#628, gated by
    ``lml_resolve_nonlibrary_release`` + ``allow_release_resolution_fallback``).
    """
    if not parsed.song or not parsed.artist:
        return [], {}

    logger.info(f"Searching for '{parsed.song}' on other releases (compilations, etc.)")

    results: list[LibraryItem] = []
    seen_ids: set[int] = set()
    discogs_titles: dict[int, ResolvedRelease] = {}
    lib_artist = library_artist_for(parsed)
    rank_carried = get_settings().lml_resolve_compilation_release

    keyword_matches = await _search_by_keyword(db, lib_artist, parsed)

    discogs_found_releases = False
    try:
        probes = await _run_discogs_probes(parsed, discogs_service, candidate_memo)
        song_search = probes.song_search
        raw_releases = probes.raw_releases

        logger.info(f"Found {len(raw_releases)} releases with '{song_search}' on Discogs")
        discogs_found_releases = len(raw_releases) > 0

        process_release = _make_release_processor(
            db, parsed, lib_artist, discogs_service, song_search
        )

        await _collect_release_matches(
            raw_releases,
            process_release,
            results=results,
            seen_ids=seen_ids,
            discogs_titles=discogs_titles,
            rank_carried=rank_carried,
        )

        fallback_found_releases = await _apply_album_title_fallback(
            parsed=parsed,
            process_release=process_release,
            album_fallback_should_fire=probes.album_fallback_should_fire,
            album_fallback_response=probes.album_fallback_response,
            album_fallback_error=probes.album_fallback_error,
            results=results,
            seen_ids=seen_ids,
            discogs_titles=discogs_titles,
            rank_carried=rank_carried,
        )
        discogs_found_releases = discogs_found_releases or fallback_found_releases
    except Exception as e:
        logger.warning(f"Failed to search for track on other releases: {e}")

    # Last resort: when Discogs found nothing at all, surface the single best
    # keyword-search hit instead of returning empty-handed.
    if not results and keyword_matches and not discogs_found_releases:
        logger.info("Discogs search found nothing, using keyword matches as fallback")
        for item in keyword_matches[:1]:
            if item.id not in seen_ids:
                results.append(item)
                seen_ids.add(item.id)

    # Prefer rows whose title contains the typed song verbatim (stable sort).
    if results and parsed.song:
        song_lower = parsed.song.lower()
        results.sort(key=lambda r: song_lower in (r.title or "").lower(), reverse=True)

    rowless = await _carry_through_nonlibrary_release(
        parsed, discogs_service, pg, allow_release_resolution_fallback, results, discogs_titles
    )
    if rowless is not None:
        return [rowless], discogs_titles

    return limit_results(results), discogs_titles
