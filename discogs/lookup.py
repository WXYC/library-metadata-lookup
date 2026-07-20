"""Helper functions for Discogs lookups.

These functions accept an optional DiscogsService instance. When provided
(e.g., from FastAPI dependency injection), the service's cache is used.
When omitted, a cacheless service is created as a fallback.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from discogs.models import DiscogsSearchRequest, DiscogsSearchResult, ReleaseInfo
from discogs.service import DiscogsService

if TYPE_CHECKING:
    from library.db import LibraryDB

logger = logging.getLogger(__name__)


def _get_service() -> DiscogsService | None:
    """Get a cacheless DiscogsService instance if token is configured.

    Prefer passing a service instance from dependency injection to benefit
    from the PostgreSQL cache.
    """
    from config.settings import get_settings

    settings = get_settings()
    if not settings.discogs_token:
        return None
    return DiscogsService(settings.discogs_token)


async def lookup_releases_by_track(
    track: str,
    artist: str | None = None,
    limit: int = 20,
    service: DiscogsService | None = None,
    artist_as_keyword: bool = False,
    db: LibraryDB | None = None,
    library_artist: str | None = None,
) -> list[tuple[str, str]]:
    """Look up all releases containing a track using Discogs.

    Validates that the track by the artist actually exists on each candidate
    release (keeps VA/compilation false hits out), but only for candidates that
    can actually surface — see the library-first gate below (LML#866).

    Args:
        track: Track title.
        artist: Optional artist name. When absent, releases are returned without
            tracklist validation (unchanged).
        limit: Maximum number of search results.
        service: Optional DiscogsService (with cache). Cacheless fallback if omitted.
        db: Optional library handle (LML#866). With an artist present, gates
            tracklist validation on library membership of the (corrected-or-typed)
            artist — a non-library artist skips every get_release fetch. When
            omitted, every candidate is validated (pre-#866 behavior, parallelized).
        library_artist: The library-side artist name to gate on — the fuzzy
            correction when the caller has one, else the typed ``artist``. Only
            consulted when ``db`` is provided.

    Returns:
        List of (artist, album) tuples for validated releases containing the track.
    """
    if service is None:
        service = _get_service()
    if not service:
        return []

    response = await service.search_releases_by_track(
        track, artist, limit, artist_as_keyword=artist_as_keyword
    )
    raw_releases = list(response.releases or [])
    if not raw_releases:
        return []

    # Without an artist there is no track/artist pair to validate — return every
    # release as-is (pre-#866 behavior; see test_no_artist_skips_validation).
    if not artist:
        return [(r.artist, r.album) for r in raw_releases]

    # Library-first gate (LML#866). The album names produced here feed only
    # ARTIST_PLUS_ALBUM, whose album-fed branch re-filters every hit to the query
    # artist. If the (corrected-or-typed) artist has no library rows, no tracklist
    # validation can change the surfaced set — so skip the O(N) rate-limited
    # get_release validations entirely. One Discogs search, zero tracklist fetches.
    if db is not None and not await _artist_has_library_rows(db, library_artist or artist):
        logger.info(
            "LML#866: skipping track validation for '%s' — artist '%s' has no library rows",
            track,
            library_artist or artist,
        )
        return []

    from lookup.concurrency import _chunked_gather
    from lookup.matching import MAX_SEARCH_RESULTS

    async def _validate_one(release: ReleaseInfo) -> bool:
        # A release with no id can't be validated; keep it (pre-#866 behavior: the
        # old loop only validated when ``artist and release.release_id``).
        if not release.release_id:
            return True
        is_valid = await service.validate_track_on_release(release.release_id, track, artist)
        if not is_valid:
            logger.info("Skipping '%s' - track/artist not validated on release", release.album)
        return is_valid

    # Bounded-concurrency validation (LML#536 pattern, ported from the step-3
    # kernel in lookup/strategies/track_release_matching.py). _chunked_gather runs
    # MAX_SEARCH_RESULTS validations per wave and enforces the per-invocation
    # Discogs API-call cap between chunks, keeping this inside the #755
    # saturation-breaker envelope. Replaces the pre-#866 sequential loop.
    #
    # NO accumulator early-exit here (unlike the step-3 kernel, which breaks once
    # it has MAX_SEARCH_RESULTS *library matches*). The releases returned here are
    # still unfiltered: the caller (``resolve_albums_for_track``) applies the
    # artist-prefix filter afterward, and ``search_releases_by_track`` interleaves
    # VA/compilation and keyword-supplement releases whose primary artist is not
    # the query artist (``ReleaseInfo.artist`` is the release's own credit). Breaking
    # at MAX_SEARCH_RESULTS *raw* validated releases would let a run of leading
    # comps starve the artist's own album (an LML#866 recall regression), so every
    # candidate up to ``limit`` is validated. Cost stays bounded — ``limit`` caps the
    # list and the between-chunk API-call cap bounds the fan-out.
    releases: list[tuple[str, str]] = []
    async for release, is_valid in _chunked_gather(raw_releases, _validate_one, MAX_SEARCH_RESULTS):
        if not is_valid:
            continue
        # NOTE(#867): release.release_id and the True verdict are known here but
        # dropped when we reduce to (artist, album). #867's candidate carry-through
        # memo will thread them; keep them reachable so that ticket needn't refetch.
        releases.append((release.artist, release.album))

    return releases


async def _artist_has_library_rows(db: LibraryDB, artist: str) -> bool:
    """True when the local library holds at least one row for ``artist``.

    A False answer must *guarantee* that ARTIST_PLUS_ALBUM's album-fed branch —
    the sole consumer of the album names this module produces — can surface
    nothing for the artist, so this has to be a sound lower bound on that branch.
    It probes the artist *column* directly (``db.search(artist=...)``, a LIKE
    filter) rather than the cross-column FTS (``db.search(query=...)``): the FTS
    path has a fixed LIMIT and no rank ordering (``library/db.py``), so a common
    single-token artist ("Women", "Low", "Wire") can have its rows crowded out of
    the page by unrelated title matches — while the album-fed branch's far more
    selective ``"{artist} {album}"`` query still surfaces them (LML#866). The
    artist-column filter is a superset of the album-fed hits (any prefix-matching
    row's artist contains the name), so it never suppresses a candidate that
    branch could surface. A local SQLite read; no Discogs call.
    """
    if not artist or not artist.strip():
        return True  # nothing to gate on; don't suppress (defensive)
    from lookup.matching import _FETCH_LIMIT, filter_results_by_artist

    rows = await db.search(artist=artist, limit=_FETCH_LIMIT)
    return bool(filter_results_by_artist(rows, artist))


async def lookup_releases_by_artist(
    artist: str,
    limit: int = 10,
    service: DiscogsService | None = None,
) -> list[DiscogsSearchResult]:
    """Look up releases by an artist using Discogs.

    Args:
        artist: Artist name to search for
        limit: Maximum number of results
        service: Optional DiscogsService instance (with cache). If not provided,
            creates a cacheless fallback service.

    Returns:
        The ranked ``DiscogsSearchResult`` list for releases by or featuring the
        artist. Callers that only need album titles read ``r.album``; the LML#631
        row-less path also reads ``r.release_id`` / ``r.release_url`` to carry the
        resolved identity for a non-library artist.
    """
    if service is None:
        service = _get_service()
    if not service:
        return []

    request = DiscogsSearchRequest(artist=artist)
    response = await service.search(request, limit=limit)

    return list(response.results)
