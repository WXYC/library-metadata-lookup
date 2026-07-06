"""Metadata enrichment for lookup results (release year, artist details, streaming links).

Extracted from ``lookup/orchestrator.py`` in LML#729 (orchestrator
decomposition PR 6a) and un-nested into a package in LML#730 (PR 6b).
``enrich_artwork_results`` is the Step-4b coordinator: it builds the frozen
``EnrichmentContext`` (``context``), runs the top-1 release/artist/bio fetch
(``top1``), fans out per-item enrichment (``item``), and schedules the
fire-and-forget bio cache warm (``background``).
"""

import asyncio
import logging

from clients.bandcamp import BandcampClient
from clients.streaming.apple_music import AppleMusicClient
from clients.streaming.matching import strip_discogs_disambig
from clients.streaming.spotify import SpotifyClient
from discogs.cache_service import DiscogsCacheService
from discogs.markup_parser import (
    CachedOnlyResolver,
    parse,
    parse_async,
)
from discogs.models import DiscogsSearchResult, ResolvedToken
from discogs.service import DiscogsService
from entity.sources import PgSource, PgSourceProtocol
from entity.store import EntityStore
from library.db import LibraryDB
from library.models import LibraryItem
from lookup.artist_resolution import (
    _artist_identity_split_gate_enabled,
    _artist_pair_verified,
)

# Submodules are referenced via module attributes (``background._warm_bio_cache``,
# ``item.enrich_one``, ``top1.fetch_top1_release_details``) rather than value
# imports, so each submodule stays the single patch/rebind seam for its own
# names — a value import here would bind a second copy on the package namespace
# that patches against the submodule silently miss.
from lookup.enrichment import background, item, top1
from lookup.enrichment.context import EnrichmentContext

logger = logging.getLogger(__name__)


async def enrich_artwork_results(
    items_with_artwork: list[tuple[LibraryItem, DiscogsSearchResult | None]],
    discogs_service: DiscogsService | None,
    song: str | None = None,
    album: str | None = None,
    artist: str | None = None,
    library_db: LibraryDB | None = None,
    *,
    extended: bool = False,
    warm_cache: bool = False,
    discogs_cache: DiscogsCacheService | None = None,
    mb_pg: PgSourceProtocol | None = None,
    apple_music: AppleMusicClient | None = None,
    spotify: SpotifyClient | None = None,
    bandcamp: BandcampClient | None = None,
    entity_store: EntityStore | None = None,
    discogs_cache_pg: PgSource | None = None,
    found_on_compilation: bool = False,
) -> list[tuple[LibraryItem, DiscogsSearchResult | None]]:
    """Enrich artwork results with release year, artist details, and streaming links.

    When library_db has a streaming_links table, uses direct URLs from the database.
    Falls back to search URLs when direct links are not available.

    ``apple_music`` is the authenticated Apple Music client (LML#443). When
    provided, ``enrich_one`` probes Apple Music for each item: the happy
    path (library row clears the LML#477 / PR #481 title gate) calls
    ``find_track_url`` to surface the Apple Music URL only; the synthesis
    path (no Discogs match OR library row fails the title gate — LML#487)
    calls ``find_track_metadata`` to surface URL + ``artwork_url`` +
    ``release_year`` from the same ``search_song`` response. Exactly one
    Apple Music API call per item either way — picking ``find_track_url``
    vs. ``find_track_metadata`` does not inflate the per-request quota.
    When ``apple_music`` is None (credentials unconfigured), the probe is
    skipped and ``apple_music_url`` is set from the library DB
    ``streaming_links`` override (if present) or stays None. The DB
    override always wins over the probe — see the final assignment
    ``apple_music_override or apple_music_url or None`` in
    ``lookup/enrichment/item.py``.

    **Behavior change vs. v0.5.0:** release/artist details (release_year,
    artist_bio, wikipedia_url) are fetched only for ``items_with_artwork[0]``.
    BS/iOS only consume the top-1 result, so paying N round-trips of Discogs
    cache (and on miss, API) latency for non-top-1 items was waste. The
    streaming-URL fallback build still runs per-result (cheap; no I/O).
    Gating is *positional*: a top-1 entry with ``artwork=None`` means no
    item in the response carries release-year/bio/wiki, even when items
    further down have artwork. BS/iOS only ever read ``results[0]`` so
    this is fine in practice; the lookup pipeline guarantees the strongest
    match is in position 0.

    **Behavior change vs. v0.6.0 (LML#401):** an item entering with
    ``artwork=None`` no longer round-trips as ``(item, None)``. It returns
    a synthesized ``DiscogsSearchResult(release_id=0, release_url="")``
    carrying only the streaming-URL fields (Apple via the authenticated
    ``AppleMusicClient`` + Spotify/YT/BC/SC search-URL fallbacks). Album-derived scalars
    (release_year, artist_bio, wikipedia_url) and the ``extended=True``
    payload stay None on the synthesized result, preserving the positional-
    gating invariant above. The ``(release_id=0, release_url="")`` pair is
    a cross-service contract: Backend-Service (BS#1185) keys off of it to
    skip ``extractAlbumMetadata`` while still consuming streaming URLs.
    Required for the WXYC/BS#1184 fix — releases on Apple Music that aren't
    in the WXYC/Discogs catalog now surface streaming buttons on iOS.

    **Behavior change vs. v0.7.0 (LML#487):** the synthesis path now also
    fires when ``artwork`` is non-None but its library row fails the
    LML#477 title gate (``score_match(album, item.title) < 80`` — the
    Noura Mint Seymali Tzenni-vs-Yenbett shape). The synthesized result
    additionally carries ``artwork_url`` and ``release_year`` from the
    ``find_track_metadata`` probe response, so flowsheet entries for an
    album absent from the WXYC catalog (but present on Apple Music)
    surface the *right* album's cover art on iOS / dj-site — not a
    sibling-album row's leaked Discogs image. ``release_year`` is sourced
    from the Apple Music ``releaseDate`` field; the Discogs-derived
    ``artist_bio`` / ``wikipedia_url`` / ``extended=True`` payload stay
    None on the synthesized result because they require a verified
    Discogs identity link (no equivalent surfaces from Apple).

    ``extended=True`` additionally populates the new DiscogsMatchResult
    fields LML already loaded during the release+artist fetches:
    ``discogs_artist_id``, ``tracklist``, ``genres``, ``styles``, ``label``,
    ``full_release_date``, ``artist_image_url``, ``profile_tokens``. Bio
    parsing uses ``CachedOnlyResolver`` when ``discogs_cache`` is provided
    — refs that miss the cache fall through as plain text, never trigger
    an inline Discogs API call. When ``discogs_cache`` is None (LML
    deployed without ``DATABASE_URL_DISCOGS``), bio parsing falls back to
    sync ``parse()`` which strips ID-based refs but keeps name-based and
    formatting tokens, so ``profile_tokens`` is still non-None for
    consistent client-side rendering.

    ``warm_cache=True`` schedules a fire-and-forget ``asyncio.create_task``
    after the response is composed that runs the *deep* async parse
    against the API-capable resolver, warming the PG cache for referenced
    entities so subsequent read-path lookups render richer. The task is
    not awaited — write-path callers (Backend-Service's flowsheet-linkage
    service) pay zero added latency. Concurrent warm tasks are bounded by
    ``_WARM_CACHE_CONCURRENCY`` to cap Discogs API amplification under
    burst load. Failures are logged via ``logger.exception`` so a stuck
    warm doesn't go silent.
    """
    if not discogs_service or not items_with_artwork:
        return items_with_artwork

    request_artist_stripped = (artist or "").strip()
    ctx = EnrichmentContext(
        discogs_service=discogs_service,
        mb_pg=mb_pg,
        apple_music=apple_music,
        spotify=spotify,
        bandcamp=bandcamp,
        entity_store=entity_store,
        discogs_cache_pg=discogs_cache_pg,
        library_db=library_db,
        song=song,
        album=album,
        artist=artist,
        request_artist_stripped=request_artist_stripped,
        artist_identity_split_enabled=_artist_identity_split_gate_enabled(),
        extended=extended,
        found_on_compilation=found_on_compilation,
    )

    # Top-1-only expensive enrichment; rationale in ``lookup/enrichment/top1.py``.
    (
        top1_year,
        top1_bio,
        top1_wiki,
        top1_release,
        top1_details,
    ) = await top1.fetch_top1_release_details(discogs_service, items_with_artwork[0][1])

    # LML#504: release-side artist-identity hop. Computed once (shared across
    # all items in this batch) since ``top1_release`` is by definition top-1-only
    # and the same value is read by every enrich_one call. The library-row
    # hop CANNOT be hoisted the same way: the per-item artwork gate (the
    # LML#487 synth path runs per item, not just for top-1) needs to know
    # whether THAT item's library row anchors the request — hoisting to
    # top-1-only suppresses every non-top-1 item's probe artwork
    # unconditionally. Per-item library-row computation in ``_artist_pair_verified``
    # is cheap (two short helper calls; both early-exit on empty inputs).
    top1_release_artist = getattr(top1_release, "artist", None)
    release_side_artist_verified = _artist_pair_verified(
        request_artist_stripped, top1_release_artist
    )
    # When ``top1_release`` was fetched but its ``.artist`` field is empty or
    # whitespace-only, treat the release hop as if there was nothing to verify
    # against and fall through to library-row-only verification. Covers the
    # LML#507 prefetch-skipped case, the artwork=None case, the
    # get_release-errored case, AND corrupted/empty Discogs release rows.
    release_anchor_present = isinstance(top1_release_artist, str) and bool(
        strip_discogs_disambig(top1_release_artist).strip()
    )

    # Cache-only deep parse of the top-1 bio for the extended path. Refs
    # that miss the cache fall through; we never fire a new API call here.
    # When the PG cache is unavailable (no DATABASE_URL_DISCOGS), fall
    # back to sync parse() — drops ID-based refs but keeps name and
    # formatting tokens, so clients still get structured rendering.
    top1_profile_tokens: list[ResolvedToken] | None = None
    if extended and top1_bio:
        if discogs_cache is not None:
            try:
                top1_profile_tokens = await parse_async(top1_bio, CachedOnlyResolver(discogs_cache))
            except Exception:
                logger.exception("Cache-only bio parse failed; falling back to sync parse")
                top1_profile_tokens = parse(top1_bio)
        else:
            top1_profile_tokens = parse(top1_bio)

    enriched = await asyncio.gather(
        *[
            item.enrich_one(
                ctx,
                library_item,
                artwork,
                is_top1=(idx == 0),
                top1_year=top1_year,
                top1_bio=top1_bio,
                top1_wiki=top1_wiki,
                top1_release=top1_release,
                top1_details=top1_details,
                top1_profile_tokens=top1_profile_tokens,
                release_side_artist_verified=release_side_artist_verified,
                release_anchor_present=release_anchor_present,
            )
            for idx, (library_item, artwork) in enumerate(items_with_artwork)
        ]
    )

    # Write-path warm: fire-and-forget deep async parse of the top-1 bio
    # using the API-capable resolver, so the PG cache gets populated for
    # `[a…]`/`[r…]`/`[m…]` references. The task is intentionally not
    # awaited — read-path latency is unaffected. The task reference is
    # parked in ``_background_tasks`` so the GC can't reap it mid-flight
    # (asyncio holds only weak refs to tasks).
    #
    # LML#504: don't warm a bio the response itself suppressed. The deep
    # parse fires per-ref Discogs API calls (DiscogsServiceResolver: cache
    # → API → write-back) — wasting those on a bio iOS will never render
    # is pure quota burn. Inspect the top-1 enriched result rather than
    # re-deriving the gate so the warm check is locked in step with the
    # actual surfaced output.
    top1_enriched_result = enriched[0][1] if enriched else None
    top1_bio_surfaced = top1_enriched_result is not None and bool(top1_enriched_result.artist_bio)
    if warm_cache and top1_bio and top1_bio_surfaced:
        task = asyncio.create_task(background._warm_bio_cache(top1_bio, discogs_service))
        background._background_tasks.add(task)
        task.add_done_callback(background._background_tasks.discard)

    return list(enriched)
