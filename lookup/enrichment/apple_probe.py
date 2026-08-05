"""LML#1101: the inline Apple Music live probe for the enrichment path.

Extracted from ``enrich_one`` (``lookup/enrichment/item.py``) to keep that file
under its module-size budget (``tests/unit/test_module_budgets.py``) — the same
extraction posture ``streaming_status.py`` took for LML#1053 and
``bandcamp_probe.py`` for LML#1098. Apple was the last inline probe still
written out longhand in ``enrich_one``; it is now a sibling module of the
Bandcamp probe, and LML#1103's YouTube Music leg becomes a third sibling rather
than a third bespoke block.

**Why a sibling module and not a config row.** LML#1101 was filed against
``204a86b``, before #1098 merged, and predicted that #1098 would add a
structurally-parallel Bandcamp *block* to ``enrich_one`` — two bespoke blocks
that one config-driven engine could collapse into registry rows. #1098 instead
extracted its probe to its own module, and reading the two side by side shows
they share a skeleton, not a body: Apple probes the TRACK cache
(``lml_cache.track_streaming_url_cache``, keyed on ``(service, artist, album,
song)``) by calling the client directly, returns a rich
``AppleMusicTrackMatch`` feeding three separate downstream consumers in
``enrich_one`` (artwork, release year, and the LML#505 sibling-invalidation
that clears five URL slots), runs on the interactive AND bulk paths gated only
by client injection, and classifies a timeout as clamped-vs-genuine with four
Sentry transaction keys; Bandcamp probes the ALBUM cache through
``resolve_streaming_url_with_cache(fail_fast=True)``, returns a bare URL, runs
bulk-and-top-1-only, and classifies breaker/429/transport sheds onto a PostHog
counter. Only ~20 lines are genuinely common (read a flag, clamp the timeout,
``wait_for``, map the outcome to a ``StreamingResolutionStatus``). A config
carrying the rest would need roughly a dozen fields of which most serve exactly
one row — a dispatch table, which is the same ``if`` chain with an indirection
in front of it.

The registry the issue wants is real, but its family boundary is
``resolve_streaming_url_with_cache``-shaped ALBUM probes — Bandcamp today, YTM
via LML#1103, Spotify via LML#1052 — not "every inline probe". Apple is the
outlier on every axis above. That family has one member right now, so the
extraction becomes correct at #1103, with three parallel rows, and Apple stays
bespoke because it genuinely is. See the LML#1101 discussion for the full
comparison.

Pure motion out of ``item.py``: no behavior change.
"""

from __future__ import annotations

import asyncio
import logging
from typing import NamedTuple

import sentry_sdk

from clients.streaming.apple_music import AppleMusicTrackMatch
from config.settings import Settings
from entity.track_streaming_url_cache import (
    APPLE_MUSIC_TRACK_SERVICE,
    get_cached_track_streaming_url,
    set_cached_track_streaming_url,
)
from generated.api_models import StreamingResolutionStatus
from lookup.enrichment.context import EnrichmentContext
from lookup.spine_deadline import clamp_probe_timeout_for
from lookup.timeouts import apple_music_lookup_timeout_s

logger = logging.getLogger(__name__)


class AppleProbeResult(NamedTuple):
    """The probe's effect on ``enrich_one``'s Apple-derived slots.

    Five values cross this seam, and ``enrich_one`` consumes each separately:

    ``apple_music_url`` — the resolved deep-link, or ``None``. Feeds the final
    ``apple_music_url or apple_music_override or None`` assignment.

    ``status`` — the LML#1053 per-service verdict: ``verified`` (resolved, from
    a live hit or an L1 track-cache hit), ``absent`` (the probe ran to
    completion and found no match — terminal), ``unresolved`` (timeout or
    swallowed exception — transient, a couldn't-ask), or ``None`` when the
    probe did not run at all (never-consulted, and the key is then omitted from
    the wire ``StreamingResolution`` entirely).

    ``probe_match`` — the raw ``AppleMusicTrackMatch`` from the synthesis path,
    or ``None``. ``enrich_one`` reads its ``album_verified`` flag for the
    LML#505 sibling-row override invalidation; that gate is load-bearing, so
    the object is returned rather than only its extracted fields.

    ``artwork_url`` / ``release_year`` — the LML#487 synthesis-path fields,
    non-``None`` only when ``probe_match`` is.
    """

    apple_music_url: str | None
    status: StreamingResolutionStatus | None
    probe_match: AppleMusicTrackMatch | None
    artwork_url: str | None
    release_year: int | None


async def run_apple_music_probe(
    ctx: EnrichmentContext,
    *,
    settings: Settings,
    # ``str``, not ``str | None``: ``enrich_one`` derives both as
    # ``... or ""``, so "missing" arrives as the empty string and the
    # falsiness checks below (rather than ``is not None``) are what gate the
    # probe — same as at the pre-LML#1101 call site.
    row_artist: str,
    search_term: str,
    library_row_acceptable: bool,
    # A pre-computed gate, NOT the override URL (LML#1101 review). The probe
    # has no business knowing that a librarian override exists, that it wins
    # the Apple slot, or that the synthesis path is exempt — that precedence
    # rule is ``enrich_one``'s, and it is already encoded there and in
    # ``resolve_streaming_status``. Handing the probe the URL would make three
    # modules co-own one rule with no single owner; handing it a bool leaves
    # it a pure did-we-run gate.
    skip_happy_probe: bool,
) -> AppleProbeResult:
    """Resolve an Apple Music deep-link for one item — bounded, L1-cache-first.

    ``library_row_acceptable`` picks ``find_track_url`` (URL only — preserves
    the LML#401 baseline); the synthesis path (LML#487) needs artwork + year
    too, so calls ``find_track_metadata``. Both make one ``search_song`` call,
    so per-request quota is identical to the LML#401 baseline. The happy-path
    probe is skipped when the librarian override would win the slot anyway
    (saves one Apple call per overridden item). The ``asyncio.wait_for``
    ceiling caps single-call latency under 429/5xx pressure (LML#449/#450). On
    timeout/error we degrade to no-Apple-anything (LML#444 swallow). The Sentry
    data key encodes the *method* so LML#462 dashboards can distinguish
    synthesis-path timeouts (artwork still leaking, high impact) from
    happy-path timeouts (URL only, low impact).
    """
    apple_music_url: str | None = None
    probe_match: AppleMusicTrackMatch | None = None
    probe_artwork_url: str | None = None
    probe_release_year: int | None = None
    # LML#1053: this item's Apple-leg resolution verdict. ``None`` until some
    # signal below sets it — stays ``None`` (never-consulted, key omitted from
    # the final ``StreamingResolution``) when no override/cache-hit/probe ever
    # ran. Finalized (override precedence) by ``resolve_streaming_status``.
    apple_status: StreamingResolutionStatus | None = None
    probe_method = "find_track_metadata" if not library_row_acceptable else "find_track_url"

    # L1 (LML#893): cache-first Apple track probe on the happy path. When the
    # lookup carries a played track (``ctx.song``), peek a track-scoped cache
    # (``lml_cache.track_streaming_url_cache``, keyed on
    # ``(service, artist, album, song)``) before paying the ~4.8s live
    # ``find_track_url``. A HIT returns the cached exact-track deep-link and
    # skips the probe; a MISS runs the probe live (first-lookup URL preserved)
    # and writes the resolved deep-link back to the TRACK cache. Both the peek
    # and the write are gated on the kill-switch flags — an incident flip
    # disables L1's cache read/write. Track deep-links live ONLY in this table:
    # never the album-keyed cache (the PR #898 poisoning bug). The synthesis
    # path (``find_track_metadata``) is untouched — track-absent lookups fall
    # back to today's album/post-process behavior.
    track_cache_enabled = (
        settings.lml_persist_streaming_urls and settings.lml_persist_streaming_url_apple_music
    )
    l1_eligible = bool(
        library_row_acceptable
        and ctx.apple_music is not None
        and row_artist
        and ctx.song
        and ctx.discogs_cache_pg is not None
        and track_cache_enabled
        and not skip_happy_probe
    )
    track_cache_hit = False
    if l1_eligible:
        # Narrowed by ``l1_eligible``; asserted for mypy + as a runtime contract.
        assert ctx.discogs_cache_pg is not None
        assert ctx.song is not None
        cached_track_url = await get_cached_track_streaming_url(
            ctx.discogs_cache_pg,
            service=APPLE_MUSIC_TRACK_SERVICE,
            artist=row_artist,
            album=ctx.album,
            song=ctx.song,
        )
        if cached_track_url is not None:
            apple_music_url = cached_track_url
            track_cache_hit = True
            apple_status = StreamingResolutionStatus.verified

    if (
        ctx.apple_music is not None
        and row_artist
        and search_term
        and not skip_happy_probe
        and not track_cache_hit
    ):
        # LML#930: clamp the per-item Apple probe's fixed 4s ceiling to the
        # remaining caller budget so one slow item can't burn the whole tail
        # past the caller's window. With no deadline (a direct
        # ``enrich_artwork_results`` caller) or no caller-budget header, this
        # returns the unbounded ceiling unchanged.
        base_apple_timeout_s = apple_music_lookup_timeout_s()
        apple_probe_timeout_s = clamp_probe_timeout_for(ctx.spine_deadline, base_apple_timeout_s)
        try:
            if library_row_acceptable:
                apple_music_url = await asyncio.wait_for(
                    ctx.apple_music.find_track_url(row_artist, search_term, album=ctx.album),
                    timeout=apple_probe_timeout_s,
                )
                # LML#1053: the probe ran to completion — a hit is verified, a
                # clean no-match is absent (terminal, not a couldn't-check).
                apple_status = (
                    StreamingResolutionStatus.verified
                    if apple_music_url is not None
                    else StreamingResolutionStatus.absent
                )
                # L1 write-back: persist a freshly-resolved track deep-link so
                # the next lookup of this ``(artist, album, song)`` is a HIT.
                # Never persist a null (#782/BS#1192) — guarded on a non-null
                # URL and the same kill-switch flags as the peek.
                if l1_eligible and apple_music_url is not None:
                    assert ctx.discogs_cache_pg is not None
                    assert ctx.song is not None
                    await set_cached_track_streaming_url(
                        ctx.discogs_cache_pg,
                        service=APPLE_MUSIC_TRACK_SERVICE,
                        artist=row_artist,
                        album=ctx.album,
                        song=ctx.song,
                        url=apple_music_url,
                    )
            else:
                probe_match = await asyncio.wait_for(
                    ctx.apple_music.find_track_metadata(row_artist, search_term, album=ctx.album),
                    timeout=apple_probe_timeout_s,
                )
                # LML#1053: same verdict rule as the happy path above — the
                # probe ran to completion either way.
                apple_status = (
                    StreamingResolutionStatus.verified
                    if probe_match is not None
                    else StreamingResolutionStatus.absent
                )
                if probe_match is not None:
                    apple_music_url = probe_match.url
                    probe_artwork_url = probe_match.artwork_url
                    probe_release_year = probe_match.release_year
        except TimeoutError:
            # LML#1053: attempted but inconclusive — transient, not terminal.
            # Covers both the genuine-outage and the self-inflicted
            # deadline-clamped shed cases below; both leave this request with
            # no confirmed verdict either way.
            apple_status = StreamingResolutionStatus.unresolved
            # LML#930 PR2: a `wait_for` timeout self-inflicted by the deadline
            # clamp above (`apple_probe_timeout_s < base_apple_timeout_s`) is
            # NOT an Apple outage — it's the budget running out, and at a
            # near-zero clamp (e.g. ~0.01s) it fires near-instantly on every
            # row, flooding the log with a signal that reads as "Apple is
            # down" when it's self-inflicted deadline pressure. Only a
            # genuine unclamped (full-ceiling) timeout still WARNs.
            deadline_clamped = apple_probe_timeout_s < base_apple_timeout_s
            if deadline_clamped:
                logger.debug(
                    "AppleMusicClient.%s timed out for %s - %s "
                    "(deadline-clamped to %.3fs, not an Apple outage)",
                    probe_method,
                    row_artist,
                    search_term,
                    apple_probe_timeout_s,
                )
            else:
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
            # synthesis-path timeouts from happy-path timeouts; the LML#930
            # ``.deadline_clamped`` key disambiguates a self-inflicted
            # deadline-clamped timeout from a genuine Apple-side one; the
            # ``.probe_timeout_s`` key records the effective clamp magnitude so
            # a DEBUG-demoted clamped timeout stays diagnosable — a value near
            # the base ceiling is a likely genuine Apple slowness, a near-zero
            # value is self-inflicted deadline pressure.
            try:
                transaction = sentry_sdk.get_current_scope().transaction
                if transaction is not None:
                    transaction.set_data("apple_music.find_track_url.timeout", True)
                    transaction.set_data("apple_music.timeout.method", probe_method)
                    transaction.set_data("apple_music.timeout.deadline_clamped", deadline_clamped)
                    transaction.set_data(
                        "apple_music.timeout.probe_timeout_s", round(apple_probe_timeout_s, 3)
                    )
            except Exception as e:
                logger.warning(
                    "Failed to project apple_music timeout onto Sentry transaction: %s",
                    e,
                )
        except Exception:
            # LML#1053: the LML#444 swallow-and-degrade path — attempted but
            # inconclusive, same bucket as a timeout.
            apple_status = StreamingResolutionStatus.unresolved
            logger.exception(
                "AppleMusicClient.%s raised for %s - %s",
                probe_method,
                row_artist,
                search_term,
            )

    return AppleProbeResult(
        apple_music_url, apple_status, probe_match, probe_artwork_url, probe_release_year
    )
