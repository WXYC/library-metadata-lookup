"""Lookup orchestrator: the core search logic extracted from request-o-matic.

This module contains the perform_lookup() function that orchestrates the full
search pipeline: artist correction -> album resolution -> search strategies ->
track validation -> artwork fetch -> metadata enrichment -> context message.
"""

import asyncio
import logging
import random
import re
import time
from collections.abc import Coroutine
from dataclasses import dataclass
from functools import partial
from typing import Any
from urllib.parse import quote

import httpx
import sentry_sdk
from wxyc_etl.text import is_compilation_artist
from wxyc_etl.text import to_match_form as normalize_for_comparison
from wxyc_fastapi.observability import RequestTelemetry, get_cache_stats_recorder

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
from discogs.memory_cache import create_ttl_cache, should_skip_cache
from discogs.models import (
    ArtistDetails,
    DiscogsSearchRequest,
    DiscogsSearchResult,
    ReleaseInfo,
    ReleaseMetadataResponse,
    ResolvedToken,
    TrackReleasesResponse,
)
from discogs.service import DiscogsService
from entity.sources import PgSourceProtocol
from entity.store import EntityStore, Identity
from generated.api_models import (
    LibraryCatalogItem,
    ReconciledIdentity,
    TrackMatchHint,
    TrackMatchSource,
)
from library.db import STOPWORDS, LibraryDB
from library.models import LibraryItem
from lookup.external_search import (
    search_external_albums,
    search_external_artists,
    search_external_tracks,
)
from lookup.models import LookupRequest, LookupResponse, LookupResultItem
from lookup.strategies import build_strategies
from release.musicbrainz_resolver import resolve_tracklist_via_musicbrainz
from services.parser import MessageType, ParsedRequest

logger = logging.getLogger(__name__)

MAX_SEARCH_RESULTS = 5
"""Maximum number of results to return from search operations."""

SELF_TITLED_PATTERNS = frozenset({"s/t", "s.t.", "self-titled", "self titled"})
"""Common abbreviations for self-titled albums (case-insensitive exact match)."""

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


CANONICAL_ARTIST_SIMILARITY_FLOOR: float = 0.70
"""Trigram-similarity floor for swapping an inbound artist name with its canonical
Discogs form.

Provisional. Replaced by the offline calibration sweep produced by
``scripts.resolver_calibration`` against the WXYC discogs-cache; see
``docs/resolver-calibration/README.md`` for the chosen value and its FP-rate
tolerance (target ≤ 0.5%). See WXYC/library-metadata-lookup#318.
"""

_resolver_cache = create_ttl_cache(maxsize=512, ttl=300)
"""TTL cache for ``resolve_canonical_artist``. Keyed on the diacritic-stripped,
lowercased input so equivalent strings within a burst share one PG round-trip.
Registered with the global cache registry so ``clear_all_caches()`` resets it.
"""


@dataclass(frozen=True)
class ResolverOutcome:
    """Result of the canonical-artist resolver pre-pass.

    Attributes:
        original: The input artist string as received from the caller.
        canonical: The canonical Discogs artist name when ``swapped`` is True;
            otherwise identical to ``original``.
        score: Trigram similarity score of the top cache candidate (0.0-1.0).
            ``0.0`` when no candidate was found.
        swapped: Whether ``canonical`` differs from ``original`` and met the
            similarity floor. Callers use this to decide whether to forward
            ``canonical`` into downstream Discogs probes.
    """

    original: str
    canonical: str
    score: float
    swapped: bool


async def resolve_canonical_artist(
    artist: str,
    *,
    cache_service: DiscogsCacheService | None,
) -> ResolverOutcome:
    """Resolve ``artist`` to the canonical Discogs name when confidence allows.

    Runs a trigram fuzzy search against ``artist`` + ``artist_name_variation``
    in the discogs-cache PG database. When the top score meets
    ``CANONICAL_ARTIST_SIMILARITY_FLOOR``, returns a ``ResolverOutcome`` with
    ``swapped=True`` and the canonical name; otherwise returns the original
    input with ``swapped=False``. Results are memoized in-process keyed on the
    diacritic-stripped lowercased input.

    Failure modes (no cache, empty input, PG error) all degrade to
    ``swapped=False`` so the resolver never breaks /lookup.

    See WXYC/library-metadata-lookup#318.
    """
    original = artist or ""

    if not original.strip():
        return ResolverOutcome(original=original, canonical=original, score=0.0, swapped=False)

    if cache_service is None:
        return ResolverOutcome(original=original, canonical=original, score=0.0, swapped=False)

    cache_key = normalize_for_comparison(original)

    if not should_skip_cache():
        cached = _resolver_cache.get(cache_key)
        if cached is not None:
            get_cache_stats_recorder().record_memory_cache_hit()
            return ResolverOutcome(
                original=original,
                canonical=cached.canonical,
                score=cached.score,
                swapped=cached.swapped,
            )
        get_cache_stats_recorder().record_memory_cache_miss()

    try:
        candidates = await cache_service.search_artists_by_name(original, limit=5)
    except Exception as e:
        logger.warning("resolver_pre_pass cache lookup failed for %r: %s", original, e)
        return ResolverOutcome(original=original, canonical=original, score=0.0, swapped=False)

    if not candidates:
        outcome = ResolverOutcome(original=original, canonical=original, score=0.0, swapped=False)
        _resolver_cache[cache_key] = outcome
        return outcome

    top = candidates[0]
    score = float(top.get("score", 0.0))
    candidate_name = top.get("name") or original
    swapped = score >= CANONICAL_ARTIST_SIMILARITY_FLOOR

    outcome = ResolverOutcome(
        original=original,
        canonical=candidate_name if swapped else original,
        score=score,
        swapped=swapped,
    )
    _resolver_cache[cache_key] = outcome
    return outcome


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


def _project_mb_rescue_attrs(attempted: bool, tracklist_found: bool) -> None:
    """Project MusicBrainz tracklist-rescue outcome onto the active Sentry trace.

    Two boolean attrs on the synth path's enrichment span:
    - ``lookup.mb_rescue.attempted``: gating condition met (top-1 + extended
      + ``mb_pg`` set + non-empty artist + non-empty album).
    - ``lookup.mb_rescue.tracklist_found``: resolver returned a non-empty
      tracklist (similarity floor cleared and PG returned rows).

    Always reports both — ``attempted=False`` with ``tracklist_found=False``
    means the gate skipped (no MB cache configured, non-extended call, or
    missing inputs). This lets the trace query "how often does the rescue
    fire, and how often does it hit?" without inferring from absence.

    Silent on Sentry SDK errors; observability never breaks /lookup.
    """
    try:
        transaction = sentry_sdk.get_current_scope().transaction
        if transaction is not None:
            transaction.set_data("lookup.mb_rescue.attempted", attempted)
            transaction.set_data("lookup.mb_rescue.tracklist_found", tracklist_found)
    except Exception as e:
        logger.warning("Failed to project mb_rescue attrs onto Sentry transaction: %s", e)


def _log_track_validation(
    *,
    source: str,
    release_id: int,
    song: str,
    artist: str | None,
    verdict: bool,
    latency_ms: float,
    sample_rate: float = 0.01,
) -> None:
    """Audit instrumentation for ``DiscogsService.validate_track_on_release`` (A7 / LML#344).

    The cascade has two validation call sites: the inline check inside
    ``TRACK_ON_COMPILATION``'s ``process_release`` and the post-cascade
    sweep in ``filter_results_by_track_validation``. The audit ticket wants
    to know how often the second call runs after the first already vetted
    the same release, and how often the two verdicts disagree.

    This helper records each call with a ``source`` label so downstream
    Sentry analysis can split (release_id, verdict) pairs by source.
    Two surfaces:

    - **Sentry breadcrumb on every call** — trace explorer queries against
      ``category:track_validation`` to count divergences without log-pipeline
      tooling. Always-on; cost is microseconds per call.
    - **Structured INFO log on a 1% sample** — cheap Railway-log grep target
      for spot-checking. The full population is too large to log at INFO,
      so the sample is randomized per call.

    Both surfaces are non-essential; any SDK exception is swallowed and the
    request proceeds. Same swallow pattern as :func:`_log_album_title_fallback`
    / :func:`_log_resolver_pre_pass`.

    The acceptance criterion from #344 is "7 days of data" — this helper
    ships the instrumentation; the decision about whether to delete one of
    the call sites is a follow-up after the data is in.
    """
    payload: dict[str, Any] = {
        "source": source,
        "release_id": release_id,
        "song": song,
        "artist": artist,
        "verdict": verdict,
        "latency_ms": round(latency_ms, 2),
    }
    try:
        sentry_sdk.add_breadcrumb(
            category="track_validation",
            level="info",
            data=payload,
        )
    except Exception as e:
        logger.warning("Failed to add track_validation breadcrumb: %s", e)

    if random.random() < sample_rate:
        logger.info("track_validation %s", payload)


def _log_resolver_pre_pass(outcome: ResolverOutcome, *, actual_swap: bool) -> None:
    """Emit shadow-mode telemetry for the resolver pre-pass.

    Runs unconditionally — regardless of the enforcement flag — so the
    queryable shadow dataset accumulates in production from day one and the
    floor can be re-calibrated against real traffic without a code change.

    ``actual_swap`` is what the orchestrator actually did this request
    (``outcome.swapped AND lml_resolve_artist_canonical``). ``would_swap`` is
    the resolver's recommendation independent of the flag — what the swap
    decision *would* be if the flag were enabled. Filtering Sentry traces on
    ``data.resolver_pre_pass.would_swap=true`` while the flag is off is the
    shadow dataset; ``swapped`` is non-zero only after the flag flips.

    Two surfaces:

    1. Structured INFO log line for log-pipeline tools.
    2. ``set_data("resolver_pre_pass", ...)`` on the active Sentry
       transaction, mirroring ``lookup/router._project_cache_stats_to_transaction``.
       No-op when there is no active transaction. Any Sentry SDK error is
       swallowed so observability cannot break /lookup.
    """
    if not outcome.original.strip():
        return
    payload = {
        "original": outcome.original,
        "candidate": outcome.canonical,
        "score": outcome.score,
        "swapped": actual_swap,
        "would_swap": outcome.swapped,
    }
    logger.info("resolver_pre_pass %s", payload)
    try:
        transaction = sentry_sdk.get_current_scope().transaction
        if transaction is not None:
            transaction.set_data("resolver_pre_pass", payload)
    except Exception as e:
        logger.warning("Failed to project resolver_pre_pass onto Sentry transaction: %s", e)


def is_self_titled(title: str) -> bool:
    """Check if an album title indicates a self-titled release.

    Args:
        title: Album title to check

    Returns:
        True if title is a common self-titled abbreviation (e.g. "S/t", "S.T.")
    """
    return title.strip().lower() in SELF_TITLED_PATTERNS


def map_library_format_to_discogs(fmt: str | None) -> str | None:
    """Map a WXYC library format value to a Discogs API format parameter.

    Library format values like "cd", "vinyl - 12\\"", "cd x 2" are mapped to
    the corresponding Discogs search API format terms ("CD", "12\\"", etc.).

    Returns None if the format is not recognized or is empty.
    """
    if not fmt:
        return None
    normalized = fmt.strip().lower()
    if normalized.startswith("cdr"):
        return "CDr"
    if normalized.startswith("cd"):
        return "CD"
    if 'vinyl - 12"' in normalized or "vinyl - 12" in normalized:
        return '12"'
    if 'vinyl - 7"' in normalized or "vinyl - 7" in normalized:
        return '7"'
    if 'vinyl - 10"' in normalized or "vinyl - 10" in normalized:
        return '10"'
    if normalized.startswith("vinyl"):
        return "Vinyl"
    return None


_FETCH_LIMIT = MAX_SEARCH_RESULTS * 10
"""Internal fetch limit for FTS queries that are post-filtered by artist.

FTS5 ranks results by term frequency, not by artist-prefix relevance, so the
target artist's entries may fall outside a tight SQL LIMIT.  Fetching more rows
ensures enough candidates survive ``filter_results_by_artist`` before we trim
back to ``MAX_SEARCH_RESULTS``.
"""


def limit_results(results: list) -> list:
    """Limit results to MAX_SEARCH_RESULTS."""
    return results[:MAX_SEARCH_RESULTS]


_LEADING_ARTICLE_RE = re.compile(r"^(the|a|an)(\s+|$)")


def _strip_leading_article(normalized: str) -> str:
    """Strip a leading English article from a lowercase-normalized name.

    Operates on the output of ``normalize_for_comparison`` (already
    lowercased and trimmed), so the article check can be anchored and
    case-blind.
    """
    return _LEADING_ARTICLE_RE.sub("", normalized, count=1)


def artist_matches_item(item: LibraryItem, artist: str) -> bool:
    """Check if a library item matches the given artist name.

    Compares against both ``item.artist`` and ``item.alternate_artist_name``.
    Tolerates a leading-article asymmetry between query and catalog —
    library catalogers commonly file "The Black Dog" as "Black Dog
    Productions" while user input and Discogs credits keep the article,
    and the reverse ("Beatles" vs "The Beatles") also occurs. Both sides
    are compared as-is first; on miss, both are also compared with the
    leading article stripped. The stripped path is skipped when stripping
    leaves the query empty so a bare "The" doesn't match arbitrary rows.
    """
    artist_normalized = normalize_for_comparison(artist)
    artist_no_article = _strip_leading_article(artist_normalized)

    for candidate in (item.artist, item.alternate_artist_name):
        if not candidate:
            continue
        cand_normalized = normalize_for_comparison(candidate)
        if cand_normalized.startswith(artist_normalized):
            return True
        if artist_no_article and _strip_leading_article(cand_normalized).startswith(
            artist_no_article
        ):
            return True
    return False


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


def filter_results_by_artist(
    results: list[LibraryItem],
    artist: str | None,
) -> list[LibraryItem]:
    """Filter library results to only include those matching the artist.

    Requires the searched artist name to appear at the START of the result's
    artist field (case-insensitive).
    """
    if not artist:
        return results

    filtered = []
    for item in results:
        if artist_matches_item(item, artist):
            filtered.append(item)

    if len(filtered) < len(results):
        logger.info(
            f"Filtered {len(results)} results to {len(filtered)} matching artist '{artist}'"
        )

    return filtered


async def search_with_alternative_interpretation(
    db: LibraryDB,
    part1: str,
    part2: str,
) -> tuple[list[LibraryItem], None]:
    """Try searching with both artist/title interpretations for 'X - Y' format."""
    raw1, raw2 = await asyncio.gather(
        db.search(query=f"{part1} {part2}", limit=_FETCH_LIMIT),
        db.search(query=f"{part2} {part1}", limit=_FETCH_LIMIT),
    )
    results1 = filter_results_by_artist(raw1, part1)
    results2 = filter_results_by_artist(raw2, part2)

    if results1 and not results2:
        logger.info(f"Alternative search matched with '{part1}' as artist")
        return results1, None
    elif results2 and not results1:
        logger.info(f"Alternative search matched with '{part2}' as artist")
        return results2, None
    elif results1 and results2:
        logger.info("Alternative search matched both interpretations, combining results")
        seen_ids = set()
        combined = []
        for item in results1 + results2:
            if item.id not in seen_ids:
                combined.append(item)
                seen_ids.add(item.id)
        return limit_results(combined), None

    return [], None


async def search_song_as_artist(
    db: LibraryDB,
    song_as_artist: str,
    discogs_service: DiscogsService | None = None,
) -> tuple[list[LibraryItem], None]:
    """Try searching using the parsed song title as an artist name."""
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
        *[search_album(album_title) for _, album_title in discogs_releases]
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


SONG_AS_TRACK_CONFIDENCE: float = 0.85
"""Default confidence floor for SONG_AS_TRACK matches.

Pinned at the master-cap value from catalog-track-search plan §5.2. The
underlying ``search_releases_by_track`` cache path doesn't currently distinguish
release- vs master-level matches, so we conservatively report the master cap.
When LML graduates onto ``library_identity`` per cross-cache-identity (#25),
this floor is replaced with ``library_identity.confidence`` per row.
"""


async def search_song_as_track(
    db: LibraryDB,
    song: str | None,
    discogs_service: DiscogsService | None = None,
) -> tuple[list[LibraryItem], dict[int, list[TrackMatchHint]]]:
    """Cross-reference song against Discogs and match releases back to library.

    Catalog-track-search §4.2 / LML#301: when SONG_AS_ARTIST returns empty for a
    song-only query, treat the song as a *track* — find Discogs releases that
    contain it, then fuzzy-match those releases against the WXYC library. Each
    surviving row carries a TrackMatchHint recording the track→release linkage.

    Args:
        db: Library database for album fuzzy search.
        song: The track title from the user query.
        discogs_service: Required. Without it, this strategy no-ops.

    Returns:
        Tuple of (library_items, matched_via_by_id). matched_via_by_id maps
        each library row's id to one-or-more TrackMatchHint entries — multiple
        hints accumulate when the same WXYC row is referenced by multiple
        Discogs releases (different pressings, etc.).
    """
    if not song or not discogs_service:
        return [], {}

    response = await discogs_service.search_releases_by_track(song, artist=None)
    raw_releases = list(response.releases or [])
    if not raw_releases:
        logger.info(f"SONG_AS_TRACK: no Discogs releases for '{song}'")
        return [], {}

    logger.info(
        f"SONG_AS_TRACK: {len(raw_releases)} Discogs releases for '{song}', "
        "matching against library"
    )

    seen_ids: set[int] = set()
    matched_items: list[LibraryItem] = []
    matched_via_by_id: dict[int, list[TrackMatchHint]] = {}

    for release in raw_releases:
        if not release.album or len(release.album.strip()) < 3:
            continue

        matches = await search_album_fuzzy(db, release.album)
        if not matches and release.is_compilation:
            matches = await search_album_fuzzy(db, f"Various {release.album}")
        if not matches:
            continue

        eligible = [m for m in matches if _release_matches_library_row(release, m)]
        if not eligible:
            continue

        # Validate the track actually appears on this release before surfacing
        # — Discogs's release-search index is keyword-driven and returns hits
        # that don't always contain the track on the tracklist. Deferred until
        # after library matching so we only pay the API cost for releases we'd
        # actually return, mirroring search_compilations_for_track.
        if release.release_id:
            _validation_start = time.monotonic()
            is_valid = await discogs_service.validate_track_on_release(
                release.release_id, song, release.artist
            )
            _log_track_validation(
                source="song_as_track",
                release_id=release.release_id,
                song=song,
                artist=release.artist,
                verdict=is_valid,
                latency_ms=(time.monotonic() - _validation_start) * 1000,
            )
            if not is_valid:
                logger.debug(
                    f"SONG_AS_TRACK: skipping '{release.album}' — track not validated on release"
                )
                continue

        for item in eligible:
            hint = TrackMatchHint(
                title=song,
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
                f"SONG_AS_TRACK: matched '{item.artist} - {item.title}' "
                f"via release '{release.album}'"
            )

            if len(matched_items) >= MAX_SEARCH_RESULTS:
                break
        if len(matched_items) >= MAX_SEARCH_RESULTS:
            break

    return matched_items, matched_via_by_id


def _release_matches_library_row(release: ReleaseInfo, item: LibraryItem) -> bool:
    """Predicate: does ``release``'s artist credit match ``item``'s library artist?

    Compilation-aware: for VA releases (``release.is_compilation``), any library
    row whose artist field is itself a compilation marker (e.g., "Various
    Artists - Rock - D") qualifies. For non-compilations, the library row's
    artist must prefix-match the Discogs release artist via the existing
    ``artist_matches_item`` rules.
    """
    if release.is_compilation and is_compilation_artist(item.artist or ""):
        return True
    if item.artist and artist_matches_item(item, release.artist):
        return True
    return False


# Minimum fuzzy score (0-100) for accepting a library-row title as a genuine
# album match for the DJ-typed album. Mirrors `_APPLE_MUSIC_MATCH_FLOOR` and
# the streaming-availability batch matcher. When the artist-fallback branches
# of `search_library_with_fallback` surface a row whose title doesn't clear
# this floor against the typed album, the row would otherwise carry the
# matched Discogs release's `release_year` / `apple_music_url` / `spotify_url`
# / `discogs_url` / `artwork_url` onto a flowsheet row tagged with a
# completely different album — the contamination shape documented in #400
# (~184k rows; 16,532 distinct Discogs URLs each attached to many distinct
# DJ-typed `(artist, album)` pairs). #390 / #398 tightened the iTunes-result
# verification; this tightens the LML lookup result itself.
_ALBUM_MATCH_FLOOR = 80.0


def _filter_results_by_album_match(
    results: list[LibraryItem],
    album: str | None,
) -> list[LibraryItem]:
    """Drop library rows whose title doesn't clear `_ALBUM_MATCH_FLOOR` against
    the typed album. No-ops when `album` is empty or whitespace-only.
    """
    if not album or not album.strip():
        return results
    from rapidfuzz import fuzz

    norm_album = normalize_for_comparison(album)
    kept: list[LibraryItem] = []
    for item in results:
        title_norm = normalize_for_comparison(item.title or "")
        if fuzz.token_set_ratio(norm_album, title_norm) >= _ALBUM_MATCH_FLOOR:
            kept.append(item)
    if len(kept) < len(results):
        logger.info(
            f"Album-match floor dropped {len(results) - len(kept)} of {len(results)} "
            f"artist-fallback candidates against typed album '{album}'"
        )
    return kept


async def search_library_with_fallback(
    db: LibraryDB,
    parsed: ParsedRequest,
    albums: list[str],
) -> tuple[list[LibraryItem], bool]:
    """Search library with artist+album(s), falling back to artist+song or artist-only.

    Returns:
        Tuple of (library_results, song_not_found_flag)
    """
    all_results: list[LibraryItem] = []
    seen_ids: set[int] = set()

    if not parsed.artist and albums:
        # No artist parsed — search by album title alone
        for album in albums:
            results = await db.search(query=album, limit=_FETCH_LIMIT)
            if results:
                return results[:MAX_SEARCH_RESULTS], False
        return [], bool(parsed.song)

    if parsed.artist and albums:

        async def search_one_album(album: str) -> list[LibraryItem]:
            query = f"{parsed.artist} {album}"
            results = await db.search(query=query, limit=_FETCH_LIMIT)
            results = filter_results_by_artist(results, parsed.artist)

            album_lower = album.lower()
            album_normalized = re.sub(r"[^\w\s]", " ", album_lower)
            album_normalized = " ".join(album_normalized.split())
            album_words = {w for w in album_normalized.split() if len(w) > 2 and w not in STOPWORDS}
            album_is_artist = parsed.artist and normalize_for_comparison(
                album
            ) == normalize_for_comparison(parsed.artist)

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

    if parsed.artist and parsed.song:
        query = f"{parsed.artist} {parsed.song}"
        results = await db.search(query=query, limit=_FETCH_LIMIT)
        results = filter_results_by_artist(results, parsed.artist)
        results = _filter_results_by_album_match(results, parsed.album)

        if results:
            song_lower = parsed.song.lower()
            results.sort(
                key=lambda r: song_lower in (r.title or "").lower(),
                reverse=True,
            )
            return results, True

    if not all_results and parsed.artist:
        logger.info(f"No results for albums {albums}, trying artist only: '{parsed.artist}'")
        results = await db.search(query=parsed.artist, limit=_FETCH_LIMIT)
        results = filter_results_by_artist(results, parsed.artist)
        results = _filter_results_by_album_match(results, parsed.album)
        if results:
            return results, True

    return all_results, bool(parsed.song)


async def search_compilations_for_track(
    db: LibraryDB,
    parsed: ParsedRequest,
    discogs_service: DiscogsService | None = None,
) -> tuple[list[LibraryItem], dict[int, str]]:
    """Search for track on compilation albums using Discogs and library keyword search."""
    if not parsed.song or not parsed.artist:
        return [], {}

    logger.info(f"Searching for '{parsed.song}' on other releases (compilations, etc.)")

    results = []
    seen_ids = set()
    discogs_titles: dict[int, str] = {}

    keyword_matches = []
    try:
        artist_words = (
            re.sub(r"[^\w\s]", " ", parsed.artist.lower()).split() if parsed.artist else []
        )
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
                    if artist_matches_item(item, parsed.artist):
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
            raw_releases = list(response.releases or [])

            # Only merge VA results if the artist-scoped search found no compilations
            has_compilation = any(r.is_compilation for r in raw_releases)
            if not has_compilation:
                seen_album_keys = {r.album.lower() for r in raw_releases}
                for r in va_response.releases or []:
                    if r.is_compilation and r.album.lower() not in seen_album_keys:
                        raw_releases.append(r)
                        seen_album_keys.add(r.album.lower())
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
        ) -> list[tuple[LibraryItem, str]]:
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

            if matches and parsed.artist and not skip_artist_match_filter:
                from rapidfuzz import fuzz as _fuzz

                filtered_matches = []
                discogs_is_compilation = release_info.is_compilation
                release_album_lower = release_album.lower()

                for match in matches:
                    if artist_matches_item(match, parsed.artist):
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

            if not matches:
                return []

            # Validate that the track actually exists on this release.
            # Deferred until after library matching so we only validate
            # releases we might actually return — saving API calls.
            if discogs_service and release_info.release_id and parsed.artist:
                _validation_start = time.monotonic()
                is_valid = await discogs_service.validate_track_on_release(
                    release_info.release_id, song_search, parsed.artist
                )
                _log_track_validation(
                    source="compilation_inline",
                    release_id=release_info.release_id,
                    song=song_search,
                    artist=parsed.artist,
                    verdict=is_valid,
                    latency_ms=(time.monotonic() - _validation_start) * 1000,
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
            return [(match, release_album) for match in matches]

        all_release_results = await asyncio.gather(*[process_release(ri) for ri in raw_releases])

        for release_matches in all_release_results:
            for match, discogs_album in release_matches:
                if match.id not in seen_ids:
                    results.append(match)
                    seen_ids.add(match.id)
                    discogs_titles[match.id] = discogs_album
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
            fallback_results = await asyncio.gather(
                *[
                    process_release(
                        ri,
                        skip_self_named_album=False,
                        skip_artist_match_filter=True,
                    )
                    for ri in fallback_releases
                ]
            )
            for release_matches in fallback_results:
                for match, discogs_album in release_matches:
                    if match.id not in seen_ids:
                        results.append(match)
                        seen_ids.add(match.id)
                        discogs_titles[match.id] = discogs_album
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

    return limit_results(results), discogs_titles


def album_title_acceptable(query_lower: str, result_lower: str) -> bool:
    """Check if a library album title is an acceptable match for a Discogs album title.

    Uses prefix matching (handles parenthetical suffixes like edition names) and
    length-sensitive fuzz.ratio to reject subset matches that token_set_ratio
    would incorrectly accept.

    Also rejects numbered series albums (e.g., "Chicago V" vs "Chicago 16",
    "Led Zeppelin II" vs "Led Zeppelin IV") by checking that when titles share
    a long common prefix, the short distinguishing suffixes are also similar.
    """
    from rapidfuzz import fuzz

    if query_lower.startswith(result_lower) or result_lower.startswith(query_lower):
        return True

    # Find common prefix length
    common = 0
    for a, b in zip(query_lower, result_lower, strict=False):
        if a != b:
            break
        common += 1

    # Reject numbered series: titles that share a dominant prefix but differ
    # in a short identifier suffix (e.g., "V" vs "16", "II" vs "IV").
    if common > 0:
        remainder_q = query_lower[common:].strip()
        remainder_r = result_lower[common:].strip()
        min_len = min(len(query_lower), len(result_lower))
        if (
            remainder_q
            and remainder_r
            and len(remainder_q) <= 5
            and len(remainder_r) <= 5
            and common >= min_len * 0.5
        ):
            if fuzz.ratio(remainder_q, remainder_r) < 50:
                return False

    return fuzz.ratio(query_lower, result_lower) >= 50


async def search_album_fuzzy(db: LibraryDB, album_title: str) -> list[LibraryItem]:
    """Search for album with fuzzy keyword matching."""
    from rapidfuzz import fuzz

    results = await db.search(query=album_title, limit=MAX_SEARCH_RESULTS)

    # Filter exact FTS5 results by title similarity to reject subset matches
    # (e.g., FTS5 matches "808 State" for query "The Best Of 808 State: Blueprint"
    # because it tokenizes and matches on shared terms "808" and "State")
    if results:
        album_lower = album_title.lower()
        results = [
            r for r in results if album_title_acceptable(album_lower, (r.title or "").lower())
        ]

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

                _validation_start = time.monotonic()
                is_valid = await discogs_service.validate_track_on_release(
                    best_result.release_id, song, artist
                )
                _log_track_validation(
                    source="step_3b",
                    release_id=best_result.release_id,
                    song=song,
                    artist=artist,
                    verdict=is_valid,
                    latency_ms=(time.monotonic() - _validation_start) * 1000,
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
) -> list[LibraryItem]:
    """Find WXYC library albums whose Discogs cache entry lists ``song`` by ``artist``.

    Used as a safety net after ``filter_results_by_track_validation`` fails to
    confirm any artist-fallback candidate. The PG cache holds full Discogs
    tracklist data with trigram-indexed track titles, so a single lookup can
    answer "which releases by this artist contain this track?" in milliseconds —
    even when the upstream ``resolve_albums_for_track`` / API path missed it.

    Cache-only by design: skips any API fallback path. Returns ``[]`` cleanly
    when the cache is unavailable, fails, or has nothing for the query.
    """
    if not discogs_service or not song or not artist:
        return []
    cache_service = getattr(discogs_service, "cache_service", None)
    if cache_service is None:
        return []

    try:
        cached_releases = await cache_service.search_releases_by_track(
            track=song, artist=artist, limit=20
        )
    except Exception as e:
        logger.warning(f"Cache lookup for track-album promotion failed: {e}")
        return []

    if not cached_releases:
        return []

    matches: list[LibraryItem] = []
    seen_ids: set[int] = set()

    for release in cached_releases:
        candidate_items = await search_album_fuzzy(db, release.album)
        for item in candidate_items:
            if item.id in seen_ids:
                continue
            if not artist_matches_item(item, artist):
                continue
            matches.append(item)
            seen_ids.add(item.id)
            if len(matches) >= limit:
                return matches

    return matches


async def _resolve_fallback_artwork(discogs_service: DiscogsService, release_id: int) -> str | None:
    """Try the release's own cover (images[0]), then artist image, then label image."""
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


async def fetch_artwork_for_items(
    items: list[LibraryItem],
    discogs_service: DiscogsService | None,
    discogs_titles: dict[int, str] | None = None,
) -> list[tuple[LibraryItem, DiscogsSearchResult | None]]:
    """Fetch artwork for multiple library items in parallel."""
    if not discogs_service:
        return [(item, None) for item in items]

    discogs_titles = discogs_titles or {}

    async def fetch_one(item: LibraryItem) -> DiscogsSearchResult | None:
        try:
            album = discogs_titles.get(item.id, item.title)

            # Self-titled albums stored as "S/t" should use the artist name
            # for Discogs search instead of the abbreviation
            if is_self_titled(album or ""):
                album = item.artist

            artist = item.alternate_artist_name or item.artist or ""
            if is_compilation_artist(artist):
                artist = "Various"

            response = await discogs_service.search(
                DiscogsSearchRequest(
                    album=album,
                    artist=artist,
                    label=item.label,
                    format=map_library_format_to_discogs(item.format),
                )
            )
            if response.results:
                result = response.results[0]
                if not result.artwork_url:
                    fallback = await _resolve_fallback_artwork(discogs_service, result.release_id)
                    if fallback:
                        result = result.model_copy(update={"artwork_url": fallback})
                return result
            return None
        except Exception as e:
            logger.warning(f"Artwork lookup failed for {item.title}: {e}")
            return None

    artwork_results = await asyncio.gather(*[fetch_one(item) for item in items])
    return list(zip(items, artwork_results, strict=True))


def _build_streaming_search_url(base: str, artist: str, term: str) -> str:
    """Build a streaming service search URL from artist + song/album."""
    query = f"{artist} {term}" if term else artist
    return f"{base}{quote(query)}"


# Minimum fuzzy score (0-100) for accepting an iTunes Search result as a genuine
# match for the requested artist + track (+ album, when provided). Mirrors the
# 80/80 artist+title floor the streaming-availability batch matcher enforces
# (clients/streaming/matching.py:is_acceptable_match). iTunes
# Search ranking is non-deterministic for obscure artists, so a bare results[0]
# intermittently surfaces a popular but wrong artist (e.g. "Pleasure"/"Joyous"
# -> Sheryl Crow, see #389), and same-named tracks on multiple of an artist's
# releases can land on the wrong album (e.g. Yenbett's "Hebebeb (Zrag)" -> the
# track of the same name on Tzenni, see #396). Verification stops the wrong
# link from freezing onto the flowsheet row.
_APPLE_MUSIC_MATCH_FLOOR = 80.0


async def _fetch_apple_music_url(
    artist: str,
    song: str,
    album: str | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> str | None:
    """Search the iTunes API for an Apple Music link. Free, no auth required.

    Requires a shared ``http_client``; returns ``None`` when one isn't
    provided so callers can degrade gracefully. Constructing a fresh
    ``httpx.AsyncClient`` per probe is what leaked FDs in the 2026-05-01
    LML outage (issue #241), so the per-call fallback was removed.

    Returns the ``trackViewUrl`` of the first result whose ``artistName`` and
    ``trackName`` (and, when ``album`` is provided, ``collectionName``) all
    clear ``_APPLE_MUSIC_MATCH_FLOOR`` against the requested ``artist`` /
    ``song`` / ``album`` (diacritics-folded fuzzy token-set match), else
    ``None``. iTunes Search ranking is unstable for obscure artists, so a bare
    ``results[0]`` can confidently return the wrong artist (#389) or — when
    the same track title appears on multiple of the artist's releases — the
    wrong album (#396); verifying avoids persisting a wrong link onto the
    flowsheet row.
    """
    if http_client is None:
        return None
    from rapidfuzz import fuzz

    try:
        query = quote(f"{artist} {song}")
        url = f"https://itunes.apple.com/search?term={query}&entity=song&media=music&limit=5"
        resp = await http_client.get(url)
        if resp.status_code != 200:
            return None
        results = resp.json().get("results", [])

        norm_artist = normalize_for_comparison(artist)
        norm_song = normalize_for_comparison(song)
        norm_album = normalize_for_comparison(album) if album else None
        # token_set_ratio (not the batch matcher's token_sort_ratio) so extra
        # tokens on either side — "The", "feat. X", "(Remastered)" — don't sink
        # an otherwise-correct match.
        for result in results:
            track_url = result.get("trackViewUrl")
            if not track_url:
                continue
            artist_score = fuzz.token_set_ratio(
                norm_artist, normalize_for_comparison(result.get("artistName", ""))
            )
            track_score = fuzz.token_set_ratio(
                norm_song, normalize_for_comparison(result.get("trackName", ""))
            )
            if artist_score < _APPLE_MUSIC_MATCH_FLOOR or track_score < _APPLE_MUSIC_MATCH_FLOOR:
                continue
            if norm_album is not None:
                album_score = fuzz.token_set_ratio(
                    norm_album, normalize_for_comparison(result.get("collectionName", ""))
                )
                if album_score < _APPLE_MUSIC_MATCH_FLOOR:
                    continue
            return track_url
        return None
    except Exception:
        return None


async def enrich_artwork_results(
    items_with_artwork: list[tuple[LibraryItem, DiscogsSearchResult | None]],
    discogs_service: DiscogsService | None,
    song: str | None = None,
    album: str | None = None,
    library_db: LibraryDB | None = None,
    http_client: httpx.AsyncClient | None = None,
    *,
    extended: bool = False,
    warm_cache: bool = False,
    discogs_cache: DiscogsCacheService | None = None,
    mb_pg: PgSourceProtocol | None = None,
) -> list[tuple[LibraryItem, DiscogsSearchResult | None]]:
    """Enrich artwork results with release year, artist details, and streaming links.

    When library_db has a streaming_links table, uses direct URLs from the database.
    Falls back to search URLs when direct links are not available.

    ``http_client`` is the shared ``httpx.AsyncClient`` used for the iTunes
    Search probe. Passing ``None`` skips the Apple Music lookup — the
    orchestrator never instantiates its own client (issue #241).

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
    carrying only the streaming-URL fields (Apple via iTunes Search +
    Spotify/YT/BC/SC search-URL fallbacks). Album-derived scalars
    (release_year, artist_bio, wikipedia_url) and the ``extended=True``
    payload stay None on the synthesized result, preserving the positional-
    gating invariant above. The ``(release_id=0, release_url="")`` pair is
    a cross-service contract: Backend-Service (BS#1185) keys off of it to
    skip ``extractAlbumMetadata`` while still consuming streaming URLs.
    Required for the WXYC/BS#1184 fix — releases on Apple Music that aren't
    in the WXYC/Discogs catalog now surface streaming buttons on iOS.

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
        if top_artwork is None:
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
        artist = item.alternate_artist_name or item.artist or ""
        search_term = song or item.title or ""

        apple_music_result = (
            await _fetch_apple_music_url(artist, search_term, album=album, http_client=http_client)
            if (artist and search_term)
            else None
        )

        # Album-derived fields are positionally gated: only on top-1, and
        # only when top-1 actually has artwork. When ``artwork`` is None
        # here (this branch is also taken for the synthesized no-Discogs-
        # match streaming-only result), these stay None — preserves the
        # docstring's positional-gating invariant.
        is_album_derived_eligible = is_top1 and artwork is not None
        year_result = top1_year if is_album_derived_eligible else None
        artist_bio = top1_bio if is_album_derived_eligible else None
        wikipedia_url = top1_wiki if is_album_derived_eligible else None

        # Get streaming URLs: prefer direct links from DB, fall back to search URLs.
        # Runs unconditionally — releases that ARE on streaming but ISN'T
        # in the WXYC catalog (a Discogs miss) still surface streaming
        # buttons via the synthesized result below (LML#401 / BS#1184).
        spotify_url = None
        apple_music_override = None
        youtube_music_url = None
        bandcamp_url = None
        soundcloud_url = None

        if library_db and getattr(library_db, "_has_streaming_links", None) is True and item.id:
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

        # Fall back to search URLs for any service without a direct link
        if artist and search_term:
            if not spotify_url:
                spotify_url = _build_streaming_search_url(
                    "https://open.spotify.com/search/", artist, search_term
                )
            if not youtube_music_url:
                youtube_music_url = _build_streaming_search_url(
                    "https://music.youtube.com/search?q=", artist, search_term
                )
            if not bandcamp_url:
                bandcamp_url = _build_streaming_search_url(
                    "https://bandcamp.com/search?q=", artist, search_term
                )
            if not soundcloud_url:
                soundcloud_url = _build_streaming_search_url(
                    "https://soundcloud.com/search?q=", artist, search_term
                )

        update: dict[str, Any] = {
            "release_year": year_result,
            "artist_bio": artist_bio,
            "wikipedia_url": wikipedia_url,
            "spotify_url": spotify_url,
            "apple_music_url": apple_music_override or apple_music_result or None,
            "youtube_music_url": youtube_music_url,
            "bandcamp_url": bandcamp_url,
            "soundcloud_url": soundcloud_url,
        }

        # Extended fields land on the top-1 result only and require artwork
        # (same positional + artwork gating as the album-derived scalars).
        # The non-top-1 items keep their lean shape so non-iOS lookup
        # callers (request line, dj-site proxy, BS catalog) don't pay
        # payload bloat for results they ignore.
        if extended and is_album_derived_eligible:
            if top1_release is not None:
                update["discogs_artist_id"] = top1_release.artist_id
                update["tracklist"] = (
                    list(top1_release.tracklist) if top1_release.tracklist else None
                )
                update["genres"] = list(top1_release.genres) if top1_release.genres else None
                update["styles"] = list(top1_release.styles) if top1_release.styles else None
                update["label"] = top1_release.label
                update["full_release_date"] = top1_release.released
            update["artist_image_url"] = (
                top1_details.image_url if top1_details is not None else None
            )
            update["profile_tokens"] = top1_profile_tokens

        if artwork is None:
            # No Discogs match: try MusicBrainz for a tracklist before
            # synthesizing the streaming-only result. Same positional gate
            # as the rest of the extended payload — only the top-1 item is
            # eligible, and only when extended mode is on.
            mb_rescue_attempted = False
            mb_rescue_hit = False
            if is_top1 and extended and mb_pg is not None and item.artist and album:
                mb_rescue_attempted = True
                mb_tracklist = await resolve_tracklist_via_musicbrainz(
                    item.artist, album, mb_pg=mb_pg
                )
                if mb_tracklist:
                    mb_rescue_hit = True
                    update["tracklist"] = mb_tracklist
            _project_mb_rescue_attrs(mb_rescue_attempted, mb_rescue_hit)

            # See docstring's "Behavior change vs. v0.6.0 (LML#401)"
            # section for the BS#1185 sentinel contract.
            return (item, DiscogsSearchResult(release_id=0, release_url="", **update))

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
    if warm_cache and top1_bio:
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
    http_client: httpx.AsyncClient | None = None,
    caller_budget_ms: int | None = None,
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
    discogs_titles: dict[int, str] = {}
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
            parsed.artist = corrected
    else:
        with telemetry.track_step("album_lookup"):
            albums_for_search, song_not_found = await resolve_albums_for_track(
                parsed, discogs_service
            )

    # Step 3: Execute search strategy pipeline
    with telemetry.track_step("library_search"):
        # Strategies hold their own ``db`` handle (no per-call db arg on the
        # runner post-#399). The discogs_service is captured via ``partial`` on
        # the three strategies that need it; ARTIST_PLUS_ALBUM and
        # SWAPPED_INTERPRETATION are library-only.
        strategies = build_strategies(
            db,
            search_library_func=search_library_with_fallback,
            search_alternative_func=search_with_alternative_interpretation,
            search_compilations_func=partial(
                search_compilations_for_track, discogs_service=discogs_service
            ),
            search_song_as_artist_func=partial(
                search_song_as_artist, discogs_service=discogs_service
            ),
            search_song_as_track_func=partial(
                search_song_as_track, discogs_service=discogs_service
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

    # Step 3b: Validate results against Discogs track data.
    if library_results and parsed.song and parsed.artist:
        if not found_on_compilation:
            # Normal case: validate all results against Discogs tracklists
            with telemetry.track_step("track_validation"):
                validated = await filter_results_by_track_validation(
                    library_results, parsed.song, parsed.artist, discogs_service
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
                    promoted = await find_library_albums_with_cached_track(
                        db, parsed.song, parsed.artist, discogs_service
                    )
                    if promoted:
                        library_results = promoted
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
                library_results, discogs_service, discogs_titles
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
                library_db=db,
                http_client=http_client,
                extended=extended_mode,
                warm_cache=warm_cache_mode,
                discogs_cache=discogs_cache,
                mb_pg=mb_pg,
            )

    # Project the request-side flags onto the active Sentry transaction so
    # the trace can be filtered by request mode (lml.lookup.extended,
    # lml.lookup.warm_cache).
    try:
        scope = sentry_sdk.get_current_scope()
        if scope.transaction is not None:
            scope.transaction.set_data("lml.lookup.extended", extended_mode)
            scope.transaction.set_data("lml.lookup.warm_cache", warm_cache_mode)
    except Exception:
        # Observability must not break the request path.
        pass

    # Step 5: Build context message
    context = build_context_message(
        parsed, found_on_compilation, song_not_found, has_results=bool(library_results)
    )

    # Step 6: Resolve external identifiers for each result's artist
    identities_by_artist: dict[str, ReconciledIdentity] = {}
    if entity_store is not None and library_results:
        with telemetry.track_step("identity_resolution"):
            identities_by_artist = await _resolve_identities(
                [item.artist for item in library_results if item.artist], entity_store
            )

    def _identity_for(item: LibraryItem) -> ReconciledIdentity | None:
        if not item.artist:
            return None
        return identities_by_artist.get(item.artist)

    # Build response (convert internal models to API contract models)
    matched_via_by_id = search_state.matched_via_by_id
    result_items = []
    if items_with_artwork:
        for item, artwork in items_with_artwork:
            result_items.append(
                LookupResultItem(
                    library_item=item.to_catalog_item(),
                    artwork=artwork.to_match_result() if artwork else None,
                    reconciled_identity=_identity_for(item),
                    matched_via=matched_via_by_id.get(item.id),
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
