"""Wikipedia-preferred artist-bio read path (Phase B of the
Wikipedia-preferred-artist-bio program, ``docs/plans/lml-1192-wikipedia-artist-bio.md``;
LML#513/#1192).

Called once by the ``lookup/enrichment/__init__.py`` coordinator, after the
top-1 Discogs prefetch and before ``item.enrich_one``. ``top1.py`` stays
Discogs-only and ``lookup/orchestrator.py`` is untouched (Project-32
posture).

``resolve_served_bio`` owns the ``CachedValue`` read and served-pair
resolution that used to be inline in the coordinator. It returns a
:class:`ServedBioResolution` rather than committing to any telemetry or
warm-scheduling itself: it runs BEFORE ``item.enrich_one``, so it cannot
know whether the LML#504 artist-identity split gate will null the bio back
out before it reaches the wire (LML#1192 review, B2-1/B2-2). The
COORDINATOR calls :func:`record_bio_adoption` and
:func:`maybe_schedule_wikipedia_bio_warm` after ``item.enrich_one`` has run,
passing in whether the decided bio actually survived the gate — the same
post-hoc comparison pattern already used for the Discogs ref-warm's
``top1_bio_is_discogs`` (``lookup/enrichment/__init__.py``).

**The variable split is the crux.** ``top1_bio`` (the raw Discogs profile
text) has two other consumers in the coordinator that must keep seeing
Discogs markup, never Wikipedia prose: the ``profile_tokens`` cache-only
parse, and the Discogs ref-warm (``background.maybe_schedule_discogs_bio_warm``).
This module never mutates its caller's ``top1_bio`` — it only returns a
NEW ``bio`` value on :class:`ServedBioResolution` for the response.

``wiki_url`` is **always** ``pick.url`` (or ``None`` when there's no pick),
in every branch below — Phase B only ever swaps which TEXT accompanies that
link, never the link itself, so the served pair can never disagree (the
LML#504 split gate downstream nulls both together, exactly as today).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from wxyc_fastapi.observability import get_cache_stats_recorder

from discogs.models import ArtistDetails
from entity.artist_wikipedia_bio import get_cached_artist_wikipedia_bio
from entity.sources import PgSource
from lookup.enrichment import wikipedia_warm
from lookup.wikipedia_url import PickedWikiUrl

logger = logging.getLogger(__name__)

BIO_PREFER_WIKIPEDIA_ENV_VAR = "LML_BIO_PREFER_WIKIPEDIA"
"""Default OFF. Inert unless ``LML_WIKIPEDIA_SLUG_MATCH`` (Phase A,
``lookup/wikipedia_url.py``) is also ON: with slug-match off, every pick's
``below_floor`` is unconditionally True, so this flag never gets past the
first gate below. See ``docs/env-vars.md``."""

_TRUE_FLAG_VALUES: frozenset[str] = frozenset({"1", "true", "yes", "on"})

CACHE_HIT_STAT_KEY = "wikipedia_bio_cache_hit"
CACHE_NEGATIVE_STAT_KEY = "wikipedia_bio_cache_negative"
CACHE_MISS_WARM_SCHEDULED_STAT_KEY = "wikipedia_bio_cache_miss_warm_scheduled"
SERVED_STAT_KEY = "wikipedia_bio_served"
FALLBACK_DISCOGS_STAT_KEY = "wikipedia_bio_fallback_discogs"


def _bio_prefer_wikipedia_enabled() -> bool:
    """Read the flag at call time (no Settings indirection) so it is a
    no-redeploy Railway lever, mirroring ``lookup.wikipedia_url._wikipedia_slug_match_enabled``."""
    raw = os.getenv(BIO_PREFER_WIKIPEDIA_ENV_VAR)
    if raw is None:
        return False
    return raw.strip().lower() in _TRUE_FLAG_VALUES


def _record(key: str) -> None:
    try:
        get_cache_stats_recorder().record(key)
    except Exception as e:
        logger.warning("Failed to record %s into cache_stats: %s", key, e)


@dataclass(frozen=True)
class ServedBioResolution:
    """What :func:`resolve_served_bio` decided, plus what the coordinator
    needs to finish the job once the LML#504 gate's outcome is known.

    ``bio``/``wiki_url`` are the pair to thread into ``item.enrich_one`` —
    unconditionally, regardless of ``wiki_is_source``: this dataclass makes
    no promise about whether the response ultimately surfaces them (that's
    the gate's call, downstream). ``wiki_is_source`` is True only for a
    fresh positive cache hit; every other outcome (flag off, no pick,
    below-floor, negative hit, miss) carries the Discogs pair with
    ``wiki_is_source=False``. ``miss_pick``/``discogs_artist_id`` are set
    ONLY on a genuine cache miss (the one case with something worth warming)
    — both ``None`` otherwise, which :func:`maybe_schedule_wikipedia_bio_warm`
    treats as "nothing to schedule."
    """

    bio: str | None
    wiki_url: str | None
    wiki_is_source: bool
    miss_pick: PickedWikiUrl | None
    discogs_artist_id: int | None


async def resolve_served_bio(
    pick: PickedWikiUrl | None,
    top1_bio: str | None,
    top1_details: ArtistDetails | None,
    discogs_cache_pg: PgSource | None,
) -> ServedBioResolution:
    """Resolve the ``(bio, wiki_url)`` pair to serve for the top-1 result.

    Flag off, no pick, or a below-floor pick → the Discogs pair
    (``top1_bio``, ``pick.url`` or ``None``) — byte-identical to the
    pre-Phase-B response. Above-floor pick, flag on: one ``CachedValue``
    read of ``lml_cache.artist_wikipedia_bio`` keyed on
    ``(discogs_artist_id, pick.url)`` — the self-healing URL-match predicate
    lives in ``entity/artist_wikipedia_bio.py``'s SQL, not here. A fresh
    positive hit serves the cached extract; a fresh negative hit falls back
    to the Discogs pair; an absent/stale row (miss) also falls back but
    carries the pick forward on ``miss_pick`` for a possible warm.

    Cache-fact telemetry (``CACHE_HIT``/``CACHE_NEGATIVE``) is recorded
    immediately here — it's true regardless of what the response ends up
    surfacing. Adoption telemetry (``SERVED``/``FALLBACK_DISCOGS``) and
    warm-scheduling are deliberately NOT done here (LML#1192 review,
    B2-1/B2-2) — see :func:`record_bio_adoption` and
    :func:`maybe_schedule_wikipedia_bio_warm`.
    """
    wiki_url = pick.url if pick is not None else None
    discogs_resolution = ServedBioResolution(
        bio=top1_bio,
        wiki_url=wiki_url,
        wiki_is_source=False,
        miss_pick=None,
        discogs_artist_id=None,
    )

    if not _bio_prefer_wikipedia_enabled() or pick is None or pick.below_floor:
        return discogs_resolution

    discogs_artist_id = getattr(top1_details, "artist_id", None)
    if discogs_cache_pg is None or not isinstance(discogs_artist_id, int) or discogs_artist_id <= 0:
        return discogs_resolution

    # A PickedWikiUrl only ever omits ``url`` when pick_artist_wikipedia_url
    # returns None for the whole object (no wikipedia.org candidate at all)
    # — this branch already required ``pick is not None`` above, so ``url``
    # is guaranteed real here. Asserted for mypy narrowing.
    assert pick.url is not None
    cached = await get_cached_artist_wikipedia_bio(
        discogs_cache_pg, discogs_artist_id=discogs_artist_id, wikipedia_url=pick.url
    )

    if cached.was_present and cached.value is not None:
        _record(CACHE_HIT_STAT_KEY)
        return ServedBioResolution(
            bio=cached.value,
            wiki_url=wiki_url,
            wiki_is_source=True,
            miss_pick=None,
            discogs_artist_id=None,
        )

    if cached.was_present:
        _record(CACHE_NEGATIVE_STAT_KEY)
        return discogs_resolution

    # Genuine miss: no row at all (absent or aged past its TTL). Carry the
    # pick + artist id forward -- the coordinator decides, post-gate,
    # whether a warm is worth scheduling.
    return ServedBioResolution(
        bio=top1_bio,
        wiki_url=wiki_url,
        wiki_is_source=False,
        miss_pick=pick,
        discogs_artist_id=discogs_artist_id,
    )


def record_bio_adoption(resolution: ServedBioResolution, *, bio_surfaced: bool) -> None:
    """Post-hoc adoption telemetry: SERVED or FALLBACK_DISCOGS, gated on
    whether the LML#504 split gate actually let the bio through.

    LML#1192 review, B2-1: the pre-fix code recorded these unconditionally
    inside ``resolve_served_bio``, before the gate ran, so the adoption rate
    counted resolutions the response never surfaced. Neither counter fires
    when ``bio_surfaced`` is False.
    """
    if not bio_surfaced:
        return
    _record(SERVED_STAT_KEY if resolution.wiki_is_source else FALLBACK_DISCOGS_STAT_KEY)


def maybe_schedule_wikipedia_bio_warm(
    resolution: ServedBioResolution,
    *,
    warm_cache: bool,
    bio_surfaced: bool,
    discogs_cache_pg: PgSource | None,
) -> None:
    """Post-hoc miss-warm scheduling, symmetric with
    ``background.maybe_schedule_discogs_bio_warm``.

    LML#1192 review, B2-2: the Discogs ref-warm already only fires when the
    response actually surfaced the bio it would warm (LML#504) — the
    pre-fix Wikipedia miss-warm lacked that same gate. A warm is scheduled
    only when caching is requested, the response actually surfaced a bio,
    AND ``resolve_served_bio`` recorded a genuine miss (``miss_pick`` is
    only ever non-None on that branch).
    """
    if not warm_cache or not bio_surfaced:
        return
    if (
        resolution.miss_pick is None
        or discogs_cache_pg is None
        or resolution.discogs_artist_id is None
    ):
        return
    scheduled = wikipedia_warm.schedule_wikipedia_bio_warm(
        discogs_artist_id=resolution.discogs_artist_id,
        pick=resolution.miss_pick,
        discogs_cache_pg=discogs_cache_pg,
    )
    if scheduled:
        _record(CACHE_MISS_WARM_SCHEDULED_STAT_KEY)
