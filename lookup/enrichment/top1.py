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

import logging

from discogs.breaker import DiscogsBreakerOpenError
from discogs.models import ArtistDetails, DiscogsSearchResult, ReleaseMetadataResponse
from discogs.service import DiscogsService
from lookup.wikipedia_url import PickedWikiUrl, pick_artist_wikipedia_url

logger = logging.getLogger(__name__)


async def fetch_top1_release_details(
    discogs_service: DiscogsService,
    top_artwork: DiscogsSearchResult | None,
) -> tuple[
    int | None,
    str | None,
    PickedWikiUrl | None,
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

        try:
            details = await discogs_service.get_artist_details(artist_id, lean=True)
        except DiscogsBreakerOpenError:
            # LML#1118: kept narrow — this module's dependency surface is
            # Discogs-only by design (module docstring: "one service handle");
            # no other breaker type can reach here.
            # LML#1049: a breaker shed on the artist-bio step is "couldn't
            # enrich the bio this time," not a reason to discard the
            # release-level enrichment (``year`` / ``release``) already
            # fetched successfully this same request. Degrade just the
            # bio/wiki/details fields — narrower than the generic ``except``
            # below, which (pre-existing, unchanged) still drops everything
            # on a non-shed failure. No negative is cached either way:
            # ``get_artist_details`` itself already guarantees that on a shed.
            return year, None, None, release, None
        if not details:
            return year, None, None, release, None

        bio = details.profile if isinstance(details.profile, str) else None
        # LML#513 (Phase A): slug-scored pick over the legacy first-substring
        # match — see lookup/wikipedia_url.py for the extractor and the
        # LML_WIKIPEDIA_SLUG_MATCH flag it reads.
        #
        # LML#1192 review round 4, P0-10: its OWN try/except -- this call
        # runs regex parsing, wxyc_etl's PyO3 bindings
        # (strip_discogs_disambig), and Sentry SDK calls
        # (_project_wikipedia_slug_pick), any of which failing must not
        # discard year/release/details already fetched successfully this
        # same request. Same LML#1049 rationale as the DiscogsBreakerOpenError
        # branch above (a bio-step failure isn't a reason to discard the
        # release-level enrichment) -- the OUTER except below is a
        # catch-all for the Discogs I/O calls, not a place a pure-function
        # bug in the extractor should also be able to reach.
        try:
            wiki = pick_artist_wikipedia_url(details.urls, details.name)
        except Exception:
            logger.exception("Wikipedia URL extraction failed for artist_id=%s", artist_id)
            wiki = None
        return year, bio, wiki, release, details
    except Exception:
        logger.exception(
            "Top-1 release/artist detail fetch failed for release_id=%s", top_artwork.release_id
        )
        return None, None, None, None, None
