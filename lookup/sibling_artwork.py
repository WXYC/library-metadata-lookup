"""Sibling-pressing artwork recovery (LML#1237 / LML#1241).

Extracted out of ``lookup/artwork.py`` to keep that file under its own
module-budget ceiling (LML#731/#751) -- this is a self-contained concern with
no dependency on ``fetch_artwork_for_items``'s control flow, the same posture
``lookup/enrichment/apple_probe.py`` and ``lookup/streaming_warm.py`` took for
their own extractions.

A Discogs release with no ``images[0]`` cover does not mean the album has no
cover: Discogs carries many pressings per master and only some have an
uploaded image. #687 was reopened on exactly this evidence -- both Autechre
albums it argued from (*Confield*, *Chiastic Slide*) have real ``R-`` covers
in the production discogs-cache, just under release ids LML did not bind.
``resolve_sibling_artwork`` recovers that cover from another pressing of the
same album via the release's ``master_id``, as the rung between the bound
release's own cover and the artist-image fallback in
``lookup.artwork._resolve_fallback_artwork``.

**Ordering relative to LML#1242's never-asked re-ask.** This rung must run
strictly AFTER ``_resolve_fallback_artwork`` has asked ``get_release`` for an
artwork-authoritative answer on the BOUND release
(``require_artwork_answer=True``), never before. LML#1241's review finding
against the original PR#1240 (WXYC/library-metadata-lookup#1240) was exactly
this ordering bug: a never-asked bound release binding a *sibling's* cover
instead of asking for its own would look like a success while doing the
wrong thing for the 96% of failures (measured on prod, LML#1237) where the
bound release's own cover was simply never checked. #1242 shipped that
re-ask first; this module only ever sees a release whose own-cover answer is
already live-authoritative, so a coverless ``release`` argument here means
Discogs was actually asked and said no.

That guarantee is *why* ``allow_release_resolution_fallback=False`` gates the
WHOLE rung, both legs, at the top of the function rather than only its live
leg. With the switch off the cover rung asks
``get_release(require_artwork_answer=False)``, so LML#542's arm-2 predicate
treats a bulk-loaded row bearing a tracklist as a PG hit even when
``artwork_checked_at IS NULL`` -- the release arrives here coverless without
Discogs ever having been asked. Running even the cheap cache leg on that
input would bind an unrelated pressing's art onto a release whose own cover
was never checked, which is exactly PR#1240's review finding 2 surviving on
the bulk path. A never-asked release stays never-asked; this rung is right
not to paper over it with a sibling's cover, and gating both legs is what
makes that true rather than merely stated.

**Cost, in the order this function pays it:**

1. **Cache leg** (``DiscogsCacheService.get_sibling_release_artwork``) --
   a local PG read, no Discogs round-trip, and reached only when
   ``allow_release_resolution_fallback`` is on (see above -- correctness,
   not cost, though it also spares the LML#1020 drain a per-row PG
   transaction across 58k+ credits). Answers only from
   ``artwork_url IS NOT NULL AND NOT not_found`` rows: a sibling that was
   never live-checked for artwork (``artwork_checked_at IS NULL``) or one
   that Discogs 404'd (the LML#510 tombstone, whose UPSERT deliberately
   preserves a stale ``artwork_url`` -- LML#1241 review finding 1) is never
   read as proof the album lacks a cover, and a tombstone is never read as
   proof it has one either. A cache failure degrades to the live leg rather
   than aborting resolution, matching every other cache-read boundary in
   this service.
2. **Live leg** (``get_master`` + ``get_release``) -- at most one of each,
   gated by ``allow_release_resolution_fallback`` (the same LML#671/#652
   bulk kill switch ``_resolve_fallback_artwork`` already threads through
   for the never-asked re-ask). The ``get_release`` here passes
   ``require_artwork_answer=True`` for the same reason the cover rung does,
   and it is load-bearing rather than defensive: without it, LML#542's arm-2
   predicate reads a never-asked main release that happens to carry a
   tracklist as a PG hit and returns ``artwork_url=None`` -- the exact value
   the cache leg one step earlier already rejected for every row under this
   master. The ``get_master`` call would then have been spent on a follow-up
   read that *cannot* answer. Arm 3 still short-circuits an already-asked
   row, so this adds nothing on the common path; it only stops the expensive
   branch from being provably unproductive. ``get_master`` has no PG cache leg of its
   own (an ``@async_cached`` L1 TTL in front of an unconditional HTTP call,
   per LML#1241 review finding 3) -- every miss with a ``master_id`` and no
   cached sibling is a guaranteed live Discogs call when this leg runs.
   That is a known, declared cost, not a fixed one: giving ``get_master`` a
   PG fallthrough leg is real follow-up work, tracked on LML#1241, not
   folded into this change. Gating the live leg on
   ``allow_release_resolution_fallback``. **Who can reach the live leg.**
   The switch alone does not keep background callers off it, and LML#1241
   review finding 4 (the drain not setting low-priority on the shared rate
   gate) should not be answered with "the drains pass the switch off" --
   that is a property of what those callers happen to pass, not a guarantee.
   Since LML#920 the switch is a caller-settable query param
   (``/lookup/bulk?allow_release_resolution_fallback=true``), a supported
   mode advertising PG-backed *release resolution*, which would otherwise
   also buy an uncached live ``/masters/{id}`` per coverless item -- a
   different cost class than the parameter's name promises. So the live leg
   is additionally gated on ``not is_discogs_low_priority()``:
   ``/lookup/bulk`` sets that contextvar unconditionally (LML#927 -- "no
   ``X-Caller-Class`` value can escalate a bulk caller out of it"), so
   unlike the switch it is a gate the caller cannot escape. Same structural
   guard the location union uses for the same purpose (LML#1026). The cheap
   cache leg stays available to those callers; it costs the bucket nothing.

   **A breaker shed is NOT handled here.** ``get_release`` re-raises one
   (LML#755 FIX 1) and the guard lives at the cascade boundary in
   ``lookup.artwork._resolve_fallback_artwork``, so every rung is covered by
   one catch rather than each remembering its own. ``get_master`` needs no
   coverage at all: it swallows a shed into ``None`` via a bare
   ``except Exception`` -- the asymmetry ``scripts/drain_master_api_tail.py``
   documents when it has to infer a shed from ``breaker_is_open`` instead.

**The cost does not amortize, unlike the rung above it.** The cover rung
writes a successful live ask back to PG, so it is at most one live call per
release, ever. This rung's ``get_master`` is backed only by a per-process
4-hour ``MASTER_CACHE`` TTL, and the cover it recovers belongs to the
*sibling's* row -- there is nowhere to write it that would help the bound
release next time, because stamping a sibling's cover onto the bound
release's row would poison every other reader of that row. So the same bound
release re-pays every 4 hours, per replica, indefinitely. A memo table keyed
on the bound release is the real fix and is follow-up work, not folded in
here; the asymmetry is named because "at most one of each" above is a
per-call bound and would otherwise read as a lifetime one.

**Sibling pick.** The cache leg is ``ORDER BY id LIMIT 1`` -- deterministic
and repeat-stable, but the lowest cached release id is not necessarily the
pressing whose sleeve best represents the album (LML#1241 review finding 5).
This is a declared trade of recall-quality for a fixed, well-understood cost
rather than a scored ranking over pressings; Discogs's own designated
canonical release (``main_release_id``, consulted on the live leg) is
deliberately NOT preferred over an already-cached hit, since honoring it
would turn every cache hit into a live round-trip to compare against it.

**Master switch: off by default -- and the reason has changed.** The whole
rung is gated by the caller on
``settings.lml_resolve_sibling_pressing_artwork`` (default ``False``), which
is NOT this module's concern to check -- see
``lookup.artwork._resolve_fallback_artwork``.

The *original* reason was viability: ``idx_release_master_id`` did not exist
on the prod ``release`` table (WXYC/discogs-etl#412 -- a copy-swap rebuild
dropped it and the post-swap index list never recreated it), so the cache
leg's query was a measured 192ms full scan of all 148,491 rows, per item,
fanned out by ``asyncio.gather``, on the interactive artwork-miss path.
**That blocker is discharged.** The index was created on prod 2026-08-20
(``EXPLAIN`` after: 0.069ms, 5 buffers, an index scan) and
WXYC/discogs-etl#415 recreates it at both copy-swap sites plus an alembic
migration, so it now survives a rebuild.

What keeps the flag off is **value, not viability**. Measured against the
known-failed population, the whole rung recovers **40 of 4,139 rows --
0.97%** (LML#1237's prod measurement), and WXYC/discogs-etl#413 shows that
ceiling belongs to the *cache's dedup ranking* rather than to this code. Its
prod census: **8,431 masters have exactly one pressing cached and no artwork
on it**, versus 80 masters with two-or-more cached pressings and no artwork
anywhere. Read against this function's control flow, that is the whole story
-- the cheap cache leg can only answer where a *second* pressing survived
dedup carrying a cover, and for the 8,431 no such row exists to find. Those
are exactly the misses that fall through to the live leg, where
``get_master`` has no PG cache (review finding 3) and every call is a live
Discogs round-trip on the interactive path; for many of them
``main_release_id`` will be the release just checked, short-circuited by the
``main_release_id == release.release_id`` guard in the function body -- after
the master round-trip has already been paid for.

So flipping the flag on today spends a live round-trip on the misses least
likely to be recoverable, to buy roughly 1%. **Re-measure after
WXYC/discogs-etl#413 widens what dedup retains** -- that number, not the
index, is what should decide the flip.
"""

import logging

from discogs.cache_service import CacheSchemaSkewError, CacheUnavailableError
from discogs.models import ReleaseMetadataResponse
from discogs.ratelimit import is_discogs_low_priority
from discogs.service import DiscogsService

logger = logging.getLogger(__name__)


async def resolve_sibling_artwork(
    discogs_service: DiscogsService,
    release: ReleaseMetadataResponse,
    *,
    allow_release_resolution_fallback: bool = True,
) -> str | None:
    """Recover a cover from another pressing of ``release``'s album.

    ``None`` when ``release`` carries no ``master_id`` (a one-off /
    self-released title Discogs never grouped) -- a strict no-op for every
    release the pre-LML#1237 cascade already handled correctly.

    Cache first (``DiscogsCacheService.get_sibling_release_artwork``); only
    when the cache can't answer does this consult the master live, and only
    when ``allow_release_resolution_fallback`` allows it. Skipped entirely
    when the master's canonical release IS the release already just
    checked, so a coverless master never pays a redundant round-trip for the
    same row. See the module docstring for the full cost and ordering
    rationale.
    """
    if not release.master_id or not allow_release_resolution_fallback:
        return None

    cache = discogs_service.cache_service
    if cache is not None:
        try:
            sibling_artwork = await cache.get_sibling_release_artwork(
                release.master_id, exclude_release_id=release.release_id
            )
            if sibling_artwork:
                return sibling_artwork
        except (CacheUnavailableError, CacheSchemaSkewError) as e:
            logger.warning(
                f"Sibling artwork cache lookup failed for master {release.master_id}: {e}"
            )

    # The cheap cache leg above is a local PG read and costs the shared Discogs
    # bucket nothing, so a background caller may have it. The live leg is a
    # different matter -- see the module docstring's "who can reach the live
    # leg" note for why this gate and not the switch alone.
    if is_discogs_low_priority():
        return None

    master = await discogs_service.get_master(release.master_id)
    if master is None or not master.main_release_id:
        return None
    if master.main_release_id == release.release_id:
        return None

    # ``lean``: only ``.artwork_url`` is read off this result, and the lean
    # shape carries it, so the non-lean N+1 child hydration is pure waste here
    # (same reasoning as ``lookup/enrichment/top1.py``). It composes with
    # ``require_artwork_answer``, which lives in ``is_pg_hit`` rather than in
    # the ``pg_read`` selection, so the artwork-authoritative narrowing holds.
    main_release = await discogs_service.get_release(
        master.main_release_id, lean=True, require_artwork_answer=True
    )
    return main_release.artwork_url if main_release else None
