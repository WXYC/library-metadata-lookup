"""The artwork path's 80/80 floor, and the override gate built on it.

Extracted from ``lookup/artwork.py`` for LML#1290 at the boundary
``tests/unit/test_module_budgets.py`` prescribes — that file is deliberately
under-granted so the honest response to growth is an extraction, not a bump.

``pin_clears_floor`` still takes its floor as a **parameter** rather than
reaching for the sibling below it. That is the correctness constraint of the
whole gate, not an indirection: the decision table compares "the pin clears the
floor" against "the matcher clears the floor", and those two verdicts are
comparable only when one function produces both. Keeping it explicit also keeps
the seam patchable, so a test can prove both sides went through one call with
one set of query variants.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable

from wxyc_fastapi.observability import get_cache_stats_recorder

from clients.streaming.matching import find_best_typed_match
from discogs.models import DiscogsSearchResult

logger = logging.getLogger(__name__)

Floor = Callable[..., DiscogsSearchResult | None]

#: Grading ran and the pin failed — the demotion signal for the pre-flip measurement.
PIN_FAILED_FLOOR_STAT_KEY = "override_pin_failed_floor"
#: Grading ran at all (denominator for the above; excludes every degrade).
PIN_GRADED_STAT_KEY = "override_pin_graded"
#: Decision-table row 2 — a track-validated carried release took the binding.
PIN_YIELDED_CARRIED_STAT_KEY = "override_pin_yielded_carried"
#: Decision-table row 3 — the floored matcher took the binding.
PIN_YIELDED_MATCHER_STAT_KEY = "override_pin_yielded_matcher"
#
# Rows 2 + 3 ARE the binding delta the flag's rollout note says to watch; row 4
# (the pin re-bound, no change) is ``failed - carried - matcher``, so it needs no
# key of its own. Recorded as cache-stats counters rather than log lines, per the
# LML#681/#683 pre-flip measurement pattern.
#
# NOT seeded into ``lookup/router.py``'s ``_LML_CACHE_STATS_EXTRA_KEYS``, and that
# is a deliberate omission rather than an oversight: that file measures **1250
# against a 1250 ceiling**, so seeding four keys there needs an extraction this
# PR has no business making. The cost is that the series read absent rather than
# zero until the first demotion — acceptable because the flag ships dark, so
# nothing records until someone flips it and is watching. Seed them when router.py
# next gets its extraction.


def _floor_candidates(
    candidates: Iterable[DiscogsSearchResult],
    *,
    artist_variants: list[str],
    album_variants: list[str],
) -> DiscogsSearchResult | None:
    """The LML#478 80/80 floor as this module applies it — one call, two callers.

    Named for LML#1290 so ``lookup.override_floor.pin_clears_floor`` can grade a
    pin through the *same* match class the matcher is held to, rather than
    re-expressing it.

    **Deliberately NOT ``lookup.typed_pair_floor.floor_best_typed_pair``.** That
    module documents itself as "one implementation, two callers" of the
    ARTIST_PLUS_ALBUM class; this is a third and a *different* class — candidate
    artist axis over raw ``r.artist_variants()`` where the shared one adds the
    LML#1206 suffix-stripped forms (a strict superset), and a bare ``release_id``
    tie-break where the shared one ranks exact raw credits first. Folding them
    changes the non-pinned search path for every lookup, so it is tracked as
    WXYC/library-metadata-lookup#1339 and must not ride here. Query-side variants
    are *lists*, which the shared helper's string signature cannot take anyway.
    """
    return find_best_typed_match(
        candidates,
        query_artist=artist_variants,
        query_title=album_variants,
        artist_fn=lambda r: r.artist_variants(),
        title_fn=lambda r: r.album,
        # LML#1097: deterministic tie-break by release_id, mirroring
        # release_resolution.py's (-score, release_id) sort key.
        key_fn=lambda r: r.release_id,
    )


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

    **Known cost, not yet addressed.** ``get_release_lean`` runs the full lean
    hydration -- five correlated ``json_agg`` subqueries including the whole
    tracklist and both ``release_track_artist`` legs, plus a second genre/style
    round-trip -- to read ``title``, ``artist`` and ``artists[].name``. On the
    53.3% "pin clears" path the same release is then hydrated *again* by
    ``_bind_resolved_release`` -> ``_resolve_fallback_artwork`` -> ``get_release``.
    A targeted ``SELECT r.title, agg`` would do, but no such method exists on
    ``DiscogsCacheService`` and adding one is not this slice's business. Weigh it
    against the post-launch hardening latency work before flipping the flag.

        The bare ``except`` is mandatory rather than stylistic: ``get_release_lean``
    routes its catch-all through ``_classify_cache_error``, which is typed
    ``NoReturn`` — it always raises and never degrades to ``None``.

    The empty-identifier guard is the LML#510 tombstone. A 404 marker carries
    ``title = ""`` / ``artist = ""`` as sentinels, and the tombstone-to-``None``
    translation lives at the *service* boundary this read bypasses, so without
    the guard a Discogs outage would score two empty strings, fail the floor,
    and demote a correct pin.
    """
    # The card side can be degenerate too, and a query-side short-circuit is
    # NOT a failed grading. ``find_best_typed_match`` drops empty/whitespace
    # variants and returns ``None`` when either axis has nothing usable left --
    # before it scores a single candidate. Reading that as "the pin failed"
    # would demote a pin on a grading that never ran, and on decision-table
    # row 2 that silently discards a hand-verified pin in favour of the carried
    # release. Defective card rows are exactly the population this gate is
    # built to be conservative about, so they keep their pins.
    if not [v for v in artist_variants if v and v.strip()]:
        return True
    if not [v for v in album_variants if v and v.strip()]:
        return True
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
    recorder = get_cache_stats_recorder()
    recorder.record(PIN_GRADED_STAT_KEY)
    cleared = (
        floor(
            [DiscogsSearchResult.from_release_metadata(metadata)],
            artist_variants=artist_variants,
            album_variants=album_variants,
        )
        is not None
    )
    if not cleared:
        recorder.record(PIN_FAILED_FLOOR_STAT_KEY)
    return cleared
