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
COORDINATOR calls :func:`finalize_bio` after ``item.enrich_one`` has run,
passing in the ENRICHED top-1 result's ``artist_bio`` — the one signal that
answers whether the decided bio actually survived the gate.

**The variable split is the crux.** ``top1_bio`` (the raw Discogs profile
text) has two other consumers in the coordinator that must keep seeing
Discogs markup, never Wikipedia prose: the ``profile_tokens`` cache-only
parse, and the Discogs ref-warm (``background.maybe_schedule_discogs_bio_warm``).
This module never mutates its caller's ``top1_bio`` — it only returns a
NEW ``bio`` value on :class:`ServedBioResolution` for the response.

``wiki_url`` is ``pick.url`` (or ``None`` when there's no pick) on every
branch EXCEPT a positive cache hit (LML#1192 review round 5). There, it is
the CACHE ROW's own stored ``wikipedia_url``, which can legitimately differ
from the caller's sync ``pick``: a fetch-validated background warm
(``lookup/enrichment/wikipedia_warm.py``, round 4's P0-2 fix) can determine
a DIFFERENT, better page than the request path's unvalidated sync pick
names — the canonical case is Low or Sade, where the sync pick is the bare
disambiguation-page url (the sync path has no live fetch to validate
against) while the validated warm writes the row under the qualified, real
article's url instead. Through round 4, the cache read was additionally
keyed on ``wikipedia_url = pick.url``, so a row written under a different
url was permanently invisible to a caller whose sync pick never changes —
see ``entity/artist_wikipedia_bio.py``'s module docstring for the full
history and why the row is authoritative now. Every OTHER branch
(below-floor, negative hit, miss, flag off) still serves ``pick.url``,
since there is no cached content to override it with — the served pair
still can never disagree (the LML#504 split gate downstream nulls both
together, exactly as today), it just isn't always sourced from the same
place.

**LML#1192 review round 3, finding 10 — why ``bio_surfaced`` is a plain
truthiness check.** ``item.enrich_one``'s LML#504 gate has exactly two
outcomes for ``top1_bio``: pass it through UNCHANGED, or null it to
``None``. So once the coordinator has the enriched top-1 result, "does its
``artist_bio`` equal what ``resolve_served_bio`` decided" and "is its
``artist_bio`` truthy" are the SAME fact — a resolution can never survive
the gate as anything other than itself. :func:`finalize_bio` takes the
enriched ``artist_bio`` directly and reduces it to one ``bool()``, rather
than re-deriving that fact via an explicit equality comparison against
``resolution.bio`` (the coordinator's pre-round-3 shape, and the same
redundant shape the sibling Discogs ref-warm gate had for
``top1_bio_is_discogs`` — see ``lookup/enrichment/background.py``, whose
``served_bio_is_discogs`` param this module's :data:`BioSource` now also
answers directly instead of via string comparison).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from enum import Enum, auto

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


class BioSource(Enum):
    """Which text ``ServedBioResolution.bio`` actually came from.

    LML#1192 review round 3, finding 10: replaces a bare ``wiki_is_source:
    bool`` so a future third source (were one ever added) is a new enum
    member + one new :data:`SOURCE_STAT_KEYS` entry, not a find-every-``if``
    edit across this module and the coordinator.
    """

    DISCOGS = auto()
    WIKIPEDIA = auto()


SOURCE_STAT_KEYS: dict[BioSource, str] = {
    BioSource.WIKIPEDIA: SERVED_STAT_KEY,
    BioSource.DISCOGS: FALLBACK_DISCOGS_STAT_KEY,
}


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
    unconditionally, regardless of ``source``: this dataclass makes no
    promise about whether the response ultimately surfaces them (that's the
    gate's call, downstream). ``source`` is ``BioSource.WIKIPEDIA`` only for
    a fresh positive cache hit; every other outcome (flag off, no pick,
    below-floor, negative hit, miss) carries the Discogs pair with
    ``source=BioSource.DISCOGS``. ``miss_details``/``discogs_artist_id`` are
    set ONLY on a genuine cache miss (the one case with something worth
    warming) — both ``None`` otherwise, which the internal miss-warm
    scheduler treats as "nothing to schedule."

    LML#1192 review round 5: ``miss_details`` carries ``top1_details``
    itself (name + urls), not a pre-computed ``PickedWikiUrl`` — the warm
    now runs its own fetch-validated candidate selection
    (``lookup.wikipedia_pick_validation.resolve_and_validate_pick``) rather
    than reusing the request path's unvalidated sync pick, so the sync
    pick's score/lang/below-floor fields are no longer useful to it.
    """

    bio: str | None
    wiki_url: str | None
    source: BioSource
    miss_details: ArtistDetails | None
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
    ``discogs_artist_id`` alone (LML#1192 review round 5 — the row is
    authoritative, not the caller's pick; see
    ``entity/artist_wikipedia_bio.py``'s module docstring). A fresh
    positive hit serves the cached extract AND the cached row's own
    ``wikipedia_url`` (which may differ from ``pick.url`` — a validated
    warm can have chosen a different, better page); a fresh negative hit
    falls back to the Discogs pair; an absent/stale row (miss) also falls
    back but carries ``top1_details`` forward on ``miss_details`` for a
    possible warm.

    Cache-fact telemetry (``CACHE_HIT``/``CACHE_NEGATIVE``) is recorded
    immediately here — it's true regardless of what the response ends up
    surfacing. Adoption telemetry and warm-scheduling are deliberately NOT
    done here (LML#1192 review, B2-1/B2-2) — see :func:`finalize_bio`.
    """
    wiki_url = pick.url if pick is not None else None
    discogs_resolution = ServedBioResolution(
        bio=top1_bio,
        wiki_url=wiki_url,
        source=BioSource.DISCOGS,
        miss_details=None,
        discogs_artist_id=None,
    )

    if not _bio_prefer_wikipedia_enabled() or pick is None or pick.below_floor:
        return discogs_resolution

    discogs_artist_id = getattr(top1_details, "artist_id", None)
    if discogs_cache_pg is None or not isinstance(discogs_artist_id, int) or discogs_artist_id <= 0:
        return discogs_resolution

    cached = await get_cached_artist_wikipedia_bio(
        discogs_cache_pg, discogs_artist_id=discogs_artist_id
    )

    if cached.was_present and cached.value is not None and cached.value.extract is not None:
        _record(CACHE_HIT_STAT_KEY)
        return ServedBioResolution(
            bio=cached.value.extract,
            wiki_url=cached.value.wikipedia_url,
            source=BioSource.WIKIPEDIA,
            miss_details=None,
            discogs_artist_id=None,
        )

    if cached.was_present:
        _record(CACHE_NEGATIVE_STAT_KEY)
        return discogs_resolution

    # Genuine miss: no row at all (absent or aged past its TTL). Carry
    # top1_details (guaranteed non-None here -- the artist_id check above
    # already required it) forward -- the coordinator decides, post-gate,
    # whether a warm is worth scheduling.
    assert top1_details is not None
    return ServedBioResolution(
        bio=top1_bio,
        wiki_url=wiki_url,
        source=BioSource.DISCOGS,
        miss_details=top1_details,
        discogs_artist_id=discogs_artist_id,
    )


def _record_bio_adoption(resolution: ServedBioResolution, *, bio_surfaced: bool) -> None:
    """Post-hoc adoption telemetry: SERVED or FALLBACK_DISCOGS, gated on
    whether the LML#504 split gate actually let the bio through.

    LML#1192 review, B2-1: the pre-fix code recorded these unconditionally
    inside ``resolve_served_bio``, before the gate ran, so the adoption rate
    counted resolutions the response never surfaced. Neither counter fires
    when ``bio_surfaced`` is False. Private: called only by
    :func:`finalize_bio` (LML#1192 review round 3, finding 10).
    """
    if not bio_surfaced:
        return
    _record(SOURCE_STAT_KEYS[resolution.source])


def _maybe_schedule_wikipedia_bio_warm(
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
    AND ``resolve_served_bio`` recorded a genuine miss (``miss_details`` is
    only ever non-None on that branch). Private: called only by
    :func:`finalize_bio` (LML#1192 review round 3, finding 10).

    LML#1192 review round 5: passes ``artist_name``/``urls`` (from
    ``miss_details``) rather than a pre-computed ``PickedWikiUrl`` — the
    warm now runs its own fetch-validated candidate selection instead of
    reusing the sync pick.
    """
    if not warm_cache or not bio_surfaced:
        return
    if (
        resolution.miss_details is None
        or discogs_cache_pg is None
        or resolution.discogs_artist_id is None
    ):
        return
    scheduled = wikipedia_warm.schedule_wikipedia_bio_warm(
        discogs_artist_id=resolution.discogs_artist_id,
        artist_name=resolution.miss_details.name,
        urls=resolution.miss_details.urls,
        discogs_cache_pg=discogs_cache_pg,
    )
    if scheduled:
        _record(CACHE_MISS_WARM_SCHEDULED_STAT_KEY)


def finalize_bio(
    resolution: ServedBioResolution,
    *,
    enriched_top1_bio: str | None,
    enriched_top1_wiki: str | None,
    warm_cache: bool,
    discogs_cache_pg: PgSource | None,
) -> bool:
    """Post-``item.enrich_one`` finish: adoption telemetry + miss-warm
    scheduling in one call.

    LML#1192 review round 3, finding 10: replaces the coordinator's own
    ``record_bio_adoption`` + ``maybe_schedule_wikipedia_bio_warm`` pair of
    calls (and the ``resolution_bio_surfaced`` boolean it hand-computed via
    an explicit ``== resolution.bio`` comparison — see the module
    docstring for why that comparison was always redundant with a plain
    truthiness check). Returns ``bio_surfaced`` so the coordinator can pass
    the SAME fact into the sibling Discogs ref-warm gate
    (``background.maybe_schedule_discogs_bio_warm``'s
    ``top1_bio_surfaced``), which needs it too.

    LML#1192 review round 4, P0-6: adoption telemetry gates on
    ``bio_surfaced`` (bio TEXT reached the wire — the right question for
    "which source got served"), but the miss-warm must NOT: an artist
    with no Discogs profile at all has ``top1_bio=None`` even on a genuine
    cache miss, so ``bio_surfaced`` is unconditionally ``False`` for
    exactly the cohort this warm exists for (Discogs has nothing; Wikipedia
    might) — gating the warm on it made the feature unreachable for that
    cohort, and ``CACHE_MISS_WARM_SCHEDULED_STAT_KEY`` invisible for them
    too. The warm instead gates on ``enriched_top1_wiki`` — nulled by the
    SAME LML#504 split gate, independently of bio text (``item.enrich_one``
    applies ``is_artist_derived_eligible`` to ``top1_wiki`` on its own line,
    not as a side effect of the bio one) — so "did the identity gate let
    this artist through at all" is answered correctly regardless of
    whether there was ever bio text to show.
    """
    bio_surfaced = bool(enriched_top1_bio)
    gate_passed = enriched_top1_wiki is not None
    _record_bio_adoption(resolution, bio_surfaced=bio_surfaced)
    _maybe_schedule_wikipedia_bio_warm(
        resolution,
        warm_cache=warm_cache,
        bio_surfaced=gate_passed,
        discogs_cache_pg=discogs_cache_pg,
    )
    return bio_surfaced
