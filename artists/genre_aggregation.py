"""Artist-level Discogs genre/style aggregation (LML#781).

Two responsibilities, split so the deterministic core is unit-testable without
any I/O:

- :func:`rank_counts` / :func:`build_aggregation` — pure frequency ranking with
  a documented, total tie-break rule and genre top-K truncation. This is the
  "majority-take aggregation is deterministic" acceptance criterion.
- :func:`resolve_artist_genres` — the per-artist cache-first / Discogs-API
  fallback orchestration.

**Aggregation.** An artist's genres/styles are a frequency count over the
artist's releases: each release contributes each of its genres (and styles) at
most once, and the counts are ranked. Genres are truncated to the top
``GENRE_TOP_K`` (the coarse iOS filter chips); styles are returned in full,
frequency-ranked, "for completeness".

**Determinism / tie-break.** Ranking sorts by count descending, then by genre
name ascending. The name key is ``(casefold, exact)`` so even case-variant
duplicates get a *total* order — the ranking is independent of the arbitrary
row order the discogs-cache ``GROUP BY`` returns and of the order releases are
fetched from the Discogs API.

**Cache-first, then API fallback.** The primary source is the discogs-cache
``release_genre`` / ``release_style`` rows keyed to the artist's releases
(:meth:`DiscogsCacheService.aggregate_artist_genre_style`). On a cache miss
(no genre *and* no style rows for the artist), the Discogs API fallback
discovers the artist's releases via ``search_release_ids_by_artist`` and fetches
each through :meth:`DiscogsService.get_release` — reusing the canonical
fallthrough write-back so the fetched releases (and their ``release_genre`` /
``release_style`` rows) are persisted to the discogs-cache. The next call for
the same artist therefore hits the cache path: the fallback is self-healing.
The fan-out is bounded by ``fallback_max_releases`` to keep an all-miss batch
inside Railway's request-timeout ceiling.

The fallback respects the existing Discogs rate-limit + saturation-breaker
handling for free: both ``search_release_ids_by_artist`` and ``get_release``
route through ``discogs/service.py``'s ``_request_with_retry`` (per-event-loop
concurrency semaphore + ``AsyncLimiter`` + LML#755 breaker).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from artists.genre_models import ArtistGenresSource
from discogs.breaker import DiscogsBreakerOpenError

if TYPE_CHECKING:
    from discogs.cache_service import DiscogsCacheService
    from discogs.service import DiscogsService

# Coarse-genre truncation: the iOS On Tour filter renders ~15 coarse Discogs
# genre chips and a headliner is described by its dominant one or two. The
# issue's "top 1-2 genres per artist" majority-take.
GENRE_TOP_K = 2

# Per-artist Discogs-API fallback fan-out cap. On a cache miss we fetch at most
# this many of the artist's releases (search-relevance ordered) to sample the
# genre/style distribution. Bounded because each fetch is a rate-limited Discogs
# call: an all-miss batch at the endpoint's per-request artist cap must stay
# inside Railway's ~5-minute request-timeout ceiling. A coarse ~15-value
# taxonomy is well-sampled by the top releases, so a small cap suffices.
FALLBACK_MAX_RELEASES = 8


def _rank_key(item: tuple[str, int]) -> tuple[int, str, str]:
    """Sort key: count descending, then name ascending (casefold, then exact).

    Negating the count sorts higher frequencies first; the ``(casefold, exact)``
    name pair is a *total* secondary order, so equal-count genres — including
    case-variant spellings — always rank in one fixed, reproducible sequence.
    """
    name, count = item
    return (-count, name.casefold(), name)


def rank_counts(counts: Mapping[str, int], *, limit: int | None = None) -> list[str]:
    """Rank genre/style names by frequency, deterministically.

    Args:
        counts: name → occurrence count (each release counted once per name).
        limit: keep only the top ``limit`` names; ``None`` keeps all.

    Returns:
        Names ordered by count descending, then name ascending — the same
        output for the same multiset regardless of input iteration order.
    """
    ranked = [name for name, _ in sorted(counts.items(), key=_rank_key)]
    return ranked if limit is None else ranked[:limit]


@dataclass
class ArtistGenreAggregation:
    """Ranked, truncated aggregation for one artist."""

    genres: list[str] = field(default_factory=list)
    styles: list[str] = field(default_factory=list)
    source: ArtistGenresSource = "not_found"


def build_aggregation(
    genre_counts: Mapping[str, int],
    style_counts: Mapping[str, int],
    source: ArtistGenresSource,
    *,
    genre_top_k: int = GENRE_TOP_K,
) -> ArtistGenreAggregation:
    """Rank both maps into an :class:`ArtistGenreAggregation`.

    Genres are truncated to ``genre_top_k``; styles are returned in full.
    """
    return ArtistGenreAggregation(
        genres=rank_counts(genre_counts, limit=genre_top_k),
        styles=rank_counts(style_counts, limit=None),
        source=source,
    )


async def _fallback_via_discogs_api(
    artist_name: str,
    discogs_service: DiscogsService,
    *,
    max_releases: int,
) -> tuple[Counter[str], Counter[str], ArtistGenresSource]:
    """Discogs-API cache-miss fallback: discover releases, fetch + persist, aggregate.

    Returns ``(genre_counts, style_counts, source)``. ``source`` distinguishes a
    genuine "no data" (``not_found``, safe to negative-cache) from a "couldn't
    ask" (``unavailable``, retry) so the caller never negative-caches a
    transient outage — the LML#759 retry-contract discipline.
    """
    try:
        release_ids = await discogs_service.search_release_ids_by_artist(
            artist_name, limit=max_releases
        )
    except DiscogsBreakerOpenError:
        return Counter(), Counter(), "unavailable"

    if release_ids is None:
        # "couldn't ask" — rate-limited / network / non-2xx (see the method's
        # None-vs-[] contract). Never a confirmed-empty verdict.
        return Counter(), Counter(), "unavailable"
    if not release_ids:
        # Discogs answered: the artist has no releases. Safe to negative-cache.
        return Counter(), Counter(), "not_found"

    genre_counts: Counter[str] = Counter()
    style_counts: Counter[str] = Counter()
    breaker_shed = False
    for release_id in release_ids:
        try:
            # get_release persists the full release tree (including
            # release_genre / release_style rows) via the fallthrough
            # write-back — the "persist to cache per existing conventions"
            # requirement — and inherits the Discogs rate-limit + breaker gates.
            release = await discogs_service.get_release(release_id)
        except DiscogsBreakerOpenError:
            # Budget exhausted mid-fan-out. Stop; keep whatever we aggregated.
            breaker_shed = True
            break
        if release is None:
            continue
        # One count per release per name (``set`` dedupes intra-release repeats).
        for genre in set(release.genres or []):
            genre_counts[genre] += 1
        for style in set(release.styles or []):
            style_counts[style] += 1

    if genre_counts or style_counts:
        return genre_counts, style_counts, "discogs_api"
    # Nothing aggregated: a mid-fan-out breaker shed means we couldn't finish
    # asking (retry); otherwise Discogs genuinely had no genre/style data.
    return Counter(), Counter(), "unavailable" if breaker_shed else "not_found"


async def resolve_artist_genres(
    *,
    artist_name: str,
    discogs_artist_id: int | None,
    discogs_cache: DiscogsCacheService,
    discogs_service: DiscogsService | None,
    genre_top_k: int = GENRE_TOP_K,
    fallback_max_releases: int = FALLBACK_MAX_RELEASES,
) -> ArtistGenreAggregation:
    """Aggregate one artist's genres/styles: discogs-cache first, then Discogs API.

    Args:
        artist_name: Artist display name (leading/trailing whitespace ignored).
        discogs_artist_id: Preferred cache key (stable, homonym-safe) when known.
        discogs_cache: The discogs-cache query layer (always required).
        discogs_service: The Discogs API service; ``None`` (no token) degrades a
            cache miss to ``source="unavailable"`` rather than raising.
        genre_top_k: Coarse-genre truncation.
        fallback_max_releases: Per-artist API fan-out cap on a cache miss.

    Returns:
        The ranked :class:`ArtistGenreAggregation`. Raises
        ``CacheUnavailableError`` only if the cache read itself fails — the
        caller maps that to a batch-level 503.
    """
    name = artist_name.strip()
    if not name:
        # A blank name can't key a cache row and would make the API search raise;
        # treat as a definite empty answer.
        return build_aggregation({}, {}, "not_found", genre_top_k=genre_top_k)

    genre_counts, style_counts = await discogs_cache.aggregate_artist_genre_style(
        artist_id=discogs_artist_id, artist_name=name
    )
    if genre_counts or style_counts:
        return build_aggregation(genre_counts, style_counts, "cache", genre_top_k=genre_top_k)

    # Cache miss. Fall back to the Discogs API (which also self-heals the cache).
    if discogs_service is None:
        return build_aggregation({}, {}, "unavailable", genre_top_k=genre_top_k)

    fb_genres, fb_styles, source = await _fallback_via_discogs_api(
        name, discogs_service, max_releases=fallback_max_releases
    )
    return build_aggregation(fb_genres, fb_styles, source, genre_top_k=genre_top_k)


__all__ = [
    "ArtistGenreAggregation",
    "FALLBACK_MAX_RELEASES",
    "GENRE_TOP_K",
    "build_aggregation",
    "rank_counts",
    "resolve_artist_genres",
]
