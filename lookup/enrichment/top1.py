"""Top-1 release/artist/bio fetch (LML#730).

The former ``fetch_top1_release_details`` closure from
``enrich_artwork_results``, un-nested into a module function. Its true
dependency surface is deliberately narrow — one service handle and the
top-1 artwork — rather than the full ``EnrichmentContext``: this stage
must not reach the per-item handles (apple_music, library_db, …), and a
caller shouldn't need a 15-field context to fetch one release. Top-1-only
expensive enrichment: the coordinator calls this once per request;
non-top-1 items reuse the same per-result streaming-URL build in
``lookup.enrichment.item``.
"""

from discogs.models import ArtistDetails, DiscogsSearchResult, ReleaseMetadataResponse
from discogs.service import DiscogsService


async def fetch_top1_release_details(
    discogs_service: DiscogsService,
    top_artwork: DiscogsSearchResult | None,
) -> tuple[
    int | None,
    str | None,
    str | None,
    ReleaseMetadataResponse | None,
    ArtistDetails | None,
]:
    """Returns (year, artist_bio, wikipedia_url, release, details) for the top-1 result.

    Returns the release + artist payloads alongside the legacy three
    scalars so the extended-field population can pluck additional
    fields without re-fetching.
    """
    # `release_id <= 0` short-circuits the LML#401 streaming-only
    # synthesis sentinel (see issue #518): the synthesized result
    # carries `release_id=0` as a BS#1185 cross-service contract,
    # and round-tripping it through Discogs hits `/releases/0` (404)
    # before the `if not release` branch silently swallows the response.
    if top_artwork is None or top_artwork.release_id <= 0:
        return None, None, None, None, None
    try:
        # LML#894 (lever L4a): the /lookup hydration path uses the lean cache
        # read, which skips the 4 children /lookup never surfaces
        # (release_video; artist_alias / artist_name_variation / artist_member).
        # This stage reads only year / artist_id / artist bio + Wikipedia URL,
        # so the lean shape is byte-identical for what it consumes while cutting
        # the top-1 hydration from 14 PG round-trips to 10.
        release = await discogs_service.get_release(top_artwork.release_id, lean=True)
        if not release:
            return None, None, None, None, None

        year = release.year if isinstance(release.year, int) else None
        artist_id = release.artist_id
        if not isinstance(artist_id, int) or artist_id <= 0:
            return year, None, None, release, None

        details = await discogs_service.get_artist_details(artist_id, lean=True)
        if not details:
            return year, None, None, release, None

        bio = details.profile if isinstance(details.profile, str) else None
        wiki = next(
            (url for url in details.urls if isinstance(url, str) and "wikipedia.org" in url),
            None,
        )
        return year, bio, wiki, release, details
    except Exception:
        return None, None, None, None, None
