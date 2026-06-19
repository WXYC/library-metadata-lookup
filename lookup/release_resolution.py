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


def _rank(releases: list[ResolvedRelease], album: str | None) -> list[ResolvedRelease]:
    """Rank validated releases by title match to ``album`` (stable id tie-break).

    Deliberately scores TITLE ONLY (not ``find_best_typed_match``, whose artist
    floor is what this module bypasses — artist is settled by track-credit
    validation). When ``album`` is empty there is nothing to rank against, so
    the stable ``release_id`` ordering stands alone.
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
) -> list[ResolvedRelease]:
    """Find and validate the Discogs release(s) a track sits on.

    Probes Discogs by track (Wave A artist-field + Wave B keyword/compilation),
    merges Wave B's V/A compilations, validates each candidate's per-track credit
    via ``validate_track_on_release`` (which matches the per-track credit, not the
    release credit), and returns the validated releases ranked by title match to
    ``album``. Empty when nothing validates.
    """
    if discogs_service is None or not song or not artist:
        return []

    wave_a, wave_b = await asyncio.gather(
        discogs_service.search_releases_by_track(song, artist),
        discogs_service.search_releases_by_track(song, artist, artist_as_keyword=True),
    )
    candidates = merge_wave_b_compilations(list(wave_a.releases or []), list(wave_b.releases or []))

    validated: list[ResolvedRelease] = []
    for release in candidates:
        if not release.release_id:
            continue
        if not await validate_release_for_track(
            discogs_service, release.release_id, song, artist, source="release_resolution"
        ):
            continue
        validated.append(
            ResolvedRelease(
                release_id=release.release_id,
                release_url=release.release_url,
                is_compilation=bool(release.is_compilation),
                album_title=release.album,
            )
        )

    return _rank(validated, album)
