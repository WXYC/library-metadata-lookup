"""Wikipedia-preferred artist-bio read path (Phase B of the
Wikipedia-preferred-artist-bio program, ``docs/plans/lml-1192-wikipedia-artist-bio.md``;
LML#513/#1192).

Called once by the ``lookup/enrichment/__init__.py`` coordinator, after the
top-1 Discogs prefetch and before ``item.enrich_one``. ``top1.py`` stays
Discogs-only and ``lookup/orchestrator.py`` is untouched (Project-32
posture).

``resolve_served_bio`` owns the ``CachedValue`` read, served-pair
resolution, and warm-scheduling decision that used to be inline in the
coordinator — extracted so the coordinator's edit stays line-neutral (it
gains only a call + unpack; see ``lookup/enrichment/__init__.py``'s
docstring for the funding relocation that pays for it).

**The variable split is the crux.** ``top1_bio`` (the raw Discogs profile
text) has two other consumers in the coordinator that must keep seeing
Discogs markup, never Wikipedia prose: the ``profile_tokens`` cache-only
parse, and the Discogs ref-warm (``background.maybe_schedule_discogs_bio_warm``).
This module never mutates its caller's ``top1_bio`` — it only returns a
NEW ``served_bio`` value for the response.

``served_wiki_url`` is **always** ``pick.url`` (or ``None`` when there's no
pick), in every branch below — Phase B only ever swaps which TEXT
accompanies that link, never the link itself, so the served pair can never
disagree (the LML#504 split gate downstream nulls both together, exactly as
today).
"""

from __future__ import annotations

import logging
import os

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


async def resolve_served_bio(
    pick: PickedWikiUrl | None,
    top1_bio: str | None,
    top1_details: ArtistDetails | None,
    discogs_cache_pg: PgSource | None,
    *,
    warm_cache: bool,
) -> tuple[str | None, str | None]:
    """Resolve the ``(served_bio, served_wiki_url)`` pair for the top-1 result.

    Flag off, no pick, or a below-floor pick → the Discogs pair
    (``top1_bio``, ``pick.url`` or ``None``) — byte-identical to the
    pre-Phase-B response. Above-floor pick, flag on: one ``CachedValue``
    read of ``lml_cache.artist_wikipedia_bio`` keyed on
    ``(discogs_artist_id, pick.url)`` — the self-healing URL-match predicate
    lives in ``entity/artist_wikipedia_bio.py``'s SQL, not here. A fresh
    positive hit serves the cached extract; a fresh negative hit or an
    absent/stale row (miss) falls back to the Discogs pair, and a miss
    additionally schedules a background warm when ``warm_cache`` is set.
    """
    served_wiki_url = pick.url if pick is not None else None
    discogs_pair = (top1_bio, served_wiki_url)

    if not _bio_prefer_wikipedia_enabled() or pick is None or pick.below_floor:
        _record(FALLBACK_DISCOGS_STAT_KEY)
        return discogs_pair

    discogs_artist_id = getattr(top1_details, "artist_id", None)
    if discogs_cache_pg is None or not isinstance(discogs_artist_id, int) or discogs_artist_id <= 0:
        _record(FALLBACK_DISCOGS_STAT_KEY)
        return discogs_pair

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
        _record(SERVED_STAT_KEY)
        return cached.value, served_wiki_url

    if cached.was_present:
        _record(CACHE_NEGATIVE_STAT_KEY)
        _record(FALLBACK_DISCOGS_STAT_KEY)
        return discogs_pair

    if warm_cache:
        scheduled = wikipedia_warm.schedule_wikipedia_bio_warm(
            discogs_artist_id=discogs_artist_id, pick=pick, discogs_cache_pg=discogs_cache_pg
        )
        if scheduled:
            _record(CACHE_MISS_WARM_SCHEDULED_STAT_KEY)

    _record(FALLBACK_DISCOGS_STAT_KEY)
    return discogs_pair
