"""Release-cover → sibling-pressing → artist-image fallback cascade.

Extracted verbatim from ``lookup/artwork.py`` (LML#1290 prep), which had
reached its module budget. The concern boundary is the one
``tests/unit/test_module_budgets.py`` names for this split: *given a release
id, produce the best artwork URL*, leaving ``lookup/artwork.py`` owning "for
these items, search and bind Discogs results".

Peer of ``lookup/sibling_artwork.py``, which this module's step 2 calls and
which was extracted from ``lookup/artwork.py`` for the same reason.

Pure move: no behaviour change. The ``DiscogsBreakerOpenError`` narrowing at
the cascade boundary (LML#1118) moves with the cascade it guards.
"""

import logging

from config.settings import get_settings
from discogs.breaker import DiscogsBreakerOpenError
from discogs.service import DiscogsService
from lookup.sibling_artwork import resolve_sibling_artwork

logger = logging.getLogger(__name__)


async def _resolve_fallback_artwork(
    discogs_service: DiscogsService,
    release_id: int,
    *,
    allow_release_resolution_fallback: bool = True,
) -> str | None:
    """Try the release's own cover, then a sibling pressing, then artist image.

    LML#687 removed the label-image rung -- a label logo is essentially never
    correct album art -- then was reopened when that removal's "no cover"
    premise turned out wrong for both motivating albums: they have real
    covers in the cache, just under sibling release ids LML did not bind.
    Two independent fixes followed, in a load-bearing order:

    1. **LML#1242, the release's OWN cover, checked first.** A bulk-loaded
       release ``get_release`` has never live-checked for artwork
       (``artwork_checked_at IS NULL``) otherwise reads as coverless without
       Discogs ever being asked -- 96% of the misdiagnosed rows in a prod
       measurement. ``get_release(..., require_artwork_answer=True)`` narrows
       ``get_release``'s LML#542 widened cache-hit predicate back down for
       this one caller so that never-asked state becomes a live re-ask
       instead. A successful ask writes back to PG, so this amortizes to at
       most once per release, not once per lookup (LML#537 unaffected for
       every other ``get_release`` caller).
    2. **LML#1237/#1241, a SIBLING pressing's cover, checked second and
       gated.** The residual case once the release's own cover has genuinely
       come back empty -- ``lookup.sibling_artwork.resolve_sibling_artwork``.
       This MUST run after step 1, never before: binding a sibling's cover
       onto a release that was never itself asked would look like a fix
       while getting the 96% case wrong, exactly the ordering bug LML#1241's
       review caught in the original PR#1240. Gated off by default via
       ``settings.lml_resolve_sibling_pressing_artwork`` -- on measured value,
       not viability. That flag's description and
       ``lookup/sibling_artwork.py``'s module docstring are the canonical
       copies of the cost/gating rationale; do not restate the numbers here.

    ``allow_release_resolution_fallback`` (default ``True``) is the bulk kill
    switch ``fetch_artwork_for_items`` documents above; two things are specific
    to this function. Off, step 1 reads whatever PG has instead of re-asking
    and step 2 is skipped ENTIRELY -- cache leg included, because step 1 not
    asking Discogs means a never-asked release reaches step 2 looking coverless
    without having been asked, and binding a sibling's cover onto it there is
    the very ordering bug this cascade is ordered to avoid (LML#1281 review).
    And a caller omitting the keyword silently gets ``True``, opting into live
    re-asking on both steps -- right for ``/lookup``, a trap for a new batch
    caller: anything walking more than a handful of releases outside one
    interactive request MUST pass ``False`` explicitly (see the LML#1020
    drain's call site).

    Structurally invalid ids (``release_id <= 0``) short-circuit before the
    Discogs round-trip — the LML#401 synthesis pattern produces a
    ``release_id=0`` sentinel that any future caller could leak in here (see
    issue #518). Discogs release ids start at 1, so the strict gate is also a
    correctness check against malformed upstream payloads.
    """
    if release_id <= 0:
        return None
    try:
        return await _artwork_rungs(
            discogs_service,
            release_id,
            allow_release_resolution_fallback=allow_release_resolution_fallback,
        )
    except DiscogsBreakerOpenError:
        # LML#1118: narrow, and at the CASCADE boundary rather than per-rung.
        # ``get_release`` re-raises a shed on purpose (LML#755 FIX 1) so a
        # *validation* caller can tell "couldn't ask" from "asked, no";
        # artwork is not such a caller. Uncaught it reaches
        # ``fetch_artwork_for_items.fetch_one``, whose ``except Exception``
        # discards the WHOLE item -- and this runs after ``result`` is built,
        # so that throws away a complete DiscogsSearchResult (release URL,
        # title, tracklist), not just the artwork. One boundary covers every
        # rung with no per-rung opt-in to forget, the posture
        # ``docs/architecture.md`` records for the breaker-fanning strategies.
        # ``get_master`` needs no coverage: it swallows a shed into ``None``.
        logger.debug(f"Breaker shed artwork resolution for release {release_id}")
        return None


async def _artwork_rungs(
    discogs_service: DiscogsService,
    release_id: int,
    *,
    allow_release_resolution_fallback: bool,
) -> str | None:
    """Rung cascade for :func:`_resolve_fallback_artwork`; see its docstring."""
    release = await discogs_service.get_release(
        release_id, require_artwork_answer=allow_release_resolution_fallback
    )
    if not release:
        return None

    # The search endpoint's `cover_image` is sometimes empty for releases whose
    # release-detail `images[0].uri` is populated. Prefer that over the
    # artist/label image fallback so enrichment-worker callers (single LML
    # round-trip) recover the same cover the /proxy/metadata/album legacy
    # two-call path produces via populateReleaseMetadata.
    if release.artwork_url:
        return release.artwork_url

    if get_settings().lml_resolve_sibling_pressing_artwork:
        sibling_artwork = await resolve_sibling_artwork(
            discogs_service,
            release,
            allow_release_resolution_fallback=allow_release_resolution_fallback,
        )
        if sibling_artwork:
            return sibling_artwork

    if release.artist_id:
        image = await discogs_service.get_artist_image(release.artist_id)
        if image:
            logger.info(f"Using artist image fallback for release {release_id}")
            return image

    return None
