"""Artwork breaker-shed reporting (LML#1398).

``lookup/artwork.py``'s ``fetch_artwork_for_items`` returns content only, so a
saturation-breaker shed and a genuine "Discogs has no cover" both come back as
``None``. This module carries the shed out of the fetch: ``fetch_one`` records
it on an ``ArtworkFetchOutcome`` and the orchestrator turns a ``True`` into
``degraded: true, degraded_reason: upstream_unavailable`` — the artwork leg's
counterpart of ``lookup/enrichment/shed.py``'s ``Top1FetchOutcome`` (LML#1420)
and the search leg's ``upstream_shed`` (LML#1126).

A shed is "couldn't ask, try later — never a confirmed miss"
(``core/exceptions.py``): callers negative-cache a null artwork for up to 7
days (WXYC/Backend-Service#2522 / #2743), so it must be distinguishable from a
real miss. Two transient shapes are recorded — ``DiscogsService.search``
*catches* the breaker and returns ``None`` (LML#918's degraded contract, see
:func:`mark_if_search_degraded`), while the lazy release-resolution probe
(``resolve_release_for_track``) re-raises it into ``fetch_one``'s ``except
BreakerOpenError``. (A shed *inside* ``_resolve_fallback_artwork`` is
deliberately not recorded here: that cascade catches it at its own boundary
(LML#1281) and still returns the bound release id, so the row is not the
no-release / no-tracklist shape callers negative-cache.) Genuine per-item
errors keep the old log-and-``None`` behaviour and are NOT recorded: they are
bugs, not upstream unavailability, and flagging a deterministic failure
degraded would make every re-asking caller retry it forever (the reasoning
LML#1420 applied to top1's generic arm).
"""

import logging
from dataclasses import dataclass

from discogs.models import DiscogsSearchResponse

logger = logging.getLogger(__name__)


@dataclass
class ArtworkFetchOutcome:
    """Out-parameter for ``fetch_artwork_for_items``: was any item shed?"""

    upstream_shed: bool = False
    """True when at least one item's artwork fetch was shed, not answered."""


def record_shed(outcome: ArtworkFetchOutcome | None) -> None:
    """Mark a breaker/degrade shed on the caller's outcome.

    Deliberately no logging here (mirrors ``lookup.enrichment.shed.record_shed``):
    the call sites log the shed and never route it through ``logger.exception``
    (the LML#755 Sentry flood).
    """
    if outcome is not None:
        outcome.upstream_shed = True


def mark_if_search_degraded(
    response: DiscogsSearchResponse | None, outcome: ArtworkFetchOutcome | None
) -> None:
    """Record the ``None`` ``DiscogsService.search`` returns for a degraded call.

    LML#918: that ``None`` means rate-limited / retries exhausted / breaker
    shed / 5xx — never a genuine empty result (that is a
    ``DiscogsSearchResponse`` with ``results == []``). Called only once the
    floor and every fallback have failed to recover the item, so the null the
    caller is about to see is "couldn't ask", not "asked and missed".
    """
    if response is None:
        record_shed(outcome)
