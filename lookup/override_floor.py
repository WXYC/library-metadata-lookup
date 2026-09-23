"""The override floor: does a hand-verified pin survive the match class?

Extracted from ``lookup/artwork.py`` for LML#1290 at the boundary
``tests/unit/test_module_budgets.py`` prescribes — that file is deliberately
under-granted so the honest response to growth is an extraction, not a bump.

The floor itself stays with its caller and is passed in. That is the
correctness constraint of the whole gate, not an indirection: the decision
table compares "the pin clears the floor" against "the matcher clears the
floor", and those two verdicts are comparable only when one function produces
both. A grading step that reached for its own floor could demote a pin for
failing a test its replacement never took.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from discogs.models import DiscogsSearchResult

logger = logging.getLogger(__name__)

Floor = Callable[..., DiscogsSearchResult | None]


async def pin_clears_floor(
    cache_service,
    release_id: int,
    *,
    floor: Floor,
    artist_variants: list[str],
    album_variants: list[str],
) -> bool:
    """Whether a hand-verified pin survives the floor (LML#1290).

    Returns ``True`` when the pin stands — and deliberately conflates two
    reasons for that: the pinned release cleared the floor, or it could not be
    graded at all. **Absence of evidence against a pin is never evidence
    against it**, so every degrade returns ``True`` and the caller binds the pin
    exactly as it does today. Only a release LML could read, and which then
    failed to match the card, returns ``False``.

    **The read is cache-only and that is load-bearing, not an optimisation.**
    ``DiscogsCacheService.get_release_lean`` reads PG in <= 2 round-trips and
    never enters the read-through's API leg, so no ``DiscogsBreakerOpenError``
    can arise here. Grading is a *validation* caller in the LML#755 sense — it
    needs to tell "couldn't ask" from "asked, no" — and the LML#1118 breaker
    catch in ``lookup/fallback_artwork.py`` is scoped to exclude exactly such
    callers ("artwork is not such a caller"). Reaching ``DiscogsService.get_release``
    from here would put a shed one ``except`` away from ``fetch_one``'s
    catch-all, which discards the WHOLE item — strictly worse than today, where
    a pinned row still binds through a breaker-open Discogs. Same shape as
    ``lookup.album_level_match._rehydrate_from_local_cache``.

    The bare ``except`` is mandatory rather than stylistic: ``get_release_lean``
    routes its catch-all through ``_classify_cache_error``, which is typed
    ``NoReturn`` — it always raises and never degrades to ``None``.

    The empty-identifier guard is the LML#510 tombstone. A 404 marker carries
    ``title = ""`` / ``artist = ""`` as sentinels, and the tombstone-to-``None``
    translation lives at the *service* boundary this read bypasses, so without
    the guard a Discogs outage would score two empty strings, fail the floor,
    and demote a correct pin.
    """
    if cache_service is None:
        return True
    try:
        metadata = await cache_service.get_release_lean(release_id)
    except Exception as exc:
        logger.warning("Override floor could not read release %s: %s", release_id, exc)
        return True
    if metadata is None:
        return True
    if not (metadata.artist or "").strip() or not (metadata.title or "").strip():
        return True
    return (
        floor(
            [DiscogsSearchResult.from_release_metadata(metadata)],
            artist_variants=artist_variants,
            album_variants=album_variants,
        )
        is not None
    )
