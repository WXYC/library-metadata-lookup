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
from discogs.memory_cache import async_cached, get_release_resolution_cache
from discogs.models import ReleaseInfo
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
) -> list[ResolvedRelease]:
    """Find and validate the Discogs release(s) a track sits on.

    Probes Discogs by track (Wave A artist-field + Wave B keyword/compilation),
    merges Wave B's V/A compilations, validates each candidate's per-track credit
    via ``validate_track_on_release`` (which matches the per-track credit, not the
    release credit), and returns the validated releases ranked by title match to
    ``album``. Empty when nothing validates.

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

    validated: list[ResolvedRelease] = []
    for release in candidates:
        if not release.release_id:
            continue
        try:
            is_valid = await validate_release_for_track(
                discogs_service, release.release_id, song, artist, source="release_resolution"
            )
        except Exception as exc:
            logger.warning(
                "Release-resolution validation failed for release %s: %s", release.release_id, exc
            )
            continue
        if not is_valid:
            continue
        validated.append(
            ResolvedRelease(
                release_id=release.release_id,
                release_url=release.release_url,
                is_compilation=bool(release.is_compilation),
                album_title=release.album,
            )
        )

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
