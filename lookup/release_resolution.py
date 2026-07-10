"""Release resolution — find and validate the Discogs release a track sits on.

Owns the probe set (Discogs release search by track + per-candidate track-credit
validation) and ranking (title match to album, stable release_id tie-break).
Does NOT own row matching (library search, artist/compilation filtering) — the
search strategies own that. Called by TRACK_ON_COMPILATION, SONG_AS_TRACK, and
(in a later change) the binding step's lazy fallback.

Deliberately a leaf module: it imports only from ``discogs`` / ``clients`` /
``wxyc_etl``, never from ``core`` or ``lookup``. Keeping it import-acyclic is
what lets ``core.search`` reference ``ResolvedRelease`` on the widened
``discogs_titles`` seam without a circular import.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass
from typing import Any

import sentry_sdk
from wxyc_etl.text import is_compilation_artist

from clients.streaming.matching import score_match
from discogs.breaker import BreakerState, DiscogsBreakerOpenError
from discogs.memory_cache import async_cached, get_release_resolution_cache
from discogs.models import ReleaseInfo
from discogs.ratelimit import get_discogs_breaker
from discogs.service import DiscogsService

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResolvedRelease:
    """A Discogs release validated to contain a specific track.

    Carried on the internal ``discogs_titles`` seam (``dict[int,
    ResolvedRelease]``) from the search strategies to the artwork-binding step.
    ``album_title`` preserves the value the seam carried as a bare string before
    the type was widened.
    """

    release_id: int
    release_url: str
    is_compilation: bool
    album_title: str
    # Confidence the binding step stamps on the surfaced result. Defaults to the
    # full 1.0 (a track-validated, album-ranked carry-through). The cached-track
    # safety net (LML#629 A4) sets it soft because it picks by track only and
    # never album-matches — so the soft value must ride the seam to the bind,
    # which otherwise can't distinguish A4 from an album-ranked carry-through.
    confidence: float = 1.0


def merge_wave_b_compilations(
    wave_a: list[ReleaseInfo], wave_b: list[ReleaseInfo]
) -> list[ReleaseInfo]:
    """Merge Wave B's V/A compilations into Wave A, unless Wave A already has one.

    Lifted from ``search_compilations_for_track`` (WXYC#527). Wave A is the
    artist-field probe; Wave B is the keyword probe filtered to
    ``format=Compilation``. Gating on ``r.is_compilation`` alone over-suppresses
    — single-artist retrospectives are flagged ``Compilation`` too — so the
    merge only fires when Wave A surfaced no *true V/A* hit (compilation AND a
    compilation-artist credit), and only adds Wave B rows that are themselves
    V/A compilations and not already present by album title.
    """
    merged = list(wave_a)
    has_va_compilation = any(
        r.is_compilation and is_compilation_artist(r.artist or "") for r in merged
    )
    if not has_va_compilation:
        seen_album_keys = {r.album.lower() for r in merged}
        for r in wave_b:
            if r.is_compilation and r.album.lower() not in seen_album_keys:
                merged.append(r)
                seen_album_keys.add(r.album.lower())
    return merged


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
    request proceeds. Same swallow pattern as ``_log_album_title_fallback``
    / ``_log_resolver_pre_pass``.

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


async def validate_release_for_track(
    discogs_service: DiscogsService,
    release_id: int,
    song: str,
    artist: str,
    *,
    source: str,
) -> bool:
    """Validate a track's per-track credit on a release, with audit telemetry.

    The shared primitive both ``TRACK_ON_COMPILATION`` (``process_release``)
    and ``SONG_AS_TRACK`` (``_validate_one``) delegate to — wrapping
    ``validate_track_on_release`` with the LML#344 timing + ``source``-labelled
    ``_log_track_validation`` breadcrumb that was previously duplicated at each
    site. Callers keep their own guards (release_id truthiness, artist presence)
    and skip-logging; this owns the validate-and-record step.
    """
    start = time.monotonic()
    verdict = await discogs_service.validate_track_on_release(release_id, song, artist)
    _log_track_validation(
        source=source,
        release_id=release_id,
        song=song,
        artist=artist,
        verdict=verdict,
        latency_ms=(time.monotonic() - start) * 1000,
    )
    return verdict


def prerank_candidates_for_validation(
    candidates: list[ReleaseInfo], album: str | None
) -> list[ReleaseInfo]:
    """Order probe candidates so the likeliest match is validated first (LML#633).

    The bounded sequential-early-exit path validates candidates in this order and
    returns the first that passes, so the ordering decides how few
    ``validate_track_on_release`` calls the happy path costs.

    With an ``album`` to match, this applies the same TITLE-only signal
    ``rank_resolved_releases`` applies *after* validation — moved *before* it so
    the top title match is tried first (stable ``release_id`` tie-break). With no
    ``album`` there is nothing to title-rank, so it falls back to the #629
    no-album rule: prefer an own-release (``is_compilation=False``) over a
    compilation, then stable ``release_id``. The non-bounded path does not call
    this — it validates every candidate and ranks ``rank_resolved_releases``
    after, so its ordering is unchanged.
    """
    album_query = (album or "").strip()
    if not album_query:
        # Deprioritize only TRUE Various-Artists compilations — the ``Compilation``
        # flag AND a compilation-artist credit ("Various") — not single-artist
        # retrospectives, which Discogs also flags ``Compilation`` yet are the
        # canonical home of an artist's original tracks. Gating on
        # ``is_compilation`` alone sank a retrospective below a later plain single
        # carrying only a remix (the "Me & Mr Jones by Plug" case). Mirrors the
        # true-V/A test in ``merge_wave_b_compilations``. ``is_compilation`` is
        # ``bool | None``; ``bool()`` coerces so a ``None`` never lands in the sort
        # tuple (``None < False`` raises TypeError).
        return sorted(
            candidates,
            key=lambda r: (
                bool(r.is_compilation) and is_compilation_artist(r.artist or ""),
                r.release_id,
            ),
        )
    return sorted(
        candidates,
        key=lambda r: (-score_match(album_query, r.album), r.release_id),
    )


def rank_resolved_releases(
    releases: list[ResolvedRelease], album: str | None
) -> list[ResolvedRelease]:
    """Rank validated releases by title match to ``album`` (stable id tie-break).

    Deliberately scores TITLE ONLY (not ``find_best_typed_match``, whose artist
    floor is what this module bypasses — artist is settled by track-credit
    validation). When ``album`` is empty there is nothing to rank against, so
    the stable ``release_id`` ordering stands alone.

    Public so the carried path (``search_compilations_for_track``) can rank its
    per-item releases the same way the lazy fallback ranks its candidate list,
    keeping the two binding paths in agreement (LML#604 deferred finding #2).
    """
    album_query = (album or "").strip()
    if not album_query:
        return sorted(releases, key=lambda r: r.release_id)
    return sorted(
        releases,
        key=lambda r: (-score_match(album_query, r.album_title), r.release_id),
    )


async def resolve_release_for_track(
    song: str,
    artist: str,
    album: str | None,
    discogs_service: DiscogsService | None,
    *,
    also_probe_album_title: bool = False,
    max_validations: int | None = None,
) -> list[ResolvedRelease]:
    """Find and validate the Discogs release(s) a track sits on.

    Probes Discogs by track (Wave A artist-field + Wave B keyword/compilation),
    merges Wave B's V/A compilations, validates each candidate's per-track credit
    via ``validate_track_on_release`` (which matches the per-track credit, not the
    release credit), and returns the validated releases ranked by title match to
    ``album``. Empty when nothing validates.

    ``max_validations`` selects between two validation modes (LML#633):

    - **Default (``None``) — validate-all, rank-after.** Every candidate is
      validated; the survivors are ranked by ``rank_resolved_releases``. This is
      the original behavior the #604 lazy-bind callers depend on, kept
      byte-for-byte; they pass no bound.
    - **Bounded (an int) — sequential-early-exit.** Candidates are pre-ranked by
      ``prerank_candidates_for_validation`` (the title signal moved *before*
      validation), validated in that order, and the **first** that validates is
      returned — stopping after at most ``max_validations`` attempts. This is the
      cold-cache cost cap #628 points at every non-library add: a popular track
      with 20-40 candidates costs 1 validate call on the happy path (top title
      match validates) instead of 20-40, and the N=5 backstop bounds the worst
      case. Sequential (not concurrent) to minimize Discogs quota — the binding
      constraint — and degrade gently under the fallthrough seam's rate-limit
      cool-down. (See the #625 decision record.)

    When ``also_probe_album_title`` is set and ``album`` is non-empty, a third
    album-title probe (``search_releases_by_album_title``) joins the Wave A/B
    gather — parity with ``search_compilations_for_track``'s album-title wave
    (#319/#237). It surfaces releases the track-artist probe misses (trio
    collaborations; V/A comps whose track-artist credit Discogs files oddly).
    Its candidates are deduped by album title against Wave A/B and gated by the
    same per-track ``validate_track_on_release``. Callers fire it only when the
    artist was *not* canonically swapped (a high-confidence swap makes the
    artist-scoped probe authoritative), mirroring the strategy's gate.

    Degrades gracefully on Discogs failures, matching the resilience of the
    ``search_compilations_for_track`` path this was lifted from: a probe failure
    yields an empty list (no candidates to resolve), and a single candidate's
    validation failure drops only that candidate — releases already validated in
    the same call survive. The live-worker caller (PR2's lazy bind) must never
    have a transient Discogs error abort the whole request.
    """
    if discogs_service is None or not song or not artist:
        return []

    # LML#755 FIX 1: if the saturation breaker is already OPEN, every live probe
    # below would shed. Raise ``DiscogsBreakerOpenError`` *before* probing so the
    # shed propagates as "couldn't ask" rather than being laundered (by
    # ``search_releases_by_track``'s never-abort swallow) into a ``[]`` that the
    # L1 ``@async_cached`` wrapper would memoize and the row-less binder would
    # pin as a 7-day known-miss. ``@async_cached`` never caches on an exception,
    # so this keeps a shed out of the durable negative caches. (The
    # ``allow_request`` epoch is not consumed here — we only read state — because
    # this is a pre-flight guard, not a request that will record an outcome.)
    if get_discogs_breaker().state is BreakerState.OPEN:
        raise DiscogsBreakerOpenError(
            f"Discogs saturation breaker open; not resolving {song!r} by {artist!r}"
        )

    fire_album = also_probe_album_title and bool(album)

    async def _album_title_probe_safe() -> Any:
        """Catch-and-return wrapper so a Discogs failure on the album-title probe
        doesn't take down the artist-scoped track probes sharing the gather —
        mirrors ``search_compilations_for_track._album_title_probe_safe``. Returns
        ``None`` on failure; the track waves alone then gate the empty path."""
        assert album is not None  # narrowed by ``fire_album``
        try:
            return await discogs_service.search_releases_by_album_title(album)
        except Exception as exc:
            logger.warning("Release-resolution album-title probe failed for %r: %s", album, exc)
            return None

    try:
        if fire_album:
            wave_a, wave_b, album_response = await asyncio.gather(
                discogs_service.search_releases_by_track(song, artist),
                discogs_service.search_releases_by_track(song, artist, artist_as_keyword=True),
                _album_title_probe_safe(),
            )
        else:
            wave_a, wave_b = await asyncio.gather(
                discogs_service.search_releases_by_track(song, artist),
                discogs_service.search_releases_by_track(song, artist, artist_as_keyword=True),
            )
            album_response = None
    except Exception as exc:
        logger.warning("Release-resolution probe failed for %r by %r: %s", song, artist, exc)
        return []
    candidates = merge_wave_b_compilations(list(wave_a.releases or []), list(wave_b.releases or []))
    if album_response is not None:
        # Append album-title candidates not already present by release_id. Dedup
        # by id (not title) so a DISTINCT same-titled pressing survives to
        # per-track validation — a track-wave release that fails validation must
        # not suppress a same-titled album-wave release that would pass. Unlike
        # the Wave B merge this is not V/A-only: the #237 trio repro is *not* a
        # compilation, so per-track validation (not a compilation flag) gates
        # precision here.
        seen_ids = {r.release_id for r in candidates if r.release_id}
        for r in album_response.releases or []:
            if r.release_id and r.release_id not in seen_ids:
                candidates.append(r)
                seen_ids.add(r.release_id)

    async def _validate_one(release: ReleaseInfo) -> ResolvedRelease | None:
        """Validate one candidate's per-track credit; return the ``ResolvedRelease``
        on a pass, ``None`` on a fail or a swallowed validation error (a single
        candidate's failure must never abort the whole call)."""
        try:
            is_valid = await validate_release_for_track(
                discogs_service, release.release_id, song, artist, source="release_resolution"
            )
        except Exception as exc:
            logger.warning(
                "Release-resolution validation failed for release %s: %s", release.release_id, exc
            )
            return None
        if not is_valid:
            return None
        return ResolvedRelease(
            release_id=release.release_id,
            release_url=release.release_url,
            is_compilation=bool(release.is_compilation),
            album_title=release.album,
        )

    if max_validations is not None:
        # Bounded sequential-early-exit (LML#633): pre-rank, validate in order,
        # return the first that passes, stop after ``max_validations`` attempts.
        # Falsy-id rows can never be validated, so drop them before counting the
        # budget — they must not consume an attempt slot.
        ranked = prerank_candidates_for_validation([r for r in candidates if r.release_id], album)
        for release in ranked[:max_validations]:
            resolved = await _validate_one(release)
            if resolved is not None:
                return [resolved]
        return []

    # Default: validate-all, rank-after (the #604 lazy-bind contract).
    validated: list[ResolvedRelease] = []
    for release in candidates:
        if not release.release_id:
            continue
        resolved = await _validate_one(release)
        if resolved is not None:
            validated.append(resolved)

    return rank_resolved_releases(validated, album)


@async_cached(get_release_resolution_cache())
async def resolve_release_for_track_cached(
    discogs_service: DiscogsService | None,
    song: str,
    artist: str,
    album: str | None,
    also_probe_album_title: bool = False,
) -> list[ResolvedRelease]:
    """L1-cached ``resolve_release_for_track`` (LML#604 negative-cache guard).

    Wraps the uncached resolver in the shared ``@async_cached`` null-pinning +
    request-coalescing pattern. The empty-list result for an unbindable row is
    cached — the decorator's write guard skips only ``None`` and the resolver
    returns ``[]`` (never ``None``) when nothing validates — so a steady poll of
    an unbindable compilation does not re-probe Discogs every time. This is the
    LML#370-372 cascade-shape guard; the binding step's lazy fallback must call
    *this*, not the uncached resolver.

    ``discogs_service`` leads the signature deliberately. ``@async_cached`` keys
    on the full argument tuple, but the service is a per-process singleton, so
    the effective key is ``(song, artist, album, also_probe_album_title)`` —
    folded through ``to_match_form`` by ``make_normalized_cache_key`` so
    diacritic/case variants collapse to a single entry. ``also_probe_album_title``
    joins the key so swapped (no album wave) and unswapped (album wave) probes
    for the same track cache independently.
    """
    return await resolve_release_for_track(
        song, artist, album, discogs_service, also_probe_album_title=also_probe_album_title
    )
