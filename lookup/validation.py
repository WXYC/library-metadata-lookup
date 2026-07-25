"""Track validation + the A4 cached-track safety net for the lookup pipeline.

Home of the Step-3b per-result track validation
(``filter_results_by_track_validation`` — confirm each artist-fallback album
actually contains the requested track via Discogs tracklists) and the LML#629
cached-track safety net (``find_library_albums_with_cached_track`` — the
Discogs PG cache answers "which releases by this artist contain this track?"
and promotes matching library rows, or surfaces the best release row-less).
Extracted verbatim from ``lookup/orchestrator.py`` (LML#728).
"""

import asyncio
import logging

from config.settings import get_settings
from discogs.breaker import DiscogsBreakerOpenError
from discogs.models import DiscogsSearchRequest
from discogs.service import DiscogsService
from library.db import LibraryDB
from library.models import LibraryItem
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


async def filter_results_by_track_validation(
    results: list[LibraryItem],
    song: str | None,
    artist: str | None,
    discogs_service: DiscogsService | None,
) -> list[LibraryItem] | None:
    """Filter fallback results to only albums that contain the requested track.

    Returns:
        Filtered list, or None if validation isn't possible.
    """
    if not discogs_service or not song or not artist or not results:
        return None

    async def validate_one(item: LibraryItem) -> LibraryItem | None:
        try:
            # Self-titled albums stored as "S/t" should use the artist name
            album_for_search = item.artist if is_self_titled(item.title or "") else item.title
            response = await discogs_service.search(
                DiscogsSearchRequest(album=album_for_search, artist=artist)
            )
            if response is None or not response.results:
                # None = degraded Discogs call (LML#918); treat like no results.
                return None

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
                    return None

                is_valid = await validate_release_for_track(
                    discogs_service, best_result.release_id, song, artist, source="step_3b"
                )
                if is_valid:
                    logger.info(
                        f"Track validation: '{song}' confirmed on '{item.title}' "
                        f"(release {best_result.release_id})"
                    )
                    return item
        except DiscogsBreakerOpenError:
            # LML#755 R2-4: the Discogs saturation breaker shed a live probe
            # (search or validate). A shed is "couldn't ask", NOT "confirmed not
            # on the album" — dropping the row here would launder the shed into
            # song-not-found (a wrong 200). KEEP the real library row unvalidated
            # so the user still gets their match, validation pending, during a
            # flood. Genuine validation errors still fall through to the broad
            # ``except`` below and drop the row as before.
            logger.info(
                "Track validation shed by Discogs breaker for '%s'; keeping row unvalidated",
                item.title,
            )
            return item
        except Exception as e:
            logger.warning(f"Track validation failed for '{item.title}': {e}")
        return None

    validation_results = await asyncio.gather(*[validate_one(item) for item in results])
    validated = [r for r in validation_results if r is not None]

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
