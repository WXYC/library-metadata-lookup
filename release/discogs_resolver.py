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
"""

from __future__ import annotations

import logging

from discogs.breaker import DiscogsBreakerOpenError
from discogs.service import DiscogsService
from release.models import CanonicalRelease, ReleaseIdentifiers, ResolveResult

logger = logging.getLogger(__name__)


async def resolve_discogs_release(service: DiscogsService, release_id: str) -> ResolveResult:
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
        return ResolveResult(
            canonical=None,
            identifiers=ReleaseIdentifiers(),
            warnings=[f"Discogs release ID '{release_id}' is not a number"],
        )

    # LML#546: Discogs release ids start at 1. A non-positive id forwarded to
    # ``service.get_release`` triggers a 404 from Discogs, which the
    # fallthrough seam tombstones (LML#510) — permanently poisoning the row
    # for every subsequent caller. The LML#518 decision record puts this
    # validation at the caller, not the service boundary. Reject here.
    if rid <= 0:
        return ResolveResult(
            canonical=None,
            identifiers=ReleaseIdentifiers(),
            warnings=[f"Discogs release ID '{rid}' is not positive"],
        )

    try:
        release = await service.get_release(rid)
    except DiscogsBreakerOpenError:
        # LML#814: ``get_release`` re-raises a saturation-breaker shed (the
        # LML#755 FIX-1 guard). The generic ``except`` below would log it at
        # ERROR via ``logger.exception`` — and the Sentry LoggingIntegration
        # (event_level=ERROR) turns each such record into a discrete event,
        # reproducing the #755 flood on this release-resolve path. A shed is
        # expected degrade: log at DEBUG and return ``canonical=None`` with a
        # *transient*, retriable warning (matching the ``release is None``
        # rate-limited phrasing below), not the hard "lookup failed" message.
        # Nothing negative is persisted: ``get_release`` re-raised before any
        # tombstone, and the orchestrator's identity write-back is gated on
        # ``canonical is not None`` so a None result writes nothing.
        logger.debug("Discogs release fetch shed by saturation breaker for %s (retriable)", rid)
        return ResolveResult(
            canonical=None,
            identifiers=ReleaseIdentifiers(discogs_release_id=rid),
            warnings=[
                f"Discogs is temporarily rate-limited; release {rid} could not be "
                "fetched right now — try again shortly."
            ],
        )
    except Exception:
        logger.exception("Discogs release fetch failed for %s", rid)
        return ResolveResult(
            canonical=None,
            identifiers=ReleaseIdentifiers(discogs_release_id=rid),
            warnings=[f"Discogs lookup failed for release {rid}"],
        )

    if release is None:
        warnings.append(
            f"Discogs release {rid} could not be fetched "
            "(rate-limited, not found, or temporarily unavailable)"
        )
        return ResolveResult(
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

    return ResolveResult(canonical=canonical, identifiers=identifiers, warnings=warnings)


async def resolve_discogs_master(_service: DiscogsService, master_id: str) -> ResolveResult:
    """Master URLs are not yet supported.

    The Discogs API would let us pivot ``master_id`` → ``main_release_id`` →
    full release, but that adds a second API call per paste. Defer until
    we see real demand for master-URL pastes.
    """
    return ResolveResult(
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
