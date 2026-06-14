"""Pydantic request / response models for ``POST /api/v1/cache/refresh-for-identities``.

LML#525. The endpoint multiplexes per-identity cache warming across many
sources (today: Discogs only) and returns per-identity results — no top-level
counters; the caller derives them via ``Counter(r.status for r in results)``,
matching the ``BulkLookupResultItem`` precedent in ``lookup/models.py``.

The models live here (not in ``generated/api_models.py``) because:

- LML's internal dispatcher is the only producer; nothing else in LML
  consumes the response shape.
- The api.yaml contract addition (so BS-side generated types track) is a
  separate commit. Until that lands, BS reads the JSON shape directly; once
  it lands, the generated Python class is structurally identical and these
  models can become re-exports if drift becomes a concern.

Per-source ``release_outcome`` and per-artist ``outcome`` are a coarse
three-value enum (``success | error | not_implemented``). Tombstones count
as ``success`` — see ``cache/dispatch.py`` for the public-boundary translation.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# ``success`` covers both cache hits (fallthrough seam returned without an API
# call) and fresh fetches (API call made, response cached) AND tombstones
# (Discogs returned 404, the boundary translates to None, but the cache state
# is current). Consumers that need to distinguish "warm-real" from
# "warm-tombstone" should read ``release.not_found`` from the cache directly.
SourceRefreshOutcomeStatus = Literal["success", "error", "not_implemented"]
ArtistRefreshOutcomeStatus = Literal["success", "error", "not_implemented"]

# Per-id rollup. Release-leg-gated priority order (cache/dispatch.py owns the
# implementation; see ``compute_per_id_status`` and the issue's rollup rules):
#   1. row absent in entity.release_identity → not_found (sources is None)
#   2. any source release_outcome == success → warmed (artist failures don't
#      promote to error — the cron's job is release-cache warmth, not artist
#      coverage)
#   3. only not_implemented sources → not_implemented
#   4. all dispatched sources errored → error
CacheRefreshItemStatus = Literal["warmed", "not_found", "not_implemented", "error"]


class ArtistRefreshOutcome(BaseModel):
    """Per-artist outcome inside a per-source artists list.

    ``external_id`` is ``str`` for future source-agnosticism — Discogs IDs
    serialize as decimal strings. The router never branches on the artist
    type; per-source dispatch knows the right interpretation.
    """

    external_id: str
    outcome: ArtistRefreshOutcomeStatus
    # Populated when outcome != "success". Stripped to the exception class
    # name (e.g. ``"HTTPError"``) to keep upstream error bodies / SQL
    # fragments out of the response. Full traceback lands in Sentry.
    message: str | None = None


class SourceRefreshOutcome(BaseModel):
    """Per-source outcome inside a per-identity ``sources`` map.

    ``artists`` carries the walk-to-artists fan-out result for the Discogs
    leg; remains an empty list on tombstone-success (the release was
    refreshed but had no artist credits to walk) or on legs that don't
    do a walk (every non-Discogs source today).
    """

    release_outcome: SourceRefreshOutcomeStatus
    artists: list[ArtistRefreshOutcome] = Field(default_factory=list)
    message: str | None = None


class CacheRefreshResultItem(BaseModel):
    """Per-identity verdict in the bulk-refresh response."""

    identity_id: int
    status: CacheRefreshItemStatus
    # ``None`` when status == "not_found" (no row in entity.release_identity
    # to dispatch against). Otherwise a dict keyed by the source vocabulary
    # the entity store uses (``"discogs_release"``, ``"discogs_master"``,
    # …); see ``entity.store._RELEASE_IDENTITY_COLUMN_TO_SOURCE``.
    sources: dict[str, SourceRefreshOutcome] | None = None
    message: str | None = None


class BulkCacheRefreshRequest(BaseModel):
    """Request body.

    The 50-id cap is enforced **manually** at the router (400, not Pydantic's
    422), matching the ``/api/v1/lookup/bulk`` precedent — Pydantic
    ``max_length`` would return 422 on oversize, contradicting the LML#525
    issue contract. The ``min_length=1`` floor returns 422 on empty, which
    matches the issue.
    """

    identity_ids: list[int] = Field(..., min_length=1)


class BulkCacheRefreshResponse(BaseModel):
    """Response body.

    No top-level counters — derived by caller via
    ``Counter(r.status for r in results)``. Mirrors the
    ``BulkLookupResultItem`` shape (``lookup/models.py``).
    """

    results: list[CacheRefreshResultItem]


__all__ = [
    "ArtistRefreshOutcome",
    "ArtistRefreshOutcomeStatus",
    "BulkCacheRefreshRequest",
    "BulkCacheRefreshResponse",
    "CacheRefreshItemStatus",
    "CacheRefreshResultItem",
    "SourceRefreshOutcome",
    "SourceRefreshOutcomeStatus",
]
