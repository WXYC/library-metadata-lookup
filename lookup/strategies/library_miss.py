"""Library-miss Discogs probe — the LML#583 step-3a fallback.

Not a pipeline strategy: ``perform_lookup`` calls
:func:`_library_miss_discogs_search` directly at step 3a, after the whole
search-strategy pipeline returned no library results and the request carries
both artist and album. A confident Discogs match (80/80-floor via
``find_best_typed_match``) is synthesized into a ``LibraryItem(id=0)`` +
``DiscogsSearchResult`` pair and handed straight to enrichment — bypassing
``fetch_artwork_for_items`` and step-3b track validation (see the step-3a
comment block in ``lookup/orchestrator.py`` for the bypass rationale).
Strategy-adjacent, so it lives in this package (LML#727).
"""

import logging

from clients.streaming.matching import find_best_typed_match
from discogs.models import DiscogsSearchRequest, DiscogsSearchResponse, DiscogsSearchResult
from discogs.service import DiscogsService
from library.models import LibraryItem
from lookup.matching import is_self_titled
from lookup.strategies.va_rescue import find_va_comp_match
from services.parser import ParsedRequest

logger = logging.getLogger(__name__)


async def _library_miss_discogs_search(
    parsed: ParsedRequest,
    discogs_service: DiscogsService | None,
) -> tuple[LibraryItem, DiscogsSearchResult] | None:
    """Search Discogs for a library-miss (artist, album) pair.

    Called only when the library search returned no results AND both
    ``parsed.artist`` and ``parsed.album`` are non-empty (after strip). Uses the
    existing ``discogs_service.search()`` fallthrough seam (cache-first, API on
    miss, outage degradation) so a Discogs outage degrades gracefully.

    Applies the same 80/80-floor as ``find_best_typed_match`` (LML#400) on both
    artist AND album jointly — different from the contamination shape in LML#400
    which was artist-fallback returning any release for any album. The new risk
    shape is near-miss typed albums (typed "Anthology" → "Anthology, Vol. 1");
    see regression tests for pinned cases.

    When every candidate from a PG-served response floor-fails, the search
    is re-issued once with ``skip_pg=True`` (LML#784 category 1): the PG arm
    joins ``release_artist`` one credit per row, so a multi-artist release
    surfaces as single credits that floor-fail singly, while the API arm's
    joined credit would pass — and the fallthrough seam treats any non-empty
    ``pg_read`` as terminal, masking that working arm. ``response.pg_served``
    is the retry gate: only the PG arm sets it, so an API-served first pass —
    including a memory-cache replay of one, which arrives ``cached=True`` —
    is never re-searched.

    When the floor still cleared nothing, compilation-credited candidates get
    one more look via :func:`find_va_comp_match` (LML#784 category 2) —
    embedded-title-segment and tracklist-credit evidence, since a V/A
    release's "Various" credit can never clear the artist axis.

    Returns ``None`` when:
    - ``discogs_service`` is not available
    - ``parsed.artist`` or ``parsed.album`` is empty / whitespace-only
    - Discogs returns no candidates
    - No candidate clears the 80/80 floor (on either arm) nor the V/A rescue
    - The first Discogs search raises (outage, rate-limit exhaustion) —
      logged and swallowed; a raised API *retry* instead degrades to the
      rescue over the cache-served candidates
    """
    if discogs_service is None:
        return None
    artist = (parsed.artist or "").strip()
    album = (parsed.album or "").strip()
    if not artist or not album:
        return None

    # LML#784 category 4: a query-side self-titled placeholder ("S/T",
    # "s.t.", "self-titled") can never match a real Discogs title — swap in
    # the artist name for both the search and the title-axis scoring,
    # mirroring the library-side swap in ``lookup/artwork.py``. The trigger
    # string is NOT kept as a scoring variant: a wrong-release candidate
    # literally titled "S/T" would clear the floor trivially.
    if is_self_titled(album):
        album = artist

    request = DiscogsSearchRequest(album=album, artist=artist)

    def _floor_best(response: DiscogsSearchResponse | None) -> DiscogsSearchResult | None:
        if not response or not response.results:
            return None
        return find_best_typed_match(
            response.results,
            query_artist=artist,
            query_title=album,
            artist_fn=lambda r: r.artist_variants(),
            title_fn=lambda r: r.album,
            # LML#1097: deterministic tie-break by release_id, mirroring
            # release_resolution.py's (-score, release_id) sort key.
            key_fn=lambda r: r.release_id,
        )

    try:
        response = await discogs_service.search(request)
    except Exception:
        logger.warning("library-miss Discogs search failed for artist=%r album=%r", artist, album)
        return None

    best = _floor_best(response)

    if best is None and response is not None and response.pg_served:
        logger.info(
            "library-miss cache candidates all floor-failed for artist=%r album=%r; "
            "retrying API-only (LML#784)",
            artist,
            album,
        )
        try:
            retry = await discogs_service.search(request, skip_pg=True)
        except Exception:
            # A raised (or, below, empty/None) retry keeps the PG-served
            # candidates: the rescue works during an API outage (segments
            # are pure CPU; the tracklist fetch reads the PG tier first).
            logger.warning(
                "library-miss Discogs API retry failed for artist=%r album=%r", artist, album
            )
        else:
            best = _floor_best(retry)
            if retry is not None and retry.results:
                # A non-empty retry supersedes even when it floor-fails: the
                # fuzzy API arm's retrieval is category 2's own premise. Only
                # an empty (or degraded None, LML#918) retry keeps the
                # PG-served candidates.
                response = retry

    if best is None and response is not None and response.results:
        # LML#784 category 2: compilation-credited candidates structurally
        # cannot clear the release-level floor ("Various" on the artist axis,
        # subtitled comp titles on the album axis). Give them one more look
        # against embedded-title-segment and tracklist-credit evidence.
        # Applies to whichever arm's candidate set we ended with. The rescue
        # self-guards against degenerate axes (artist ≈ album, incl. the
        # category-4 swap above) and compilation-artist queries.
        best = await find_va_comp_match(
            response.results,
            query_artist=artist,
            query_album=album,
            discogs_service=discogs_service,
        )

    if best is None:
        return None

    library_item = LibraryItem(
        id=0,
        artist=best.artist or artist,
        title=best.album or album,
    )
    return library_item, best
