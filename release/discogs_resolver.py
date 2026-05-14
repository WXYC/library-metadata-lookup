"""Resolve a Discogs release ID to canonical fields + identifiers.

Wraps the existing ``DiscogsService.get_release`` call (which itself reads
the discogs-cache PG-backed cache and falls back to the Discogs API) so this
endpoint inherits all the caching behavior already in production.

The Discogs cache schema does not currently store ``country`` or ``formats``
at the release level, so those fields are left null on cache hits. They could
be backfilled later if the music director asks for them; see the plan doc.
``catno`` comes from the first label in the ``labels`` array.

Discogs master URLs are not yet supported. Pasting a master URL returns a
warning suggesting the user paste a release URL instead — masters require
an extra API hop (``/masters/<id>`` → ``main_release``) that's not worth the
rate-limit budget for v1 of this endpoint.

Provenance (per #329): every successful resolution carries a ``matched_via``
tag. ``discogs_cache`` means the row came from the local PostgreSQL cache;
``discogs_live_api`` means the local cache missed and we fell through to
the live Discogs API (which then writes the row back into the cache, so the
next request for the same release is free). Live-tier hits also increment
an ad-hoc ``discogs_live_release_hit`` counter on the request's cache_stats
recorder so freshness-gap impact is visible in PostHog / Sentry.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

from wxyc_fastapi.observability import get_cache_stats_recorder

from discogs.service import DiscogsService
from release.models import CanonicalRelease, ReleaseIdentifiers

logger = logging.getLogger(__name__)

# Per #329: the provenance tier values are a closed set. New tiers (e.g.
# ``library_identity`` or ``musicbrainz_cache``) should be added here so
# downstream consumers can switch on Literal exhaustiveness.
MatchedVia = Literal["discogs_cache", "discogs_live_api"]

# Cache-stats key for the live-tier counter. Defined here so tests can import
# the same symbol the production code records under.
LIVE_RELEASE_METRIC_KEY = "discogs_live_release_hit"


@dataclass
class DiscogsResolveResult:
    """Result of resolving a single Discogs release ID."""

    canonical: CanonicalRelease | None
    identifiers: ReleaseIdentifiers
    warnings: list[str]
    matched_via: MatchedVia | None = None


async def resolve_discogs_release(service: DiscogsService, release_id: str) -> DiscogsResolveResult:
    """Look up a Discogs release and project to canonical form.

    Returns a result whose ``canonical`` is None when the release could not
    be fetched (rate limited, not found, network error). The ``warnings``
    field carries a human-readable explanation in that case so the form
    can fall back to manual entry without hiding the failure from the user.
    """
    warnings: list[str] = []

    try:
        rid = int(release_id)
    except ValueError:
        return DiscogsResolveResult(
            canonical=None,
            identifiers=ReleaseIdentifiers(),
            warnings=[f"Discogs release ID '{release_id}' is not a number"],
        )

    try:
        release = await service.get_release(rid)
    except Exception:
        logger.exception("Discogs release fetch failed for %s", rid)
        return DiscogsResolveResult(
            canonical=None,
            identifiers=ReleaseIdentifiers(discogs_release_id=rid),
            warnings=[f"Discogs lookup failed for release {rid}"],
        )

    if release is None:
        warnings.append(
            f"Discogs release {rid} could not be fetched "
            "(rate-limited, not found, or temporarily unavailable)"
        )
        return DiscogsResolveResult(
            canonical=None,
            identifiers=ReleaseIdentifiers(discogs_release_id=rid),
            warnings=warnings,
        )

    # First label's catno is the canonical "this album's catalog number".
    catno: str | None = None
    if release.labels:
        catno = release.labels[0].catno or None

    canonical = CanonicalRelease(
        artist=release.artist or "",
        title=release.title or "",
        label=release.label or None,
        catno=catno,
        year=release.year,
        # country / formats not available from the Discogs cache today.
        country=None,
        formats=[],
    )

    identifiers = ReleaseIdentifiers(
        discogs_release_id=rid,
        discogs_artist_id=release.artist_id,
    )

    # The cache_service.get_release() projection sets cached=True on a PG
    # cache hit; the DiscogsService.get_release() API branch sets cached=False
    # when it falls through to the live Discogs API and writes back into PG.
    # That single bool is the source of truth for the live-vs-cached tier
    # surfaced by #329. ``cached`` is ``bool | None`` in the generated schema —
    # treat None the same as False (the API branch never produces None, only
    # legacy fixtures might).
    matched_via: MatchedVia = "discogs_cache" if release.cached else "discogs_live_api"
    if matched_via == "discogs_live_api":
        # Ad-hoc counter; the recorder permits undeclared keys (see
        # wxyc-fastapi CacheStatsRecorder.record docstring). Wrapped in a
        # try/except so a misconfigured request context never blocks the
        # actual release lookup — the metric is observability, not contract.
        try:
            get_cache_stats_recorder().record(LIVE_RELEASE_METRIC_KEY)
        except Exception:
            logger.debug("Failed to record live-release metric", exc_info=True)

    return DiscogsResolveResult(
        canonical=canonical,
        identifiers=identifiers,
        warnings=warnings,
        matched_via=matched_via,
    )


async def resolve_discogs_master(_service: DiscogsService, master_id: str) -> DiscogsResolveResult:
    """Master URLs are not yet supported.

    The Discogs API would let us pivot ``master_id`` → ``main_release_id`` →
    full release, but that adds a second API call per paste. Defer until
    we see real demand for master-URL pastes.
    """
    return DiscogsResolveResult(
        canonical=None,
        identifiers=ReleaseIdentifiers(discogs_master_id=_safe_int(master_id)),
        warnings=[
            "Discogs master URLs are not supported yet. "
            "Open the master and paste one of its release URLs instead."
        ],
    )


def _safe_int(value: str) -> int | None:
    try:
        return int(value)
    except ValueError:
        return None
