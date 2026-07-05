"""Lookup orchestrator: the core search logic extracted from request-o-matic.

This module contains the perform_lookup() function that orchestrates the full
search pipeline: artist correction -> album resolution -> search strategies ->
track validation -> artwork fetch -> metadata enrichment -> context message.
"""

import asyncio
import logging
import re
from collections.abc import Coroutine
from functools import partial
from typing import Any
from urllib.parse import quote

import sentry_sdk
from wxyc_etl.text import is_compilation_artist
from wxyc_etl.text import to_match_form as normalize_for_comparison
from wxyc_fastapi.observability import (
    RequestTelemetry,
    get_cache_stats_recorder,
)

from clients.bandcamp import BandcampClient
from clients.streaming.apple_music import AppleMusicClient, AppleMusicTrackMatch
from clients.streaming.matching import (
    SCORE_MATCH_ACCEPTANCE_FLOOR,
    find_best_typed_match,
    score_match,
    score_match_track,
    strip_discogs_disambig,
    strip_track_suffix,
)
from clients.streaming.spotify import SpotifyClient
from config.settings import get_settings
from core.search import (
    execute_search_pipeline,
    get_search_type_from_state,
)
from discogs.cache_service import DiscogsCacheService
from discogs.lookup import lookup_releases_by_artist, lookup_releases_by_track
from discogs.markup_parser import (
    CachedOnlyResolver,
    DiscogsServiceResolver,
    parse,
    parse_async,
)
from discogs.models import (
    ArtistDetails,
    DiscogsSearchRequest,
    DiscogsSearchResult,
    ReleaseInfo,
    ReleaseMetadataResponse,
    ResolvedToken,
    TrackReleasesResponse,
)
from discogs.service import DiscogsService, find_track_position
from discogs.writer_roles import writer_credits_from_release
from entity.release_resolution_cache import (
    ReleaseResolution,
    get_cached_release_id,
    set_cached_release_id,
)
from entity.sources import PgSource, PgSourceProtocol
from entity.store import EntityStore, Identity
from generated.api_models import (
    LibraryCatalogItem,
    ReconciledIdentity,
    TrackMatchHint,
    TrackMatchSource,
)
from library.db import STOPWORDS, LibraryDB
from library.models import LibraryItem
from lookup.artist_resolution import (
    ResolverOutcome,
    _artist_identity_split_gate_enabled,
    _artist_pair_verified,
    _log_artist_identity_split_gate,
    _log_release_resolution_bind,
    _log_resolver_pre_pass,
    _mb_rescue_song_match_required,
    _project_mb_rescue_attrs,
    resolve_canonical_artist,
)
from lookup.concurrency import _chunked_gather
from lookup.external_search import (
    search_external_albums,
    search_external_artists,
    search_external_tracks,
)
from lookup.matching import (
    _FALLBACK_ARTIST_SIMILARITY_FLOOR,
    _FETCH_LIMIT,
    _TRAILING_PARENTHETICAL_RE,
    MAX_SEARCH_RESULTS,
    _filter_results_by_album_match,
    _filter_results_by_song_as_album_title,
    _release_matches_library_row,
    _va_series_title_match,
    album_title_acceptable,
    artist_matches_item,
    filter_results_by_artist,
    is_self_titled,
    library_artist_for,
    limit_results,
    map_library_format_to_discogs,
)
from lookup.models import LookupRequest, LookupResponse, LookupResultItem
from lookup.release_resolution import (
    ResolvedRelease,
    merge_wave_b_compilations,
    prerank_candidates_for_validation,
    rank_resolved_releases,
    resolve_release_for_track,
    resolve_release_for_track_cached,
    validate_release_for_track,
)
from lookup.strategies import build_strategies
from lookup.streaming_url_postprocess import apply_streaming_url_postprocess
from lookup.timeouts import apple_music_lookup_timeout_s
from release.musicbrainz_resolver import resolve_tracklist_via_musicbrainz
from services.parser import MessageType, ParsedRequest

logger = logging.getLogger(__name__)

COMPILATION_ARTIST_SEARCH_FORM = "Various"
"""The bare form Discogs's search endpoint accepts for compilation artists."""

COMPILATION_ARTIST_CANONICAL_FORM = "Various Artists"
"""The full form Discogs's response payloads typically carry. Kept paired with
``COMPILATION_ARTIST_SEARCH_FORM`` for the variant-scoring path in
``fetch_artwork_for_items``; any change to one MUST change the other or
``score_match("Various","Various Artists")=63.6`` will start flipping
compilations to None at the 80/80 floor (LML#478 round-2 finding).

Module-public (no underscore prefix) so ``scripts/measure_artwork_match_floor.py``
can import the same constants the runtime path uses — keeping the
measurement's compilation handling provably-aligned with production."""

_WARM_CACHE_CONCURRENCY: int = 4
"""Process-wide cap on concurrent bio cache-warm tasks.

A single warm task can fan out to many Discogs API calls (one per
unresolved `[a…]`/`[r…]`/`[m…]` ref the local cache misses). The semaphore
bounds the total in-flight API load when several flowsheet entries get
committed in quick succession. 4 is conservative — Discogs's published
rate limit is 60 RPM authenticated; warm bursts at 4 concurrent × a few
refs each still leave headroom for the read path.
"""

_warm_cache_semaphore: asyncio.Semaphore | None = None
"""Lazily-constructed (needs a running event loop). Re-bind on the first call."""


_ProbeResult = TrackReleasesResponse | tuple[TrackReleasesResponse | None, str | None]
"""Heterogeneous return type for the gathered probes in ``search_compilations_for_track``.

The two artist-scoped probes return ``TrackReleasesResponse`` directly; the
optional album-title probe returns ``tuple[TrackReleasesResponse | None,
str | None]`` (response, error_str) via its catch-and-return wrapper. The
gather sees the union; element-by-element narrowing happens via isinstance
asserts at the call site.
"""


_background_tasks: set[asyncio.Task] = set()
"""References to fire-and-forget tasks scheduled by ``enrich_artwork_results``.

`asyncio.create_task` returns weak references — without anchoring the
task somewhere strong, the GC can reap it mid-execution and the warm
silently drops. The standard pattern is a module-level set; each task
removes itself in a done_callback. See
https://docs.python.org/3/library/asyncio-task.html#asyncio.create_task
"""


def _log_album_title_fallback(
    *,
    album: str,
    n_candidates: int,
    surfaced_library_match: bool,
    error: str | None = None,
) -> None:
    """Emit telemetry for the album-title fallback (#319 / #237).

    Mirrors the ``_log_resolver_pre_pass`` shape. The fallback's firing
    population grew significantly when the gate changed from ``not raw_releases``
    to ``not results`` (see WXYC/library-metadata-lookup#322 review), so each
    fire is recorded both as an INFO log line and as a Sentry transaction
    ``data.album_title_fallback`` attribute. Sentry can answer "what
    percentage of /lookup calls trigger this fallback, and what's the
    surface rate?" without re-pulling Railway logs.

    No-op when there's no active Sentry transaction. Any SDK error is
    swallowed so observability never breaks /lookup.
    """
    payload: dict[str, Any] = {
        "album": album,
        "n_candidates": n_candidates,
        "surfaced_library_match": surfaced_library_match,
    }
    if error is not None:
        payload["error"] = error
        logger.warning("album_title_fallback %s", payload)
    else:
        logger.info("album_title_fallback %s", payload)
    try:
        transaction = sentry_sdk.get_current_scope().transaction
        if transaction is not None:
            transaction.set_data("album_title_fallback", payload)
    except Exception as e:
        logger.warning("Failed to project album_title_fallback onto Sentry transaction: %s", e)


async def resolve_albums_for_track(
    parsed: ParsedRequest,
    discogs_service: DiscogsService | None = None,
) -> tuple[list[str], bool]:
    """Resolve album names for a track if not provided.

    Searches Discogs for ALL releases containing the track, not just the first one.

    Returns:
        Tuple of (list of album names, song_not_found_flag)
    """
    album_is_missing = not parsed.album
    album_is_artist = (
        parsed.album
        and parsed.artist
        and normalize_for_comparison(parsed.album).strip()
        == normalize_for_comparison(parsed.artist).strip()
    )

    if parsed.song and parsed.artist and (album_is_missing or album_is_artist):
        if album_is_artist:
            logger.info(f"Album '{parsed.album}' appears to be artist name, looking up albums")
        try:
            releases = await lookup_releases_by_track(
                parsed.song, parsed.artist, limit=10, service=discogs_service
            )
            if releases:
                albums = []
                artist_normalized = normalize_for_comparison(parsed.artist)
                for release_artist, album in releases:
                    if normalize_for_comparison(release_artist).startswith(artist_normalized):
                        if album not in albums:
                            albums.append(album)
                if albums:
                    logger.info(f"Found {len(albums)} albums for song '{parsed.song}': {albums}")
                    return albums, False
            logger.info(f"Could not find albums for song '{parsed.song}'")
            return [], True
        except Exception as e:
            logger.warning(f"Track lookup failed: {e}")
            return [], True
    return [parsed.album] if parsed.album else [], False


async def _narrow_swapped_by_track(
    db: LibraryDB,
    artist: str,
    track: str,
    discogs_service: DiscogsService | None,
    *,
    pg: PgSource | None = None,
    allow_release_resolution_fallback: bool = True,
) -> tuple[list[LibraryItem], dict[int, list[TrackMatchHint]], dict[int, ResolvedRelease]]:
    """Narrow a swapped-interpretation artist match to the release holding ``track``.

    LML#622: once SWAPPED_INTERPRETATION identifies the artist side, the *other*
    part is cross-referenced as a track via the shared
    :func:`_match_track_releases_to_library` kernel — the same release→library
    matcher SONG_AS_TRACK uses, so the deferred tracklist validation, the
    ``_chunked_gather`` API-call budget, and the MAX_SEARCH_RESULTS early-exit
    all apply here too. ``artist=artist`` scopes the Discogs search and
    ``require_artist=artist`` re-filters the matched library rows, so the result
    stays the identified artist's own release(s) — a request for one track never
    returns that artist's whole discography, and the keyword-supplement fallback
    in ``search_releases_by_track`` can't leak another artist's release in.

    Returns ``([], {}, {})`` when nothing cross-references; the caller keeps its
    artist-filtered fallback. When the #628 carry-through fires (the identified
    artist's release containing the track is *not* shelved), the third element
    carries ``{0: ResolvedRelease}`` and the first a single ``LibraryItem(id=0)``
    — the row-less surface, which the kernel produces by bypassing
    ``require_artist`` (no library row exists to filter on that path).
    """
    return await _match_track_releases_to_library(
        db,
        discogs_service,
        track,
        artist=artist,
        source="swapped_interpretation",
        require_artist=artist,
        pg=pg,
        allow_release_resolution_fallback=allow_release_resolution_fallback,
    )


async def search_with_alternative_interpretation(
    db: LibraryDB,
    part1: str,
    part2: str,
    discogs_service: DiscogsService | None = None,
    *,
    pg: PgSource | None = None,
    allow_release_resolution_fallback: bool = True,
) -> tuple[list[LibraryItem], dict[int, list[TrackMatchHint]], dict[int, ResolvedRelease]]:
    """Try searching with both artist/title interpretations for 'X - Y' format.

    Once the artist side is identified, the *other* part is cross-referenced as a
    track against Discogs (LML#622): if it resolves to a release present in the
    library the result is narrowed to that release (carrying a ``TrackMatchHint``
    via the second tuple element); otherwise the artist-filtered result is
    returned unchanged with an empty hint map.

    The third tuple element is the ``discogs_titles`` seam: empty on every
    in-library path, and ``{0: ResolvedRelease}`` only when the #628 row-less
    carry-through surfaces a validated non-library release for the identified
    artist (``pg`` threads the #632 resolution cache into the kernel).
    """
    raw1, raw2 = await asyncio.gather(
        db.search(query=f"{part1} {part2}", limit=_FETCH_LIMIT),
        db.search(query=f"{part2} {part1}", limit=_FETCH_LIMIT),
    )
    results1 = filter_results_by_artist(raw1, part1)
    results2 = filter_results_by_artist(raw2, part2)

    # Single-artist branches narrow via track cross-reference (LML#622); the
    # kernel already caps at MAX_SEARCH_RESULTS, so the narrowed list needs no
    # further limit_results().
    if results1 and not results2:
        logger.info(f"Alternative search matched with '{part1}' as artist")
        narrowed, matched_via, titles = await _narrow_swapped_by_track(
            db,
            part1,
            part2,
            discogs_service,
            pg=pg,
            allow_release_resolution_fallback=allow_release_resolution_fallback,
        )
        return (narrowed, matched_via, titles) if narrowed else (results1, {}, {})
    elif results2 and not results1:
        logger.info(f"Alternative search matched with '{part2}' as artist")
        narrowed, matched_via, titles = await _narrow_swapped_by_track(
            db,
            part2,
            part1,
            discogs_service,
            pg=pg,
            allow_release_resolution_fallback=allow_release_resolution_fallback,
        )
        return (narrowed, matched_via, titles) if narrowed else (results2, {}, {})
    elif results1 and results2:
        # Both readings resolve to a library artist — too ambiguous to pick a
        # track side, so return the union un-narrowed (no hints). Narrowing is
        # deliberately scoped to the unambiguous single-artist branches above.
        logger.info("Alternative search matched both interpretations, combining results")
        seen_ids = set()
        combined = []
        for item in results1 + results2:
            if item.id not in seen_ids:
                combined.append(item)
                seen_ids.add(item.id)
        return limit_results(combined), {}, {}

    return [], {}, {}


async def search_song_as_artist(
    db: LibraryDB,
    song_as_artist: str,
    discogs_service: DiscogsService | None = None,
    *,
    allow_release_resolution_fallback: bool = True,
) -> tuple[list[LibraryItem], dict[int, ResolvedRelease] | None]:
    """Try searching using the parsed song title as an artist name.

    Primarily serves request-o-matic listener requests. When the typed token is
    an artist WXYC owns, the library cross-reference returns those rows. When it
    resolves on Discogs but has no ``library.db`` row, LML#631 surfaces a
    *row-less* result — ``LibraryItem(id=0)`` paired with the resolved release on
    the ``discogs_titles`` seam — so rom can post the Discogs context to Slack.
    The row-less path is gated on ``LML_RESOLVE_NONLIBRARY_RELEASE`` (shared with
    the #628 carry-through) and on the typed token normalizing-equal to the
    resolved Discogs artist name; the second tuple element carries that release
    (``None`` on the library-backed paths, which are unchanged).

    ``allow_release_resolution_fallback`` is the bulk kill switch (LML#652):
    ``False`` on /lookup/bulk suppresses the row-less surface (no row-less item,
    hence no per-row ``bind_carried`` artwork fetch downstream), parity with the
    #628 carry-through and #604's lazy fallback. Unlike those, this path does no
    resolve fan-out or cache write — the gate only suppresses the row-less pick
    over releases the artist probe already fetched.
    """
    logger.info(f"Trying song '{song_as_artist}' as artist name")

    results = await db.search(query=song_as_artist, limit=_FETCH_LIMIT)
    results = filter_results_by_artist(results, song_as_artist)
    if results:
        logger.info(f"Found {len(results)} results treating '{song_as_artist}' as artist")
        return results, None

    logger.info(f"No direct matches, searching Discogs for releases by '{song_as_artist}'")
    discogs_releases = await lookup_releases_by_artist(
        song_as_artist, limit=10, service=discogs_service
    )

    if not discogs_releases:
        logger.info(f"No Discogs releases found for '{song_as_artist}'")
        return [], None

    logger.info(f"Found {len(discogs_releases)} Discogs releases for '{song_as_artist}'")

    async def search_album(album_title: str) -> list[LibraryItem]:
        if not album_title:
            return []
        album_results = await db.search(query=album_title, limit=_FETCH_LIMIT)
        return [
            item
            for item in album_results
            if artist_matches_item(item, song_as_artist) or is_compilation_artist(item.artist or "")
        ]

    all_matches = await asyncio.gather(
        *[search_album(release.album or "") for release in discogs_releases]
    )

    seen_ids: set[int] = set()
    for matches in all_matches:
        for item in matches:
            if item.id not in seen_ids:
                results.append(item)
                seen_ids.add(item.id)
                logger.info(f"Found '{item.artist} - {item.title}' via Discogs cross-reference")
            if len(results) >= MAX_SEARCH_RESULTS:
                break
        if len(results) >= MAX_SEARCH_RESULTS:
            break

    if results:
        logger.info(
            f"Found {len(results)} results via Discogs cross-reference for '{song_as_artist}'"
        )
        return limit_results(results), None

    # LML#631 — no WXYC catalog row for this artist. If the token resolves
    # cleanly on Discogs, surface the best-representative release row-less so rom
    # can post Discogs context to Slack. Gated behind the shared non-library flag
    # and, per LML#652, the per-request bulk kill switch — /lookup/bulk passes
    # ``allow_release_resolution_fallback=False`` so a song-only backfill item
    # never surfaces a row-less result (and never pays the downstream per-row
    # ``bind_carried`` artwork fetch).
    if get_settings().lml_resolve_nonlibrary_release and allow_release_resolution_fallback:
        rowless = _select_rowless_artist_release(song_as_artist, discogs_releases)
        if rowless is not None:
            item, resolved = rowless
            logger.info(
                f"Surfacing row-less Discogs release {resolved.release_id} for "
                f"non-library artist '{song_as_artist}'"
            )
            return [item], {ROWLESS_LIBRARY_ID: resolved}

    return [], None


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


SONG_AS_TRACK_CONFIDENCE: float = 0.85
"""Default confidence floor for SONG_AS_TRACK matches.

Pinned at the master-cap value from catalog-track-search plan §5.2. The
underlying ``search_releases_by_track`` cache path doesn't currently distinguish
release- vs master-level matches, so we conservatively report the master cap.
When LML graduates onto ``library_identity`` per cross-cache-identity (#25),
this floor is replaced with ``library_identity.confidence`` per row.
"""


ROWLESS_NO_ALBUM_CONFIDENCE: float = 0.8
"""Soft confidence for a row-less non-library release that was not album-matched
(A2, LML#629).

Applied when a row-less bind is *not* a user-confirmed album — either because the
request typed no album (song-only / artist+song), or because the binding route
never matched the release against the typed album. The latter is the cached-track
safety net (A4), which picks by track only; it stamps this on the seam so the
bind stays soft even when an album was typed. An album-ranked carry-through
(A1/#628 with a typed album) keeps the full 1.0 — the typed album shaped the
pick. Lets a consumer (request-o-matic) treat the soft results as tentative.
"""

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


async def _match_track_releases_to_library(
    db: LibraryDB,
    discogs_service: DiscogsService | None,
    track: str | None,
    *,
    artist: str | None,
    source: str,
    require_artist: str | None = None,
    album: str | None = None,
    pg: PgSource | None = None,
    allow_release_resolution_fallback: bool = True,
) -> tuple[list[LibraryItem], dict[int, list[TrackMatchHint]], dict[int, ResolvedRelease]]:
    """Find Discogs releases containing ``track`` and match them back to the library.

    The shared kernel behind two strategies:

    - **SONG_AS_TRACK** (``artist=None``, ``require_artist=None``) — song-only:
      any artist's release carrying the track is a valid hit, including VA
      compilations (the ``Various <album>`` re-query + the compilation arm of
      :func:`_release_matches_library_row`).
    - **SWAPPED_INTERPRETATION** (``artist=<identified>``,
      ``require_artist=<identified>``) — the query already resolved one side to
      an artist, so the result must stay scoped to *that* artist's own releases.
      ``require_artist`` re-filters surviving rows to the identified artist,
      which closes the precision hole where ``search_releases_by_track``'s
      under-3-results keyword supplement (``discogs/service.py``) returns
      other-artists' releases. VA-filed rows are out of scope on this path by
      design — SONG_AS_TRACK is the compilation path.

    Mechanics shared by both: find Discogs releases carrying the track,
    fuzzy-match each to the WXYC library, defer per-release tracklist validation
    until after a library hit so we only pay the API cost for rows we'd actually
    surface, and bound the per-request validate fan-out through
    :func:`_chunked_gather` with a ``MAX_SEARCH_RESULTS`` early-exit (LML#536).
    Each surviving row carries a ``TrackMatchHint``; ``source`` labels the
    validation breadcrumb (LML#344) and the log lines.

    **A1 carry-through (LML#628):** when the library walk surfaces *no* row and
    ``lml_resolve_nonlibrary_release`` is on, the kernel resolves + validates the
    release the track sits on (via :func:`_resolve_nonlibrary_release`, bounded +
    #632-cached) and emits a single **row-less** ``LibraryItem(id=0)`` carrying
    that :class:`ResolvedRelease` on the third tuple element (the
    ``discogs_titles`` seam ``fetch_artwork_for_items`` binds). This **bypasses
    the ``require_artist`` library re-filter** — there is no library row to
    filter, and the artist is already settled by the typed-artist
    ``search_releases_by_track`` probe + the per-track ``validate_track_on_release``
    credit check. The #632 cache is keyed on the typed anchor artist (the kernel's
    ``artist`` for SWAPPED; the surfaced release's own credit for SONG_AS_TRACK,
    which has no typed artist). **LML#649:** when that SONG_AS_TRACK fallback
    credit is a compilation marker ("Various" for a V/A comp), it is not a usable
    per-track artist — the carry-through is suppressed entirely rather than
    resolved/validated/cached under it (which would validate only against the
    release-level "Various" credit and collide same-titled tracks across comps on
    the ``("various", <title>, True)`` cache key).

    Returns:
        Tuple of (library_items, matched_via_by_id, discogs_titles).
        matched_via_by_id maps each library row's id to one-or-more
        TrackMatchHint entries — multiple hints accumulate when the same WXYC row
        is referenced by multiple Discogs releases (different pressings, etc.).
        discogs_titles is empty on the in-library path; on the row-less
        carry-through it carries ``{0: ResolvedRelease}``. ``([], {}, {})`` when
        there's no service, no track, or nothing cross-references.
    """
    if not discogs_service or not track or not track.strip():
        return [], {}, {}

    label = source.upper()
    response = await discogs_service.search_releases_by_track(track, artist=artist)
    raw_releases = list(response.releases or [])
    if not raw_releases:
        logger.info(f"{label}: no Discogs releases for '{track}'")
        return [], {}, {}

    logger.info(
        f"{label}: {len(raw_releases)} Discogs releases for '{track}', matching against library"
    )

    seen_ids: set[int] = set()
    matched_items: list[LibraryItem] = []
    matched_via_by_id: dict[int, list[TrackMatchHint]] = {}

    async def _validate_one(release: ReleaseInfo) -> list[LibraryItem] | None:
        """Library-match + validate one Discogs release.

        Returns the list of eligible library rows for this release when the
        track is validated on it, or None when the release should be dropped
        (too-short album title, no library hits, no eligible rows, or
        validation rejected). Order-preserving dedup happens in the caller's
        post-gather walk; this helper is order-agnostic.
        """
        if not release.album or len(release.album.strip()) < 3:
            return None

        matches = await search_album_fuzzy(db, release.album)
        if not matches and release.is_compilation:
            matches = await search_album_fuzzy(db, f"Various {release.album}")
        if not matches:
            return None

        eligible = [m for m in matches if _release_matches_library_row(release, m)]
        # SWAPPED scopes to the identified artist: drop rows that don't match it,
        # guarding against the keyword-supplement leaking other-artists' releases.
        if require_artist is not None:
            eligible = [m for m in eligible if artist_matches_item(m, require_artist)]
        if not eligible:
            return None

        # Validate the track actually appears on this release before surfacing
        # — Discogs's release-search index is keyword-driven and returns hits
        # that don't always contain the track on the tracklist. Deferred until
        # after library matching so we only pay the API cost for releases we'd
        # actually return, mirroring search_compilations_for_track.
        if release.release_id:
            is_valid = await validate_release_for_track(
                discogs_service,
                release.release_id,
                track,
                release.artist,
                source=source,
            )
            if not is_valid:
                logger.debug(
                    f"{label}: skipping '{release.album}' — track not validated on release"
                )
                return None

        return eligible

    # Walk releases in input order through ``_chunked_gather``. Order
    # preservation comes from the iteration order of the generator itself
    # (chunks are dispatched and yielded in input order); accumulating
    # into ``matched_items`` / ``matched_via_by_id`` in that same order
    # guarantees a later release's hint never gets recorded before an
    # earlier release's. Breaking out of the loop after the response cap
    # is reached aborts the generator before un-fired chunks dispatch —
    # the per-request validate budget stays bounded (LML#536).
    done = False
    async for release, eligible in _chunked_gather(raw_releases, _validate_one, MAX_SEARCH_RESULTS):
        if eligible is None:
            continue

        for item in eligible:
            hint = TrackMatchHint(
                title=track,
                artist_credit=release.artist if release.is_compilation else None,
                position=None,
                confidence=SONG_AS_TRACK_CONFIDENCE,
                source=TrackMatchSource.discogs_release,
            )

            if item.id in seen_ids:
                matched_via_by_id[item.id].append(hint)
                continue

            seen_ids.add(item.id)
            matched_items.append(item)
            matched_via_by_id[item.id] = [hint]
            logger.debug(
                f"{label}: matched '{item.artist} - {item.title}' via release '{release.album}'"
            )

            if len(matched_items) >= MAX_SEARCH_RESULTS:
                done = True
                break
        if done:
            break

    if matched_items:
        return matched_items, matched_via_by_id, {}

    # A1 carry-through (LML#628): the library walk dropped every candidate (no
    # WXYC row), but a release may still resolve + validate from the inputs.
    # Surface it row-less rather than returning empty. Gated; off by default.
    # LML#652: the bulk kill switch also gates the carry-through — /lookup/bulk
    # passes ``allow_release_resolution_fallback=False`` so the backfill never
    # pays the per-row resolve + #632 cache write (parity with #604's lazy
    # fallback). Covers SONG_AS_TRACK and SWAPPED, both of which reach here.
    if get_settings().lml_resolve_nonlibrary_release and allow_release_resolution_fallback:
        # Anchor artist: the kernel's typed ``artist`` (SWAPPED's identified
        # side). SONG_AS_TRACK has none, so fall back to the surfaced release's
        # own credit — the only artist signal a song-only query carries. Either
        # way this bypasses ``require_artist`` (no library row to re-filter; the
        # per-track credit check settles the artist).
        anchor_artist = (
            artist or next((r.artist for r in raw_releases if r.artist and r.artist.strip()), "")
        ).strip()
        # LML#660: a compilation-marker anchor ("Various") — or no anchor at all —
        # is not a usable per-track artist. SONG_AS_TRACK on a V/A comp lands here
        # with the release-level "Various" credit, the exact case the carry-through
        # targets. Anchoring on it would (a) validate only against that release-
        # level credit, blind to the track's actual performer, and (b) key the
        # #632 cache on ``("various", <title>, True)``, collapsing same-titled
        # tracks across different comps onto one row. So recover the track's
        # *actual per-track credit* from the release tracklist (LML#660) and anchor
        # on that instead. When none is recoverable, suppress the carry-through
        # rather than surface an imprecise, collision-prone item — the LML#649
        # fallback, preserved. SWAPPED and TRACK_ON_COMPILATION carry a typed
        # artist and never reach this recovery.
        if not anchor_artist or is_compilation_artist(anchor_artist):
            recovered = await _recover_track_credit(discogs_service, raw_releases, track)
            if not recovered:
                logger.info(
                    f"{label}: suppressing row-less carry-through — no per-track "
                    f"credit recoverable for '{track}' (anchor would be '{anchor_artist}')"
                )
                return matched_items, matched_via_by_id, {}
            logger.info(
                f"{label}: anchoring row-less carry-through on recovered per-track "
                f"credit '{recovered}' (release-level anchor was '{anchor_artist}')"
            )
            anchor_artist = recovered
        resolved = await _resolve_nonlibrary_release(
            discogs_service,
            pg,
            song=track,
            artist=anchor_artist,
            album=album,
            is_track=True,
        )
        if resolved is not None:
            rowless = _make_rowless_item(artist=anchor_artist, title=resolved.album_title)
            logger.info(
                f"{label}: surfacing row-less Discogs release {resolved.release_id} "
                f"('{resolved.album_title}') — validated, not in library"
            )
            return [rowless], {}, {ROWLESS_LIBRARY_ID: resolved}

    return matched_items, matched_via_by_id, {}


async def search_song_as_track(
    db: LibraryDB,
    song: str | None,
    discogs_service: DiscogsService | None = None,
    *,
    pg: PgSource | None = None,
    allow_release_resolution_fallback: bool = True,
) -> tuple[list[LibraryItem], dict[int, list[TrackMatchHint]], dict[int, ResolvedRelease]]:
    """Cross-reference song against Discogs and match releases back to library.

    Catalog-track-search §4.2 / LML#301: when SONG_AS_ARTIST returns empty for a
    song-only query, treat the song as a *track* — find Discogs releases that
    contain it, then fuzzy-match those releases against the WXYC library. Each
    surviving row carries a TrackMatchHint recording the track→release linkage.
    Thin wrapper over :func:`_match_track_releases_to_library` (song-only, so
    ``artist=None``); SWAPPED_INTERPRETATION shares the same kernel.

    Args:
        db: Library database for album fuzzy search.
        song: The track title from the user query.
        discogs_service: Required. Without it, this strategy no-ops.
        pg: Discogs-cache PG handle for the #632 row-less resolution cache. When
            ``None`` the A1 carry-through (LML#628) still resolves, just uncached.
        allow_release_resolution_fallback: Bulk kill switch (LML#652). ``False``
            on /lookup/bulk suppresses the row-less carry-through entirely.

    Returns:
        Tuple of (library_items, matched_via_by_id, discogs_titles). See the
        kernel :func:`_match_track_releases_to_library` for the per-element
        contract; ``discogs_titles`` carries the row-less ``{0: ResolvedRelease}``
        only when the #628 carry-through fires.
    """
    return await _match_track_releases_to_library(
        db,
        discogs_service,
        song,
        artist=None,
        source="song_as_track",
        pg=pg,
        allow_release_resolution_fallback=allow_release_resolution_fallback,
    )


async def _library_miss_discogs_search(
    parsed: ParsedRequest,
    discogs_service: DiscogsService | None,
) -> tuple[LibraryItem, DiscogsSearchResult] | None:
    """Search Discogs for a library-miss (artist, album) pair.

    Called only when the library search returned no results AND both
    ``parsed.artist`` and ``parsed.album`` are non-empty (after strip). Uses the
    existing ``discogs_service.search()`` fallthrough seam (cache-first, API on
    miss, outage degradation) so a Discogs outage degrades gracefully.

    Applies the same 80/80-floor as ``find_best_typed_match`` (LML#400) on both
    artist AND album jointly — different from the contamination shape in LML#400
    which was artist-fallback returning any release for any album. The new risk
    shape is near-miss typed albums (typed "Anthology" → "Anthology, Vol. 1");
    see regression tests for pinned cases.

    Returns ``None`` when:
    - ``discogs_service`` is not available
    - ``parsed.artist`` or ``parsed.album`` is empty / whitespace-only
    - Discogs returns no candidates
    - No candidate clears the 80/80 floor
    - Discogs raises (outage, rate-limit exhaustion) — logged and swallowed
    """
    if discogs_service is None:
        return None
    artist = (parsed.artist or "").strip()
    album = (parsed.album or "").strip()
    if not artist or not album:
        return None

    try:
        response = await discogs_service.search(DiscogsSearchRequest(album=album, artist=artist))
    except Exception:
        logger.warning("library-miss Discogs search failed for artist=%r album=%r", artist, album)
        return None

    if not response or not response.results:
        return None

    best = find_best_typed_match(
        response.results,
        query_artist=artist,
        query_title=album,
        artist_fn=lambda r: r.artist,
        title_fn=lambda r: r.album,
    )
    if best is None:
        return None

    library_item = LibraryItem(
        id=0,
        artist=best.artist or artist,
        title=best.album or album,
    )
    return library_item, best


async def search_library_with_fallback(
    db: LibraryDB,
    parsed: ParsedRequest,
    albums: list[str],
) -> tuple[list[LibraryItem], bool]:
    """Search library with artist+album(s), falling back to artist+song or artist-only.

    Library channel of the two-channel seam (WXYC/library-metadata-lookup#626):
    every artist-keyed library operation here uses ``library_artist_for(parsed)``
    — the fuzzy correction when present, else the typed name — so a misspelled
    *library* artist still finds its row. The typed ``parsed.artist`` is reserved
    for the Discogs-facing paths elsewhere.

    Returns:
        Tuple of (library_results, song_not_found_flag)
    """
    all_results: list[LibraryItem] = []
    seen_ids: set[int] = set()
    lib_artist = library_artist_for(parsed)

    if not lib_artist and albums:
        # No artist parsed — search by album title alone
        for album in albums:
            results = await db.search(query=album, limit=_FETCH_LIMIT)
            if results:
                return results[:MAX_SEARCH_RESULTS], False
        return [], bool(parsed.song)

    if lib_artist and albums:

        async def search_one_album(album: str) -> list[LibraryItem]:
            query = f"{lib_artist} {album}"
            results = await db.search(query=query, limit=_FETCH_LIMIT)
            results = filter_results_by_artist(results, lib_artist)

            album_lower = album.lower()
            album_normalized = re.sub(r"[^\w\s]", " ", album_lower)
            album_normalized = " ".join(album_normalized.split())
            album_words = {w for w in album_normalized.split() if len(w) > 2 and w not in STOPWORDS}
            album_is_artist = lib_artist and normalize_for_comparison(
                album
            ) == normalize_for_comparison(lib_artist)

            filtered_results = []
            for item in results:
                if album_is_artist and is_self_titled(item.title or ""):
                    filtered_results.append(item)
                    continue

                item_title_lower = (item.title or "").lower()
                item_normalized = re.sub(r"[^\w\s]", " ", item_title_lower)
                item_normalized = " ".join(item_normalized.split())
                item_words = {
                    w for w in item_normalized.split() if len(w) > 2 and w not in STOPWORDS
                }
                common_words = album_words & item_words
                if len(item_words) <= 2:
                    if album_normalized.startswith(item_normalized):
                        filtered_results.append(item)
                elif len(common_words) >= 2:
                    filtered_results.append(item)
            return filtered_results

        album_results = await asyncio.gather(*[search_one_album(a) for a in albums])

        for results in album_results:
            for item in results:
                if item.id not in seen_ids:
                    seen_ids.add(item.id)
                    all_results.append(item)

        if all_results:
            primary_album_lower = albums[0].lower()
            song_lower = (parsed.song or "").lower()

            # When the request specifies a song, prefer a candidate whose title
            # matches the song name (the title-album beats a same-artist
            # compilation that also contains the track). albums[0] is whatever
            # the upstream Discogs track-lookup returned first, which is
            # non-deterministic when several releases tie on track-title
            # similarity in the PG cache; the song key forces a deterministic,
            # semantically correct order. albums[0] is kept as a secondary
            # tiebreak so album-only requests (parsed.song unset) preserve the
            # existing primary-album order.
            def sort_key(r: LibraryItem) -> tuple[bool, bool]:
                title_lower = (r.title or "").lower()
                return (
                    bool(song_lower) and song_lower in title_lower,
                    primary_album_lower in title_lower,
                )

            all_results.sort(key=sort_key, reverse=True)
            return all_results, False

        # When Discogs found albums but none matched the library, fall through to
        # artist+song and artist-only search.  filter_results_by_track_validation()
        # (called by perform_lookup after the search pipeline) validates fallback
        # results against Discogs tracklists to prevent false positives.
        logger.info(
            f"Discogs found albums {albums} but none matched in library; "
            "falling through to artist search"
        )

    if lib_artist and parsed.song:
        query = f"{lib_artist} {parsed.song}"
        results = await db.search(query=query, limit=_FETCH_LIMIT)
        results = filter_results_by_artist(results, lib_artist)
        results = _filter_results_by_album_match(results, parsed.album)

        if results:
            song_lower = parsed.song.lower()
            results.sort(
                key=lambda r: song_lower in (r.title or "").lower(),
                reverse=True,
            )
            return results, True

    if not all_results and lib_artist:
        logger.info(f"No results for albums {albums}, trying artist only: '{lib_artist}'")
        results = await db.search(query=lib_artist, limit=_FETCH_LIMIT)
        results = filter_results_by_artist(results, lib_artist)
        results = _filter_results_by_album_match(results, parsed.album)
        if results:
            return results, True

    return all_results, bool(parsed.song)


async def search_compilations_for_track(
    db: LibraryDB,
    parsed: ParsedRequest,
    discogs_service: DiscogsService | None = None,
    *,
    pg: PgSource | None = None,
    allow_release_resolution_fallback: bool = True,
) -> tuple[list[LibraryItem], dict[int, ResolvedRelease]]:
    """Search for track on compilation albums using Discogs and library keyword search.

    The second tuple element maps each surfaced library id to the
    :class:`~lookup.release_resolution.ResolvedRelease` it matched — the widened
    ``discogs_titles`` seam consumed by the artwork-binding step.

    Two-channel seam (WXYC/library-metadata-lookup#626): the Discogs probes and
    ``validate_release_for_track`` use the typed ``parsed.artist`` (via
    ``artist_for_probes``); the library-side keyword search and the two
    ``artist_matches_item`` match-backs use ``lib_artist`` (the correction when
    present, else the typed name).

    A1 carry-through (LML#628): when the whole search surfaces no library row and
    ``lml_resolve_nonlibrary_release`` is on, the track's release is resolved +
    validated (via :func:`_resolve_nonlibrary_release`, keyed on the typed
    ``parsed.artist``) and surfaced row-less as ``LibraryItem(id=0)`` +
    ``discogs_titles[0]``. ``pg`` threads the #632 cache; ``None`` resolves
    uncached. ``allow_release_resolution_fallback`` is the bulk kill switch
    (LML#652): ``False`` on /lookup/bulk suppresses that carry-through entirely
    (no resolve, no cache write, no row-less item).
    """
    if not parsed.song or not parsed.artist:
        return [], {}

    logger.info(f"Searching for '{parsed.song}' on other releases (compilations, etc.)")

    results = []
    seen_ids = set()
    discogs_titles: dict[int, ResolvedRelease] = {}
    lib_artist = library_artist_for(parsed)

    # Carried-release ranking (LML#604 deferred finding #2). When the flag is on,
    # a library row matched by multiple releases binds the title-best one — the
    # same ranking the lazy fallback applies — so both binding paths agree. When
    # off, first-seen (Wave A / library-match order) wins, preserving pre-PR2
    # behavior. The rank target is the matched library row's own title.
    rank_carried = get_settings().lml_resolve_compilation_release

    def _record_resolved(match: LibraryItem, resolved: ResolvedRelease) -> None:
        existing = discogs_titles.get(match.id)
        if existing is None:
            discogs_titles[match.id] = resolved
        elif rank_carried:
            discogs_titles[match.id] = rank_resolved_releases([existing, resolved], match.title)[0]

    keyword_matches = []
    try:
        artist_words = re.sub(r"[^\w\s]", " ", lib_artist.lower()).split() if lib_artist else []
        song_words = re.sub(r"[^\w\s]", " ", parsed.song.lower()).split() if parsed.song else []

        sig_artist = [w for w in artist_words if len(w) > 3 and w not in STOPWORDS]
        sig_song = [w for w in song_words if len(w) > 3 and w not in STOPWORDS]

        query_words = sig_artist[:2] + sig_song[:2]

        if query_words:
            keyword_query = " ".join(query_words)
            logger.info(f"Trying direct keyword search: '{keyword_query}'")
            keyword_results = await db.search(query=keyword_query, limit=_FETCH_LIMIT)

            if keyword_results:
                filtered_results = []
                for item in keyword_results:
                    if lib_artist and artist_matches_item(item, lib_artist):
                        filtered_results.append(item)
                    elif is_compilation_artist(item.artist or ""):
                        filtered_results.append(item)

                if filtered_results:
                    logger.info(
                        f"Found {len(filtered_results)} matches via keyword search "
                        f"(after artist filter)"
                    )
                    keyword_matches = filtered_results
    except Exception as e:
        logger.warning(f"Keyword search failed: {e}")
        keyword_matches = []

    discogs_found_releases = False

    try:
        raw_lower = parsed.raw_message.lower()
        song_search = parsed.song

        remix_match = re.search(r"\((.*?(?:remix|mix|version|edit).*?)\)", raw_lower, re.IGNORECASE)
        if remix_match and parsed.song.lower() in raw_lower:
            song_search = f"{parsed.song} ({remix_match.group(1)})"
            logger.info(f"Using full track name with version info: '{song_search}'")

        # Resolver pre-pass: when the inbound artist string trigram-matches a
        # canonical Discogs name with confidence >= the floor, use the canonical
        # form for both Discogs probes. Gated by ``lml_resolve_artist_canonical``
        # — the flag controls *both* the trigram lookup and the swap, so the
        # default-off path pays no PG cost. See WXYC/library-metadata-lookup#343
        # Option 2.
        enforce_swap = bool(get_settings().lml_resolve_artist_canonical)
        if enforce_swap:
            cache_service = getattr(discogs_service, "cache_service", None)
            outcome = await resolve_canonical_artist(parsed.artist, cache_service=cache_service)
            actual_swap = outcome.swapped
            _log_resolver_pre_pass(outcome, actual_swap=actual_swap)
            artist_for_probes = outcome.canonical if actual_swap else parsed.artist
        else:
            outcome = ResolverOutcome(
                original=parsed.artist or "",
                canonical=parsed.artist or "",
                score=0.0,
                swapped=False,
            )
            artist_for_probes = parsed.artist

        # Get raw releases from Discogs without per-release validation.
        # We search the library first and only validate releases that match,
        # avoiding expensive API calls for releases not in our catalog.
        raw_releases: list[ReleaseInfo] = []
        # The album-title fallback's preconditions (`parsed.album` set, no
        # resolver swap) are decidable here, so its probe joins the same
        # `asyncio.gather` as the two artist-scoped probes (WXYC/library-
        # metadata-lookup#339). Cold-cache wall time drops from A+B+C to
        # max(A,B,C); the cost is one speculative API call when Wave A
        # already succeeds — that call warms the cache.
        album_fallback_should_fire = (
            discogs_service is not None and bool(parsed.album) and not outcome.swapped
        )
        album_fallback_response: TrackReleasesResponse | None = None
        album_fallback_error: str | None = None

        async def _album_title_probe_safe() -> tuple[TrackReleasesResponse | None, str | None]:
            """Catch-and-return wrapper so a Discogs failure on the album-title
            probe doesn't take down the artist-scoped probes in the same gather.
            Mirrors the existing fallback's try/except, which logs via
            `_log_album_title_fallback(..., error=str(e))`."""
            assert discogs_service is not None  # narrow for type-checker
            assert parsed.album is not None
            try:
                resp = await discogs_service.search_releases_by_album_title(parsed.album)
                return resp, None
            except Exception as exc:
                return None, str(exc)

        if discogs_service:
            # Two artist-scoped probes return TrackReleasesResponse; the optional
            # album-title probe returns tuple[TrackReleasesResponse | None, str | None].
            # _ProbeResult (module-scope alias) captures the heterogeneous shape.
            probes: list[Coroutine[Any, Any, _ProbeResult]] = [
                discogs_service.search_releases_by_track(song_search, artist_for_probes),
                discogs_service.search_releases_by_track(
                    song_search, artist_for_probes, artist_as_keyword=True
                ),
            ]
            if album_fallback_should_fire:
                probes.append(_album_title_probe_safe())

            gathered = await asyncio.gather(*probes)
            # gathered[0] / gathered[1] are TrackReleasesResponse by construction
            # (the order matches the `probes` list above); narrow via assert.
            assert isinstance(gathered[0], TrackReleasesResponse)
            assert isinstance(gathered[1], TrackReleasesResponse)
            response, va_response = gathered[0], gathered[1]
            if album_fallback_should_fire:
                probe_tuple = gathered[2]
                assert isinstance(probe_tuple, tuple)
                album_fallback_response, album_fallback_error = probe_tuple

            # Merge Wave B's V/A compilations into Wave A unless Wave A already
            # surfaced a true-V/A hit (WXYC#527). The merge logic is owned by
            # ``lookup.release_resolution.merge_wave_b_compilations`` so the
            # release-resolution module and this strategy can't drift apart.
            raw_releases = merge_wave_b_compilations(
                list(response.releases or []), list(va_response.releases or [])
            )
        else:
            # No injected service — fall back to lookup helper (validates all)
            tuples = await lookup_releases_by_track(song_search, parsed.artist, service=None)
            raw_releases = [
                ReleaseInfo(
                    album=album,
                    artist=artist,
                    release_id=0,
                    release_url="",
                    is_compilation=is_compilation_artist(artist),
                )
                for artist, album in tuples
            ]

        logger.info(f"Found {len(raw_releases)} releases with '{song_search}' on Discogs")
        discogs_found_releases = len(raw_releases) > 0

        async def process_release(
            release_info: ReleaseInfo,
            *,
            skip_self_named_album: bool = True,
            skip_artist_match_filter: bool = False,
        ) -> list[tuple[LibraryItem, ResolvedRelease]]:
            """Process one Discogs release: library search, filter, validate.

            ``skip_self_named_album`` defaults True to preserve existing behavior
            for callers that arrived via artist-scoped probes. The album-title
            fallback (WXYC/library-metadata-lookup#319) passes False because the
            trio-collaboration case has ``album == artist`` by design.

            ``skip_artist_match_filter`` defaults False. When True, the
            library-side ``artist_matches_item`` prefix-filter is skipped and
            artist gating is deferred to ``validate_track_on_release``'s fuzzy
            ``rapidfuzz.token_set_ratio`` (PR #236). The fallback path passes
            True because the library row's artist string for trio/collaborative
            releases (e.g. ``"Orcutt, Bill / Shelley, Chris / Miller, Mette"``)
            won't prefix-match a user-typed ``"Orcutt Shelley Miller"``, but the
            fuzzy validator on the Discogs side does accept it.
            """
            release_album = release_info.album

            if skip_self_named_album:
                album_clean = release_album.lower().replace('"', "").replace("'", "").strip()
                if (
                    parsed.artist
                    and album_clean
                    == parsed.artist.lower().replace('"', "").replace("'", "").strip()
                ):
                    logger.debug(
                        f"Skipping '{release_album}' - appears to be artist name, not album"
                    )
                    return []

            if len(release_album.strip()) < 3:
                return []

            matches = await search_album_fuzzy(db, release_album)

            # If album-only search failed for a compilation, retry with "Various"
            # to help FTS5 match entries stored as "Various Artists - ..."
            if not matches and release_info.is_compilation:
                matches = await search_album_fuzzy(db, f"Various {release_album}")

            if matches and lib_artist and not skip_artist_match_filter:
                from rapidfuzz import fuzz as _fuzz

                filtered_matches = []
                discogs_is_compilation = release_info.is_compilation
                release_album_lower = release_album.lower()

                for match in matches:
                    if artist_matches_item(match, lib_artist):
                        filtered_matches.append(match)
                    elif discogs_is_compilation and is_compilation_artist(match.artist or ""):
                        title_score = _fuzz.ratio(release_album_lower, (match.title or "").lower())
                        if title_score >= 80:
                            filtered_matches.append(match)
                        else:
                            logger.debug(
                                f"Rejected '{match.title}' for '{release_album}' "
                                f"(title_score={title_score:.0f})"
                            )
                matches = filtered_matches
            elif matches and lib_artist and skip_artist_match_filter:
                # The strict prefix filter above is deferred on this path, but
                # ``validate_track_on_release`` only vets the Discogs release —
                # not the surfaced library row. Apply a lenient library-side
                # artist backstop so an album-title fuzz collision can't bind a
                # wrong-artist row (see ``_FALLBACK_ARTIST_SIMILARITY_FLOOR``).
                from rapidfuzz import fuzz as _fuzz

                lib_artist_norm = normalize_for_comparison(lib_artist)
                discogs_is_compilation = release_info.is_compilation
                release_album_lower = release_album.lower()

                def _fallback_row_acceptable(match: LibraryItem) -> bool:
                    # Legitimate Various-Artists compilation rows share no artist
                    # tokens with a typed solo/track artist, so the fuzzy floor
                    # would wrongly drop them. Keep them on a title match instead,
                    # mirroring the strict branch's compilation carve-out above.
                    # ``is_compilation_artist`` is False for coincidental
                    # wrong-artist collisions (e.g. "Galaxy 2 Galaxy"), so this
                    # does not re-open the #717 bug.
                    if discogs_is_compilation and is_compilation_artist(match.artist or ""):
                        title_score = _fuzz.ratio(release_album_lower, (match.title or "").lower())
                        return title_score >= 80
                    return (
                        max(
                            _fuzz.token_set_ratio(
                                lib_artist_norm, normalize_for_comparison(match.artist or "")
                            ),
                            _fuzz.token_set_ratio(
                                lib_artist_norm,
                                normalize_for_comparison(match.alternate_artist_name or ""),
                            ),
                        )
                        >= _FALLBACK_ARTIST_SIMILARITY_FLOOR
                    )

                matches = [match for match in matches if _fallback_row_acceptable(match)]

            if not matches:
                return []

            # Validate that the track actually exists on this release.
            # Deferred until after library matching so we only validate
            # releases we might actually return — saving API calls (LML#536).
            if discogs_service and release_info.release_id and parsed.artist:
                is_valid = await validate_release_for_track(
                    discogs_service,
                    release_info.release_id,
                    song_search,
                    parsed.artist,
                    source="compilation_inline",
                )
                if not is_valid:
                    logger.info(
                        f"Skipping '{release_album}' - track/artist not validated on release"
                    )
                    return []

            logger.info(
                f"Found '{parsed.song}' in library on '{matches[0].title}' "
                f"(matched from Discogs: '{release_album}')"
            )
            resolved = ResolvedRelease(
                release_id=release_info.release_id,
                release_url=release_info.release_url,
                is_compilation=bool(release_info.is_compilation),
                album_title=release_album,
            )
            return [(match, resolved) for match in matches]

        # Chunked dispatch so the per-request validate budget stays bounded
        # once the response cap is hit. Without chunking, asyncio.gather
        # would schedule (and pay for) every candidate's
        # ``validate_track_on_release`` even after MAX_SEARCH_RESULTS
        # matches had already landed — see LML#536 and the matching
        # treatment in ``search_song_as_track``.
        async for _, release_matches in _chunked_gather(
            raw_releases, process_release, MAX_SEARCH_RESULTS
        ):
            for match, resolved in release_matches:
                if match.id not in seen_ids:
                    results.append(match)
                    seen_ids.add(match.id)
                _record_resolved(match, resolved)
            if len(results) >= MAX_SEARCH_RESULTS:
                break

        # Album-title fallback (#319 + #237): when the artist-scoped probes
        # produced no library results AND the request supplied an album AND
        # the resolver pre-pass did not produce a high-confidence canonical,
        # process the title-only Discogs candidates with the trio-aware
        # kwargs (no self-named-album guard, no library-side artist filter —
        # defer artist gating to validate_track_on_release).
        #
        # The probe itself was fired speculatively in the same `asyncio.gather`
        # as the artist-scoped probes above (#339). When it returned results
        # we already paid the Discogs API cost; here we just decide whether
        # to *consume* those candidates, gated on `not results` from Wave A.
        #
        # Trade-off: the gate is ``not results`` rather than
        # ``len(results) < MAX_SEARCH_RESULTS``. A partial initial pass (e.g.
        # one valid match) suppresses the fallback entirely, even when more
        # spots are available. Conservative-by-design — the fallback's
        # purpose is to backfill the zero-results case, not augment partial
        # ones. Revisit if measurements show partial-result requests would
        # benefit from supplementation.
        pre_fallback_results_count = len(results)
        # `album_fallback_should_fire` implies `parsed.album is not None`
        # (see the precondition where the flag is set); each branch below
        # re-asserts to narrow the type locally for the type-checker.
        if album_fallback_should_fire and album_fallback_error is not None:
            # The probe ran but raised; mirror the pre-#339 behavior of
            # logging via `_log_album_title_fallback(..., error=...)`.
            assert parsed.album is not None
            _log_album_title_fallback(
                album=parsed.album,
                n_candidates=0,
                surfaced_library_match=False,
                error=album_fallback_error,
            )
        elif album_fallback_should_fire and not results and album_fallback_response is not None:
            assert parsed.album is not None
            fallback_releases = list(album_fallback_response.releases or [])
            if fallback_releases:
                logger.info(
                    f"Album-title fallback returned {len(fallback_releases)} candidates "
                    f"for '{parsed.album}'"
                )

            fallback_worker = partial(
                process_release,
                skip_self_named_album=False,
                skip_artist_match_filter=True,
            )
            async for _, release_matches in _chunked_gather(
                fallback_releases, fallback_worker, MAX_SEARCH_RESULTS
            ):
                for match, resolved in release_matches:
                    if match.id not in seen_ids:
                        results.append(match)
                        seen_ids.add(match.id)
                    _record_resolved(match, resolved)
                if len(results) >= MAX_SEARCH_RESULTS:
                    break
            if fallback_releases:
                discogs_found_releases = True
            _log_album_title_fallback(
                album=parsed.album,
                n_candidates=len(fallback_releases),
                surfaced_library_match=len(results) > pre_fallback_results_count,
            )
    except Exception as e:
        logger.warning(f"Failed to search for track on other releases: {e}")

    if not results and keyword_matches and not discogs_found_releases:
        logger.info("Discogs search found nothing, using keyword matches as fallback")
        for item in keyword_matches[:1]:
            if item.id not in seen_ids:
                results.append(item)
                seen_ids.add(item.id)

    if results and parsed.song:
        song_lower = parsed.song.lower()
        results.sort(
            key=lambda r: song_lower in (r.title or "").lower(),
            reverse=True,
        )

    # A1 carry-through (LML#628): the artist+keyword+album waves dropped every
    # candidate on the library gate, but the track may still resolve + validate
    # off a non-library release. Surface it row-less rather than empty. Keyed on
    # the typed ``parsed.artist`` (NOT ``lib_artist`` / the canonical-swapped
    # ``artist_for_probes``), consistent with the #626 two-channel decision and
    # the #632 cache contract. Gated; off by default. LML#652: also gated on the
    # bulk kill switch — /lookup/bulk passes ``allow_release_resolution_fallback
    # =False`` so the backfill never pays the per-row resolve + #632 cache write
    # (parity with #604's lazy fallback).
    if (
        not results
        and get_settings().lml_resolve_nonlibrary_release
        and allow_release_resolution_fallback
        and parsed.song
    ):
        # Distinct name from the #604 ``resolved`` bound earlier in this function
        # (a non-optional ``ResolvedRelease``) — the helper returns an Optional,
        # and reusing the name trips a mypy assignment-type collision.
        resolved_nonlibrary = await _resolve_nonlibrary_release(
            discogs_service,
            pg,
            song=parsed.song,
            artist=parsed.artist or "",
            album=parsed.album,
            is_track=True,
        )
        if resolved_nonlibrary is not None:
            rowless = _make_rowless_item(
                artist=parsed.artist or "", title=resolved_nonlibrary.album_title
            )
            discogs_titles[ROWLESS_LIBRARY_ID] = resolved_nonlibrary
            logger.info(
                f"TRACK_ON_COMPILATION: surfacing row-less Discogs release "
                f"{resolved_nonlibrary.release_id} ('{resolved_nonlibrary.album_title}') "
                f"— validated, not in library"
            )
            return [rowless], discogs_titles

    return limit_results(results), discogs_titles


async def search_album_fuzzy(db: LibraryDB, album_title: str) -> list[LibraryItem]:
    """Search for album with fuzzy keyword matching."""
    from rapidfuzz import fuzz

    # Exact-title pre-pass. The FTS5 path below truncates to
    # ``MAX_SEARCH_RESULTS`` rows in implementation-defined (rowid) order, which
    # silently drops literal-title matches whose library row was added later in
    # the catalog. Discogs typically hands us the album title verbatim, so a
    # literal hit is the most reliable signal — surface it before falling
    # through to fuzzy scoring.
    exact = await db.exact_title(album_title)
    if exact:
        return exact

    async def _search_and_filter(query: str) -> list[LibraryItem]:
        raw = await db.search(query=query, limit=MAX_SEARCH_RESULTS)
        if not raw:
            return []
        q_lower = query.lower()
        return [
            r
            for r in raw
            if _va_series_title_match(q_lower, r)
            or album_title_acceptable(q_lower, (r.title or "").lower())
        ]

    # First try the un-stripped query. If it produces no surviving candidates,
    # retry with all trailing parenthetical groups stripped — this rescues
    # the over-specific Discogs subtitle case (WXYC#531). The full Discogs
    # title fails the FTS5 search layer in three distinct ways depending on
    # its shape, all of which strip-retry repairs:
    #   1. FTS5 throws on special characters in the subtitle (``&`` is the
    #      common offender), ``db.search`` swallows the error and falls
    #      through to the LIKE / fuzzy fallback layers.
    #   2. ``_fallback_like_search`` uses implicit-AND across title+artist,
    #      requiring every significant word to appear — terse library rows
    #      (``Disco Not Disco, vol. 1``) lack the subtitle's words and
    #      return empty.
    #   3. ``_fuzzy_search`` candidate-trawls on the longest word's 3-char
    #      prefix and returns tangentially related rows that the post-filter
    #      then drops.
    # In all three shapes the actual library row goes unseen until the
    # paren-strip retry produces a query FTS5 can answer cleanly.
    results = await _search_and_filter(album_title)

    if not results:
        stripped = _TRAILING_PARENTHETICAL_RE.sub("", album_title).strip()
        if stripped and stripped != album_title:
            results = await _search_and_filter(stripped)

    if not results:
        words = re.sub(r"[^\w\s]", " ", album_title.lower()).split()
        significant_words = [w for w in words if len(w) > 3 and w not in STOPWORDS]

        if significant_words:
            album_lower = album_title.lower()
            # Require roughly half the keywords to match — lenient enough for
            # abbreviated titles (e.g., "Punk 82-88" vs "Post Punk 1982 - 1988")
            # but strict enough to reject unrelated albums that share a few words
            # (e.g., "20th Anniversary Concert" vs "Trax Records 20th Anniversary Edition").
            # The similarity and album_title_acceptable checks provide additional gating.
            min_keywords = max(2, (len(significant_words) + 1) // 2)

            # Try progressively shorter queries to handle word mismatches between
            # Discogs and library titles (e.g., "Edition" vs "Collection").
            # Filter inside the loop so false positives don't block shorter queries.
            max_words = min(4, len(significant_words))
            for n_words in range(max_words, 1, -1):
                fuzzy_query = " ".join(significant_words[:n_words])
                logger.info(
                    f"Exact match failed for '{album_title}', trying fuzzy: '{fuzzy_query}'"
                )
                raw_results = await db.search(query=fuzzy_query, limit=MAX_SEARCH_RESULTS)
                if not raw_results:
                    continue

                filtered_results = []
                for result in raw_results:
                    result_title_lower = (result.title or "").lower()

                    keyword_matches = sum(
                        1 for word in significant_words if word in result_title_lower
                    )
                    similarity = fuzz.token_set_ratio(album_lower, result_title_lower)
                    title_ok = album_title_acceptable(album_lower, result_title_lower)

                    if keyword_matches >= min_keywords and similarity >= 60 and title_ok:
                        logger.debug(
                            f"Album match: '{result.title}' "
                            f"(keywords={keyword_matches}/{len(significant_words)}, "
                            f"similarity={similarity})"
                        )
                        filtered_results.append(result)
                    else:
                        logger.debug(
                            f"Album rejected: '{result.title}' "
                            f"(keywords={keyword_matches}/{len(significant_words)}, "
                            f"similarity={similarity})"
                        )

                if filtered_results:
                    results = filtered_results
                    break

    return results


async def filter_results_by_track_validation(
    results: list[LibraryItem],
    song: str | None,
    artist: str | None,
    discogs_service: DiscogsService | None,
) -> list[LibraryItem] | None:
    """Filter fallback results to only albums that contain the requested track.

    Returns:
        Filtered list, or None if validation isn't possible.
    """
    if not discogs_service or not song or not artist or not results:
        return None

    async def validate_one(item: LibraryItem) -> LibraryItem | None:
        try:
            # Self-titled albums stored as "S/t" should use the artist name
            album_for_search = item.artist if is_self_titled(item.title or "") else item.title
            response = await discogs_service.search(
                DiscogsSearchRequest(album=album_for_search, artist=artist)
            )
            if not response.results:
                return None

            best_result = response.results[0]
            if best_result.release_id:
                # Verify the Discogs result is actually the same album, not a
                # different release that shares words with the library title.
                # e.g., searching for "808 State" might return "The Best Of
                # 808 State: Blueprint" — a different album entirely.
                discogs_album = (best_result.album or "").lower()
                library_title = (item.title or "").lower()
                if not album_title_acceptable(library_title, discogs_album):
                    logger.debug(
                        f"Track validation: Discogs returned '{best_result.album}' "
                        f"for library item '{item.title}' — album mismatch, skipping"
                    )
                    return None

                is_valid = await validate_release_for_track(
                    discogs_service, best_result.release_id, song, artist, source="step_3b"
                )
                if is_valid:
                    logger.info(
                        f"Track validation: '{song}' confirmed on '{item.title}' "
                        f"(release {best_result.release_id})"
                    )
                    return item
        except Exception as e:
            logger.warning(f"Track validation failed for '{item.title}': {e}")
        return None

    validation_results = await asyncio.gather(*[validate_one(item) for item in results])
    validated = [r for r in validation_results if r is not None]

    if validated:
        logger.info(
            f"Track validation filtered {len(results)} albums to {len(validated)} "
            f"containing '{song}'"
        )
        return validated

    logger.info(f"Track validation could not confirm '{song}' on any album")
    return None


async def find_library_albums_with_cached_track(
    db: LibraryDB,
    song: str | None,
    artist: str | None,
    discogs_service: DiscogsService | None,
    limit: int = MAX_SEARCH_RESULTS,
    *,
    match_artist: str | None = None,
    allow_release_resolution_fallback: bool = True,
) -> tuple[list[LibraryItem], dict[int, ResolvedRelease]]:
    """Find WXYC library albums whose Discogs cache entry lists ``song`` by ``artist``.

    Used as a safety net after ``filter_results_by_track_validation`` fails to
    confirm any artist-fallback candidate. The PG cache holds full Discogs
    tracklist data with trigram-indexed track titles, so a single lookup can
    answer "which releases by this artist contain this track?" in milliseconds —
    even when the upstream ``resolve_albums_for_track`` / API path missed it.

    Two-channel artist (LML#626): the Discogs-cache probe keys on the typed
    ``artist`` (the cache holds Discogs-credited names), while the library
    match-back keys on ``match_artist`` when supplied — the library-corrected
    name — so a misspelled library artist still promotes its catalog row.
    ``match_artist`` defaults to ``artist`` to preserve single-channel behavior
    for callers that don't distinguish the two.

    Returns ``(items, discogs_titles)``. On the in-library path ``items`` are the
    promoted WXYC rows and ``discogs_titles`` is empty. **A4 carry-through
    (LML#629):** when the cache confirms the track on a release but *no* library
    row artist-matches — and ``lml_resolve_nonlibrary_release`` is on — a single
    **row-less** ``LibraryItem(id=0)`` is returned with the resolved release on
    the ``{0: ResolvedRelease}`` seam, reusing #628's carry-through so the
    ``release_id`` (hence ``discogs_url``) still surfaces instead of being
    dropped for want of a matching catalog row. This A4 row-less surface is the
    *fifth* row-less producer (LML#652): it honors the per-request bulk kill
    switch ``allow_release_resolution_fallback`` exactly as the four strategy
    producers do — ``False`` on /lookup/bulk suppresses it (the in-library
    promotion above is unaffected, returning before the gate).

    Cache-only by design: skips any API fallback path. Returns ``([], {})``
    cleanly when the cache is unavailable, fails, or has nothing for the query —
    and, with the flag off, when nothing artist-matches (pre-#629 behavior).
    """
    if not discogs_service or not song or not artist:
        return [], {}
    match_against = match_artist or artist
    cache_service = getattr(discogs_service, "cache_service", None)
    if cache_service is None:
        return [], {}

    try:
        cached_releases = await cache_service.search_releases_by_track(
            track=song, artist=artist, limit=20
        )
    except Exception as e:
        logger.warning(f"Cache lookup for track-album promotion failed: {e}")
        return [], {}

    if not cached_releases:
        return [], {}

    matches: list[LibraryItem] = []
    seen_ids: set[int] = set()

    for release in cached_releases:
        candidate_items = await search_album_fuzzy(db, release.album)
        for item in candidate_items:
            if item.id in seen_ids:
                continue
            if not artist_matches_item(item, match_against):
                continue
            matches.append(item)
            seen_ids.add(item.id)
            if len(matches) >= limit:
                return matches, {}

    if matches:
        return matches, {}

    # A4 carry-through (LML#629): the cache confirmed the track on a release, but
    # no WXYC library row artist-matches. Rather than drop a resolvable release,
    # surface the best one row-less so its release_id (hence discogs_url) still
    # binds via #628's {0: ResolvedRelease} seam. Gated on the same flag as the
    # other carry-through sites: when off, fetch_artwork_for_items won't bind a
    # row-less item, so we preserve the pre-#629 drop. Cache rows are already
    # track-confirmed by the trigram query, so no re-validation is needed.
    #
    # Ordering reuses the shared #629 no-album rule (prefer is_compilation=False,
    # then stable release_id) instead of restating it — keeping this path and the
    # bounded-resolve path on one definition. There is no typed album to rank
    # against here (the cache keyed on track only), so confidence is soft: the
    # pick was never album-matched, and the soft value rides the seam so the bind
    # surfaces it even when the request did type an album.
    #
    # LML#652: gated on the bulk kill switch too — /lookup/bulk passes
    # ``allow_release_resolution_fallback=False`` so this A4 row-less surface (the
    # fifth row-less producer) never reaches the per-row ``bind_carried`` artwork
    # fetch on the backfill path. The in-library promotion above returns before
    # this gate, so it stays available on bulk.
    if not (get_settings().lml_resolve_nonlibrary_release and allow_release_resolution_fallback):
        return [], {}
    # Require a title as well as an id: a title-less release would surface a
    # degenerate row-less item (title=""), exactly what the sibling rehydrate
    # path (_rehydrate_resolved_release) guards against.
    ranked = prerank_candidates_for_validation(
        [r for r in cached_releases if r.release_id and r.album], None
    )
    if not ranked:
        return [], {}
    best = ranked[0]
    rowless = _make_rowless_item(artist=artist or "", title=best.album)
    resolved = ResolvedRelease(
        release_id=best.release_id,
        release_url=best.release_url or "",
        is_compilation=bool(best.is_compilation),
        album_title=best.album or "",
        confidence=ROWLESS_NO_ALBUM_CONFIDENCE,
    )
    logger.info(
        f"cached-track safety net: surfacing row-less Discogs release "
        f"{best.release_id} ('{best.album}') — track-confirmed in cache, not in library"
    )
    return [rowless], {ROWLESS_LIBRARY_ID: resolved}


async def _resolve_fallback_artwork(discogs_service: DiscogsService, release_id: int) -> str | None:
    """Try the release's own cover (images[0]), then artist image, then label image.

    Structurally invalid ids (``release_id <= 0``) short-circuit before the
    Discogs round-trip — the LML#401 synthesis pattern produces a
    ``release_id=0`` sentinel that any future caller could leak in here (see
    issue #518). Discogs release ids start at 1, so the strict gate is also a
    correctness check against malformed upstream payloads.
    """
    if release_id <= 0:
        return None
    release = await discogs_service.get_release(release_id)
    if not release:
        return None

    # The search endpoint's `cover_image` is sometimes empty for releases whose
    # release-detail `images[0].uri` is populated. Prefer that over the
    # artist/label image fallback so enrichment-worker callers (single LML
    # round-trip) recover the same cover the /proxy/metadata/album legacy
    # two-call path produces via populateReleaseMetadata.
    if release.artwork_url:
        return release.artwork_url

    if release.artist_id:
        image = await discogs_service.get_artist_image(release.artist_id)
        if image:
            logger.info(f"Using artist image fallback for release {release_id}")
            return image

    if release.label_id:
        image = await discogs_service.get_label_image(release.label_id)
        if image:
            logger.info(f"Using label image fallback for release {release_id}")
            return image

    return None


async def _bind_resolved_release(
    discogs_service: DiscogsService,
    release: ResolvedRelease,
    item: LibraryItem,
    *,
    album: str | None = None,
) -> DiscogsSearchResult:
    """Trust-and-bind an already-validated ``ResolvedRelease`` (LML#604).

    The release was validated this same request — either carried on the
    ``discogs_titles`` seam by the search strategy or just validated by
    ``resolve_release_for_track`` — so the artist-floor re-search is skipped
    entirely. Art comes from the release's own cover via
    ``_resolve_fallback_artwork``; downstream ``enrich_artwork_results`` fills
    year/label/genres via ``get_release`` exactly as for a floor-matched result.

    The floor was never a validation backstop — it is a search disambiguator,
    and validation already settled the artist — so ``confidence`` defaults to the
    maximum 1.0.

    **A2 soft confidence (LML#629):** a *row-less* (id==0) bind is softened in two
    cases, and the result takes the lower (softer) of the two signals:

    - **No typed album in the request** — the release is the best-ranked guess,
      not a user-confirmed album, so any row-less bind drops to
      ``ROWLESS_NO_ALBUM_CONFIDENCE``.
    - **The seam already carries a soft confidence** (``release.confidence``) —
      the cached-track safety net (A4) picks by track only and never
      album-matches, so it stamps the seam soft *regardless* of a typed album;
      the bind can't tell A4 from an album-ranked carry-through otherwise. An
      album-ranked carry-through (A1/#628 with a typed album) leaves the seam at
      1.0, so a typed-album request keeps the full confidence there.

    An in-library bind (id != 0) is never softened.
    """
    artwork_url = await _resolve_fallback_artwork(discogs_service, release.release_id)
    confidence = release.confidence
    if item.id == ROWLESS_LIBRARY_ID and not (album or "").strip():
        confidence = min(confidence, ROWLESS_NO_ALBUM_CONFIDENCE)
    return DiscogsSearchResult(
        release_id=release.release_id,
        release_url=release.release_url,
        album=release.album_title,
        artist=item.artist,
        artwork_url=artwork_url,
        confidence=confidence,
    )


async def fetch_artwork_for_items(
    items: list[LibraryItem],
    discogs_service: DiscogsService | None,
    discogs_titles: dict[int, ResolvedRelease] | None = None,
    *,
    song: str | None = None,
    album: str | None = None,
    allow_release_resolution_fallback: bool = True,
    found_on_compilation: bool = False,
) -> list[tuple[LibraryItem, DiscogsSearchResult | None]]:
    """Fetch artwork for multiple library items in parallel.

    ``song`` (the request-level track, when present) and the
    ``lml_resolve_compilation_release`` flag enable the LML#604 lazy
    release-resolution fallback in ``fetch_one``: when the artist-floor search
    rejects a Various-Artists compilation row, resolve and trust-bind the
    validated release instead of leaving ``discogs_url`` unbound.
    ``allow_release_resolution_fallback`` is the bulk kill switch — the
    /lookup/bulk drain passes ``False`` so the 35k-album backfill never triggers
    the per-row Discogs fan-out.

    ``album`` (the request-level typed album, when present) only gates the A2
    soft-confidence on a row-less carried bind (LML#629): a no-album query's
    row-less release binds at ``ROWLESS_NO_ALBUM_CONFIDENCE`` rather than 1.0.

    ``found_on_compilation`` (LML#684): when the result came from
    ``search_compilations_for_track`` (TRACK_ON_COMPILATION), an in-library row
    carries an already-validated ``ResolvedRelease`` on the seam. The artist-floor
    re-search systematically rejects *non*-Various-Artists trio / collaboration
    credits (the row is filed under one member, e.g. "Bill Orcutt", while Discogs
    credits the full trio), leaving the result with no artwork. When the floor
    rejects every candidate and a release is carried, that validated release is
    trust-bound for artwork — independent of ``lml_resolve_compilation_release``,
    since binding an already-carried release costs no extra Discogs fan-out
    (unlike the flag-gated lazy ``resolve_release_for_track`` fallback below).
    """
    if not discogs_service:
        return [(item, None) for item in items]

    discogs_titles = discogs_titles or {}
    # Bound to a distinct name: ``fetch_one`` rebinds a local ``album`` (the
    # per-item search title), which would shadow this request-level value.
    request_album = album
    settings = get_settings()
    resolve_compilation_release = settings.lml_resolve_compilation_release
    resolve_nonlibrary_release = settings.lml_resolve_nonlibrary_release
    resolve_artist_canonical = settings.lml_resolve_artist_canonical

    async def fetch_one(item: LibraryItem) -> DiscogsSearchResult | None:
        try:
            # The widened seam carries a ResolvedRelease; its album_title is the
            # value the seam used to carry as a bare string. Falls back to the
            # library row's own title when no release was resolved for this id.
            resolved = discogs_titles.get(item.id)

            # Carried-release trust-and-bind: the search strategy already
            # resolved and validated this release this same request, so bind it
            # by id and skip the artist-floor re-search. Two flag-gated callers:
            #   - LML#604: an in-library compilation row whose floor search the
            #     ``lml_resolve_compilation_release`` flag rescues.
            #   - LML#628: a *row-less* (id==0) non-library release the A1
            #     carry-through synthesized; ``lml_resolve_nonlibrary_release``
            #     gates it. There is no library row to floor-search against, so
            #     binding the carried release is the only way to surface it.
            # Flag-off on both re-searches exactly as before (the release's
            # album_title still seeds that search below for non-row-less items).
            # LML#652: the row-less (id==0) bind also honors the bulk kill switch —
            # belt-and-suspenders, since once the five row-less producers (the four
            # Discogs-aware strategies + the A4 cached-track safety net) are gated
            # no id==0 item reaches here on /lookup/bulk. The #604 compilation
            # trust-bind (the first operand) is NOT gated here; its own lazy
            # fallback already respects the switch below.
            bind_carried = resolve_compilation_release or (
                resolve_nonlibrary_release
                and item.id == ROWLESS_LIBRARY_ID
                and allow_release_resolution_fallback
            )
            if bind_carried and resolved is not None:
                return await _bind_resolved_release(
                    discogs_service, resolved, item, album=request_album
                )

            album = resolved.album_title if resolved is not None else item.title

            # Self-titled albums stored as "S/t" should use the artist name
            # for Discogs search instead of the abbreviation
            if is_self_titled(album or ""):
                album = item.artist

            # The *track* artist (pre-compilation-form mutation). The lazy
            # release-resolution fallback validates the per-track credit, so it
            # must probe with this — never the bare "Various" search form below.
            track_artist = item.alternate_artist_name or item.artist or ""

            artist = track_artist
            if is_compilation_artist(artist):
                artist = COMPILATION_ARTIST_SEARCH_FORM

            response = await discogs_service.search(
                DiscogsSearchRequest(
                    album=album,
                    artist=artist,
                    label=item.label,
                    format=map_library_format_to_discogs(item.format),
                )
            )
            # Score candidates against (artist, album) with an 80/80 floor.
            # Returns None when nothing clears — better than serving wrong
            # artwork for releases that share a title across multiple albums
            # (LML#478, e.g. Noura Mint Seymali's "Hebebeb (Zrag)" on both
            # *Tzenni* and *Yenbett*).
            #
            # Query variants:
            # - Artist: the compilation search uses bare "Various" because
            #   that's the form Discogs's search endpoint accepts, but
            #   Discogs's canonical artist field for compilations is often
            #   "Various Artists" (sometimes with a "(N)" disambig suffix).
            #   Score against both forms. Numeric disambigs clear via
            #   token_sort_ratio's tolerance; descriptive disambigs
            #   ("Brazilian Soul" etc.) are a known floor-rejection edge
            #   case — accept the loss in exchange for the floor's gain.
            # - Album: when discogs_titles[item.id] overrides the library
            #   title with a long Discogs-canonical form (compilation rescue
            #   path), Discogs's own search results may carry just the short
            #   library-side title. Score against both. Don't readmit a
            #   self-titled trigger ("S/t" etc.) as a variant — that would
            #   let a wrong-release candidate with album="S/t" clear the
            #   floor trivially.
            artist_variants = [artist]
            if artist == COMPILATION_ARTIST_SEARCH_FORM:
                artist_variants.append(COMPILATION_ARTIST_CANONICAL_FORM)
            album_variants = [album or ""]
            if item.title and item.title != album and not is_self_titled(item.title):
                album_variants.append(item.title)
            result = find_best_typed_match(
                response.results,
                query_artist=artist_variants,
                query_title=album_variants,
                artist_fn=lambda r: r.artist,
                title_fn=lambda r: r.album,
            )
            if result is None:
                # LML#684: a found-on-compilation in-library row carries a
                # release the search strategy already resolved AND validated this
                # same request (via validate_release_for_track in
                # search_compilations_for_track). The artist-floor re-search above
                # just rejected every candidate — the systematic failure for a
                # *non*-Various-Artists trio / collaboration credit (the row is
                # filed under one member, e.g. "Bill Orcutt", but Discogs credits
                # the full trio on "Orcutt Shelley Miller", release 34993109).
                # Trust-bind the carried, validated release for artwork rather than
                # dropping it. Independent of lml_resolve_compilation_release:
                # binding an already-carried release costs no extra Discogs fan-out
                # (only a cached get_release), unlike the lazy fallback below. The
                # row-less (id==0) carry-through is handled by the flag-gated
                # bind_carried branch at the top, so it is excluded here.
                if found_on_compilation and resolved is not None and item.id != ROWLESS_LIBRARY_ID:
                    return await _bind_resolved_release(
                        discogs_service, resolved, item, album=request_album
                    )
                # Lazy release-resolution fallback (LML#604): the artist floor
                # rejected every candidate — the systematic failure for a V/A
                # compilation row filed under the track artist. When the flag is
                # on, a song is present, no release was carried on the seam, and
                # the bulk kill switch allows it, resolve and trust-bind the
                # validated release (bypassing the floor — validation settles the
                # artist via the per-track credit). resolve_release_for_track
                # returns [] on probe failure, so a transient Discogs error can
                # never abort the request here.
                if (
                    resolve_compilation_release
                    and resolved is None
                    and song
                    and allow_release_resolution_fallback
                ):
                    # Probe parity with search_compilations_for_track (LML#604
                    # deferred finding #1): apply the resolver canonical-swap
                    # when lml_resolve_artist_canonical is on, and fire the
                    # album-title wave only when the artist was NOT swapped (a
                    # high-confidence swap makes the artist-scoped probe
                    # authoritative — same gate the strategy uses).
                    probe_artist = track_artist
                    swapped = False
                    if resolve_artist_canonical:
                        cache_service = getattr(discogs_service, "cache_service", None)
                        outcome = await resolve_canonical_artist(
                            track_artist, cache_service=cache_service
                        )
                        if outcome.swapped:
                            probe_artist = outcome.canonical
                            swapped = True
                    candidates = await resolve_release_for_track_cached(
                        discogs_service,
                        song,
                        probe_artist,
                        album,
                        bool(album) and not swapped,
                    )
                    best = candidates[0] if candidates else None
                    _log_release_resolution_bind(
                        song=song,
                        artist=track_artist,
                        album=album,
                        bound=best is not None,
                        release_id=best.release_id if best is not None else None,
                    )
                    if best is not None:
                        return await _bind_resolved_release(
                            discogs_service, best, item, album=request_album
                        )
                return None
            if not result.artwork_url:
                fallback = await _resolve_fallback_artwork(discogs_service, result.release_id)
                if fallback:
                    result = result.model_copy(update={"artwork_url": fallback})
            return result
        except Exception as e:
            logger.warning(f"Artwork lookup failed for {item.title}: {e}")
            return None

    artwork_results = await asyncio.gather(*[fetch_one(item) for item in items])
    return list(zip(items, artwork_results, strict=True))


def _build_streaming_search_url(base: str, artist: str, term: str) -> str:
    """Build a streaming service search URL from artist + song/album."""
    query = f"{artist} {term}" if term else artist
    return f"{base}{quote(query)}"


async def enrich_artwork_results(
    items_with_artwork: list[tuple[LibraryItem, DiscogsSearchResult | None]],
    discogs_service: DiscogsService | None,
    song: str | None = None,
    album: str | None = None,
    artist: str | None = None,
    library_db: LibraryDB | None = None,
    *,
    extended: bool = False,
    warm_cache: bool = False,
    discogs_cache: DiscogsCacheService | None = None,
    mb_pg: PgSourceProtocol | None = None,
    apple_music: AppleMusicClient | None = None,
    spotify: SpotifyClient | None = None,
    bandcamp: BandcampClient | None = None,
    entity_store: EntityStore | None = None,
    discogs_cache_pg: PgSource | None = None,
    found_on_compilation: bool = False,
) -> list[tuple[LibraryItem, DiscogsSearchResult | None]]:
    """Enrich artwork results with release year, artist details, and streaming links.

    When library_db has a streaming_links table, uses direct URLs from the database.
    Falls back to search URLs when direct links are not available.

    ``apple_music`` is the authenticated Apple Music client (LML#443). When
    provided, the orchestrator probes Apple Music for each item: the happy
    path (library row clears the LML#477 / PR #481 title gate) calls
    ``find_track_url`` to surface the Apple Music URL only; the synthesis
    path (no Discogs match OR library row fails the title gate — LML#487)
    calls ``find_track_metadata`` to surface URL + ``artwork_url`` +
    ``release_year`` from the same ``search_song`` response. Exactly one
    Apple Music API call per item either way — picking ``find_track_url``
    vs. ``find_track_metadata`` does not inflate the per-request quota.
    When ``apple_music`` is None (credentials unconfigured), the probe is
    skipped and ``apple_music_url`` is set from the library DB
    ``streaming_links`` override (if present) or stays None. The DB
    override always wins over the probe — see the final assignment
    ``apple_music_override or apple_music_url or None``.

    **Behavior change vs. v0.5.0:** release/artist details (release_year,
    artist_bio, wikipedia_url) are fetched only for ``items_with_artwork[0]``.
    BS/iOS only consume the top-1 result, so paying N round-trips of Discogs
    cache (and on miss, API) latency for non-top-1 items was waste. The
    streaming-URL fallback build still runs per-result (cheap; no I/O).
    Gating is *positional*: a top-1 entry with ``artwork=None`` means no
    item in the response carries release-year/bio/wiki, even when items
    further down have artwork. BS/iOS only ever read ``results[0]`` so
    this is fine in practice; the lookup pipeline guarantees the strongest
    match is in position 0.

    **Behavior change vs. v0.6.0 (LML#401):** an item entering with
    ``artwork=None`` no longer round-trips as ``(item, None)``. It returns
    a synthesized ``DiscogsSearchResult(release_id=0, release_url="")``
    carrying only the streaming-URL fields (Apple via the authenticated
    ``AppleMusicClient`` + Spotify/YT/BC/SC search-URL fallbacks). Album-derived scalars
    (release_year, artist_bio, wikipedia_url) and the ``extended=True``
    payload stay None on the synthesized result, preserving the positional-
    gating invariant above. The ``(release_id=0, release_url="")`` pair is
    a cross-service contract: Backend-Service (BS#1185) keys off of it to
    skip ``extractAlbumMetadata`` while still consuming streaming URLs.
    Required for the WXYC/BS#1184 fix — releases on Apple Music that aren't
    in the WXYC/Discogs catalog now surface streaming buttons on iOS.

    **Behavior change vs. v0.7.0 (LML#487):** the synthesis path now also
    fires when ``artwork`` is non-None but its library row fails the
    LML#477 title gate (``score_match(album, item.title) < 80`` — the
    Noura Mint Seymali Tzenni-vs-Yenbett shape). The synthesized result
    additionally carries ``artwork_url`` and ``release_year`` from the
    ``find_track_metadata`` probe response, so flowsheet entries for an
    album absent from the WXYC catalog (but present on Apple Music)
    surface the *right* album's cover art on iOS / dj-site — not a
    sibling-album row's leaked Discogs image. ``release_year`` is sourced
    from the Apple Music ``releaseDate`` field; the Discogs-derived
    ``artist_bio`` / ``wikipedia_url`` / ``extended=True`` payload stay
    None on the synthesized result because they require a verified
    Discogs identity link (no equivalent surfaces from Apple).

    ``extended=True`` additionally populates the new DiscogsMatchResult
    fields LML already loaded during the release+artist fetches:
    ``discogs_artist_id``, ``tracklist``, ``genres``, ``styles``, ``label``,
    ``full_release_date``, ``artist_image_url``, ``profile_tokens``. Bio
    parsing uses ``CachedOnlyResolver`` when ``discogs_cache`` is provided
    — refs that miss the cache fall through as plain text, never trigger
    an inline Discogs API call. When ``discogs_cache`` is None (LML
    deployed without ``DATABASE_URL_DISCOGS``), bio parsing falls back to
    sync ``parse()`` which strips ID-based refs but keeps name-based and
    formatting tokens, so ``profile_tokens`` is still non-None for
    consistent client-side rendering.

    ``warm_cache=True`` schedules a fire-and-forget ``asyncio.create_task``
    after the response is composed that runs the *deep* async parse
    against the API-capable resolver, warming the PG cache for referenced
    entities so subsequent read-path lookups render richer. The task is
    not awaited — write-path callers (Backend-Service's flowsheet-linkage
    service) pay zero added latency. Concurrent warm tasks are bounded by
    ``_WARM_CACHE_CONCURRENCY`` to cap Discogs API amplification under
    burst load. Failures are logged via ``logger.exception`` so a stuck
    warm doesn't go silent.
    """
    if not discogs_service or not items_with_artwork:
        return items_with_artwork

    artist_identity_split_enabled = _artist_identity_split_gate_enabled()
    request_artist_stripped = (artist or "").strip()

    # Top-1-only expensive enrichment. fetch_release_details runs once;
    # non-top-1 items reuse the same per-result streaming-URL build.
    async def fetch_top1_release_details() -> tuple[
        int | None,
        str | None,
        str | None,
        ReleaseMetadataResponse | None,
        ArtistDetails | None,
    ]:
        """Returns (year, artist_bio, wikipedia_url, release, details) for the top-1 result.

        Returns the release + artist payloads alongside the legacy three
        scalars so the extended-field population can pluck additional
        fields without re-fetching.
        """
        top_artwork = items_with_artwork[0][1]
        # `release_id <= 0` short-circuits the LML#401 streaming-only
        # synthesis sentinel (see issue #518): the synthesized result
        # carries `release_id=0` as a BS#1185 cross-service contract,
        # and round-tripping it through Discogs hits `/releases/0` (404)
        # before the `if not release` branch silently swallows the response.
        if top_artwork is None or top_artwork.release_id <= 0:
            return None, None, None, None, None
        try:
            release = await discogs_service.get_release(top_artwork.release_id)
            if not release:
                return None, None, None, None, None

            year = release.year if isinstance(release.year, int) else None
            artist_id = release.artist_id
            if not isinstance(artist_id, int) or artist_id <= 0:
                return year, None, None, release, None

            details = await discogs_service.get_artist_details(artist_id)
            if not details:
                return year, None, None, release, None

            bio = details.profile if isinstance(details.profile, str) else None
            wiki = next(
                (url for url in details.urls if isinstance(url, str) and "wikipedia.org" in url),
                None,
            )
            return year, bio, wiki, release, details
        except Exception:
            return None, None, None, None, None

    top1_year, top1_bio, top1_wiki, top1_release, top1_details = await fetch_top1_release_details()

    # LML#504: release-side artist-identity hop. Computed once (shared across
    # all items in this batch) since ``top1_release`` is by definition top-1-only
    # and the same value is read by every enrich_one call. The library-row
    # hop CANNOT be hoisted the same way: the per-item artwork gate (the
    # LML#487 synth path runs per item, not just for top-1) needs to know
    # whether THAT item's library row anchors the request — hoisting to
    # top-1-only suppresses every non-top-1 item's probe artwork
    # unconditionally. Per-item library-row computation in ``_artist_pair_verified``
    # is cheap (two short helper calls; both early-exit on empty inputs).
    top1_release_artist = getattr(top1_release, "artist", None)
    release_side_artist_verified = _artist_pair_verified(
        request_artist_stripped, top1_release_artist
    )
    # When ``top1_release`` was fetched but its ``.artist`` field is empty or
    # whitespace-only, treat the release hop as if there was nothing to verify
    # against and fall through to library-row-only verification. Covers the
    # LML#507 prefetch-skipped case, the artwork=None case, the
    # get_release-errored case, AND corrupted/empty Discogs release rows.
    release_anchor_present = isinstance(top1_release_artist, str) and bool(
        strip_discogs_disambig(top1_release_artist).strip()
    )

    # Cache-only deep parse of the top-1 bio for the extended path. Refs
    # that miss the cache fall through; we never fire a new API call here.
    # When the PG cache is unavailable (no DATABASE_URL_DISCOGS), fall
    # back to sync parse() — drops ID-based refs but keeps name and
    # formatting tokens, so clients still get structured rendering.
    top1_profile_tokens: list[ResolvedToken] | None = None
    if extended and top1_bio:
        if discogs_cache is not None:
            try:
                top1_profile_tokens = await parse_async(top1_bio, CachedOnlyResolver(discogs_cache))
            except Exception:
                logger.exception("Cache-only bio parse failed; falling back to sync parse")
                top1_profile_tokens = parse(top1_bio)
        else:
            top1_profile_tokens = parse(top1_bio)

    async def enrich_one(
        item: LibraryItem,
        artwork: DiscogsSearchResult | None,
        *,
        is_top1: bool,
    ) -> tuple[LibraryItem, DiscogsSearchResult | None]:
        # ``row_artist`` (not ``artist``) — the outer parameter ``artist``
        # carries the request artist used by the LML#504 gate; shadowing it
        # here would silently break the gate for any future modification
        # that reaches for the request-side value.
        row_artist = item.alternate_artist_name or item.artist or ""
        search_term = song or item.title or ""

        # LML#477: only trust the library row when its title plausibly
        # matches the requested album. ``_fuzzy_search`` in library/db.py
        # accepts token_set_ratio >= 70 — permissive enough to surface
        # sibling-album rows (same artist, different release) whose
        # verified streaming URLs (and Discogs artwork) would otherwise
        # propagate as if they pointed at the requested album. The 80
        # floor mirrors ``is_acceptable_match`` in
        # ``clients/streaming/matching``. When no album was requested
        # (artist-only lookup) or the row's title is missing, there is
        # no signal to gate against — fall through.
        row_title_matches_requested_album = (
            not album
            or not item.title
            or score_match(album, item.title) >= SCORE_MATCH_ACCEPTANCE_FLOOR
            # LML#628: a row-less carry-through item (id == ROWLESS_LIBRARY_ID,
            # carrying an already-validated Discogs release) has no library row,
            # so the LML#487 sibling-leak concern — a *row's* artwork/streaming
            # links bleeding onto a mismatched album — cannot apply. The
            # carry-through resolves by *track*, so its release title (``item.title``)
            # routinely differs from a typed album; gating it here clobbered the
            # validated ``release_id`` down to the BS#1185 ``release_id=0`` sentinel
            # the feature exists to avoid. The release_id was validated to carry the
            # track, and item.title IS that release's title, so trust the binding.
            or (item.id == ROWLESS_LIBRARY_ID and artwork is not None and artwork.release_id > 0)
            # LML#684: an in-library found_on_compilation row is the analog of the
            # row-less carry-through above — TRACK_ON_COMPILATION located the track
            # on a release that IS shelved, and validate_release_for_track confirmed
            # the track sits on it. Its release title ("Orcutt-Shelley-Miller")
            # legitimately differs from the typed album (the trio/collab name
            # "Orcutt Shelley Miller", which scores 61.9 here), so the sibling-leak
            # gate would clobber the validated release_id to the release_id=0
            # sentinel — the silent-no-artwork bug this fix exists to kill. The row
            # was track-validated, so the leak concern doesn't apply.
            or (found_on_compilation and artwork is not None and artwork.release_id > 0)
        )

        # LML#487: the library row is "acceptable" (a real match for the
        # requested album) only when it carries Discogs artwork AND
        # clears the title gate. Otherwise the row's Discogs artwork /
        # release-year would be a sibling-album leak (Noura Mint Seymali
        # Tzenni-vs-Yenbett shape) — same risk as the PR #481 streaming-
        # link leak, just on a different field. When not acceptable, we
        # synthesize a streaming-only result (LML#401 / BS#1185 sentinel
        # contract) and try the Apple Music external probe to surface
        # the *right* album's artwork.
        library_row_acceptable = artwork is not None and row_title_matches_requested_album

        # Hoist the librarian-curated streaming_links override BEFORE the
        # Apple Music probe so the happy-path probe can short-circuit when
        # the override would win anyway (the final assignment is
        # ``apple_music_override or apple_music_url or None``). Saves one
        # Apple Music quota slot + up to apple_music_lookup_timeout_s() of
        # wall-clock per overridden item. The override gate still requires
        # row_title_matches_requested_album per PR #481 (LML#477).
        spotify_url = None
        apple_music_override = None
        youtube_music_url = None
        bandcamp_url = None
        soundcloud_url = None

        if (
            library_db
            and getattr(library_db, "_has_streaming_links", None) is True
            and item.id
            and row_title_matches_requested_album
        ):
            try:
                links = await library_db.get_streaming_links(item.id)
            except Exception:
                links = None
            if links:
                spotify_url = links.get("spotify_url")
                apple_music_override = links.get("apple_music_url")
                youtube_music_url = links.get("youtube_music_url")
                bandcamp_url = links.get("bandcamp_url")
                soundcloud_url = links.get("soundcloud_url")

        # Apple Music probe. ``library_row_acceptable`` picks ``find_track_url``
        # (URL only — preserves LML#401 baseline); the synthesis path
        # (LML#487) needs artwork + year too, so calls ``find_track_metadata``.
        # Both make one ``search_song`` call, so per-request quota is identical
        # to the LML#401 baseline. Skip the happy-path probe when the
        # librarian override would win anyway (saves one Apple call per
        # overridden item). The ``asyncio.wait_for`` ceiling caps single-call
        # latency under 429/5xx pressure (LML#449/#450). On timeout/error we
        # degrade to no-Apple-anything (LML#444 swallow). The Sentry data key
        # encodes the *method* so LML#462 dashboards can distinguish synthesis-
        # path timeouts (artwork still leaking, high impact) from happy-path
        # timeouts (URL only, low impact).
        apple_music_url: str | None = None
        probe_match: AppleMusicTrackMatch | None = None
        probe_artwork_url: str | None = None
        probe_release_year: int | None = None
        probe_method = "find_track_metadata" if not library_row_acceptable else "find_track_url"
        skip_happy_probe = library_row_acceptable and apple_music_override
        if apple_music is not None and row_artist and search_term and not skip_happy_probe:
            try:
                if library_row_acceptable:
                    apple_music_url = await asyncio.wait_for(
                        apple_music.find_track_url(row_artist, search_term, album=album),
                        timeout=apple_music_lookup_timeout_s(),
                    )
                else:
                    probe_match = await asyncio.wait_for(
                        apple_music.find_track_metadata(row_artist, search_term, album=album),
                        timeout=apple_music_lookup_timeout_s(),
                    )
                    if probe_match is not None:
                        apple_music_url = probe_match.url
                        probe_artwork_url = probe_match.artwork_url
                        probe_release_year = probe_match.release_year
            except TimeoutError:
                logger.warning(
                    "AppleMusicClient.%s timed out for %s - %s",
                    probe_method,
                    row_artist,
                    search_term,
                )
                # LML#462: project onto the active Sentry transaction so the
                # trace explorer can distinguish "Apple Music timed out" from
                # "Apple Music never ran" — the cancelled inner
                # `apple_music.search` span never sets its `result` data, so
                # without this marker the timeout shape is queryably
                # indistinguishable from a no-op. The legacy key stays for
                # dashboard continuity; the ``.method`` key disambiguates
                # synthesis-path timeouts from happy-path timeouts.
                try:
                    transaction = sentry_sdk.get_current_scope().transaction
                    if transaction is not None:
                        transaction.set_data("apple_music.find_track_url.timeout", True)
                        transaction.set_data("apple_music.timeout.method", probe_method)
                except Exception as e:
                    logger.warning(
                        "Failed to project apple_music timeout onto Sentry transaction: %s",
                        e,
                    )
            except Exception:
                logger.exception(
                    "AppleMusicClient.%s raised for %s - %s",
                    probe_method,
                    row_artist,
                    search_term,
                )

        # LML#505: post-hoc invalidation of sibling-row override URLs on
        # the synthesis branch. The LML#477 title gate
        # (``row_title_matches_requested_album``) clears on Deluxe /
        # Remaster / Reissue / Bonus / Limited / Expanded / Anniversary
        # suffixes because ``clients/streaming/matching.score_match``
        # strips the parenthetical before scoring —
        # ``score_match("Album X", "Album X (Deluxe Edition)") == 100.0``.
        # So a library row for the sibling original propagates its five
        # curated streaming URLs through the override block above when
        # the request is for the Deluxe. When Discogs lacks the Deluxe
        # (synthesis branch, ``not library_row_acceptable``) the Apple
        # probe fetches the *requested* album — ``find_track_metadata``
        # enforces ``album_score >= 80`` at
        # ``clients/streaming/apple_music.py:407-412`` — proving the
        # row's URLs are for a sibling, not the request. Clear them so
        # the precedence at the final ``update`` assignment lets the
        # probe URL win the Apple slot and the other four services
        # downgrade to ``_build_streaming_search_url`` placeholders
        # instead of leaking the wrong release to iOS / dj-site.
        #
        # The ``album`` and ``item.title`` guards exclude two paths the
        # collapse-to-``probe_match is not None`` rule mishandles:
        # artist-only lookups (``album=None`` → probe ran without the
        # ``album_score`` floor, so a probe match does not imply
        # override is wrong) and title-less rows (no row title to be
        # 'wrong' against). Both retain the override; the title-less
        # branch is a latent leak tracked as an open question on LML#505
        # and explicitly out of scope here.
        if not library_row_acceptable and probe_match is not None and album and item.title:
            apple_music_override = None
            spotify_url = None
            youtube_music_url = None
            bandcamp_url = None
            soundcloud_url = None

        # Album-derived fields are positionally gated: only on top-1, and
        # only when top-1 actually carries an *acceptable* library row.
        # LML#487 fall-through: when the row is not acceptable, the probe
        # supplies release_year on the synthesized result. The probe
        # already cost zero (same response that produced ``probe_artwork_url``),
        # so the original top1-only positional rationale doesn't apply —
        # surface ``probe_release_year`` whenever the synthesis branch ran.
        is_album_derived_eligible = is_top1 and library_row_acceptable
        year_result = top1_year if is_album_derived_eligible else probe_release_year

        # LML#688: the resolved release's Discogs master_id, gated like the
        # other release-sourced fields (top-1 + acceptable library row). Lets a
        # catalog-popularity caller (Backend) collapse pressings/formats of one
        # logical album by the master. ``None`` when the release has no master
        # (one-offs, self-released) or the top-1 release never resolved. Unlike
        # the extended-only fields below, it rides the album-derived gate alone
        # (not ``extended``): it is a lightweight release-identity integer that
        # the non-extended bulk-drain path also needs to group by.
        master_id_result = (
            top1_release.master_id
            if is_album_derived_eligible and top1_release is not None
            else None
        )

        # LML#504: library-row hop. ``artist_matches_item``
        # (orchestrator.py:527) and ``library/db.py``'s ``_fuzzy_search``
        # both consult ``item.artist`` AND ``item.alternate_artist_name``
        # — the gate mirrors that or it'd suppress bio on rows the
        # library code surfaced via the alternate name (cataloger
        # asymmetry: 'The Black Dog' filed as 'Black Dog Productions'
        # with the canonical form in alternate_artist_name). Computed
        # per-item: the artwork gate at the synth branch below uses
        # this for THIS item's row (each non-top-1 synth item has its
        # own probe artwork to verify), so hoisting to top-1-only would
        # silently suppress every non-top-1 probe artwork.
        library_row_artist_verified = _artist_pair_verified(
            request_artist_stripped, item.artist
        ) or _artist_pair_verified(request_artist_stripped, item.alternate_artist_name)
        # Composite: when the release has no usable artist anchor
        # (``top1_release`` is None OR ``release.artist`` is empty/whitespace),
        # fall through to library-row-only verification. Covers the
        # LML#507 prefetch-skipped case AND the corrupted-release case.
        # When the library row has NO usable artist anchor either (both
        # ``item.artist`` and ``item.alternate_artist_name`` empty — rare
        # but possible per the ``str | None`` schema), fall back to legacy
        # gate semantics rather than over-suppressing.
        library_row_anchor_present = bool(
            (item.artist or "").strip() or (item.alternate_artist_name or "").strip()
        )
        artist_identity_verified = library_row_artist_verified and (
            not release_anchor_present or release_side_artist_verified
        )
        # Rollout scope: the split-gate is opt-in via ``extended=True`` so
        # legacy non-extended consumers (request-o-matic request line,
        # dj-site proxy) stay on the broader ``is_album_derived_eligible``
        # gate. Backend-Service forces ``extended=true`` on every wire
        # call (BS' ``lookup-coordinator.ts``), so the split immediately
        # exercises on all BS write-path traffic (iOS reads + flowsheet
        # writes) without exposing the request-line / picker callers.
        # Additional fallbacks to legacy gate:
        # * empty ``request_artist`` (album-only lookups, ``parsed.artist=None``
        #   at orchestrator.py:888) — first hop would always fail with no
        #   anchor to score against.
        # * empty library-row anchor (corrupted/sparse catalog row) — the
        #   first hop would always fail with no candidate to score against.
        use_split_gate = (
            extended
            and artist_identity_split_enabled
            and bool(request_artist_stripped)
            and library_row_anchor_present
        )
        is_artist_derived_eligible = is_top1 and (
            artist_identity_verified if use_split_gate else library_row_acceptable
        )
        artist_bio = top1_bio if is_artist_derived_eligible else None
        wikipedia_url = top1_wiki if is_artist_derived_eligible else None

        # LML#504 rollout monitor: shadow-mode telemetry whenever the new
        # gate would land bio/wiki on a result where the legacy gate would
        # not (the synth-recovery this ticket exists for) or vice-versa
        # (gate-tightening regressions). Fires *regardless of*
        # ``artist_identity_split_enabled`` so the rollback flag preserves
        # the divergence signal needed to plan re-enablement. Gated on
        # ``extended`` + non-empty ``request_artist`` + library-row anchor
        # present — outside those preconditions the split gate can never
        # apply, so a "divergence" is just the trivial empty-input case
        # and would flood the dashboard with non-actionable noise. Pairs
        # ``set_data`` (queryable) with a 1% sampled INFO log, matching
        # ``_log_track_validation`` / ``_log_resolver_pre_pass``.
        if (
            is_top1
            and extended
            and bool(request_artist_stripped)
            and library_row_anchor_present
            and artist_identity_verified != library_row_acceptable
        ):
            _log_artist_identity_split_gate(
                library_row_acceptable=library_row_acceptable,
                artist_identity_verified=artist_identity_verified,
                library_row_artist_verified=library_row_artist_verified,
                release_side_artist_verified=release_side_artist_verified,
                release_anchor_present=release_anchor_present,
                use_split_gate=use_split_gate,
            )

        # Fall back to search URLs for any service without a direct link.
        # Spotify's templated fallback was deleted in LML#573 — the persistent
        # streaming-URL cache post-process below now backstops spotify_url with
        # a real album page (and mints the identity) instead of a generic search
        # URL. Bandcamp's fallback is DEFERRED past the post-process (LML#573
        # PR-3): the post-process only fires when its URL field is ``None``, so a
        # pre-filled search URL would silently disable the Bandcamp leg — the
        # search URL is applied below, only if the cache/probe leaves it None.
        # YouTube Music / SoundCloud have no album-cache tier, so they keep
        # their pre-post-process templated fallbacks.
        if row_artist and search_term:
            if not youtube_music_url:
                youtube_music_url = _build_streaming_search_url(
                    "https://music.youtube.com/search?q=", row_artist, search_term
                )
            if not soundcloud_url:
                soundcloud_url = _build_streaming_search_url(
                    "https://soundcloud.com/search?q=", row_artist, search_term
                )

        update: dict[str, Any] = {
            "release_year": year_result,
            "master_id": master_id_result,
            "artist_bio": artist_bio,
            "wikipedia_url": wikipedia_url,
            # spotify_url / bandcamp_url are normalized to None (like
            # apple_music_url) so an empty-string streaming_links override
            # (library.db returns the column verbatim, no '' -> None coercion) is
            # treated as "absent" by the post-process active-filter (`is None`).
            # Without this, "" skips the cache/probe leg AND either surfaces
            # straight to the client (spotify has no fallback) or gets a search
            # URL while the leg was skipped (bandcamp's deferred `not …`
            # fallback). youtube / soundcloud aren't post-process services and
            # overwrite "" with a search URL above, so they need no normalization.
            "spotify_url": spotify_url or None,
            "apple_music_url": apple_music_override or apple_music_url or None,
            "youtube_music_url": youtube_music_url,
            "bandcamp_url": bandcamp_url or None,
            "soundcloud_url": soundcloud_url,
        }

        # LML "streaming URLs for non-library albums" (LML#573) — when the
        # existing per-item probe + override couldn't surface a service URL,
        # the polymorphic post-process runs a cache-backed probe per configured
        # service (Apple + Spotify + Bandcamp) with the REQUEST's (artist, album)
        # — not the library row's. Fixes the wrong-fallback-row attack
        # (non-library album like Hyd / "Hold Onto Me Infinity" falls back to a
        # same-titled library row by a different artist, in-line probe runs with
        # the wrong artist name → null). Results persist to
        # ``lml_cache.album_streaming_url_cache`` so future lookups short-circuit
        # the upstream API, and live resolutions mint the parsed ID into
        # ``entity.release_identity``. The ``clients`` dict may carry ``None``
        # values (e.g. Spotify creds unconfigured) — the post-process filters
        # them. Gated by the master + per-service flags.
        await apply_streaming_url_postprocess(
            update,
            clients={"apple_music": apple_music, "spotify": spotify, "bandcamp": bandcamp},
            pg=discogs_cache_pg,
            entity_store=entity_store,
            request_artist=artist,
            request_album=album,
            settings=get_settings(),
        )

        # Bandcamp's templated search-URL fallback, deferred past the
        # post-process (LML#573 PR-3): apply it only if the cache/probe (and any
        # librarian-curated streaming_links override) left bandcamp_url empty, so
        # a resolved album page / direct link always wins over the generic search
        # link. Priority: direct link > cache/probe > search URL.
        if row_artist and search_term and not update["bandcamp_url"]:
            update["bandcamp_url"] = _build_streaming_search_url(
                "https://bandcamp.com/search?q=", row_artist, search_term
            )

        # Extended fields land on the top-1 result only and require artwork
        # (same positional + artwork gating as the album-derived scalars).
        # The non-top-1 items keep their lean shape so non-iOS lookup
        # callers (request line, dj-site proxy, BS catalog) don't pay
        # payload bloat for results they ignore.
        if extended and is_album_derived_eligible:
            if top1_release is not None:
                update["tracklist"] = (
                    list(top1_release.tracklist) if top1_release.tracklist else None
                )
                update["genres"] = list(top1_release.genres) if top1_release.genres else None
                update["styles"] = list(top1_release.styles) if top1_release.styles else None
                update["label"] = top1_release.label
                update["full_release_date"] = top1_release.released
                # BMI songwriter/composer credits (LML#699). Prefer the played
                # track's per-track writers (``provenance="track"``) when ``song``
                # resolves to a tracklist position; otherwise the release-level
                # writer-role subset of the already-fetched ``extra_artists``
                # (``provenance="release"``). Cache-only — the position is scanned
                # over the in-scope ``top1_release`` with no new Discogs call;
                # ``None`` for comps (release-level) / when no writer resolves.
                #
                # Unlike the sibling album-derived fields above, writer credits
                # are *person* attribution consumed for BMI royalty reporting, so
                # they additionally require artist-identity verification
                # (``is_artist_derived_eligible``) — the intersection of the album
                # and artist gates, for BOTH precisions. A fuzzy album-title
                # collision with a *different* artist's release
                # (``library_row_acceptable`` true but the artist gate false) must
                # not leak that artist's composers (release- or track-level) as
                # the played track's writers. No-op when the split gate is off
                # (``is_artist_derived_eligible`` then equals
                # ``library_row_acceptable``); also skips the position scan then.
                if is_artist_derived_eligible:
                    resolved_track_position = (
                        find_track_position(top1_release, song) if song else None
                    )
                    update["writer_credits"] = writer_credits_from_release(
                        top1_release, track_position=resolved_track_position
                    )
                else:
                    update["writer_credits"] = None
            # ``artist_image_url`` stays gated on ``is_album_derived_eligible``
            # despite being artist-scoped: neither wxyc-ios-64 nor
            # wxyc-dj-tool-ios mounts a UI affordance for it
            # (``ArtistMetadata`` at ``Shared/Metadata/Sources/Metadata/
            # PlaycutMetadata.swift`` doesn't carry the field), so surfacing
            # it on the synthesis path would be payload waste. Re-gate when
            # iOS adds an artist-image mount.
            update["artist_image_url"] = (
                top1_details.image_url if top1_details is not None else None
            )

        # ``profile_tokens`` parses from ``top1_bio``; ``discogs_artist_id``
        # IS ``release.artist_id``. Both are strictly artist-scoped, so they
        # ride the artist-identity gate (not the album-derived gate). This
        # keeps the API contract coherent: any response that carries
        # ``artist_bio`` also carries the ``discogs_artist_id`` key that
        # iOS/BS need to key an artist-metadata cache against (see
        # ``generated/api_models.DiscogsMatchResult.discogs_artist_id``).
        if extended and is_artist_derived_eligible:
            update["profile_tokens"] = top1_profile_tokens
            if top1_release is not None:
                update["discogs_artist_id"] = top1_release.artist_id

        if not library_row_acceptable:
            # No acceptable Discogs match: try MusicBrainz for a tracklist
            # before synthesizing the streaming-only result. Same positional
            # gate as the rest of the extended payload — only the top-1 item
            # is eligible, and only when extended mode is on.
            if is_top1 and extended and mb_pg is not None and item.artist and album:
                mb_tracklist = await resolve_tracklist_via_musicbrainz(
                    item.artist, album, mb_pg=mb_pg
                )
                # LML#506 post-rescue song-sanity check. The resolver runs a
                # pg_trgm ``LIMIT 1`` with a lenient 0.70 floor, so on the
                # Deluxe-vs-Original sibling-album shape (long shared
                # substring, both sides clear 0.70) it can return Original's
                # tracklist for a Deluxe request. When the DJ's song doesn't
                # appear in the rescued tracks, the candidate is almost
                # certainly the wrong release; drop it rather than surface a
                # wrong tracklist to the picker (which writes it
                # unchallenged to the flowsheet).
                #
                # Known limitation: only the bonus-only-track variant is
                # caught here. Shared-track-Deluxe leaks (DJ requests a
                # track present on both editions) pass the check
                # undetected. The fix lives in the resolver — top-K
                # candidates filtered by song-presence — and is filed as
                # a follow-up. Telemetry from ``mb_resolver.requested_album``
                # / ``mb_resolver.returned_album`` sizes whether the bigger
                # swing is justified.
                # Strip song upfront so whitespace-only inputs (``song='   '``)
                # follow the same skip path as ``song is None`` — a truthy
                # blank would otherwise enter the check, normalize to empty
                # inside ``score_match_track``, score 0 against every track,
                # and unconditionally drop the rescue. The acceptance floor
                # is the same 80 used across LML#477 / LML#504 / Apple Music
                # probe (``SCORE_MATCH_ACCEPTANCE_FLOOR``); imported rather
                # than re-declared so calibration bumps propagate uniformly.
                song_stripped = (song or "").strip()
                # Pre-strip the song once (loop-invariant) AND verify the
                # post-strip form is non-empty. If the request's song was
                # entirely variant-marker (e.g. ``song="(Live)"`` from a
                # malformed parse), the post-strip form is "" and
                # ``score_match("", t.title)`` returns 0 for every track —
                # falsely dropping the rescue. Treat the all-marker case
                # the same as ``song=None`` / whitespace-only: skip the
                # check rather than emit a misleading rejection.
                song_match_target = strip_track_suffix(song_stripped)
                song_sanity_checked = False
                song_sanity_rejected = False
                if mb_tracklist and song_match_target and _mb_rescue_song_match_required():
                    song_sanity_checked = True
                    # Require a non-empty stripped track title for the
                    # iteration to count as a hit. ``score_match_track("", "")``
                    # returns 100 by rapidfuzz convention, so without this
                    # guard a corrupt MB row with all-empty titles would
                    # falsely pass the check when the query side also
                    # normalizes to empty.
                    if not any(
                        (t.title or "").strip()
                        and score_match_track(song_match_target, t.title)
                        >= SCORE_MATCH_ACCEPTANCE_FLOOR
                        for t in mb_tracklist
                    ):
                        logger.info(
                            "mb_rescue: dropping tracklist for (%r, %r) — song %r "
                            "not in rescued tracks (likely sibling-release leak)",
                            item.artist,
                            album,
                            song_stripped,
                        )
                        mb_tracklist = None
                        song_sanity_rejected = True
                if mb_tracklist:
                    update["tracklist"] = mb_tracklist
                _project_mb_rescue_attrs(
                    attempted=True,
                    tracklist_found=bool(mb_tracklist),
                    song_sanity_checked=song_sanity_checked,
                    song_sanity_rejected=song_sanity_rejected,
                )

            # LML#487: surface the Apple Music probe's artwork URL on the
            # synthesized result. ``probe_artwork_url`` is non-None when
            # ``find_track_metadata`` returned a match clearing the 80/80(/80)
            # floor; ``None`` falls through to the no-artwork shape (legacy
            # LML#401 behaviour preserved when no probe match was found, no
            # Apple credentials configured, or the probe timed out / raised).
            #
            # LML#504: the probe was called with ``row_artist`` — when that
            # disagrees with the request artist (fuzzy-collision library row),
            # the probe returned the WRONG artist's artwork. Gate on the
            # library-row hop of the new predicate so a synth-path lookup
            # that failed artist verification doesn't surface a stranger's
            # cover art. Gated on the same ``use_split_gate`` predicate as
            # ``artist_bio`` / ``wikipedia_url`` so the rollback flag and
            # the extended-only rollout scope apply uniformly to all
            # LML#504-introduced gating — non-extended callers and operators
            # who flip the env-var to false get bit-for-bit legacy
            # LML#487 behavior back.
            if use_split_gate and not library_row_artist_verified:
                update["artwork_url"] = None
            else:
                update["artwork_url"] = probe_artwork_url

            # See docstring's "Behavior change vs. v0.6.0 (LML#401)"
            # section for the BS#1185 sentinel contract.
            return (item, DiscogsSearchResult(release_id=0, release_url="", **update))

        # ``library_row_acceptable`` ⟹ ``artwork is not None`` (by definition
        # on line above). Asserted for mypy narrowing — the runtime cost is
        # nil, and the explicit precondition makes the contract local.
        assert artwork is not None
        return (item, artwork.model_copy(update=update))

    enriched = await asyncio.gather(
        *[
            enrich_one(item, artwork, is_top1=(idx == 0))
            for idx, (item, artwork) in enumerate(items_with_artwork)
        ]
    )

    # Write-path warm: fire-and-forget deep async parse of the top-1 bio
    # using the API-capable resolver, so the PG cache gets populated for
    # `[a…]`/`[r…]`/`[m…]` references. The task is intentionally not
    # awaited — read-path latency is unaffected. The task reference is
    # parked in ``_background_tasks`` so the GC can't reap it mid-flight
    # (asyncio holds only weak refs to tasks).
    #
    # LML#504: don't warm a bio the response itself suppressed. The deep
    # parse fires per-ref Discogs API calls (DiscogsServiceResolver: cache
    # → API → write-back) — wasting those on a bio iOS will never render
    # is pure quota burn. Inspect the top-1 enriched result rather than
    # re-deriving the gate so the warm check is locked in step with the
    # actual surfaced output.
    top1_enriched_result = enriched[0][1] if enriched else None
    top1_bio_surfaced = top1_enriched_result is not None and bool(top1_enriched_result.artist_bio)
    if warm_cache and top1_bio and top1_bio_surfaced:
        task = asyncio.create_task(_warm_bio_cache(top1_bio, discogs_service))
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)

    return list(enriched)


async def _warm_bio_cache(bio: str, discogs_service: DiscogsService) -> None:
    """Background task: deep-async parse of an artist bio to warm caches.

    Resolves every `[a<id>]` / `[r<id>]` / `[m<id>]` reference through
    ``DiscogsServiceResolver`` (cache → API → cache write-back), so
    subsequent ``parse_async(..., CachedOnlyResolver)`` calls on this bio
    return typed tokens instead of plain text. Bounded by the module-level
    semaphore to cap concurrent Discogs API amplification under burst
    load. Errors are logged and swallowed — the task must never propagate
    to the event loop. No Sentry tag is set here: the request scope has
    long since closed by the time this runs, so a tag on the active scope
    would land on whatever unrelated request is running next.
    """
    global _warm_cache_semaphore
    if _warm_cache_semaphore is None:
        _warm_cache_semaphore = asyncio.Semaphore(_WARM_CACHE_CONCURRENCY)
    try:
        async with _warm_cache_semaphore:
            await parse_async(bio, DiscogsServiceResolver(discogs_service))
    except Exception:
        logger.exception("Background bio cache-warm failed")


def build_context_message(
    parsed: ParsedRequest,
    found_on_compilation: bool,
    song_not_found: bool,
    has_results: bool = True,
) -> str | None:
    """Build context message based on search results."""
    if found_on_compilation:
        return f'Found "{parsed.song}" by {parsed.artist} on:'

    if song_not_found and has_results:
        if parsed.song and parsed.album:
            return (
                f'"{parsed.album}" not found in the library, '
                f"but here are other albums by {parsed.artist}:"
            )
        elif parsed.song:
            return (
                f'"{parsed.song}" is not on any album in the library, '
                f"but here are some albums by {parsed.artist}:"
            )
    elif song_not_found and not has_results:
        if parsed.song and parsed.artist:
            return f'"{parsed.song}" by {parsed.artist} not found in library.'

    return None


def _identity_to_reconciled(identity: Identity) -> ReconciledIdentity:
    """Convert an EntityStore Identity dataclass to the shared ReconciledIdentity schema."""
    return ReconciledIdentity(
        discogs_artist_id=identity.discogs_artist_id,
        musicbrainz_artist_id=identity.musicbrainz_artist_id,
        wikidata_qid=identity.wikidata_qid,
        spotify_artist_id=identity.spotify_artist_id,
        apple_music_artist_id=identity.apple_music_artist_id,
        bandcamp_id=identity.bandcamp_id,
    )


async def _resolve_identities(
    artist_names: list[str], entity_store: EntityStore
) -> dict[str, ReconciledIdentity]:
    """Look up reconciled identities for unique artist names.

    Returns a dict keyed by the artist name. Names not found in the entity
    store are omitted, so callers should treat a missing key as "no identity."
    Lookups across the unique names run concurrently.
    """
    unique = list({name for name in artist_names if name})
    if not unique:
        return {}

    identities = await asyncio.gather(
        *(entity_store.get_identity(name) for name in unique),
        return_exceptions=True,
    )

    result: dict[str, ReconciledIdentity] = {}
    for name, identity in zip(unique, identities, strict=True):
        if isinstance(identity, BaseException):
            logger.warning("EntityStore.get_identity failed for %r: %s", name, identity)
            continue
        if identity is not None:
            result[name] = _identity_to_reconciled(identity)
    return result


async def perform_lookup(
    request: LookupRequest,
    db: LibraryDB,
    discogs_service: DiscogsService | None,
    telemetry: RequestTelemetry,
    *,
    entity_store: EntityStore | None = None,
    discogs_cache: DiscogsCacheService | None = None,
    mb_pg: PgSourceProtocol | None = None,
    apple_music: AppleMusicClient | None = None,
    spotify: SpotifyClient | None = None,
    bandcamp: BandcampClient | None = None,
    discogs_cache_pg: PgSource | None = None,
    caller_budget_ms: int | None = None,
    allow_release_resolution_fallback: bool = True,
) -> LookupResponse:
    """Orchestrate the full lookup pipeline.

    Steps:
    1. Correct artist spelling
    2. Resolve album names from Discogs (if song provided without album)
    3. Execute search strategy pipeline
    4. Validate fallback results against Discogs tracklists
    5. Fetch artwork for results
    6. Build context message
    """
    # Build a ParsedRequest from the LookupRequest for compatibility with
    # search functions that expect ParsedRequest
    parsed = ParsedRequest(
        song=request.song,
        album=request.album,
        artist=request.artist,
        is_request=True,
        message_type=MessageType.REQUEST,
        raw_message=request.raw_message,
    )

    library_results: list[LibraryItem] = []
    items_with_artwork: list[tuple[LibraryItem, DiscogsSearchResult | None]] = []
    song_not_found = False
    found_on_compilation = False
    discogs_titles: dict[int, ResolvedRelease] = {}
    corrected_artist: str | None = None

    # Steps 1+2: Correct artist spelling and resolve albums (parallel)
    if parsed.artist:
        correction_task = db.find_similar_artist(parsed.artist)
        with telemetry.track_step("album_lookup"):
            if parsed.song and not parsed.album:
                telemetry.record_api_call("discogs")
            corrected, (albums_for_search, song_not_found) = await asyncio.gather(
                correction_task,
                resolve_albums_for_track(parsed, discogs_service),
            )
        if corrected:
            corrected_artist = corrected
            # Two-channel seam (WXYC/library-metadata-lookup#626): do NOT
            # overwrite ``parsed.artist``. The typed value must keep flowing to
            # every Discogs-facing path (the three Discogs-aware strategies'
            # probes, ``validate_release_for_track``, and the library-miss
            # Discogs probe), or a *distinct* non-library artist one edit from a
            # library name gets snapped into the library's vocabulary on Discogs.
            # Thread the correction on ``library_artist``, read only by the
            # library-side legs (``db.search`` queries + ``artist_matches_item``
            # match-backs).
            parsed.library_artist = corrected
    else:
        with telemetry.track_step("album_lookup"):
            albums_for_search, song_not_found = await resolve_albums_for_track(
                parsed, discogs_service
            )

    # Step 3: Execute search strategy pipeline
    with telemetry.track_step("library_search"):
        # Strategies hold their own ``db`` handle (no per-call db arg on the
        # runner post-#399). The discogs_service is captured via ``partial`` on
        # the strategies that need it; ARTIST_PLUS_ALBUM is the only
        # library-only strategy (SWAPPED_INTERPRETATION now cross-references the
        # non-artist token as a track via Discogs — LML#622).
        # ``discogs_cache_pg`` threads the #632 row-less resolution cache into the
        # three Discogs-aware strategies' A1 carry-through (LML#628). The library
        # channel and ARTIST_PLUS_ALBUM / SONG_AS_ARTIST never touch it.
        # LML#652: the per-request bulk kill switch (``False`` on /lookup/bulk)
        # threads into the four Discogs-aware row-less producers here via the
        # partials, and into the fifth (the A4 cached-track safety net) at its
        # own Step-3b call site below — exactly as it already reaches
        # ``fetch_artwork_for_items`` (Step 4) for #604's lazy fallback. So no
        # row-less item surfaces, nor any carry-through resolve or #632 cache
        # write, on the backfill path.
        strategies = build_strategies(
            db,
            search_library_func=search_library_with_fallback,
            search_alternative_func=partial(
                search_with_alternative_interpretation,
                discogs_service=discogs_service,
                pg=discogs_cache_pg,
                allow_release_resolution_fallback=allow_release_resolution_fallback,
            ),
            search_compilations_func=partial(
                search_compilations_for_track,
                discogs_service=discogs_service,
                pg=discogs_cache_pg,
                allow_release_resolution_fallback=allow_release_resolution_fallback,
            ),
            search_song_as_artist_func=partial(
                search_song_as_artist,
                discogs_service=discogs_service,
                allow_release_resolution_fallback=allow_release_resolution_fallback,
            ),
            search_song_as_track_func=partial(
                search_song_as_track,
                discogs_service=discogs_service,
                pg=discogs_cache_pg,
                allow_release_resolution_fallback=allow_release_resolution_fallback,
            ),
        )

        search_state = await execute_search_pipeline(
            parsed=parsed,
            raw_message=request.raw_message or "",
            strategies=strategies,
            albums_for_search=albums_for_search,
            song_not_found=song_not_found,
            caller_budget_ms=caller_budget_ms,
        )

        library_results = limit_results(search_state.results)
        song_not_found = search_state.song_not_found
        found_on_compilation = search_state.found_on_compilation
        discogs_titles = search_state.discogs_titles
        search_type = get_search_type_from_state(search_state)

        if found_on_compilation:
            telemetry.record_api_call("discogs")

    # Step 3a: Library-miss Discogs search (LML#583).
    # When the entire search pipeline returned no library results AND the request
    # carries both artist and album, probe Discogs directly. A confident match
    # (80/80-floor via find_best_typed_match) is synthesized into a
    # LibraryItem(id=0) + DiscogsSearchResult pair and handed directly to Step
    # 4b (enrich_artwork_results) — bypassing fetch_artwork_for_items (which
    # would re-search and risk a second floor mis-selection) and bypassing Step
    # 3b track validation (the synthesized item is already 80-floored on
    # (artist, album) jointly; routing it through an unfloored top-1 re-search
    # has non-zero probability of flipping to a different release).
    #
    # Gated on ``allow_release_resolution_fallback`` (the bulk kill switch, LML#671):
    # /lookup/bulk passes ``False`` so the 35k-album backfill never pays a per-row
    # Discogs ``search()`` here, nor surfaces a row-less ``LibraryItem(id=0)``.
    # An album-only backfill item (artist+album, no song) hits *this* producer —
    # not the five song-bearing LML#652 producers — so gating it is what makes the
    # "no per-row Discogs surfacing/cost on bulk" guarantee true for the real drain.
    # /lookup keeps the default (``True``), so #583 fires there exactly as before.
    library_miss_outcome: str | None = None
    if (
        not library_results
        and allow_release_resolution_fallback
        and parsed.artist
        and parsed.album
        and parsed.album.strip()
    ):
        with telemetry.track_step("library_miss_discogs_search"):
            miss_match = await _library_miss_discogs_search(parsed, discogs_service)
        if miss_match is not None:
            synthesized_lib_item, discogs_result = miss_match
            items_with_artwork = [(synthesized_lib_item, discogs_result)]
            # A synthesized Discogs match resolves the request — clear song_not_found so
            # build_context_message and LookupResponse.song_not_found reflect reality.
            song_not_found = False
            library_miss_outcome = "library_miss_discogs_match"
            telemetry.record_api_call("discogs")
        elif discogs_service is not None:
            # Discogs was available and searched but found no confident match.
            library_miss_outcome = "library_miss_no_discogs_match"

    # Step 3b: Validate results against Discogs track data.
    # Synthesized id=0 items from Step 3a are excluded: library_results is empty
    # on that path, so the gate below never fires. The filter on real_results is
    # a defensive guard for any future path that might add id=0 items to
    # library_results.
    real_results = [r for r in library_results if r.id != 0]
    if real_results and parsed.song and parsed.artist:
        if not found_on_compilation:
            # Normal case: validate all results against Discogs tracklists
            with telemetry.track_step("track_validation"):
                validated = await filter_results_by_track_validation(
                    real_results, parsed.song, parsed.artist, discogs_service
                )
                if validated:
                    library_results = validated
                    song_not_found = False
                elif song_not_found:
                    # Per-result validation confirmed nothing. Ask the local
                    # PG cache directly: "any release by this artist whose
                    # tracklist contains this song?" — and promote the matching
                    # library album. Catches the case where the upstream
                    # track→releases lookup missed a release the cache holds.
                    promoted, promoted_titles = await find_library_albums_with_cached_track(
                        db,
                        parsed.song,
                        parsed.artist,
                        discogs_service,
                        match_artist=library_artist_for(parsed),
                        allow_release_resolution_fallback=allow_release_resolution_fallback,
                    )
                    if promoted:
                        library_results = promoted
                        # A4 (LML#629): a row-less promotion carries its resolved
                        # release on the seam so Step 4 binds discogs_url by id.
                        discogs_titles = {**discogs_titles, **promoted_titles}
                        song_not_found = False
                    else:
                        # Last resort before declaring song-not-found: the
                        # request shape "<album-title>, <artist>" can route to
                        # us as ``song=<album-title>``. If a surviving result's
                        # title clears the floor against ``parsed.song``, the
                        # user wanted that album. Surfacing it as found-the-
                        # album avoids the misleading 'not on any album'
                        # message about a row sitting in the result list.
                        title_matches = _filter_results_by_song_as_album_title(
                            library_results, parsed.song
                        )
                        if title_matches:
                            logger.info(
                                f"Promoted {len(title_matches)} of {len(library_results)} "
                                f"artist-fallback row(s) whose title matches song "
                                f"'{parsed.song}' — treating as album request"
                            )
                            library_results = title_matches
                            song_not_found = False
        elif search_state.artist_fallback_results:
            # Compilation found, but the artist's own album may also contain the track.
            # Validate the artist fallback results (saved before compilation search
            # replaced them) and prepend any confirmed matches.
            with telemetry.track_step("track_validation"):
                validated = await filter_results_by_track_validation(
                    search_state.artist_fallback_results,
                    parsed.song,
                    parsed.artist,
                    discogs_service,
                )
                if validated:
                    compilation_ids = {r.id for r in library_results}
                    merged = [r for r in validated if r.id not in compilation_ids]
                    merged.extend(library_results)
                    library_results = merged

    # Step 3c: Populate streaming status
    if library_results and getattr(db, "_has_streaming_links", None) is True:
        streaming_status = await db.get_streaming_status([r.id for r in library_results])
        for result in library_results:
            result.on_streaming = streaming_status.get(result.id, False)

    # Step 4: Fetch artwork
    with telemetry.track_step("artwork_fetch"):
        if library_results:
            for _ in library_results:
                telemetry.record_api_call("discogs")
            items_with_artwork = await fetch_artwork_for_items(
                library_results,
                discogs_service,
                discogs_titles,
                song=parsed.song,
                album=parsed.album,
                allow_release_resolution_fallback=allow_release_resolution_fallback,
                found_on_compilation=found_on_compilation,
            )

    # The generated LookupRequest models these as bool | None (api.yaml
    # describes them with `default: false` but the field is not in the
    # required set, so callers can omit them). Coerce once and reuse.
    extended_mode = bool(request.extended)
    warm_cache_mode = bool(request.warm_cache)

    # Step 4b: Enrich with release year, artist details, streaming links
    with telemetry.track_step("metadata_enrichment"):
        if items_with_artwork:
            items_with_artwork = await enrich_artwork_results(
                items_with_artwork,
                discogs_service,
                song=parsed.song,
                album=parsed.album,
                artist=parsed.artist,
                library_db=db,
                extended=extended_mode,
                warm_cache=warm_cache_mode,
                discogs_cache=discogs_cache,
                mb_pg=mb_pg,
                apple_music=apple_music,
                spotify=spotify,
                bandcamp=bandcamp,
                entity_store=entity_store,
                discogs_cache_pg=discogs_cache_pg,
                found_on_compilation=found_on_compilation,
            )

    # Project the request-side flags and result-quality signals onto the
    # active Sentry transaction so the trace can be filtered by request mode
    # (lml.lookup.extended, lml.lookup.warm_cache) and by match outcome
    # (lookup.results_count, lookup.match_type — LML#158). Mirrors the
    # cache_stats projection pattern (LML#213).
    try:
        scope = sentry_sdk.get_current_scope()
        if scope.transaction is not None:
            scope.transaction.set_data("lml.lookup.extended", extended_mode)
            scope.transaction.set_data("lml.lookup.warm_cache", warm_cache_mode)
            scope.transaction.set_data(
                "lookup.results_count",
                len(items_with_artwork) if items_with_artwork else len(library_results),
            )
            scope.transaction.set_data("lookup.match_type", search_type)
            if library_miss_outcome is not None:
                scope.transaction.set_data("lookup.outcome", library_miss_outcome)
    except Exception:
        # Observability must not break the request path.
        pass

    # Step 5: Build context message
    context = build_context_message(
        parsed,
        found_on_compilation,
        song_not_found,
        has_results=bool(library_results) or bool(items_with_artwork),
    )

    # Step 6: Resolve external identifiers for each result's artist.
    # Collect artist names from library_results (normal path) and from
    # items_with_artwork (synthesized Step 3a path, where library_results is empty
    # but the synthesized LibraryItem carries a real artist name worth resolving).
    identities_by_artist: dict[str, ReconciledIdentity] = {}
    _identity_artist_names = [item.artist for item in library_results if item.artist] + [
        item.artist for item, _ in items_with_artwork if item.id == 0 and item.artist
    ]
    if entity_store is not None and _identity_artist_names:
        with telemetry.track_step("identity_resolution"):
            identities_by_artist = await _resolve_identities(_identity_artist_names, entity_store)

    def _identity_for(item: LibraryItem) -> ReconciledIdentity | None:
        if not item.artist:
            return None
        return identities_by_artist.get(item.artist)

    # Build response (convert internal models to API contract models)
    matched_via_by_id = search_state.matched_via_by_id
    result_items = []
    if items_with_artwork:
        for item, artwork in items_with_artwork:
            # Synthesized items (id=0, from Step 3a) have no library call-number
            # components; build LibraryCatalogItem directly with the "(external)"
            # sentinel that Backend-Service already understands (same contract as
            # the Step 7 include_external_caches path).
            catalog_item = (
                LibraryCatalogItem(
                    id=0,
                    artist=item.artist,
                    title=item.title,
                    call_number="(external)",
                    library_url="",
                )
                if item.id == 0
                else item.to_catalog_item()
            )
            result_items.append(
                LookupResultItem(
                    library_item=catalog_item,
                    artwork=artwork.to_match_result() if artwork else None,
                    reconciled_identity=_identity_for(item),
                    # Synthesized items (id=0) carry no track-title-provenance hint;
                    # do not look up key 0 in matched_via_by_id to prevent accidental
                    # collision with any future strategy that might write to that key.
                    matched_via=None if item.id == 0 else matched_via_by_id.get(item.id),
                )
            )
    elif library_results:
        for item in library_results:
            result_items.append(
                LookupResultItem(
                    library_item=item.to_catalog_item(),
                    reconciled_identity=_identity_for(item),
                    matched_via=matched_via_by_id.get(item.id),
                )
            )

    # Step 7: External-cache fallback (Phase 1.5 + 1.7 mojibake recovery).
    # Opt-in via include_external_caches. The lossy-mojibake matcher sends
    # column-typed bodies, so we dispatch by which skeleton field is set:
    # artist takes precedence (highest-precision lookup), then album, then
    # song. A bare raw_message with no typed field skips the fallback —
    # LABEL_NAME is too noisy to be useful here.
    external_source: str | None = "library" if result_items else None
    if not result_items and request.include_external_caches:
        candidates: list[dict[str, Any]] = []
        source: str | None = None
        if parsed.artist:
            with telemetry.track_step("external_cache_fallback"):
                rows, source = await search_external_artists(
                    parsed.artist,
                    discogs_cache=discogs_cache,
                    mb_pg=mb_pg,
                )
            candidates = [{"artist": r["name"], "title": ""} for r in rows]
        elif parsed.album:
            with telemetry.track_step("external_cache_fallback"):
                rows, source = await search_external_albums(
                    parsed.album,
                    discogs_cache=discogs_cache,
                    mb_pg=mb_pg,
                )
            candidates = [{"artist": r["artist"], "title": r["title"]} for r in rows]
        elif parsed.song:
            with telemetry.track_step("external_cache_fallback"):
                rows, source = await search_external_tracks(
                    parsed.song,
                    discogs_cache=discogs_cache,
                    mb_pg=mb_pg,
                )
            candidates = [{"artist": r["artist"], "title": r["title"]} for r in rows]

        if candidates:
            external_source = source
            for candidate in candidates:
                result_items.append(
                    LookupResultItem(
                        library_item=LibraryCatalogItem(
                            id=0,
                            artist=candidate["artist"],
                            title=candidate["title"] or None,
                            call_number="(external)",
                            library_url="",
                        ),
                    )
                )

    return LookupResponse(
        results=result_items,
        search_type=search_type,
        song_not_found=song_not_found,
        found_on_compilation=found_on_compilation,
        context_message=context,
        corrected_artist=corrected_artist,
        external_source=external_source,
        timeout=search_state.timed_out,
    )
