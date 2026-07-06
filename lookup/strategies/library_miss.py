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
from discogs.models import DiscogsSearchRequest, DiscogsSearchResult
from discogs.service import DiscogsService
from library.models import LibraryItem
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

    Returns ``None`` when:
    - ``discogs_service`` is not available
    - ``parsed.artist`` or ``parsed.album`` is empty / whitespace-only
    - Discogs returns no candidates
    - No candidate clears the 80/80 floor
    - Discogs raises (outage, rate-limit exhaustion) — logged and swallowed
    """
    if discogs_service is None:
        return None
    artist = (parsed.artist or "").strip()
    album = (parsed.album or "").strip()
    if not artist or not album:
        return None

    try:
        response = await discogs_service.search(DiscogsSearchRequest(album=album, artist=artist))
    except Exception:
        logger.warning("library-miss Discogs search failed for artist=%r album=%r", artist, album)
        return None

    if not response or not response.results:
        return None

    best = find_best_typed_match(
        response.results,
        query_artist=artist,
        query_title=album,
        artist_fn=lambda r: r.artist,
        title_fn=lambda r: r.album,
    )
    if best is None:
        return None

    library_item = LibraryItem(
        id=0,
        artist=best.artist or artist,
        title=best.album or album,
    )
    return library_item, best
