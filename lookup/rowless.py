"""Row-less / non-library release synthesis for the lookup pipeline.

Home of the LML#628 "A1 carry-through" kernel (``_resolve_nonlibrary_release``
and its #632 cache re-hydrate ``_rehydrate_resolved_release``), the LML#631
SONG_AS_ARTIST row-less pick (``_select_rowless_artist_release`` /
``_own_release_credit``), the LML#660 per-track credit recovery
(``_recover_track_credit``), and the synthetic ``LibraryItem(id=0)`` chokepoint
(``ROWLESS_LIBRARY_ID`` / ``NONLIBRARY_RELEASE_SURFACED_STAT_KEY`` /
``_make_rowless_item``) that every ``lml_resolve_nonlibrary_release``-gated
row-less producer routes through. Extracted verbatim from
``lookup/orchestrator.py`` (LML#725).
"""

import logging
import re

from wxyc_etl.text import to_match_form as normalize_for_comparison
from wxyc_fastapi.observability import get_cache_stats_recorder

from discogs.models import DiscogsSearchResult, ReleaseInfo
from discogs.service import DiscogsService
from entity.release_resolution_cache import (
    ReleaseResolution,
    get_cached_release_id,
    set_cached_release_id,
)
from entity.sources import PgSource
from library.models import LibraryItem
from lookup.release_resolution import ResolvedRelease, resolve_release_for_track

logger = logging.getLogger(__name__)


# LML#663: ``r.artist`` is a reliable credit only on the PG-cache path (the clean
# ``artist_name`` column). On the live Discogs path it is title-derived —
# ``DiscogsService._parse_title`` splits the search title on ``" - "`` and yields
# ``artist=""`` when the title has no such separator, packing the whole title into
# ``r.album``. A self-titled release (title is just the artist name) or one whose
# title uses a non-ASCII dash then drops a genuine own-release, so a cold-cache
# request falls through to not-found while a warm-cache one surfaces it. This
# separator set recovers the leading credit from such a packed title.
_PACKED_TITLE_SEPARATOR = re.compile(r"\s[-–—]\s")


def _own_release_credit(r: DiscogsSearchResult, token_form: str) -> tuple[str, str] | None:
    """``(artist, album_title)`` to surface when ``r`` credits the typed token, else None.

    Recovers a title-derived-empty credit the live Discogs path left in ``r.album``
    (LML#663). Still requires exact normalized-equality to ``token_form``, so V/A
    "Various" and collaborations stay excluded — the gate is re-sourced, not loosened.
    """
    clean = (r.artist or "").strip()
    if normalize_for_comparison(clean) == token_form:
        return clean, r.album or ""
    if clean:
        # A non-empty credit that simply differs — a real mismatch (coincidental
        # token hit, V/A "Various", or a collaboration). Drop without recovery.
        return None
    # Empty title-derived credit: the live path packed the whole Discogs title into
    # ``r.album``. Recover the artist from it.
    packed = (r.album or "").strip()
    if not packed:
        return None
    if normalize_for_comparison(packed) == token_form:
        # Self-titled: the title is just the artist name.
        return packed, packed
    parts = _PACKED_TITLE_SEPARATOR.split(packed, maxsplit=1)
    if len(parts) == 2 and normalize_for_comparison(parts[0]) == token_form:
        return parts[0].strip(), parts[1].strip()
    return None


def _select_rowless_artist_release(
    token: str, discogs_releases: list[DiscogsSearchResult]
) -> tuple[LibraryItem, ResolvedRelease] | None:
    """Pick the release that represents a non-library artist request (LML#631).

    Credit gate: keep only releases whose credited artist normalizes-equal to
    the typed token (the artist-only analog of ``find_best_typed_match``'s
    floor). A coincidental token→name hit is dropped, as is any release whose
    credit differs from the token — V/A compilations credited to "Various" and
    collaborations credited "<artist> & <other>" among them — so the survivors
    are releases credited to the artist alone.

    Among the survivors, take the highest Discogs ``confidence``, breaking a tie
    by **input order**. The upstream query is artist-only
    (``DiscogsSearchRequest(artist=token)`` in ``lookup_releases_by_artist``), so
    ``calculate_confidence`` scores every exact-credit match identically (~0.4,
    no album term) — confidence only separates an exact credit from a fuzzier
    diacritic variant, so the input-order tiebreak is the effective selector in
    the common all-identical-credit case. ``lookup_releases_by_artist`` returns
    the list ``service.search`` already sorted by confidence descending — a
    *stable* sort that preserves Discogs' own result order within a confidence
    tie — so first-survivor-wins surfaces the release Discogs ranked highest,
    not the oldest-cataloged pressing a ``release_id`` tiebreak would pick.
    Deterministic for a given Discogs response. True community-count ranking
    (have/want) needs a per-release ``get_release`` fetch and is deferred to
    #633.

    A non-positive ``release_id`` is dropped before selection: such an id would
    fail ``_resolve_fallback_artwork``'s ``release_id > 0`` guard downstream and
    collapse to the BS#1185 ``release_id=0`` sentinel — the silent not-found this
    path exists to kill. Discogs ids are positive, so this only closes a
    malformed-upstream path.
    """
    token_form = normalize_for_comparison(token)
    # (release, (recovered_artist, album_title)) for every release crediting the
    # typed token. ``_own_release_credit`` recovers a credit the live Discogs path
    # left title-derived-empty (LML#663), so a cold-cache request matches the
    # warm-cache (clean ``r.artist``) outcome instead of dropping to not-found.
    own = [
        (r, credit)
        for r in discogs_releases
        if r.release_id > 0 and (credit := _own_release_credit(r, token_form)) is not None
    ]
    if not own:
        return None
    # Highest confidence wins; ties break by input index (ascending), so the
    # first survivor — Discogs' highest-ranked release within a confidence tie —
    # is chosen rather than the lowest release_id (the oldest pressing). ``own``
    # preserves ``discogs_releases`` order, which ``service.search`` sorted by
    # confidence descending with a stable sort.
    best, (best_artist, best_album) = min(
        enumerate(own), key=lambda iv: (-iv[1][0].confidence, iv[0])
    )[1]
    # One recovered album title feeds both the synthetic row's title and the
    # carried release's album_title, so they can't drift.
    item = _make_rowless_item(artist=best_artist or token, title=best_album)
    resolved = ResolvedRelease(
        release_id=best.release_id,
        release_url=best.release_url,
        is_compilation=False,
        album_title=best_album,
    )
    return item, resolved


# Synthetic library-id for a row-less result — a Discogs release with no WXYC
# catalog row. Shared by the Step-3a library-miss search and the #628 A1
# carry-through: Step-3b excludes it from re-validation, fetch_artwork_for_items
# binds its carried release, and Step-6 serializes it with the "(external)"
# call-number sentinel BS already understands.
ROWLESS_LIBRARY_ID = 0

# LML#681 observability. Recorded once per flag-gated row-less emission at the
# single ``_make_rowless_item`` chokepoint that all four
# ``lml_resolve_nonlibrary_release``-gated producers route through (SONG_AS_ARTIST,
# the SONG_AS_TRACK + SWAPPED kernel, TRACK_ON_COMPILATION, the A4 cached-track
# safety net). Deliberately scoped to the flag, NOT to all ``id=0`` items: the
# LML#583 library-miss search (``_library_miss_discogs_search``) also surfaces
# ``LibraryItem(id=0)`` but builds it directly — counting it would break the
# "0 when the flag is off" property and conflate two independent features. Hence
# the ``nonlibrary_release_surfaced`` name over a generic ``rowless_surfaced``.
NONLIBRARY_RELEASE_SURFACED_STAT_KEY = "nonlibrary_release_surfaced"


def _make_rowless_item(*, artist: str, title: str) -> LibraryItem:
    """Build the synthetic ``LibraryItem(id=0)`` for a row-less carry-through.

    Carries the resolved release's artist + album title so Step-6's "(external)"
    catalog item and the Step-4b identity resolution have real names to use. The
    real Discogs identity rides alongside on the ``discogs_titles`` seam, not on
    this row.

    LML#681: this is the single chokepoint every flag-gated row-less producer
    routes through, so recording ``nonlibrary_release_surfaced`` here counts each
    flag-gated emission exactly once. ``record`` is a silent no-op when
    ``init_cache_stats`` wasn't called for the current context, so this can't
    raise on the uninitialized-stats path.
    """
    get_cache_stats_recorder().record(NONLIBRARY_RELEASE_SURFACED_STAT_KEY)
    return LibraryItem(id=ROWLESS_LIBRARY_ID, artist=artist, title=title)


async def _resolve_nonlibrary_release(
    discogs_service: DiscogsService | None,
    pg: PgSource | None,
    *,
    song: str,
    artist: str,
    album: str | None,
    is_track: bool = True,
) -> ResolvedRelease | None:
    """Resolve + validate the Discogs release a non-library track sits on (#628).

    The shared kernel behind the A1 carry-through: the three Discogs-aware
    strategies call this at their library-gate drop point, when a candidate
    release validated from the inputs but matched no WXYC catalog row. Returns
    the validated :class:`ResolvedRelease` to surface row-less, or ``None`` when
    nothing resolves.

    Three layers, in order:

    1. **#632 positive cache read** (``get_cached_release_id``), keyed on the
       **typed** ``artist`` (never ``library_artist_for`` — the library channel —
       and never a canonical-swapped probe name). Three-valued
       :class:`ReleaseResolution`: a fresh hit re-hydrates via ``get_release``; a
       fresh known miss (``was_present=True, release_id=None``) short-circuits to
       ``None`` *without* a live probe; an absent/stale entry falls through.
    2. **Bounded resolve on a cold miss** — the **uncached**
       ``resolve_release_for_track(..., also_probe_album_title=bool(album),
       max_validations=5)`` (#633). The album-title wave (#646, gated on a
       present ``album``) fires the third ``search_releases_by_album_title``
       probe so a non-library release discoverable *only* by its album title
       — the #319/#237 trio-collaboration / odd-credit shape the track-artist
       probes miss — still resolves, at parity with the in-library #604
       lazy-bind. Deliberately NOT ``resolve_release_for_track_cached``: the L1
       wrapper's key omits ``max_validations``, so a bounded ``[]`` would
       coalesce with a default ``[]`` (the #633 landmine). Cold-cache
       durability comes from the #632 PG cache here instead.
    3. **#632 cache write-back** — the resolved id, or ``None`` to record a known
       miss so the next identical add short-circuits.

    ``pg`` is best-effort: ``None`` (or a PG failure, swallowed inside the cache
    helpers) degrades to an uncached bounded resolve. ``album`` (often absent on
    the kernel paths) both fires the album-title probe (layer 2) and ranks the
    validated candidates by title; the bounded resolver returns at most one.
    """
    if discogs_service is None or not song or not artist:
        return None

    cached_positive_unhydrated = False
    if pg is not None:
        cached: ReleaseResolution = await get_cached_release_id(
            pg, artist=artist, title=song, is_track=is_track
        )
        if cached.was_present:
            if cached.release_id is None:
                # Fresh known miss — the live probe came up empty recently.
                return None
            rehydrated = await _rehydrate_resolved_release(discogs_service, cached.release_id)
            if rehydrated is not None:
                return rehydrated
            # The id is cached but its metadata is unfetchable right now; fall
            # through to a live resolve rather than fabricate a release — but
            # remember we hold a known-good id so the write-back below doesn't
            # demote it on a transient outage.
            cached_positive_unhydrated = True

    candidates = await resolve_release_for_track(
        song,
        artist,
        album,
        discogs_service,
        also_probe_album_title=bool(album),
        max_validations=5,
    )
    best = candidates[0] if candidates else None

    # Never overwrite a known-good cached id with a miss: if we already held a
    # positive entry that merely failed to re-hydrate (transient get_release /
    # validate outage) and the live resolve also came up empty, leave the
    # positive entry intact so it self-heals once the outage clears — rather
    # than pinning a 7-day miss over good data.
    if pg is not None and not (cached_positive_unhydrated and best is None):
        await set_cached_release_id(
            pg,
            artist=artist,
            title=song,
            is_track=is_track,
            release_id=best.release_id if best is not None else None,
        )

    return best


async def _rehydrate_resolved_release(
    discogs_service: DiscogsService, release_id: int
) -> ResolvedRelease | None:
    """Rebuild a :class:`ResolvedRelease` from a #632 cache-hit release_id.

    The cache stores only the id; ``get_release`` (its own by-id cache) fills in
    the title + URL. ``is_compilation`` is not needed downstream of
    ``_bind_resolved_release`` (which keys on id/url/title), so it is left
    ``False``. Returns ``None`` when the release can't be fetched **or rehydrates
    to an empty title** (a malformed/title-less but non-404 Discogs release),
    letting the caller fall through to a live resolve rather than surface a
    degenerate row-less item with ``title=""``.
    """
    try:
        metadata = await discogs_service.get_release(release_id)
    except Exception as exc:
        logger.warning("Row-less cache re-hydrate failed for release %s: %s", release_id, exc)
        return None
    if metadata is None or not metadata.release_id or not metadata.title:
        return None
    return ResolvedRelease(
        release_id=metadata.release_id,
        release_url=metadata.release_url or "",
        is_compilation=False,
        album_title=metadata.title or "",
    )


# How many candidate releases the SONG_AS_TRACK carry-through fetches while
# recovering a per-track credit before giving up and suppressing (LML#660). The
# credit is a track-level property, so the first release that carries it answers
# the question; the cap only bounds the worst case (a popular track whose comps
# all credit at release level). Mirrors the resolve path's ``max_validations=5``.
_MAX_CREDIT_RECOVERY_FETCHES = 5


async def _recover_track_credit(
    discogs_service: DiscogsService,
    raw_releases: list[ReleaseInfo],
    track: str,
) -> str | None:
    """Recover ``track``'s actual per-track credit from the surfaced releases (LML#660).

    The SONG_AS_TRACK carry-through carries no typed artist, so it can't anchor
    the row-less resolve on a query artist; falling back to the release-level
    credit yields "Various" for a V/A comp — the LML#649 hazard this replaces.
    This walks the releases the track probe already surfaced (compilations first,
    since that is where Discogs files per-track credits), fetching each — bounded
    by :data:`_MAX_CREDIT_RECOVERY_FETCHES` — until one exposes the track's own
    per-track ``artists``. Returns that credit to anchor the resolve and the #632
    cache key, or ``None`` when none is recoverable (the caller then suppresses,
    preserving the LML#649 fallback).
    """
    # Drop id-less releases before the budget cap: an unfetchable release must not
    # burn a fetch slot (the cap bounds *real* fetches), else a creditful release
    # behind id-less ones could fall outside the window. Then compilations first:
    # a V/A comp is where the per-track credit lives; an own-artist release usually
    # carries only the release-level credit. Stable within each group, so the
    # ordering is deterministic; ``bool()`` coerces ``is_compilation``'s ``None``.
    fetchable = [r for r in raw_releases if r.release_id]
    ordered = sorted(fetchable, key=lambda r: not bool(r.is_compilation))
    for release in ordered[:_MAX_CREDIT_RECOVERY_FETCHES]:
        credit = await discogs_service.get_track_credit_on_release(release.release_id, track)
        if credit:
            return credit
    return None
