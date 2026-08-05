"""LML#1098: the inline Bandcamp live probe for the enrichment path.

Extracted from ``enrich_one`` (``lookup/enrichment/item.py``) to keep that file
under its module-size budget (``tests/unit/test_module_budgets.py``) — the same
extraction posture ``streaming_status.py`` took for LML#1053. The probe is a
self-contained concern: given the enrichment context + the current Bandcamp URL
slot, it either resolves a DIRECT Bandcamp album URL synchronously (bounded,
cache-first, single fail-fast call) or leaves the slot to the deferred
search-URL fallback, returning the per-service resolution verdict either way.

Why synchronous, and why only here: the enrichment (``/lookup/bulk``) path is
the CDC worker call a DJ's flowsheet entry rides. Apple Music is the only
service with a live probe there today; this gives Bandcamp the same
first-time-fill treatment, modeled on that probe but with Bandcamp's
rate-limit guardrails (LML#1106's fail-fast mode + saturation breaker). It is
NOT added to the interactive ``/lookup`` hot path — that is exactly the
unbounded-inline shape LML#573/#651 rolled back.
"""

from __future__ import annotations

import asyncio
import logging
from typing import NamedTuple

from wxyc_fastapi.observability import get_posthog_client

from clients.bandcamp import BandcampRateLimitedError, BandcampTransportError
from config.settings import Settings
from core.exceptions import BreakerOpenError
from entity.streaming_url_cache import resolve_streaming_url_with_cache
from generated.api_models import StreamingResolutionStatus
from lookup.enrichment.context import EnrichmentContext
from lookup.streaming_url_postprocess import (
    STREAMING_URL_CACHE_CONFIG,
    should_suppress_streaming_warm,
)
from lookup.streaming_warm import _mint_identity
from lookup.timeouts import bandcamp_probe_timeout_s
from streaming.service import ALBUM_CACHE_KEYS, StreamingService

logger = logging.getLogger(__name__)

# The album-cache storage key for Bandcamp ("bandcamp") — derived from the
# shared enum (not a free-floating literal) so the live probe reads and writes
# the SAME ``lml_cache.album_streaming_url_cache`` rows as the streaming
# post-process and the LML#1069 offline drain.
_BANDCAMP_ALBUM_SERVICE = ALBUM_CACHE_KEYS[StreamingService.BANDCAMP]

# The Bandcamp cache/mint registry entry -- reused by the FIX 4 mint call
# below instead of a re-indexed literal at each call site.
_BANDCAMP_CACHE_CONFIG = STREAMING_URL_CACHE_CONFIG[StreamingService.BANDCAMP]

_SHED_EVENT = "bandcamp_live_probe_shed"
_SHED_POSTHOG_EVENT_PREFIX = "bandcamp_live_probe"
_SHED_POSTHOG_DISTINCT_ID = "library-metadata-lookup-service"

# LML#1106 review round 2, FIX C: unlike the resolve-with-cache call above
# (bounded by its own asyncio.wait_for, clamped to the caller budget), this
# mint's PG write has nothing else bounding it -- enrich_metadata runs as a
# bare ``await run_step()`` (not run_within_spine_deadline), should_shed_tail
# only checks BETWEEN steps, and PgSource.acquire passes no ``timeout=`` to
# pool.acquire(). An exhausted pool (lock contention on
# entity.release_identity) would otherwise pin this response-path item
# indefinitely. A fixed, local ceiling (not env-tunable -- this is a
# best-effort local PG write, not a knob operators need to retune) is enough:
# see run_bandcamp_live_probe's mint call site for the failure posture (log
# and continue, never fail the lookup).
_MINT_TIMEOUT_S = 3.0


class BandcampProbeResult(NamedTuple):
    """The probe's effect on the Bandcamp slot.

    ``bandcamp_url`` — the (possibly unchanged) URL slot: the resolved direct
    album URL on a hit, else the ``current_bandcamp_url`` passed in.

    ``status`` — the LML#1053 per-service verdict: ``verified`` (resolved),
    ``absent`` (sourced no-match, terminal), ``unresolved`` (shed/timeout,
    transient — the probe answered NOTHING and wrote NOTHING), or ``None``
    when the probe did not run at all (never-consulted). See
    :func:`probe_owns_bandcamp_leg` (LML#1106 review, FIX 3) for which of
    these should withhold the client from the streaming post-process —
    ``status is not None`` is NOT the right predicate for that: it also
    matches ``unresolved``, which has no fill and no cache write to protect.
    """

    bandcamp_url: str | None
    status: StreamingResolutionStatus | None


def probe_owns_bandcamp_leg(status: StreamingResolutionStatus | None) -> bool:
    """Whether ``status`` (a :class:`BandcampProbeResult`'s ``status``) is a
    SOURCED verdict that should withhold the client from the streaming
    post-process (``lookup/enrichment/item.py``'s ``apply_streaming_url_
    postprocess`` call).

    Only ``verified`` and ``absent`` qualify (LML#1106 review, FIX 3): the
    probe actually produced -- and for ``absent``, cache-wrote -- a verdict,
    so handing the same service to the post-process too would be redundant
    (or, worse, let ``postprocess_status`` clobber it). ``unresolved`` (a
    breaker shed, 429, transport failure, or timeout) and ``None``
    (never-consulted) are NOT sourced: ``unresolved`` in particular answered
    nothing and wrote nothing to the cache, so withholding the client anyway
    would leave the item with no fill AND no #1087 background warm — the
    next lookup of the same album is equally cold and sheds again, strictly
    WORSE than leaving the probe off entirely (where #1087 alone would have
    queued a retrying off-path warm).
    """
    return status in (StreamingResolutionStatus.verified, StreamingResolutionStatus.absent)


async def run_bandcamp_live_probe(
    ctx: EnrichmentContext,
    *,
    settings: Settings,
    current_bandcamp_url: str | None,
    is_top1: bool,
) -> BandcampProbeResult:
    """Resolve a DIRECT Bandcamp album URL inline — bounded, cache-first.

    Runs only when every precondition holds:
      * ``is_top1`` — the probe keys on the REQUEST-level ``(ctx.artist,
        ctx.album)``, identical across every item ``enrich_one`` is called
        for. Without this gate, a response with N results (up to
        ``MAX_SEARCH_RESULTS``) fires N identical concurrent resolves — up to
        3 HTTP calls each, no dedup (the warm path this displaced had
        ``_streaming_warm_in_flight``; the probe has none) — and a later
        probe can exceed the shared breaker's ceiling, so one response could
        carry a direct URL on one row and a search URL on its siblings
        (LML#1106 review, FIX 2). Matches ``enrich_artwork_results``'s own
        documented top-1-only convention ("BS/iOS only consume the top-1
        result, so paying N round-trips was waste"). A non-top-1 item is
        simply unprobed — its slot falls through to the deferred search-URL
        fallback in ``enrich_one``, never a wrong/stale verdict;
      * **the bulk enrichment path** — ``should_suppress_streaming_warm()`` is
        set ``True`` only by the bulk handler (``router.py``), so it is the
        reliable "am I on ``/lookup/bulk``" signal. The interactive ``/lookup``
        path injects the same Bandcamp client and reads the same flag, so
        without this gate a globally-set flag would run this synchronous probe
        on the interactive hot path — the exact LML#573/#651 regression;
      * ``lml_bandcamp_live_probe`` on, AND-gated with the two persist
        kill-switches (``lml_persist_streaming_urls`` +
        ``lml_persist_streaming_url_bandcamp``) so an incident flip restores
        exactly today's behavior;
      * a Bandcamp client and a PG source are wired;
      * the request carries an album (Bandcamp deep-links are album-level);
      * no verified URL already won the slot (a librarian override or the
        LML#505 sibling-invalidation left it empty) — we never overwrite a
        verified value.

    On a run it delegates to LML#1106's ``resolve_streaming_url_with_cache(
    fail_fast=True)`` under an ``asyncio.wait_for`` ceiling clamped to the
    caller budget (LML#930 parity): a cache hit / live resolve -> ``verified``
    with the URL (``live_resolved`` also mints the external_id into
    ``entity.release_identity`` -- FIX 4, since the withheld client means
    ``_warm_streaming_url_cache`` never runs to mint it instead); a clean
    live/known miss -> ``absent`` (a fresh ``live_miss``/``cache_miss_recent``
    negative-cache); a fresh LML#1121 error row (``cache_error_recent`` --
    the cache read happens BEFORE the ``fail_fast`` branch, so this probe can
    still land on one) -> ``unresolved`` (LML#1121 FIX 1: a couldn't-ask, not
    a confirmed absence); a breaker shed / 429 / non-429 transport failure /
    timeout -> ``unresolved`` with NO cache write (``fail_fast`` re-raises
    before the UPSERT). Exactly one ``find_album_match`` — no title-variant
    fan-out (LML#1094).
    """
    if not (
        # is_top1 FIRST (cheapest check, no attribute/ContextVar read): a
        # non-top-1 item never runs the probe (LML#1106 review, FIX 2) — see
        # the docstring's is_top1 bullet.
        is_top1
        # BULK-path-only gate NEXT (short-circuits the interactive path before
        # any flag read): the interactive /lookup injects the same client and
        # reads the same flag, so this ContextVar — set True only by the bulk
        # handler — is what keeps the synchronous probe off the hot path. This
        # reuses the same bulk-only signal #1087's bulk-warm exemption keys off
        # (a shared, existing assumption, not a new one); if a future change
        # ever sets suppression True on a non-bulk path, gate this on a
        # dedicated is_bulk context flag instead.
        and should_suppress_streaming_warm()
        # ``getattr`` default: the new field may be absent on a partial Settings
        # stub (the two persist flags below are guaranteed on every stub that
        # reaches enrichment), so read it defensively — never an AttributeError.
        and getattr(settings, "lml_bandcamp_live_probe", False)
        and settings.lml_persist_streaming_urls
        and settings.lml_persist_streaming_url_bandcamp
        and ctx.bandcamp is not None
        and ctx.discogs_cache_pg is not None
        and ctx.artist
        and ctx.album
        and not current_bandcamp_url
    ):
        return BandcampProbeResult(current_bandcamp_url, None)

    base_timeout_s = bandcamp_probe_timeout_s()
    probe_timeout_s = (
        ctx.spine_deadline.clamp_probe_timeout_s(base_timeout_s)
        if ctx.spine_deadline is not None
        else base_timeout_s
    )
    try:
        outcome = await asyncio.wait_for(
            resolve_streaming_url_with_cache(
                ctx.discogs_cache_pg,
                ctx.bandcamp,
                service=_BANDCAMP_ALBUM_SERVICE,
                artist=ctx.artist,
                album=ctx.album,
                fail_fast=True,
            ),
            timeout=probe_timeout_s,
        )
    except (
        BreakerOpenError,
        BandcampRateLimitedError,
        BandcampTransportError,
    ) as exc:
        # LML#1118 FIX 1: widened from ``BandcampBreakerOpenError`` to the
        # shared ``BreakerOpenError`` base. This parenthesized tuple was the
        # 20th breaker-catch site -- an AST walk over every ``except`` handler
        # found it; the #1118 audit's ``except DiscogsBreakerOpenError`` grep
        # structurally could not see a handler that names the OTHER concrete
        # type. #1103's future YouTube Music breaker reaches this same probe
        # through the service-generic
        # ``resolve_streaming_url_with_cache(fail_fast=True)`` seam; without
        # this widening its shed would fall to the ``except Exception`` below
        # and log at ERROR, reproducing the #755 flood misattributed to
        # Bandcamp. A breaker-OPEN shed (any service), a 429-driven shed on
        # the single fail-fast attempt, or a non-429 transport failure (5xx,
        # Cloudflare 403/1015, connect timeout, non-200 --
        # clients/bandcamp.py's BandcampTransportError, LML#1106 review FIX
        # 2): all are a "couldn't ask", not an unexpected raise -- transient,
        # no sourced verdict, no cache write. Emit the unsampled shed counter
        # so sustained saturation (rate-limit OR transport) stays queryable
        # without a per-event Sentry flood (LML#879/#1049 pattern).
        _capture_shed(type(exc).__name__, settings=settings)
        return BandcampProbeResult(current_bandcamp_url, StreamingResolutionStatus.unresolved)
    except TimeoutError:
        # The wall-clock ceiling tripped (genuine Bandcamp slowness or a
        # near-zero deadline clamp) — transient, but NOT a breaker shed, so no
        # shed counter. No cache write (wait_for cancelled before the UPSERT).
        logger.debug("Bandcamp live probe timed out for %s - %s", ctx.artist, ctx.album)
        return BandcampProbeResult(current_bandcamp_url, StreamingResolutionStatus.unresolved)
    except Exception as exc:
        # Defensive degrade (LML#444 posture): a probe fault must never fail
        # the item — attempted-but-inconclusive maps to ``unresolved``. Names
        # the actual exception type (LML#1118 FIX 1) rather than assuming
        # Bandcamp -- the widened catch above no longer funnels every
        # breaker-typed shed through here for that assumption to hold.
        logger.exception(
            "%s raised from the Bandcamp live probe for %s - %s",
            type(exc).__name__,
            ctx.artist,
            ctx.album,
        )
        return BandcampProbeResult(current_bandcamp_url, StreamingResolutionStatus.unresolved)

    if outcome.url is not None:
        # FIX 4 (LML#1106 review): mint only a brand-new live_resolved -- a
        # cache_hit already minted; _mint_identity swallows its own failures
        # (never fails the lookup). FIX C (LML#1106 review round 2): this
        # await sits AFTER the probe's own asyncio.wait_for has already
        # returned, so nothing else bounds it -- wrap it in its own ceiling
        # so a stuck PG mint (pool exhaustion / lock contention on
        # entity.release_identity) can't pin this response-path item past
        # the caller's budget. A timeout here is the SAME best-effort
        # posture as a mint failure inside _mint_identity itself: log and
        # continue, the URL still surfaces either way.
        if outcome.source == "live_resolved" and ctx.entity_store is not None:
            try:
                await asyncio.wait_for(
                    _mint_identity(
                        _BANDCAMP_ALBUM_SERVICE,
                        _BANDCAMP_CACHE_CONFIG,
                        outcome.url,
                        ctx.entity_store,
                    ),
                    timeout=_MINT_TIMEOUT_S,
                )
            except TimeoutError:
                logger.warning(
                    "Bandcamp release-identity mint timed out for %s - %s (url=%s)",
                    ctx.artist,
                    ctx.album,
                    outcome.url,
                )
        return BandcampProbeResult(outcome.url, StreamingResolutionStatus.verified)
    if outcome.source == "cache_error_recent":
        # LML#1121 FIX 1: the cache read above runs BEFORE the fail_fast
        # branch, so a fail_fast=True probe can still land on a fresh
        # LML#1121 error row (a couldn't-ask, not a confirmed absence) --
        # this was previously falling through to the ``absent`` return below
        # unconditionally, which is a SOURCED verdict that
        # ``probe_owns_bandcamp_leg`` withholds the client for, leaving the
        # item with no fill AND no background retry. Map it to
        # ``unresolved`` instead, exactly like a breaker shed / 429 /
        # transport failure / timeout -- none of which reached a verdict
        # either.
        return BandcampProbeResult(current_bandcamp_url, StreamingResolutionStatus.unresolved)
    # live_miss / cache_miss_recent: a sourced, terminal no-match. Leave the URL
    # to the deferred search fallback; the verdict says "checked, absent", not
    # "couldn't check".
    return BandcampProbeResult(current_bandcamp_url, StreamingResolutionStatus.absent)


def _capture_shed(reason: str, *, settings: Settings) -> None:
    """Emit the unsampled PostHog counter for one Bandcamp live-probe shed.

    Mirrors ``discogs.service._capture_artist_breaker_shed`` (LML#1049):
    best-effort, gated by ``enable_telemetry``, wired through the shared
    ``get_posthog_client`` accessor. Every branch is wrapped so a telemetry
    failure can never turn a shed into a lookup failure.
    """
    try:
        if not getattr(settings, "enable_telemetry", False):
            return
        client = get_posthog_client(event_prefix=_SHED_POSTHOG_EVENT_PREFIX)
        if client is None:
            return
        client.capture(
            distinct_id=_SHED_POSTHOG_DISTINCT_ID,
            event=_SHED_EVENT,
            properties={
                "reason": reason,
                "environment": getattr(settings, "environment", None),
            },
        )
    except Exception:
        logger.warning("Failed to emit %s counter", _SHED_EVENT, exc_info=True)
