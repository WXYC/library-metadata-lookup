"""Top-1 enrichment breaker-shed reporting (LML#1420).

``top1.fetch_top1_release_details`` returns content only, so a Discogs
saturation-breaker shed and a genuine "no Discogs profile" both come back as
``None`` fields. This module carries the shed out of the fetch and decides
whether it cost the response content; the orchestrator turns a ``True`` into
``degraded: true, degraded_reason: upstream_unavailable``, the enrichment
leg's counterpart of the search leg's ``upstream_shed`` (LML#1126).
"""

from dataclasses import dataclass


@dataclass
class Top1FetchOutcome:
    """Out-parameter for ``top1.fetch_top1_release_details``: was it shed?"""

    upstream_shed: bool = False
    """True when either top-1 step was shed by the Discogs saturation breaker.
    Set ONLY by the two ``DiscogsBreakerOpenError`` branches; the generic
    ``except Exception`` deliberately leaves it False (see that branch)."""


def record_shed(outcome: Top1FetchOutcome | None) -> None:
    """Mark a breaker shed on the caller's outcome. Deliberately no logging
    here: a shed must never reach ``logger.exception`` (LML#1192 / LML#755)."""
    if outcome is not None:
        outcome.upstream_shed = True


def shed_cost_content(
    outcome: Top1FetchOutcome, should_prefetch_top1: bool, *, served_bio: str | None
) -> bool:
    """LML#1420: did a top-1 breaker shed actually cost the response content?

    Only then is the response marked degraded (the conservative reading: a
    consumer that re-asks on ``degraded`` should re-ask only when the answer
    could be richer). Three terms:

    - ``outcome.upstream_shed``: one of the two ``DiscogsBreakerOpenError``
      branches in ``top1.py`` fired. The generic ``except`` there never sets it.
    - ``should_prefetch_top1``: the LML#507 gate said the fetch's output would
      be consumed. With the kill switch off the fetch also runs when its
      output is discarded anyway; a shed there loses nothing.
    - ``served_bio is None``: decision on ``LML_BIO_PREFER_WIKIPEDIA``
      (LML#1192). If a bio still reached the wire, the field consumers
      settle on is present, so the response is not degraded for it. Today
      this cannot happen on a shed: the Wikipedia cache is keyed on the
      shed step's own ``artist_id``. The term pins the rule for a future key.

    Known over-approximation: an artist-step shed is still marked when the
    LML#504 gate would have nulled the bio anyway (title matched, artist
    identity did not verify). Telling those apart would mean re-deriving
    ``item.enrich_one``'s gate here.
    """
    return outcome.upstream_shed and should_prefetch_top1 and served_bio is None
