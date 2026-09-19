"""Track validation + the A4 cached-track safety net for the lookup pipeline.

Home of the Step-3b per-result track validation
(``filter_results_by_track_validation`` — confirm each artist-fallback album
actually contains the requested track via Discogs tracklists), the LML#629
cached-track safety net (``find_library_albums_with_cached_track`` — the
Discogs PG cache answers "which releases by this artist contain this track?"
and promotes matching library rows, or surfaces the best release row-less),
and the Step-3b orchestration cascade itself (``apply_track_validation_cascade``
— per-result validation, the A4 promotion, the LML#717 song-as-album-title
promotion, the row-less-to-shelf-row reverse probe that narrows A4's row-less
carry-through just before it surfaces, and the compilation artist-fallback
merge, in the order the spine used to run them inline). Extracted verbatim
from ``lookup/orchestrator.py`` (LML#728, LML#750).
"""

import logging
from dataclasses import dataclass

from wxyc_etl.text import to_match_form as normalize_for_comparison

from config.settings import get_settings
from discogs.breaker import DiscogsBreakerOpenError
from discogs.models import DiscogsSearchRequest
from discogs.service import DiscogsService
from entity.library_release_override import get_library_release_overrides
from entity.sources import PgSource
from library.db import LibraryDB
from library.models import LibraryItem
from lookup.concurrency import _chunked_gather
from lookup.matching import (
    MAX_SEARCH_RESULTS,
    album_title_acceptable,
    artist_matches_item,
    is_self_titled,
)
from lookup.release_resolution import (
    ResolvedRelease,
    prerank_candidates_for_validation,
    validate_release_for_track,
)
from lookup.rowless import (
    ROWLESS_LIBRARY_ID,
    ROWLESS_NO_ALBUM_CONFIDENCE,
    _make_rowless_item,
)
from lookup.strategies.track_release_matching import search_album_fuzzy

logger = logging.getLogger(__name__)

# Minimum fuzzy score for promoting an artist-fallback row whose *title*
# matches the requested "song" — i.e. recognising the parsed song as the
# album the user actually wanted. Set higher than the album-match floor
# because the consequence here is asserting the user's intent (album,
# not track); a borderline match should *not* override the song-not-found
# message.
_SONG_AS_ALBUM_TITLE_FLOOR = 90.0


async def filter_results_by_track_validation(
    results: list[LibraryItem],
    song: str | None,
    artist: str | None,
    discogs_service: DiscogsService | None,
) -> list[LibraryItem] | None:
    """Filter fallback results to only albums that contain the requested track.

    Bounded via :func:`_chunked_gather` (LML#808): probes dispatch
    ``MAX_SEARCH_RESULTS`` at a time, subject to the shared per-invocation
    ``LML_SEARCH_MAX_API_CALLS`` cap, so a wide fallback candidate list (e.g.
    the artist-only fallback, widened by LML#808 to survive past the
    response-size truncation) can't fan out into an unbounded Discogs probe
    burst. A breaker shed (:class:`DiscogsBreakerOpenError`) stops dispatching
    further chunks — the row that was already in flight when the breaker
    opened is kept (per the R2-4 policy below), but no more probes go out
    once the breaker is known to be open.

    Returns:
        Filtered list, or None if validation isn't possible.
    """
    if not discogs_service or not song or not artist or not results:
        return None

    async def validate_one(item: LibraryItem) -> tuple[LibraryItem | None, bool]:
        try:
            # Self-titled albums stored as "S/t" should use the artist name
            album_for_search = item.artist if is_self_titled(item.title or "") else item.title
            response = await discogs_service.search(
                DiscogsSearchRequest(album=album_for_search, artist=artist)
            )
            if response is None or not response.results:
                # None = degraded Discogs call (LML#918); treat like no results.
                return None, False

            best_result = response.results[0]
            if best_result.release_id:
                # Verify the Discogs result is actually the same album, not a
                # different release that shares words with the library title.
                # e.g., searching for "808 State" might return "The Best Of
                # 808 State: Blueprint" — a different album entirely.
                discogs_album = (best_result.album or "").lower()
                library_title = (item.title or "").lower()
                if not album_title_acceptable(library_title, discogs_album):
                    logger.debug(
                        f"Track validation: Discogs returned '{best_result.album}' "
                        f"for library item '{item.title}' — album mismatch, skipping"
                    )
                    return None, False

                is_valid = await validate_release_for_track(
                    discogs_service, best_result.release_id, song, artist, source="step_3b"
                )
                if is_valid:
                    logger.info(
                        f"Track validation: '{song}' confirmed on '{item.title}' "
                        f"(release {best_result.release_id})"
                    )
                    return item, False
        except DiscogsBreakerOpenError:
            # LML#1118: kept narrow — this validation only ever calls
            # ``discogs_service``; no other breaker type can reach here.
            # LML#755 R2-4: the Discogs saturation breaker shed a live probe
            # (search or validate). A shed is "couldn't ask", NOT "confirmed not
            # on the album" — dropping the row here would launder the shed into
            # song-not-found (a wrong 200). KEEP the real library row unvalidated
            # so the user still gets their match, validation pending, during a
            # flood. Genuine validation errors still fall through to the broad
            # ``except`` below and drop the row as before.
            #
            # LML#808: also signal the shed back to the caller so it stops
            # dispatching further ``_chunked_gather`` chunks — the breaker is
            # open, so more probes would just shed too.
            logger.info(
                "Track validation shed by Discogs breaker for '%s'; keeping row unvalidated",
                item.title,
            )
            return item, True
        except Exception as e:
            logger.warning(f"Track validation failed for '{item.title}': {e}")
        return None, False

    validated: list[LibraryItem] = []
    breaker_shed = False
    dispatched = 0
    async for _item, (validated_item, shed) in _chunked_gather(
        results, validate_one, MAX_SEARCH_RESULTS
    ):
        dispatched += 1
        if validated_item is not None:
            validated.append(validated_item)
        breaker_shed = breaker_shed or shed
        if len(validated) >= MAX_SEARCH_RESULTS:
            break
        # Chunk-granular stop (mirrors ``_chunked_gather``'s own between-chunks
        # gate): let the in-flight chunk finish — those probes already went
        # out — but don't let a later chunk dispatch once the breaker's open.
        if breaker_shed and dispatched % MAX_SEARCH_RESULTS == 0:
            logger.info(
                "Track validation: Discogs breaker open, stopping fan-out "
                "after %d of %d candidates",
                dispatched,
                len(results),
            )
            break

    if validated:
        logger.info(
            f"Track validation filtered {len(results)} albums to {len(validated)} "
            f"containing '{song}'"
        )
        return validated

    logger.info(f"Track validation could not confirm '{song}' on any album")
    return None


async def find_library_albums_with_cached_track(
    db: LibraryDB,
    song: str | None,
    artist: str | None,
    discogs_service: DiscogsService | None,
    limit: int = MAX_SEARCH_RESULTS,
    *,
    match_artist: str | None = None,
    allow_release_resolution_fallback: bool = True,
) -> tuple[list[LibraryItem], dict[int, ResolvedRelease]]:
    """Find WXYC library albums whose Discogs cache entry lists ``song`` by ``artist``.

    Used as a safety net after ``filter_results_by_track_validation`` fails to
    confirm any artist-fallback candidate. The PG cache holds full Discogs
    tracklist data with trigram-indexed track titles, so a single lookup can
    answer "which releases by this artist contain this track?" in milliseconds —
    even when the upstream ``resolve_albums_for_track`` / API path missed it.

    Two-channel artist (LML#626): the Discogs-cache probe keys on the typed
    ``artist`` (the cache holds Discogs-credited names), while the library
    match-back keys on ``match_artist`` when supplied — the library-corrected
    name — so a misspelled library artist still promotes its catalog row.
    ``match_artist`` defaults to ``artist`` to preserve single-channel behavior
    for callers that don't distinguish the two.

    Returns ``(items, discogs_titles)``. On the in-library path ``items`` are the
    promoted WXYC rows and ``discogs_titles`` is empty. **A4 carry-through
    (LML#629):** when the cache confirms the track on a release but *no* library
    row artist-matches — and ``lml_resolve_nonlibrary_release`` is on — a single
    **row-less** ``LibraryItem(id=0)`` is returned with the resolved release on
    the ``{0: ResolvedRelease}`` seam, reusing #628's carry-through so the
    ``release_id`` (hence ``discogs_url``) still surfaces instead of being
    dropped for want of a matching catalog row. This A4 row-less surface is the
    *fifth* row-less producer (LML#652): it honors the per-request bulk kill
    switch ``allow_release_resolution_fallback`` exactly as the four strategy
    producers do — ``False`` on /lookup/bulk suppresses it (the in-library
    promotion above is unaffected, returning before the gate).

    Cache-only by design: skips any API fallback path. Returns ``([], {})``
    cleanly when the cache is unavailable, fails, or has nothing for the query —
    and, with the flag off, when nothing artist-matches (pre-#629 behavior).
    """
    if not discogs_service or not song or not artist:
        return [], {}
    match_against = match_artist or artist
    cache_service = getattr(discogs_service, "cache_service", None)
    if cache_service is None:
        return [], {}

    try:
        cached_releases = await cache_service.search_releases_by_track(
            track=song, artist=artist, limit=20
        )
    except Exception as e:
        logger.warning(f"Cache lookup for track-album promotion failed: {e}")
        return [], {}

    if not cached_releases:
        return [], {}

    matches: list[LibraryItem] = []
    seen_ids: set[int] = set()

    for release in cached_releases:
        candidate_items = await search_album_fuzzy(db, release.album)
        for item in candidate_items:
            if item.id in seen_ids:
                continue
            if not artist_matches_item(item, match_against):
                continue
            matches.append(item)
            seen_ids.add(item.id)
            if len(matches) >= limit:
                return matches, {}

    if matches:
        return matches, {}

    # A4 carry-through (LML#629): the cache confirmed the track on a release, but
    # no WXYC library row artist-matches. Rather than drop a resolvable release,
    # surface the best one row-less so its release_id (hence discogs_url) still
    # binds via #628's {0: ResolvedRelease} seam. Gated on the same flag as the
    # other carry-through sites: when off, fetch_artwork_for_items won't bind a
    # row-less item, so we preserve the pre-#629 drop. Cache rows are already
    # track-confirmed by the trigram query, so no re-validation is needed.
    #
    # Ordering reuses the shared #629 no-album rule (prefer is_compilation=False,
    # then stable release_id) instead of restating it — keeping this path and the
    # bounded-resolve path on one definition. There is no typed album to rank
    # against here (the cache keyed on track only), so confidence is soft: the
    # pick was never album-matched, and the soft value rides the seam so the bind
    # surfaces it even when the request did type an album.
    #
    # LML#652: gated on the bulk kill switch too — /lookup/bulk passes
    # ``allow_release_resolution_fallback=False`` so this A4 row-less surface (the
    # fifth row-less producer) never reaches the per-row ``bind_carried`` artwork
    # fetch on the backfill path. The in-library promotion above returns before
    # this gate, so it stays available on bulk.
    if not (get_settings().lml_resolve_nonlibrary_release and allow_release_resolution_fallback):
        return [], {}
    # Require a title as well as an id: a title-less release would surface a
    # degenerate row-less item (title=""), exactly what the sibling rehydrate
    # path (_rehydrate_resolved_release) guards against.
    ranked = prerank_candidates_for_validation(
        [r for r in cached_releases if r.release_id and r.album], None
    )
    if not ranked:
        return [], {}
    best = ranked[0]
    rowless = _make_rowless_item(artist=artist or "", title=best.album)
    resolved = ResolvedRelease(
        release_id=best.release_id,
        release_url=best.release_url or "",
        is_compilation=bool(best.is_compilation),
        album_title=best.album or "",
        confidence=ROWLESS_NO_ALBUM_CONFIDENCE,
    )
    logger.info(
        f"cached-track safety net: surfacing row-less Discogs release "
        f"{best.release_id} ('{best.album}') — track-confirmed in cache, not in library"
    )
    return [rowless], {ROWLESS_LIBRARY_ID: resolved}


def _filter_results_by_song_as_album_title(
    results: list[LibraryItem],
    song: str | None,
) -> list[LibraryItem]:
    """Pick artist-fallback rows whose title matches the requested song.

    Handles the request shape "on patrol, sun araw" — request-o-matic routes
    it as ``song="On Patrol"`` / ``artist="Sun Araw"``, but the user typed
    an album name. The artist+song FTS branch of
    ``search_library_with_fallback`` surfaces the matching album because
    the album title contains the song words; per-result track validation
    then reasonably comes back empty (no track titled "On Patrol" exists on
    that album — it IS the album) and ``song_not_found`` stays set,
    producing the misleading 'not on any album' context message about a
    result sitting in its own list.

    Floor is ``_SONG_AS_ALBUM_TITLE_FLOOR`` (>= 90 via
    ``rapidfuzz.fuzz.token_set_ratio``) — high enough that a coincidental
    word overlap won't override the song-not-found path.

    Returns the subset of ``results`` whose normalised title clears the
    floor against the normalised song. Empty input or whitespace-only song
    returns ``[]`` cleanly.
    """
    if not song or not song.strip() or not results:
        return []
    from rapidfuzz import fuzz

    norm_song = normalize_for_comparison(song)
    matches: list[LibraryItem] = []
    for item in results:
        title_norm = normalize_for_comparison(item.title or "")
        if fuzz.token_set_ratio(norm_song, title_norm) >= _SONG_AS_ALBUM_TITLE_FLOOR:
            matches.append(item)
    return matches


def _is_rowless_only(items: list[LibraryItem]) -> bool:
    """True when every row in ``items`` is the id=0 carry-through sentinel.

    Separates A4's two return shapes: shelved library rows the Discogs cache
    confirmed (strong — it promotes over the unvalidated fallback) from the
    single row-less release it synthesizes when nothing in the catalog
    artist-matched (weak — it is an assertion that the library does not have
    the record, reached through a title-keyed match-back that a
    catalog/Discogs title divergence defeats).
    """
    return bool(items) and all(item.id == ROWLESS_LIBRARY_ID for item in items)


# How many of the artist's shelf rows ``_rebind_rowless_release_via_override``
# probes before giving up. Bounds a title-divergent artist's shelf count into
# one bounded PG query rather than an unbounded one; "typically under 20 rows"
# for a WXYC-shelved artist in practice, so this rarely truncates a real hit.
_SHELF_REBIND_PROBE_LIMIT = 20


async def _rebind_rowless_release_via_override(
    pg: PgSource | None,
    *,
    release_id: int,
    shelf_rows: list[LibraryItem],
) -> LibraryItem | None:
    """Reverse-probe: does one of the artist's OWN shelf rows already carry a
    hand-verified override (LML#850, ``lml_cache.library_release_override``)
    pinning it to ``release_id``?

    Title-based match-back (``search_album_fuzzy``, and the ``find_best_typed_match``
    80/80 floor behind ``resolve_typed_album_level_match``) cannot bridge the
    Broadcast/Minimoonstar class of divergence: WXYC shelves the record as
    "Broadcast & the Focus Group Investigate...", Discogs titles the release
    itself just "Investigate Witch Cults Of The Radio Age" — a 51-point
    ``token_sort_ratio``, well under any reasonable floor, because the two
    strings share one token. But that same shelf row is exactly the kind of
    title-vs-catalog mismatch LML#850's override table exists to correct — DJ
    Alex L.'s manual card-catalog walk (``entity/library_release_override.py``)
    already carries a verified pin for it. This asks the SAME question the A4
    carry-through was about to answer "no" to, against a source that needs no
    title similarity at all: an exact ``library_id -> discogs_release_id``
    lookup, so a same-artist/different-album row (a "Hiding Places" shelf row
    against a "High Places" query) is rejected by id inequality, not by a
    title floor this function does not restate.

    Bounded to the first :data:`_SHELF_REBIND_PROBE_LIMIT` rows and fetched in
    ONE query (``get_library_release_overrides``, best-effort — a PG failure
    or an empty/absent ``pg`` degrades to no match, never a crash). Returns the
    first shelf row whose override matches, in ``shelf_rows`` order.
    """
    if pg is None or not shelf_rows:
        return None
    candidate_rows = [row for row in shelf_rows[:_SHELF_REBIND_PROBE_LIMIT] if row.id > 0]
    if not candidate_rows:
        return None
    overrides = await get_library_release_overrides(pg, [row.id for row in candidate_rows])
    for row in candidate_rows:
        if overrides.get(row.id) == release_id:
            return row
    return None


@dataclass
class Step3bResult:
    """Outcome of :func:`apply_track_validation_cascade`, ready to rebind onto ``LookupState``."""

    library_results: list[LibraryItem]
    song_not_found: bool
    discogs_titles: dict[int, ResolvedRelease]

    found_on_compilation: bool | None = None
    """Desired ``state.found_on_compilation`` after the caller rebinds this.

    ``None`` means "leave alone" — the same convention
    :attr:`core.search.Outcome.song_not_found_after` uses, and the value every
    tier but one returns, because the cascade narrows and promotes *within* a
    compilation verdict the search pipeline already reached. The exception is
    the LML#1184 row-less append, which is the one tier that can invalidate
    that verdict: see :func:`apply_track_validation_cascade`.
    """


async def apply_track_validation_cascade(
    *,
    real_results: list[LibraryItem],
    library_results: list[LibraryItem],
    found_on_compilation: bool,
    song_not_found: bool,
    discogs_titles: dict[int, ResolvedRelease],
    artist_fallback_results: list[LibraryItem],
    song: str | None,
    artist: str | None,
    match_artist: str | None,
    db: LibraryDB,
    discogs_service: DiscogsService | None,
    allow_release_resolution_fallback: bool,
    pg: PgSource | None = None,
) -> Step3bResult:
    """Step-3b policy cascade: sequence the tiers that promote/narrow ``library_results``.

    Not-on-a-compilation tier, strongest evidence first: per-result Discogs
    track validation over ``real_results``
    (``filter_results_by_track_validation``); on a total miss, the LML#629 A4
    cached-track safety net (``find_library_albums_with_cached_track``) when it
    promotes **shelved** rows; on a further miss, the LML#717
    song-as-album-title promotion over ``library_results``
    (``_filter_results_by_song_as_album_title``); and last, A4's **row-less**
    carry-through, which asserts the library does not have the record and so
    ranks below any shelved row that answers the request (see the comment at
    that branch for the title-divergence shape it gets wrong). Each promotes
    over the previous tier's result only when it finds something.

    On-a-compilation tier: validates the artist-fallback rows saved before
    ``TRACK_ON_COMPILATION`` replaced them, and prepends any confirmed matches
    ahead of the compilation results. When nothing confirms *and* the matched
    compilation is row-less (LML#1184), the stashed rows are appended behind it
    rather than dropped, and ``Step3bResult.found_on_compilation`` comes back
    ``False`` — the one case where this cascade overturns the search pipeline's
    compilation verdict.

    Callers must apply the same gate ``_step_validate_tracks`` uses before
    invoking this (both ``song`` and ``artist`` typed, plus either a non-empty
    ``real_results`` or — on the compilation tier — a non-empty
    ``artist_fallback_results``); this function does not re-check it.

    ``pg`` (the discogs-cache PG source that also backs the LML#850 override
    table, best-effort like every other cache read in this pipeline) is
    forwarded to the row-less reverse probe below; ``None`` degrades that
    probe to no match, never a crash.
    """
    if not found_on_compilation:
        validated = await filter_results_by_track_validation(
            real_results, song, artist, discogs_service
        )
        if validated:
            return Step3bResult(validated, False, discogs_titles)
        if not song_not_found:
            return Step3bResult(library_results, song_not_found, discogs_titles)

        # Per-result validation confirmed nothing. Ask the local PG cache
        # directly: "any release by this artist whose tracklist contains this
        # song?" — and promote the matching library album. Catches the case
        # where the upstream track->releases lookup missed a release the
        # cache holds.
        promoted, promoted_titles = await find_library_albums_with_cached_track(
            db,
            song,
            artist,
            discogs_service,
            match_artist=match_artist,
            allow_release_resolution_fallback=allow_release_resolution_fallback,
        )
        if promoted and not _is_rowless_only(promoted):
            # A4's in-library promotion (LML#629): shelved rows whose Discogs
            # tracklist the cache confirms. Strongest tier below per-result
            # validation, so it still runs first.
            return Step3bResult(promoted, False, {**discogs_titles, **promoted_titles})

        # Last resort before declaring song-not-found: the request shape
        # "<album-title>, <artist>" can route to us as ``song=<album-title>``.
        # If a surviving result's title clears the floor against ``song``,
        # the user wanted that album. Surfacing it as found-the-album avoids
        # the misleading 'not on any album' message about a row sitting in
        # the result list.
        # LML#1318 review fix 1: the id=0 degrade row is excluded — its title
        # IS the typed album (the DOGA/Eliana shape scores 100 by construction),
        # and promoting it would flip ``song_not_found`` to False on a row
        # nothing ever track-confirmed, while ``search_type`` stays ``fallback``.
        # Only shelved rows can earn the LML#717 "user meant the album" reading.
        title_matches = _filter_results_by_song_as_album_title(
            [r for r in library_results if r.id != ROWLESS_LIBRARY_ID], song
        )
        if title_matches:
            logger.info(
                f"Promoted {len(title_matches)} of {len(library_results)} "
                f"artist-fallback row(s) whose title matches song "
                f"'{song}' — treating as album request"
            )
            return Step3bResult(title_matches, False, discogs_titles)

        if promoted:
            # A4's row-less carry-through (LML#629). It fires when the cache
            # confirms the track on a release *no library row artist-matches*
            # — but that match-back is keyed on album title
            # (``search_album_fuzzy(db, release.album)``), so a catalog/Discogs
            # title divergence reads as "not in the library" when the record is
            # on the shelf. Prod, 2026-09-14: WXYC files release 1350337 as
            # *Minimoonstar*; Discogs titles it *Vasco EP Part 1*, with
            # "Minimoonstar" as a track. A4 confirmed the track, missed the row,
            # and surfaced the same physical record row-less — over a shelved
            # row the listener had named exactly.
            #
            # The consequence is not cosmetic: request-o-matic strips id=0 rows
            # from the request channel (``routers/request.py``, ROM#256) because
            # a DJ cannot pull a non-shelved album, then re-derives
            # ``song_not_found`` from what survives. A row-less answer that
            # displaced a shelf row therefore reaches the DJ as
            # '"<song>" by <artist> not found in library'.
            #
            # Stopgap: before conceding row-less, ask whether one of the
            # artist's OWN shelf rows already carries a hand-verified override
            # to the SAME release (LML#850) — ``real_results`` is already the
            # artist-filtered shelf set per-result validation just failed to
            # confirm the track on, so no new library fetch is needed, and an
            # id-equality check needs no title floor at all. This is exactly
            # the "Broadcast" / "The Be Colony" shape: WXYC shelves the split
            # LP abbreviated as "Broadcast & the Focus Group Investigate...",
            # Discogs's own track search returns just "Investigate Witch Cults
            # Of The Radio Age" (a 51-point token_sort_ratio — no title floor
            # bridges that), but the shelf row already carries a verified pin
            # to the same release from DJ Alex L.'s card-catalog walk.
            resolved_release = promoted_titles.get(ROWLESS_LIBRARY_ID)
            if resolved_release is not None:
                shelf_row = await _rebind_rowless_release_via_override(
                    pg,
                    release_id=resolved_release.release_id,
                    shelf_rows=real_results,
                )
                if shelf_row is not None:
                    logger.info(
                        "Row-less reverse probe rebound release %s to shelved row "
                        "%r via its LML#850 override instead of surfacing it row-less",
                        resolved_release.release_id,
                        shelf_row.title,
                    )
                    return Step3bResult([shelf_row], False, discogs_titles)

            # A row-less release is the weakest thing this tier can return, so
            # it yields to any shelved row that answers the request and keeps
            # its seam entry for everything else.
            return Step3bResult(promoted, False, {**discogs_titles, **promoted_titles})

        return Step3bResult(library_results, song_not_found, discogs_titles)

    # Compilation found, but the artist's own album may also contain the
    # track. Validate the artist fallback results (saved before compilation
    # search replaced them) and prepend any confirmed matches.
    rowless_only = not any(r.id != 0 for r in library_results)
    # LML#1184 admitted a shape that previously reached step 3b never at all, so
    # this leg goes from zero Discogs calls to a live probe per candidate. The
    # stash is un-truncated (``_FETCH_LIMIT`` = 10x ``MAX_SEARCH_RESULTS``), and
    # the branch that consumes the verdict is by construction the "nothing
    # confirmed" one, so the full fan-out would be paid on *every* firing —
    # 15-24 calls and 16-17s of wall-time per ``lookup/concurrency.py``, on a
    # hot path the post-launch-hardening project exists to make faster.
    # Bounding the input costs recall only for a confirmation sitting deeper
    # than ``MAX_SEARCH_RESULTS`` in the stash; that is the trade this makes,
    # deliberately. The pre-existing shelved-comp lane keeps the full list —
    # its cost is not new and narrowing it here would be an unrelated behavior
    # change.
    fallback_candidates = (
        artist_fallback_results[:MAX_SEARCH_RESULTS] if rowless_only else artist_fallback_results
    )
    validated = await filter_results_by_track_validation(
        fallback_candidates, song, artist, discogs_service
    )
    compilation_ids = {r.id for r in library_results}
    if validated:
        merged = [r for r in validated if r.id not in compilation_ids]
        merged.extend(library_results)
        return Step3bResult(merged, song_not_found, discogs_titles)

    # LML#1184: nothing confirmed, and the compilation that matched is one WXYC
    # does not shelve — every row here is the id=0 carry-through (LML#628/#631).
    # Dropping the stash now would answer "what does WXYC have by this artist?"
    # with silence about albums that are physically on the shelf, so append them
    # behind the row-less row instead. Failure to *confirm* the track on those
    # albums is not evidence against the albums; it is frequently just a
    # misspelled request, and the artist rows are what a DJ can act on either
    # way. The comp keeps position 0 so top-1 artwork still binds to the release
    # that actually carries the track (LML#628/#630).
    #
    # Scoped to the row-less case on purpose: when the matched compilation *is*
    # shelved, the DJ can pull it, the response is already actionable, and an
    # unconfirmed fallback stays suppressed exactly as before.
    if rowless_only and artist_fallback_results:
        merged = list(library_results)
        merged.extend(r for r in artist_fallback_results if r.id not in compilation_ids)
        # The stash is un-truncated (``search_library_with_fallback`` fetches at
        # ``_FETCH_LIMIT`` = 10x this cap), and every row appended here costs a
        # per-item artwork fetch, streaming probe and identity resolve
        # downstream. The confirmed branch above inherits the same cap from
        # ``filter_results_by_track_validation``; this one has to apply it.
        merged = merged[:MAX_SEARCH_RESULTS]
        # The only row carrying the track is unshelved, so the shelf-facing
        # reading of "Found <song> on:" is false. Hand the caller the same
        # song-not-found framing this request already gets when Discogs finds
        # nothing at all — now with the artist's albums under it.
        return Step3bResult(merged, True, discogs_titles, found_on_compilation=False)

    return Step3bResult(library_results, song_not_found, discogs_titles)
