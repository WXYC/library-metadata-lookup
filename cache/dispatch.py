"""Per-identity cache-refresh dispatcher (LML#525).

Owns the logic for: per-source release-cache refresh, walk-to-artists,
walk-site sentinel guard, and per-identity ``status`` rollup. Split out from
``cache/router.py`` so the pure-logic pieces (sentinel guard, rollup) are
unit-testable without spinning up the FastAPI app.

The router calls ``refresh_identity`` once per ``identity_id`` under the
batch-level semaphore from ``core/bulk_concurrency.py``. The dispatcher
itself does not gate concurrency — Discogs work goes through
``discogs/ratelimit.py``'s per-event-loop semaphore + rate limiter
(inherited for free via ``DiscogsService.get_release`` /
``DiscogsService.get_artist_details``).

Sentry spans:

- ``cache.refresh.identity`` — one span per identity_id, attributes set at
  span open.
- ``cache.refresh.release`` — one span per (identity_id, source) release leg.
- ``cache.refresh.artist`` — one span per (identity_id, source, artist_id)
  walk target.

Attribute naming mirrors BS#1081's convention: numeric attributes
(``identity_id``, ``external_id`` when integer-shaped) go on
``span.set_data`` immediately after open, never via late binding through a
helper. String attributes follow the same shape for symmetry.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import sentry_sdk

from cache.models import (
    ArtistRefreshOutcome,
    CacheRefreshItemStatus,
    CacheRefreshResultItem,
    SourceRefreshOutcome,
)
from discogs.models import ReleaseMetadataResponse

if TYPE_CHECKING:
    from discogs.service import DiscogsService

logger = logging.getLogger(__name__)


def extract_discogs_artist_ids(release: ReleaseMetadataResponse | None) -> list[int]:
    """Return the Discogs artist_ids worth refreshing for ``release.artists``.

    The ``> 0`` guard is the LML#525-specific walk-to-artist contribution to
    the LML#518 / LML#546 caller-validates audit posture. Discogs releases
    routinely include ``artist_id: 0`` as the "Various Artists" sentinel
    (the original BS-side ``isValidArtistId`` motivation); a missing
    ``artist_id`` is also possible if the API response shape is partial.

    Without this guard, the dispatcher would become a fresh unguarded caller
    of ``get_artist_details(0)`` every time it walks a compilation release —
    re-introducing the very leak LML#518/#546 close at other call sites.

    Returns an empty list when ``release`` is ``None`` (the public
    ``get_release`` boundary translates tombstones to ``None``; we have
    nothing to walk in that case, which is correct — the release-cache leg
    still counts as success because the cache state is current).
    """
    if release is None or release.artists is None:
        return []
    return [c.artist_id for c in release.artists if c.artist_id is not None and c.artist_id > 0]


def compute_per_id_status(
    sources: dict[str, SourceRefreshOutcome],
) -> CacheRefreshItemStatus:
    """Roll up per-source outcomes into the per-id four-value enum.

    Issue priority order (release-leg-gated):

    1. Any source ``release_outcome == "success"`` → ``warmed``. Artist
       failures inside the source's ``artists`` list do NOT promote to
       error — the cron's job is "make release-cache warm"; an artist 404
       inside a release walk is partial value, not a retry trigger.
    2. Any source ``release_outcome == "not_implemented"`` AND no source
       was ``success`` → ``not_implemented``.
    3. All dispatched sources errored → ``error``.

    Empty ``sources`` (a row whose per-source columns are all NULL — legal
    but never produced by the v1 mint protocol) rolls up to
    ``not_implemented``: nothing was dispatched, so there is no error to
    surface, just an absence of wiring.

    The ``not_found`` enum value is reserved for the "no row in
    ``entity.release_identity``" case and is set by the router before
    calling this function — so it is not a possible return value here.
    """
    if not sources:
        return "not_implemented"
    has_success = False
    has_not_implemented = False
    for source_outcome in sources.values():
        if source_outcome.release_outcome == "success":
            has_success = True
        elif source_outcome.release_outcome == "not_implemented":
            has_not_implemented = True
    if has_success:
        return "warmed"
    if has_not_implemented:
        return "not_implemented"
    return "error"


async def _refresh_discogs_release(
    discogs_service: DiscogsService, external_id: str
) -> tuple[ReleaseMetadataResponse | None, SourceRefreshOutcome]:
    """Run the Discogs release leg.

    Returns ``(release_record, source_outcome)``. The release record is
    handed back so the caller can walk its artists; the source outcome
    omits the artists list, which the caller fills in.
    """
    try:
        release_id = int(external_id)
        release = await discogs_service.get_release(release_id)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.exception("cache refresh: discogs release leg failed external_id=%r", external_id)
        return None, SourceRefreshOutcome(release_outcome="error", message=type(exc).__name__)
    # Tombstone-as-success: the boundary returns None for a Discogs 404,
    # which is "cache state is current" — the next reader short-circuits on
    # the tombstone row without re-burning the rate-limit budget. Per the
    # LML#525 contract, we record this as success with an empty artists list
    # (no artists to walk on a tombstone).
    return release, SourceRefreshOutcome(release_outcome="success")


async def _refresh_discogs_artist(
    discogs_service: DiscogsService, artist_id: int
) -> ArtistRefreshOutcome:
    """Run the Discogs artist leg for a single walk target."""
    with sentry_sdk.start_span(op="cache.refresh.artist", name=f"discogs:{artist_id}") as span:
        span.set_data("lml.cache.source", "discogs")
        span.set_data("lml.cache.external_id", artist_id)
        try:
            await discogs_service.get_artist_details(artist_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("cache refresh: discogs artist leg failed artist_id=%d", artist_id)
            span.set_data("lml.cache.outcome", "error")
            return ArtistRefreshOutcome(
                external_id=str(artist_id),
                outcome="error",
                message=type(exc).__name__,
            )
        span.set_data("lml.cache.outcome", "success")
        return ArtistRefreshOutcome(external_id=str(artist_id), outcome="success")


# Sources for which the release leg has no PG-backed cache today. Returning
# ``not_implemented`` (rather than swallowing) keeps the dashboard signal
# legible — "leg not wired" is operationally distinct from "leg failed."
#
# `discogs_master`: gated on adding `artists: list[DiscogsArtistCredit]` to
# `MasterRelease` and on a PG-backed master cache (`DiscogsService.get_master`
# is API-only via `@async_cached(MASTER_CACHE)`, no fallthrough seam).
#
# `musicbrainz_release`: gated on LML#217 (no client-side release-with-artists
# model exists today).
#
# `bandcamp` / `spotify_album` / `apple_music_album`: no cache routers today.
_NOT_IMPLEMENTED_SOURCES = frozenset(
    {
        "discogs_master",
        "musicbrainz_release",
        "spotify_album",
        "apple_music_album",
        "bandcamp",
    }
)


async def _dispatch_source(
    discogs_service: DiscogsService,
    identity_id: int,
    source: str,
    external_id: str,
) -> SourceRefreshOutcome:
    """Run the release leg for one source and walk its artists if any."""
    with sentry_sdk.start_span(op="cache.refresh.release", name=f"{source}:{external_id}") as span:
        span.set_data("lml.cache.identity_id", identity_id)
        span.set_data("lml.cache.source", source)
        span.set_data("lml.cache.external_id", external_id)

        if source == "discogs_release":
            release, outcome = await _refresh_discogs_release(discogs_service, external_id)
            span.set_data("lml.cache.release_outcome", outcome.release_outcome)
            # Walk artists only if the release leg succeeded AND yielded a
            # non-None record. Tombstones (success with release=None) walk
            # nothing — empty artists list.
            if outcome.release_outcome == "success" and release is not None:
                artist_ids = extract_discogs_artist_ids(release)
                # Sequential walks within a single release — the per-replica
                # Discogs semaphore + rate limiter cap is in `discogs/ratelimit`
                # and applies through `get_artist_details`. Parallelizing here
                # would queue against the same gates and gain nothing.
                outcome.artists = [
                    await _refresh_discogs_artist(discogs_service, artist_id)
                    for artist_id in artist_ids
                ]
            return outcome

        if source in _NOT_IMPLEMENTED_SOURCES:
            outcome = SourceRefreshOutcome(release_outcome="not_implemented")
            span.set_data("lml.cache.release_outcome", "not_implemented")
            return outcome

        # Future-proofing: an unknown source key from the store should be
        # logged so a schema/dispatcher drift surfaces at the leg level
        # rather than corrupting the per-id rollup. Treat as not_implemented
        # (most conservative — no API call, no false-success signal).
        logger.warning(
            "cache refresh: unknown source=%r for identity_id=%d; treating as not_implemented",
            source,
            identity_id,
        )
        span.set_data("lml.cache.release_outcome", "not_implemented")
        return SourceRefreshOutcome(
            release_outcome="not_implemented",
            message=f"unknown source: {source!r}",
        )


async def refresh_identity(
    *,
    identity_id: int,
    source_pairs: list[tuple[str, str]],
    discogs_service: DiscogsService,
) -> CacheRefreshResultItem:
    """Run all source legs for one identity_id and roll up the per-id status.

    Args:
        identity_id: The ``entity.release_identity.id`` being refreshed.
        source_pairs: ``(source, external_id)`` tuples from the bulk store
            read. Empty list means the row exists but has no per-source
            columns populated — rolls up to ``not_implemented`` (no leg ran).
        discogs_service: The Discogs API + cache service. The dispatcher does
            not gate concurrency itself; the per-replica Discogs semaphore /
            rate limiter in ``discogs/ratelimit.py`` apply through
            ``get_release`` / ``get_artist_details``.

    Returns:
        A ``CacheRefreshResultItem`` with per-source outcomes and the
        rolled-up ``status``. ``status == "not_found"`` is set by the router
        when there is no entity.release_identity row at all and is NOT a
        possible value here.
    """
    with sentry_sdk.start_span(op="cache.refresh.identity", name=str(identity_id)) as span:
        span.set_data("lml.cache.identity_id", identity_id)
        span.set_data("lml.cache.source_count", len(source_pairs))

        sources: dict[str, SourceRefreshOutcome] = {}
        for source, external_id in source_pairs:
            sources[source] = await _dispatch_source(
                discogs_service, identity_id, source, external_id
            )

        status = compute_per_id_status(sources)
        span.set_data("lml.cache.status", status)

    return CacheRefreshResultItem(
        identity_id=identity_id,
        status=status,
        sources=sources,
    )


__all__ = [
    "compute_per_id_status",
    "extract_discogs_artist_ids",
    "refresh_identity",
]
