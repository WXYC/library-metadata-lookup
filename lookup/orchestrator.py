"""Lookup orchestrator: the core search logic extracted from request-o-matic.

This module contains the perform_lookup() function that orchestrates the full
search pipeline: artist correction -> album resolution -> search strategies ->
track validation -> artwork fetch -> metadata enrichment -> context message.
"""

import asyncio
import logging
from functools import partial
from typing import Any

import sentry_sdk
from wxyc_etl.text import to_match_form as normalize_for_comparison
from wxyc_fastapi.observability import (
    RequestTelemetry,
)

from clients.bandcamp import BandcampClient
from clients.streaming.apple_music import AppleMusicClient
from clients.streaming.spotify import SpotifyClient
from core.search import (
    execute_search_pipeline,
    get_search_type_from_state,
)
from discogs.cache_service import DiscogsCacheService
from discogs.lookup import lookup_releases_by_track
from discogs.models import (
    DiscogsSearchResult,
)
from discogs.service import DiscogsService
from entity.sources import PgSource, PgSourceProtocol
from entity.store import EntityStore, Identity
from generated.api_models import (
    LibraryCatalogItem,
    ReconciledIdentity,
)
from library.db import LibraryDB
from library.models import LibraryItem
from lookup.artwork import fetch_artwork_for_items
from lookup.enrichment import enrich_artwork_results
from lookup.external_search import (
    search_external_albums,
    search_external_artists,
    search_external_tracks,
)
from lookup.matching import (
    _filter_results_by_song_as_album_title,
    library_artist_for,
    limit_results,
)
from lookup.models import LookupRequest, LookupResponse, LookupResultItem
from lookup.release_resolution import (
    ResolvedRelease,
)
from lookup.strategies import build_strategies
from lookup.strategies.artist_plus_album import search_library_with_fallback
from lookup.strategies.library_miss import _library_miss_discogs_search
from lookup.strategies.song_as_artist import search_song_as_artist
from lookup.strategies.song_as_track import search_song_as_track
from lookup.strategies.swapped_interpretation import search_with_alternative_interpretation
from lookup.strategies.track_on_compilation import search_compilations_for_track
from lookup.validation import (
    filter_results_by_track_validation,
    find_library_albums_with_cached_track,
)
from services.parser import MessageType, ParsedRequest

logger = logging.getLogger(__name__)


async def resolve_albums_for_track(
    parsed: ParsedRequest,
    discogs_service: DiscogsService | None = None,
) -> tuple[list[str], bool]:
    """Resolve album names for a track if not provided.

    Searches Discogs for ALL releases containing the track, not just the first one.

    Returns:
        Tuple of (list of album names, song_not_found_flag)
    """
    album_is_missing = not parsed.album
    album_is_artist = (
        parsed.album
        and parsed.artist
        and normalize_for_comparison(parsed.album).strip()
        == normalize_for_comparison(parsed.artist).strip()
    )

    if parsed.song and parsed.artist and (album_is_missing or album_is_artist):
        if album_is_artist:
            logger.info(f"Album '{parsed.album}' appears to be artist name, looking up albums")
        try:
            releases = await lookup_releases_by_track(
                parsed.song, parsed.artist, limit=10, service=discogs_service
            )
            if releases:
                albums = []
                artist_normalized = normalize_for_comparison(parsed.artist)
                for release_artist, album in releases:
                    if normalize_for_comparison(release_artist).startswith(artist_normalized):
                        if album not in albums:
                            albums.append(album)
                if albums:
                    logger.info(f"Found {len(albums)} albums for song '{parsed.song}': {albums}")
                    return albums, False
            logger.info(f"Could not find albums for song '{parsed.song}'")
            return [], True
        except Exception as e:
            logger.warning(f"Track lookup failed: {e}")
            return [], True
    return [parsed.album] if parsed.album else [], False


def build_context_message(
    parsed: ParsedRequest,
    found_on_compilation: bool,
    song_not_found: bool,
    has_results: bool = True,
) -> str | None:
    """Build context message based on search results."""
    if found_on_compilation:
        return f'Found "{parsed.song}" by {parsed.artist} on:'

    if song_not_found and has_results:
        if parsed.song and parsed.album:
            return (
                f'"{parsed.album}" not found in the library, '
                f"but here are other albums by {parsed.artist}:"
            )
        elif parsed.song:
            return (
                f'"{parsed.song}" is not on any album in the library, '
                f"but here are some albums by {parsed.artist}:"
            )
    elif song_not_found and not has_results:
        if parsed.song and parsed.artist:
            return f'"{parsed.song}" by {parsed.artist} not found in library.'

    return None


def _identity_to_reconciled(identity: Identity) -> ReconciledIdentity:
    """Convert an EntityStore Identity dataclass to the shared ReconciledIdentity schema."""
    return ReconciledIdentity(
        discogs_artist_id=identity.discogs_artist_id,
        musicbrainz_artist_id=identity.musicbrainz_artist_id,
        wikidata_qid=identity.wikidata_qid,
        spotify_artist_id=identity.spotify_artist_id,
        apple_music_artist_id=identity.apple_music_artist_id,
        bandcamp_id=identity.bandcamp_id,
    )


async def _resolve_identities(
    artist_names: list[str], entity_store: EntityStore
) -> dict[str, ReconciledIdentity]:
    """Look up reconciled identities for unique artist names.

    Returns a dict keyed by the artist name. Names not found in the entity
    store are omitted, so callers should treat a missing key as "no identity."
    Lookups across the unique names run concurrently.
    """
    unique = list({name for name in artist_names if name})
    if not unique:
        return {}

    identities = await asyncio.gather(
        *(entity_store.get_identity(name) for name in unique),
        return_exceptions=True,
    )

    result: dict[str, ReconciledIdentity] = {}
    for name, identity in zip(unique, identities, strict=True):
        if isinstance(identity, BaseException):
            logger.warning("EntityStore.get_identity failed for %r: %s", name, identity)
            continue
        if identity is not None:
            result[name] = _identity_to_reconciled(identity)
    return result


async def perform_lookup(
    request: LookupRequest,
    db: LibraryDB,
    discogs_service: DiscogsService | None,
    telemetry: RequestTelemetry,
    *,
    entity_store: EntityStore | None = None,
    discogs_cache: DiscogsCacheService | None = None,
    mb_pg: PgSourceProtocol | None = None,
    apple_music: AppleMusicClient | None = None,
    spotify: SpotifyClient | None = None,
    bandcamp: BandcampClient | None = None,
    discogs_cache_pg: PgSource | None = None,
    caller_budget_ms: int | None = None,
    allow_release_resolution_fallback: bool = True,
) -> LookupResponse:
    """Orchestrate the full lookup pipeline.

    Steps:
    1. Correct artist spelling
    2. Resolve album names from Discogs (if song provided without album)
    3. Execute search strategy pipeline
    4. Validate fallback results against Discogs tracklists
    5. Fetch artwork for results
    6. Build context message
    """
    # Build a ParsedRequest from the LookupRequest for compatibility with
    # search functions that expect ParsedRequest
    parsed = ParsedRequest(
        song=request.song,
        album=request.album,
        artist=request.artist,
        is_request=True,
        message_type=MessageType.REQUEST,
        raw_message=request.raw_message,
    )

    library_results: list[LibraryItem] = []
    items_with_artwork: list[tuple[LibraryItem, DiscogsSearchResult | None]] = []
    song_not_found = False
    found_on_compilation = False
    discogs_titles: dict[int, ResolvedRelease] = {}
    corrected_artist: str | None = None

    # Steps 1+2: Correct artist spelling and resolve albums (parallel)
    if parsed.artist:
        correction_task = db.find_similar_artist(parsed.artist)
        with telemetry.track_step("album_lookup"):
            if parsed.song and not parsed.album:
                telemetry.record_api_call("discogs")
            corrected, (albums_for_search, song_not_found) = await asyncio.gather(
                correction_task,
                resolve_albums_for_track(parsed, discogs_service),
            )
        if corrected:
            corrected_artist = corrected
            # Two-channel seam (WXYC/library-metadata-lookup#626): do NOT
            # overwrite ``parsed.artist``. The typed value must keep flowing to
            # every Discogs-facing path (the three Discogs-aware strategies'
            # probes, ``validate_release_for_track``, and the library-miss
            # Discogs probe), or a *distinct* non-library artist one edit from a
            # library name gets snapped into the library's vocabulary on Discogs.
            # Thread the correction on ``library_artist``, read only by the
            # library-side legs (``db.search`` queries + ``artist_matches_item``
            # match-backs).
            parsed.library_artist = corrected
    else:
        with telemetry.track_step("album_lookup"):
            albums_for_search, song_not_found = await resolve_albums_for_track(
                parsed, discogs_service
            )

    # Step 3: Execute search strategy pipeline
    with telemetry.track_step("library_search"):
        # Strategies hold their own ``db`` handle (no per-call db arg on the
        # runner post-#399). The discogs_service is captured via ``partial`` on
        # the strategies that need it; ARTIST_PLUS_ALBUM is the only
        # library-only strategy (SWAPPED_INTERPRETATION now cross-references the
        # non-artist token as a track via Discogs — LML#622).
        # ``discogs_cache_pg`` threads the #632 row-less resolution cache into the
        # three Discogs-aware strategies' A1 carry-through (LML#628). The library
        # channel and ARTIST_PLUS_ALBUM / SONG_AS_ARTIST never touch it.
        # LML#652: the per-request bulk kill switch (``False`` on /lookup/bulk)
        # threads into the four Discogs-aware row-less producers here via the
        # partials, and into the fifth (the A4 cached-track safety net) at its
        # own Step-3b call site below — exactly as it already reaches
        # ``fetch_artwork_for_items`` (Step 4) for #604's lazy fallback. So no
        # row-less item surfaces, nor any carry-through resolve or #632 cache
        # write, on the backfill path.
        strategies = build_strategies(
            db,
            search_library_func=search_library_with_fallback,
            search_alternative_func=partial(
                search_with_alternative_interpretation,
                discogs_service=discogs_service,
                pg=discogs_cache_pg,
                allow_release_resolution_fallback=allow_release_resolution_fallback,
            ),
            search_compilations_func=partial(
                search_compilations_for_track,
                discogs_service=discogs_service,
                pg=discogs_cache_pg,
                allow_release_resolution_fallback=allow_release_resolution_fallback,
            ),
            search_song_as_artist_func=partial(
                search_song_as_artist,
                discogs_service=discogs_service,
                allow_release_resolution_fallback=allow_release_resolution_fallback,
            ),
            search_song_as_track_func=partial(
                search_song_as_track,
                discogs_service=discogs_service,
                pg=discogs_cache_pg,
                allow_release_resolution_fallback=allow_release_resolution_fallback,
            ),
        )

        search_state = await execute_search_pipeline(
            parsed=parsed,
            raw_message=request.raw_message or "",
            strategies=strategies,
            albums_for_search=albums_for_search,
            song_not_found=song_not_found,
            caller_budget_ms=caller_budget_ms,
        )

        library_results = limit_results(search_state.results)
        song_not_found = search_state.song_not_found
        found_on_compilation = search_state.found_on_compilation
        discogs_titles = search_state.discogs_titles
        search_type = get_search_type_from_state(search_state)

        if found_on_compilation:
            telemetry.record_api_call("discogs")

    # Step 3a: Library-miss Discogs search (LML#583).
    # When the entire search pipeline returned no library results AND the request
    # carries both artist and album, probe Discogs directly. A confident match
    # (80/80-floor via find_best_typed_match) is synthesized into a
    # LibraryItem(id=0) + DiscogsSearchResult pair and handed directly to Step
    # 4b (enrich_artwork_results) — bypassing fetch_artwork_for_items (which
    # would re-search and risk a second floor mis-selection) and bypassing Step
    # 3b track validation (the synthesized item is already 80-floored on
    # (artist, album) jointly; routing it through an unfloored top-1 re-search
    # has non-zero probability of flipping to a different release).
    #
    # Gated on ``allow_release_resolution_fallback`` (the bulk kill switch, LML#671):
    # /lookup/bulk passes ``False`` so the 35k-album backfill never pays a per-row
    # Discogs ``search()`` here, nor surfaces a row-less ``LibraryItem(id=0)``.
    # An album-only backfill item (artist+album, no song) hits *this* producer —
    # not the five song-bearing LML#652 producers — so gating it is what makes the
    # "no per-row Discogs surfacing/cost on bulk" guarantee true for the real drain.
    # /lookup keeps the default (``True``), so #583 fires there exactly as before.
    library_miss_outcome: str | None = None
    if (
        not library_results
        and allow_release_resolution_fallback
        and parsed.artist
        and parsed.album
        and parsed.album.strip()
    ):
        with telemetry.track_step("library_miss_discogs_search"):
            miss_match = await _library_miss_discogs_search(parsed, discogs_service)
        if miss_match is not None:
            synthesized_lib_item, discogs_result = miss_match
            items_with_artwork = [(synthesized_lib_item, discogs_result)]
            # A synthesized Discogs match resolves the request — clear song_not_found so
            # build_context_message and LookupResponse.song_not_found reflect reality.
            song_not_found = False
            library_miss_outcome = "library_miss_discogs_match"
            telemetry.record_api_call("discogs")
        elif discogs_service is not None:
            # Discogs was available and searched but found no confident match.
            library_miss_outcome = "library_miss_no_discogs_match"

    # Step 3b: Validate results against Discogs track data.
    # Synthesized id=0 items from Step 3a are excluded: library_results is empty
    # on that path, so the gate below never fires. The filter on real_results is
    # a defensive guard for any future path that might add id=0 items to
    # library_results.
    real_results = [r for r in library_results if r.id != 0]
    if real_results and parsed.song and parsed.artist:
        if not found_on_compilation:
            # Normal case: validate all results against Discogs tracklists
            with telemetry.track_step("track_validation"):
                validated = await filter_results_by_track_validation(
                    real_results, parsed.song, parsed.artist, discogs_service
                )
                if validated:
                    library_results = validated
                    song_not_found = False
                elif song_not_found:
                    # Per-result validation confirmed nothing. Ask the local
                    # PG cache directly: "any release by this artist whose
                    # tracklist contains this song?" — and promote the matching
                    # library album. Catches the case where the upstream
                    # track→releases lookup missed a release the cache holds.
                    promoted, promoted_titles = await find_library_albums_with_cached_track(
                        db,
                        parsed.song,
                        parsed.artist,
                        discogs_service,
                        match_artist=library_artist_for(parsed),
                        allow_release_resolution_fallback=allow_release_resolution_fallback,
                    )
                    if promoted:
                        library_results = promoted
                        # A4 (LML#629): a row-less promotion carries its resolved
                        # release on the seam so Step 4 binds discogs_url by id.
                        discogs_titles = {**discogs_titles, **promoted_titles}
                        song_not_found = False
                    else:
                        # Last resort before declaring song-not-found: the
                        # request shape "<album-title>, <artist>" can route to
                        # us as ``song=<album-title>``. If a surviving result's
                        # title clears the floor against ``parsed.song``, the
                        # user wanted that album. Surfacing it as found-the-
                        # album avoids the misleading 'not on any album'
                        # message about a row sitting in the result list.
                        title_matches = _filter_results_by_song_as_album_title(
                            library_results, parsed.song
                        )
                        if title_matches:
                            logger.info(
                                f"Promoted {len(title_matches)} of {len(library_results)} "
                                f"artist-fallback row(s) whose title matches song "
                                f"'{parsed.song}' — treating as album request"
                            )
                            library_results = title_matches
                            song_not_found = False
        elif search_state.artist_fallback_results:
            # Compilation found, but the artist's own album may also contain the track.
            # Validate the artist fallback results (saved before compilation search
            # replaced them) and prepend any confirmed matches.
            with telemetry.track_step("track_validation"):
                validated = await filter_results_by_track_validation(
                    search_state.artist_fallback_results,
                    parsed.song,
                    parsed.artist,
                    discogs_service,
                )
                if validated:
                    compilation_ids = {r.id for r in library_results}
                    merged = [r for r in validated if r.id not in compilation_ids]
                    merged.extend(library_results)
                    library_results = merged

    # Step 3c: Populate streaming status
    if library_results and getattr(db, "_has_streaming_links", None) is True:
        streaming_status = await db.get_streaming_status([r.id for r in library_results])
        for result in library_results:
            result.on_streaming = streaming_status.get(result.id, False)

    # Step 4: Fetch artwork
    with telemetry.track_step("artwork_fetch"):
        if library_results:
            for _ in library_results:
                telemetry.record_api_call("discogs")
            items_with_artwork = await fetch_artwork_for_items(
                library_results,
                discogs_service,
                discogs_titles,
                song=parsed.song,
                album=parsed.album,
                allow_release_resolution_fallback=allow_release_resolution_fallback,
                found_on_compilation=found_on_compilation,
            )

    # The generated LookupRequest models these as bool | None (api.yaml
    # describes them with `default: false` but the field is not in the
    # required set, so callers can omit them). Coerce once and reuse.
    extended_mode = bool(request.extended)
    warm_cache_mode = bool(request.warm_cache)

    # Step 4b: Enrich with release year, artist details, streaming links
    with telemetry.track_step("metadata_enrichment"):
        if items_with_artwork:
            items_with_artwork = await enrich_artwork_results(
                items_with_artwork,
                discogs_service,
                song=parsed.song,
                album=parsed.album,
                artist=parsed.artist,
                library_db=db,
                extended=extended_mode,
                warm_cache=warm_cache_mode,
                discogs_cache=discogs_cache,
                mb_pg=mb_pg,
                apple_music=apple_music,
                spotify=spotify,
                bandcamp=bandcamp,
                entity_store=entity_store,
                discogs_cache_pg=discogs_cache_pg,
                found_on_compilation=found_on_compilation,
            )

    # Project the request-side flags and result-quality signals onto the
    # active Sentry transaction so the trace can be filtered by request mode
    # (lml.lookup.extended, lml.lookup.warm_cache) and by match outcome
    # (lookup.results_count, lookup.match_type — LML#158). Mirrors the
    # cache_stats projection pattern (LML#213).
    try:
        scope = sentry_sdk.get_current_scope()
        if scope.transaction is not None:
            scope.transaction.set_data("lml.lookup.extended", extended_mode)
            scope.transaction.set_data("lml.lookup.warm_cache", warm_cache_mode)
            scope.transaction.set_data(
                "lookup.results_count",
                len(items_with_artwork) if items_with_artwork else len(library_results),
            )
            scope.transaction.set_data("lookup.match_type", search_type)
            if library_miss_outcome is not None:
                scope.transaction.set_data("lookup.outcome", library_miss_outcome)
    except Exception:
        # Observability must not break the request path.
        pass

    # Step 5: Build context message
    context = build_context_message(
        parsed,
        found_on_compilation,
        song_not_found,
        has_results=bool(library_results) or bool(items_with_artwork),
    )

    # Step 6: Resolve external identifiers for each result's artist.
    # Collect artist names from library_results (normal path) and from
    # items_with_artwork (synthesized Step 3a path, where library_results is empty
    # but the synthesized LibraryItem carries a real artist name worth resolving).
    identities_by_artist: dict[str, ReconciledIdentity] = {}
    _identity_artist_names = [item.artist for item in library_results if item.artist] + [
        item.artist for item, _ in items_with_artwork if item.id == 0 and item.artist
    ]
    if entity_store is not None and _identity_artist_names:
        with telemetry.track_step("identity_resolution"):
            identities_by_artist = await _resolve_identities(_identity_artist_names, entity_store)

    def _identity_for(item: LibraryItem) -> ReconciledIdentity | None:
        if not item.artist:
            return None
        return identities_by_artist.get(item.artist)

    # Build response (convert internal models to API contract models)
    matched_via_by_id = search_state.matched_via_by_id
    result_items = []
    if items_with_artwork:
        for item, artwork in items_with_artwork:
            # Synthesized items (id=0, from Step 3a) have no library call-number
            # components; build LibraryCatalogItem directly with the "(external)"
            # sentinel that Backend-Service already understands (same contract as
            # the Step 7 include_external_caches path).
            catalog_item = (
                LibraryCatalogItem(
                    id=0,
                    artist=item.artist,
                    title=item.title,
                    call_number="(external)",
                    library_url="",
                )
                if item.id == 0
                else item.to_catalog_item()
            )
            result_items.append(
                LookupResultItem(
                    library_item=catalog_item,
                    artwork=artwork.to_match_result() if artwork else None,
                    reconciled_identity=_identity_for(item),
                    # Synthesized items (id=0) carry no track-title-provenance hint;
                    # do not look up key 0 in matched_via_by_id to prevent accidental
                    # collision with any future strategy that might write to that key.
                    matched_via=None if item.id == 0 else matched_via_by_id.get(item.id),
                )
            )
    elif library_results:
        for item in library_results:
            result_items.append(
                LookupResultItem(
                    library_item=item.to_catalog_item(),
                    reconciled_identity=_identity_for(item),
                    matched_via=matched_via_by_id.get(item.id),
                )
            )

    # Step 7: External-cache fallback (Phase 1.5 + 1.7 mojibake recovery).
    # Opt-in via include_external_caches. The lossy-mojibake matcher sends
    # column-typed bodies, so we dispatch by which skeleton field is set:
    # artist takes precedence (highest-precision lookup), then album, then
    # song. A bare raw_message with no typed field skips the fallback —
    # LABEL_NAME is too noisy to be useful here.
    external_source: str | None = "library" if result_items else None
    if not result_items and request.include_external_caches:
        candidates: list[dict[str, Any]] = []
        source: str | None = None
        if parsed.artist:
            with telemetry.track_step("external_cache_fallback"):
                rows, source = await search_external_artists(
                    parsed.artist,
                    discogs_cache=discogs_cache,
                    mb_pg=mb_pg,
                )
            candidates = [{"artist": r["name"], "title": ""} for r in rows]
        elif parsed.album:
            with telemetry.track_step("external_cache_fallback"):
                rows, source = await search_external_albums(
                    parsed.album,
                    discogs_cache=discogs_cache,
                    mb_pg=mb_pg,
                )
            candidates = [{"artist": r["artist"], "title": r["title"]} for r in rows]
        elif parsed.song:
            with telemetry.track_step("external_cache_fallback"):
                rows, source = await search_external_tracks(
                    parsed.song,
                    discogs_cache=discogs_cache,
                    mb_pg=mb_pg,
                )
            candidates = [{"artist": r["artist"], "title": r["title"]} for r in rows]

        if candidates:
            external_source = source
            for candidate in candidates:
                result_items.append(
                    LookupResultItem(
                        library_item=LibraryCatalogItem(
                            id=0,
                            artist=candidate["artist"],
                            title=candidate["title"] or None,
                            call_number="(external)",
                            library_url="",
                        ),
                    )
                )

    return LookupResponse(
        results=result_items,
        search_type=search_type,
        song_not_found=song_not_found,
        found_on_compilation=found_on_compilation,
        context_message=context,
        corrected_artist=corrected_artist,
        external_source=external_source,
        timeout=search_state.timed_out,
    )
