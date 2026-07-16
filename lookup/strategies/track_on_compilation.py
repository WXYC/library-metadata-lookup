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
from typing import Any, ClassVar

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
from lookup.concurrency import _chunked_gather
from lookup.matching import (
    _FALLBACK_ARTIST_SIMILARITY_FLOOR,
    _FETCH_LIMIT,
    MAX_SEARCH_RESULTS,
    artist_matches_item,
    library_artist_for,
    limit_results,
)
from lookup.release_resolution import (
    ResolvedRelease,
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
"""Heterogeneous return type for the gathered probes in ``search_compilations_for_track``.

The two artist-scoped probes return ``TrackReleasesResponse`` directly; the
optional album-title probe returns ``tuple[TrackReleasesResponse | None,
str | None]`` (response, error_str) via its catch-and-return wrapper. The
gather sees the union; element-by-element narrowing happens via isinstance
asserts at the call site.
"""


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


async def search_compilations_for_track(
    db: LibraryDB,
    parsed: ParsedRequest,
    discogs_service: DiscogsService | None = None,
    *,
    pg: PgSource | None = None,
    allow_release_resolution_fallback: bool = True,
) -> tuple[list[LibraryItem], dict[int, ResolvedRelease]]:
    """Search for track on compilation albums using Discogs and library keyword search.

    The second tuple element maps each surfaced library id to the
    :class:`~lookup.release_resolution.ResolvedRelease` it matched — the widened
    ``discogs_titles`` seam consumed by the artwork-binding step.

    Two-channel seam (WXYC/library-metadata-lookup#626): the Discogs probes and
    ``validate_release_for_track`` use the typed ``parsed.artist`` (via
    ``artist_for_probes``); the library-side keyword search and the two
    ``artist_matches_item`` match-backs use ``lib_artist`` (the correction when
    present, else the typed name).

    A1 carry-through (LML#628): when the whole search surfaces no library row and
    ``lml_resolve_nonlibrary_release`` is on, the track's release is resolved +
    validated (via :func:`_resolve_nonlibrary_release`, keyed on the typed
    ``parsed.artist``) and surfaced row-less as ``LibraryItem(id=0)`` +
    ``discogs_titles[0]``. ``pg`` threads the #632 cache; ``None`` resolves
    uncached. ``allow_release_resolution_fallback`` is the bulk kill switch
    (LML#652): ``False`` on /lookup/bulk suppresses that carry-through entirely
    (no resolve, no cache write, no row-less item).
    """
    if not parsed.song or not parsed.artist:
        return [], {}

    logger.info(f"Searching for '{parsed.song}' on other releases (compilations, etc.)")

    results = []
    seen_ids = set()
    discogs_titles: dict[int, ResolvedRelease] = {}
    lib_artist = library_artist_for(parsed)

    # Carried-release ranking (LML#604 deferred finding #2). When the flag is on,
    # a library row matched by multiple releases binds the title-best one — the
    # same ranking the lazy fallback applies — so both binding paths agree. When
    # off, first-seen (Wave A / library-match order) wins, preserving pre-PR2
    # behavior. The rank target is the matched library row's own title.
    rank_carried = get_settings().lml_resolve_compilation_release

    def _record_resolved(match: LibraryItem, resolved: ResolvedRelease) -> None:
        existing = discogs_titles.get(match.id)
        if existing is None:
            discogs_titles[match.id] = resolved
        elif rank_carried:
            discogs_titles[match.id] = rank_resolved_releases([existing, resolved], match.title)[0]

    keyword_matches = []
    try:
        artist_words = re.sub(r"[^\w\s]", " ", lib_artist.lower()).split() if lib_artist else []
        song_words = re.sub(r"[^\w\s]", " ", parsed.song.lower()).split() if parsed.song else []

        sig_artist = [w for w in artist_words if len(w) > 3 and w not in STOPWORDS]
        sig_song = [w for w in song_words if len(w) > 3 and w not in STOPWORDS]

        query_words = sig_artist[:2] + sig_song[:2]

        if query_words:
            keyword_query = " ".join(query_words)
            logger.info(f"Trying direct keyword search: '{keyword_query}'")
            keyword_results = await db.search(query=keyword_query, limit=_FETCH_LIMIT)

            if keyword_results:
                filtered_results = []
                for item in keyword_results:
                    if lib_artist and artist_matches_item(item, lib_artist):
                        filtered_results.append(item)
                    elif is_compilation_artist(item.artist or ""):
                        filtered_results.append(item)

                if filtered_results:
                    logger.info(
                        f"Found {len(filtered_results)} matches via keyword search "
                        f"(after artist filter)"
                    )
                    keyword_matches = filtered_results
    except Exception as e:
        logger.warning(f"Keyword search failed: {e}")
        keyword_matches = []

    discogs_found_releases = False

    try:
        raw_lower = parsed.raw_message.lower()
        song_search = parsed.song

        remix_match = re.search(r"\((.*?(?:remix|mix|version|edit).*?)\)", raw_lower, re.IGNORECASE)
        if remix_match and parsed.song.lower() in raw_lower:
            song_search = f"{parsed.song} ({remix_match.group(1)})"
            logger.info(f"Using full track name with version info: '{song_search}'")

        # Resolver pre-pass: when the inbound artist string trigram-matches a
        # canonical Discogs name with confidence >= the floor, use the canonical
        # form for both Discogs probes. Gated by ``lml_resolve_artist_canonical``
        # — the flag controls *both* the trigram lookup and the swap, so the
        # default-off path pays no PG cost. See WXYC/library-metadata-lookup#343
        # Option 2.
        enforce_swap = bool(get_settings().lml_resolve_artist_canonical)
        if enforce_swap:
            cache_service = getattr(discogs_service, "cache_service", None)
            outcome = await resolve_canonical_artist(parsed.artist, cache_service=cache_service)
            actual_swap = outcome.swapped
            _log_resolver_pre_pass(outcome, actual_swap=actual_swap)
            artist_for_probes = outcome.canonical if actual_swap else parsed.artist
        else:
            outcome = ResolverOutcome(
                original=parsed.artist or "",
                canonical=parsed.artist or "",
                score=0.0,
                swapped=False,
            )
            artist_for_probes = parsed.artist

        # Get raw releases from Discogs without per-release validation.
        # We search the library first and only validate releases that match,
        # avoiding expensive API calls for releases not in our catalog.
        raw_releases: list[ReleaseInfo] = []
        # The album-title fallback's preconditions (`parsed.album` set, no
        # resolver swap) are decidable here, so its probe joins the same
        # `asyncio.gather` as the two artist-scoped probes (WXYC/library-
        # metadata-lookup#339). Cold-cache wall time drops from A+B+C to
        # max(A,B,C); the cost is one speculative API call when Wave A
        # already succeeds — that call warms the cache.
        album_fallback_should_fire = (
            discogs_service is not None and bool(parsed.album) and not outcome.swapped
        )
        album_fallback_response: TrackReleasesResponse | None = None
        album_fallback_error: str | None = None

        async def _album_title_probe_safe() -> tuple[TrackReleasesResponse | None, str | None]:
            """Catch-and-return wrapper so a Discogs failure on the album-title
            probe doesn't take down the artist-scoped probes in the same gather.
            Mirrors the existing fallback's try/except, which logs via
            `_log_album_title_fallback(..., error=str(e))`."""
            assert discogs_service is not None  # narrow for type-checker
            assert parsed.album is not None
            try:
                resp = await discogs_service.search_releases_by_album_title(parsed.album)
                return resp, None
            except Exception as exc:
                return None, str(exc)

        if discogs_service:
            # Two artist-scoped probes return TrackReleasesResponse; the optional
            # album-title probe returns tuple[TrackReleasesResponse | None, str | None].
            # _ProbeResult (module-scope alias) captures the heterogeneous shape.
            #
            # Wave A (artist-scoped) reads the PG cache CTE
            # (``_SEARCH_BY_TRACK_ARTIST_SQL``); Wave B (``artist_as_keyword=True``)
            # does NOT. ``DiscogsService.search_releases_by_track`` sets
            # ``pg_read_hook=None`` for the keyword probe and hits the Discogs API
            # ``format=Compilation`` search directly, so the #802 CTE
            # ``rta.extra = 0`` guard governs Wave A only. A V/A comp whose
            # performer is credited on the matching track via ``extra = 1``
            # (guest/featured) still reaches ``merge_wave_b_compilations`` through
            # Wave B's API arm, never pruned by that guard — LML#817 finding C2,
            # reproduced-and-refuted and pinned by
            # ``tests/integration/test_va_comp_wave_b_extra_credit.py``.
            probes: list[Coroutine[Any, Any, _ProbeResult]] = [
                discogs_service.search_releases_by_track(song_search, artist_for_probes),
                discogs_service.search_releases_by_track(
                    song_search, artist_for_probes, artist_as_keyword=True
                ),
            ]
            if album_fallback_should_fire:
                probes.append(_album_title_probe_safe())

            gathered = await asyncio.gather(*probes)
            # gathered[0] / gathered[1] are TrackReleasesResponse by construction
            # (the order matches the `probes` list above); narrow via assert.
            assert isinstance(gathered[0], TrackReleasesResponse)
            assert isinstance(gathered[1], TrackReleasesResponse)
            response, va_response = gathered[0], gathered[1]
            if album_fallback_should_fire:
                probe_tuple = gathered[2]
                assert isinstance(probe_tuple, tuple)
                album_fallback_response, album_fallback_error = probe_tuple

            # Merge Wave B's V/A compilations into Wave A unless Wave A already
            # surfaced a true-V/A hit (WXYC#527). The merge logic is owned by
            # ``lookup.release_resolution.merge_wave_b_compilations`` so the
            # release-resolution module and this strategy can't drift apart.
            raw_releases = merge_wave_b_compilations(
                list(response.releases or []), list(va_response.releases or [])
            )
        else:
            # No injected service — fall back to lookup helper (validates all)
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

        logger.info(f"Found {len(raw_releases)} releases with '{song_search}' on Discogs")
        discogs_found_releases = len(raw_releases) > 0

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

            if skip_self_named_album:
                album_clean = release_album.lower().replace('"', "").replace("'", "").strip()
                if (
                    parsed.artist
                    and album_clean
                    == parsed.artist.lower().replace('"', "").replace("'", "").strip()
                ):
                    logger.debug(
                        f"Skipping '{release_album}' - appears to be artist name, not album"
                    )
                    return []

            if len(release_album.strip()) < 3:
                return []

            matches = await search_album_fuzzy(db, release_album)

            # If album-only search failed for a compilation, retry with "Various"
            # to help FTS5 match entries stored as "Various Artists - ..."
            if not matches and release_info.is_compilation:
                matches = await search_album_fuzzy(db, f"Various {release_album}")

            if matches and lib_artist and not skip_artist_match_filter:
                from rapidfuzz import fuzz as _fuzz

                filtered_matches = []
                discogs_is_compilation = release_info.is_compilation
                release_album_lower = release_album.lower()

                for match in matches:
                    if artist_matches_item(match, lib_artist):
                        filtered_matches.append(match)
                    elif discogs_is_compilation and is_compilation_artist(match.artist or ""):
                        title_score = _fuzz.ratio(release_album_lower, (match.title or "").lower())
                        if title_score >= 80:
                            filtered_matches.append(match)
                        else:
                            logger.debug(
                                f"Rejected '{match.title}' for '{release_album}' "
                                f"(title_score={title_score:.0f})"
                            )
                matches = filtered_matches
            elif matches and lib_artist and skip_artist_match_filter:
                # The strict prefix filter above is deferred on this path, but
                # ``validate_track_on_release`` only vets the Discogs release —
                # not the surfaced library row. Apply a lenient library-side
                # artist backstop so an album-title fuzz collision can't bind a
                # wrong-artist row (see ``_FALLBACK_ARTIST_SIMILARITY_FLOOR``).
                from rapidfuzz import fuzz as _fuzz

                lib_artist_norm = normalize_for_comparison(lib_artist)
                discogs_is_compilation = release_info.is_compilation
                release_album_lower = release_album.lower()

                def _fallback_row_acceptable(match: LibraryItem) -> bool:
                    # Legitimate Various-Artists compilation rows share no artist
                    # tokens with a typed solo/track artist, so the fuzzy floor
                    # would wrongly drop them. Keep them on a title match instead,
                    # mirroring the strict branch's compilation carve-out above.
                    # ``is_compilation_artist`` is False for coincidental
                    # wrong-artist collisions (e.g. "Galaxy 2 Galaxy"), so this
                    # does not re-open the #717 bug.
                    if discogs_is_compilation and is_compilation_artist(match.artist or ""):
                        title_score = _fuzz.ratio(release_album_lower, (match.title or "").lower())
                        return title_score >= 80
                    return (
                        max(
                            _fuzz.token_set_ratio(
                                lib_artist_norm, normalize_for_comparison(match.artist or "")
                            ),
                            _fuzz.token_set_ratio(
                                lib_artist_norm,
                                normalize_for_comparison(match.alternate_artist_name or ""),
                            ),
                        )
                        >= _FALLBACK_ARTIST_SIMILARITY_FLOOR
                    )

                matches = [match for match in matches if _fallback_row_acceptable(match)]

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
                    logger.info(
                        f"Skipping '{release_album}' - track/artist not validated on release"
                    )
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

        # Chunked dispatch so the per-request validate budget stays bounded
        # once the response cap is hit. Without chunking, asyncio.gather
        # would schedule (and pay for) every candidate's
        # ``validate_track_on_release`` even after MAX_SEARCH_RESULTS
        # matches had already landed — see LML#536 and the matching
        # treatment in ``search_song_as_track``.
        async for _, release_matches in _chunked_gather(
            raw_releases, process_release, MAX_SEARCH_RESULTS
        ):
            for match, resolved in release_matches:
                if match.id not in seen_ids:
                    results.append(match)
                    seen_ids.add(match.id)
                _record_resolved(match, resolved)
            if len(results) >= MAX_SEARCH_RESULTS:
                break

        # Album-title fallback (#319 + #237): when the artist-scoped probes
        # produced no library results AND the request supplied an album AND
        # the resolver pre-pass did not produce a high-confidence canonical,
        # process the title-only Discogs candidates with the trio-aware
        # kwargs (no self-named-album guard, no library-side artist filter —
        # defer artist gating to validate_track_on_release).
        #
        # The probe itself was fired speculatively in the same `asyncio.gather`
        # as the artist-scoped probes above (#339). When it returned results
        # we already paid the Discogs API cost; here we just decide whether
        # to *consume* those candidates, gated on `not results` from Wave A.
        #
        # Trade-off: the gate is ``not results`` rather than
        # ``len(results) < MAX_SEARCH_RESULTS``. A partial initial pass (e.g.
        # one valid match) suppresses the fallback entirely, even when more
        # spots are available. Conservative-by-design — the fallback's
        # purpose is to backfill the zero-results case, not augment partial
        # ones. Revisit if measurements show partial-result requests would
        # benefit from supplementation.
        pre_fallback_results_count = len(results)
        # `album_fallback_should_fire` implies `parsed.album is not None`
        # (see the precondition where the flag is set); each branch below
        # re-asserts to narrow the type locally for the type-checker.
        if album_fallback_should_fire and album_fallback_error is not None:
            # The probe ran but raised; mirror the pre-#339 behavior of
            # logging via `_log_album_title_fallback(..., error=...)`.
            assert parsed.album is not None
            _log_album_title_fallback(
                album=parsed.album,
                n_candidates=0,
                surfaced_library_match=False,
                error=album_fallback_error,
            )
        elif album_fallback_should_fire and not results and album_fallback_response is not None:
            assert parsed.album is not None
            fallback_releases = list(album_fallback_response.releases or [])
            if fallback_releases:
                logger.info(
                    f"Album-title fallback returned {len(fallback_releases)} candidates "
                    f"for '{parsed.album}'"
                )

            fallback_worker = partial(
                process_release,
                skip_self_named_album=False,
                skip_artist_match_filter=True,
            )
            async for _, release_matches in _chunked_gather(
                fallback_releases, fallback_worker, MAX_SEARCH_RESULTS
            ):
                for match, resolved in release_matches:
                    if match.id not in seen_ids:
                        results.append(match)
                        seen_ids.add(match.id)
                    _record_resolved(match, resolved)
                if len(results) >= MAX_SEARCH_RESULTS:
                    break
            if fallback_releases:
                discogs_found_releases = True
            _log_album_title_fallback(
                album=parsed.album,
                n_candidates=len(fallback_releases),
                surfaced_library_match=len(results) > pre_fallback_results_count,
            )
    except Exception as e:
        logger.warning(f"Failed to search for track on other releases: {e}")

    if not results and keyword_matches and not discogs_found_releases:
        logger.info("Discogs search found nothing, using keyword matches as fallback")
        for item in keyword_matches[:1]:
            if item.id not in seen_ids:
                results.append(item)
                seen_ids.add(item.id)

    if results and parsed.song:
        song_lower = parsed.song.lower()
        results.sort(
            key=lambda r: song_lower in (r.title or "").lower(),
            reverse=True,
        )

    # A1 carry-through (LML#628): the artist+keyword+album waves dropped every
    # candidate on the library gate, but the track may still resolve + validate
    # off a non-library release. Surface it row-less rather than empty. Keyed on
    # the typed ``parsed.artist`` (NOT ``lib_artist`` / the canonical-swapped
    # ``artist_for_probes``), consistent with the #626 two-channel decision and
    # the #632 cache contract. Gated; off by default. LML#652: also gated on the
    # bulk kill switch — /lookup/bulk passes ``allow_release_resolution_fallback
    # =False`` so the backfill never pays the per-row resolve + #632 cache write
    # (parity with #604's lazy fallback).
    if (
        not results
        and get_settings().lml_resolve_nonlibrary_release
        and allow_release_resolution_fallback
        and parsed.song
    ):
        # Distinct name from the #604 ``resolved`` bound earlier in this function
        # (a non-optional ``ResolvedRelease``) — the helper returns an Optional,
        # and reusing the name trips a mypy assignment-type collision.
        resolved_nonlibrary = await _resolve_nonlibrary_release(
            discogs_service,
            pg,
            song=parsed.song,
            artist=parsed.artist or "",
            album=parsed.album,
            is_track=True,
        )
        if resolved_nonlibrary is not None:
            rowless = _make_rowless_item(
                artist=parsed.artist or "", title=resolved_nonlibrary.album_title
            )
            discogs_titles[ROWLESS_LIBRARY_ID] = resolved_nonlibrary
            logger.info(
                f"TRACK_ON_COMPILATION: surfacing row-less Discogs release "
                f"{resolved_nonlibrary.release_id} ('{resolved_nonlibrary.album_title}') "
                f"— validated, not in library"
            )
            return [rowless], discogs_titles

    return limit_results(results), discogs_titles
