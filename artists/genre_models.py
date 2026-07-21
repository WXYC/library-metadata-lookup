"""Pydantic request / response models for ``POST /api/v1/artists/genres/bulk``.

LML#781. Bulk artist-level Discogs *genre* / *style* aggregation for the iOS
On Tour genre filter (wxyc-ios-64#474 R2), consumed server-to-server by
Backend-Service's nightly concerts enrichment (Backend-Service#1624).

The models live here (not in ``generated/api_models.py``) for the same reason
the ``cache/`` refresh models do: the api.yaml wire-contract addition (so the
Backend-Service side generates a structurally identical type) is a separate,
coordinated commit in wxyc-shared. Until that lands, Backend-Service reads the
JSON shape directly; once it lands, the generated Python class is structurally
identical and these models can become re-exports if drift becomes a concern.

Genre vs. style semantics (from the issue):

- ``genres`` is the coarse ~15-value Discogs *genre* taxonomy the iOS filter
  chips render (Rock, Electronic, Jazz, Folk World & Country, …). Truncated to
  the top ``GENRE_TOP_K`` per artist (``artists/genre_aggregation.py``) — the
  "top 1-2 genres per artist" majority-take.
- ``styles`` is the finer Discogs *style* taxonomy, aggregated the same way but
  returned in full (frequency-ranked, untruncated) "for completeness" — the
  iOS filter ignores it; it is a detail-view treatment.

WXYC's internal library genre taxonomy (``genre_artist_crossreference``) is a
record-retrieval filing system and is deliberately NOT a source here — these
values come only from the Discogs cache's per-release ``release_genre`` /
``release_style`` rows.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# ``source`` reports where each artist's answer came from, so the caller can
# distinguish a genuine "Discogs has no genre data for this artist" (safe to
# negative-cache) from a transient "couldn't ask Discogs" (retry). Mirrors the
# resolve endpoint's ``escalation_unavailable`` retry-contract discipline
# (LML#759): never let a consumer negative-cache a "couldn't ask" verdict.
#   - ``cache``       — served from the discogs-cache ``release_genre`` rows.
#   - ``discogs_api`` — cache miss; the Discogs API fallback produced genres.
#   - ``not_found``   — cache miss; the API answered but the artist has no
#                       release/genre data. Safe to negative-cache.
#   - ``unavailable`` — cache miss and the API could not be consulted (no
#                       DISCOGS_TOKEN, saturation breaker open, or a transient
#                       fetch failure). Retry; do NOT negative-cache.
ArtistGenresSource = Literal["cache", "discogs_api", "not_found", "unavailable"]


class ArtistGenresInput(BaseModel):
    """One artist to aggregate genres/styles for.

    ``discogs_artist_id`` is optional but strongly preferred: it keys the
    aggregation on the stable Discogs artist entity (``release_artist.artist_id``)
    and avoids the homonym ambiguity of the name-only fallback. Backend-Service's
    concerts pipeline resolves it via ``POST /api/v1/artists/resolve/bulk``
    (LML#759) before calling this endpoint, so it is usually present.
    """

    artist_name: str = Field(..., min_length=1, description="Artist display name.")
    discogs_artist_id: int | None = Field(
        None,
        description=(
            "Discogs artist ID. When present the cache aggregation keys on it "
            "(stable, homonym-safe); when absent it falls back to a "
            "case-insensitive exact ``artist_name`` match."
        ),
    )


class BulkArtistGenresRequest(BaseModel):
    """Request body for ``POST /api/v1/artists/genres/bulk``.

    The per-request cap is enforced manually at the router (413, before
    Pydantic) matching the artists-route family; ``min_length=1`` returns 422
    on an empty batch.
    """

    artists: list[ArtistGenresInput] = Field(..., min_length=1)


class ArtistGenresResultItem(BaseModel):
    """Per-artist aggregation, index-aligned with the request ``artists`` list."""

    artist_name: str = Field(..., description="Echoed from the request for index alignment.")
    discogs_artist_id: int | None = Field(
        None, description="Echoed from the request (null when not supplied)."
    )
    genres: list[str] = Field(
        default_factory=list,
        description=(
            "Top coarse Discogs genres (majority-take, frequency-ranked, "
            "truncated to the top-K). Empty when unknown."
        ),
    )
    styles: list[str] = Field(
        default_factory=list,
        description=(
            "Frequency-ranked Discogs styles (full list, untruncated). "
            "Empty when unknown. The iOS filter ignores this; it is a "
            "detail-view treatment."
        ),
    )
    source: ArtistGenresSource = Field(
        ..., description="Where the answer came from (see ArtistGenresSource)."
    )
    bio: str | None = Field(
        None,
        description=(
            "Artist biography from the Discogs profile, keyed on "
            "`discogs_artist_id` via a cache-only bulk read. Null when the "
            "artist has no id (name-only), is not cached, has a blank profile, "
            "or is a not-found tombstone. Additive + nullable and independent "
            "of `source`, which continues to describe *genre* provenance only "
            "(bio provenance can diverge — a cached profile may have no genre "
            "rows, or vice-versa). Raw Discogs markup, parsed downstream."
        ),
    )


class BulkArtistGenresResponse(BaseModel):
    """Response body.

    No top-level counters — the caller derives them via
    ``Counter(r.source for r in results)``, matching the bulk-family precedent
    (``BulkLookupResultItem`` / ``CacheRefreshResultItem``).
    """

    results: list[ArtistGenresResultItem]


__all__ = [
    "ArtistGenresInput",
    "ArtistGenresResultItem",
    "ArtistGenresSource",
    "BulkArtistGenresRequest",
    "BulkArtistGenresResponse",
]
