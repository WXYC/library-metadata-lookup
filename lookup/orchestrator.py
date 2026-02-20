"""Lookup orchestrator: the core search logic extracted from request-parser.

This module contains the perform_lookup() function that orchestrates the full
search pipeline: artist correction -> album resolution -> search strategies ->
track validation -> artwork fetch -> context message.
"""

import asyncio
import logging
import re
from functools import partial

from core.matching import (
    MAX_SEARCH_RESULTS,
    STOPWORDS,
    is_compilation_artist,
    normalize_for_comparison,
)
from core.search import (
    build_strategies,
    execute_search_pipeline,
    get_search_type_from_state,
)
from core.telemetry import RequestTelemetry
from discogs.lookup import lookup_releases_by_artist, lookup_releases_by_track
from discogs.models import DiscogsSearchRequest, DiscogsSearchResult
from discogs.service import DiscogsService
from library.db import LibraryDB
from library.models import LibraryItem
from lookup.models import LookupRequest, LookupResponse, LookupResultItem
from services.parser import MessageType, ParsedRequest

logger = logging.getLogger(__name__)


def limit_results(results: list) -> list:
    """Limit results to MAX_SEARCH_RESULTS."""
    return results[:MAX_SEARCH_RESULTS]


def artist_matches_item(item: LibraryItem, artist: str) -> bool:
    """Check if a library item matches the given artist name.

    Checks both the primary artist and alternate_artist_name fields.
    """
    artist_normalized = normalize_for_comparison(artist)
    if normalize_for_comparison(item.artist).startswith(artist_normalized):
        return True
    if item.alternate_artist_name:
        if normalize_for_comparison(item.alternate_artist_name).startswith(artist_normalized):
            return True
    return False


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


def filter_results_by_artist(
    results: list[LibraryItem],
    artist: str | None,
) -> list[LibraryItem]:
    """Filter library results to only include those matching the artist.

    Requires the searched artist name to appear at the START of the result's
    artist field (case-insensitive).
    """
    if not artist:
        return results

    filtered = []
    for item in results:
        if artist_matches_item(item, artist):
            filtered.append(item)

    if len(filtered) < len(results):
        logger.info(
            f"Filtered {len(results)} results to {len(filtered)} matching artist '{artist}'"
        )

    return filtered


async def search_with_alternative_interpretation(
    db: LibraryDB,
    part1: str,
    part2: str,
) -> tuple[list[LibraryItem], None]:
    """Try searching with both artist/title interpretations for 'X - Y' format."""
    query1 = f"{part1} {part2}"
    results1 = await db.search(query=query1, limit=MAX_SEARCH_RESULTS)
    results1 = filter_results_by_artist(results1, part1)

    query2 = f"{part2} {part1}"
    results2 = await db.search(query=query2, limit=MAX_SEARCH_RESULTS)
    results2 = filter_results_by_artist(results2, part2)

    if results1 and not results2:
        logger.info(f"Alternative search matched with '{part1}' as artist")
        return results1, None
    elif results2 and not results1:
        logger.info(f"Alternative search matched with '{part2}' as artist")
        return results2, None
    elif results1 and results2:
        logger.info("Alternative search matched both interpretations, combining results")
        seen_ids = set()
        combined = []
        for item in results1 + results2:
            if item.id not in seen_ids:
                combined.append(item)
                seen_ids.add(item.id)
        return limit_results(combined), None

    return [], None


async def search_song_as_artist(
    db: LibraryDB,
    song_as_artist: str,
    discogs_service: DiscogsService | None = None,
) -> tuple[list[LibraryItem], None]:
    """Try searching using the parsed song title as an artist name."""
    logger.info(f"Trying song '{song_as_artist}' as artist name")

    results = await db.search(query=song_as_artist, limit=MAX_SEARCH_RESULTS)
    results = filter_results_by_artist(results, song_as_artist)
    if results:
        logger.info(f"Found {len(results)} results treating '{song_as_artist}' as artist")
        return results, None

    logger.info(f"No direct matches, searching Discogs for releases by '{song_as_artist}'")
    discogs_releases = await lookup_releases_by_artist(
        song_as_artist, limit=10, service=discogs_service
    )

    if not discogs_releases:
        logger.info(f"No Discogs releases found for '{song_as_artist}'")
        return [], None

    logger.info(f"Found {len(discogs_releases)} Discogs releases for '{song_as_artist}'")

    seen_ids = set()
    for _discogs_artist, album_title in discogs_releases:
        if not album_title:
            continue

        album_results = await db.search(query=album_title, limit=MAX_SEARCH_RESULTS)

        for item in album_results:
            if item.id in seen_ids:
                continue

            if artist_matches_item(item, song_as_artist) or is_compilation_artist(
                item.artist or ""
            ):
                results.append(item)
                seen_ids.add(item.id)
                logger.info(f"Found '{item.artist} - {item.title}' via Discogs cross-reference")

        if len(results) >= MAX_SEARCH_RESULTS:
            break

    if results:
        logger.info(
            f"Found {len(results)} results via Discogs cross-reference for '{song_as_artist}'"
        )

    return limit_results(results), None


async def search_library_with_fallback(
    db: LibraryDB,
    parsed: ParsedRequest,
    albums: list[str],
) -> tuple[list[LibraryItem], bool]:
    """Search library with artist+album(s), falling back to artist+song or artist-only.

    Returns:
        Tuple of (library_results, song_not_found_flag)
    """
    all_results: list[LibraryItem] = []
    seen_ids: set[int] = set()

    if parsed.artist and albums:
        for album in albums:
            query = f"{parsed.artist} {album}"
            results = await db.search(query=query, limit=MAX_SEARCH_RESULTS)
            results = filter_results_by_artist(results, parsed.artist)

            album_lower = album.lower()
            album_normalized = re.sub(r"[^\w\s]", " ", album_lower)
            album_normalized = " ".join(album_normalized.split())
            album_words = {w for w in album_normalized.split() if len(w) > 2 and w not in STOPWORDS}
            filtered_results = []
            for item in results:
                item_title_lower = (item.title or "").lower()
                item_normalized = re.sub(r"[^\w\s]", " ", item_title_lower)
                item_normalized = " ".join(item_normalized.split())
                item_words = {
                    w for w in item_normalized.split() if len(w) > 2 and w not in STOPWORDS
                }
                common_words = album_words & item_words
                if len(item_words) <= 2:
                    if album_normalized.startswith(item_normalized):
                        filtered_results.append(item)
                elif len(common_words) >= 2:
                    filtered_results.append(item)
            results = filtered_results

            for item in results:
                if item.id not in seen_ids:
                    seen_ids.add(item.id)
                    all_results.append(item)

        if all_results:
            primary_album_lower = albums[0].lower()
            all_results.sort(
                key=lambda r: primary_album_lower in (r.title or "").lower(),
                reverse=True,
            )
            return all_results, False

    if parsed.artist and parsed.song:
        query = f"{parsed.artist} {parsed.song}"
        results = await db.search(query=query, limit=MAX_SEARCH_RESULTS)
        results = filter_results_by_artist(results, parsed.artist)

        if results:
            song_lower = parsed.song.lower()
            results.sort(
                key=lambda r: song_lower in (r.title or "").lower(),
                reverse=True,
            )
            return results, True

    if not all_results and parsed.artist:
        logger.info(f"No results for albums {albums}, trying artist only: '{parsed.artist}'")
        results = await db.search(query=parsed.artist, limit=MAX_SEARCH_RESULTS)
        results = filter_results_by_artist(results, parsed.artist)
        if results:
            return results, True

    return all_results, False


async def search_compilations_for_track(
    db: LibraryDB,
    parsed: ParsedRequest,
    discogs_service: DiscogsService | None = None,
) -> tuple[list[LibraryItem], dict[int, str]]:
    """Search for track on compilation albums using Discogs and library keyword search."""
    if not parsed.song or not parsed.artist:
        return [], {}

    logger.info(f"Searching for '{parsed.song}' on other releases (compilations, etc.)")

    results = []
    seen_ids = set()
    discogs_titles: dict[int, str] = {}

    keyword_matches = []
    try:
        artist_words = (
            re.sub(r"[^\w\s]", " ", parsed.artist.lower()).split() if parsed.artist else []
        )
        song_words = re.sub(r"[^\w\s]", " ", parsed.song.lower()).split() if parsed.song else []

        sig_artist = [w for w in artist_words if len(w) > 3 and w not in STOPWORDS]
        sig_song = [w for w in song_words if len(w) > 3 and w not in STOPWORDS]

        query_words = sig_artist[:2] + sig_song[:2]

        if query_words:
            keyword_query = " ".join(query_words)
            logger.info(f"Trying direct keyword search: '{keyword_query}'")
            keyword_results = await db.search(query=keyword_query, limit=MAX_SEARCH_RESULTS)

            if keyword_results:
                filtered_results = []
                for item in keyword_results:
                    if artist_matches_item(item, parsed.artist):
                        filtered_results.append(item)
                    elif is_compilation_artist(item.artist or ""):
                        filtered_results.append(item)

                if filtered_results:
                    logger.info(
                        f"Found {len(filtered_results)} matches via keyword search "
                        f"(after artist filter)"
                    )
                    keyword_matches = filtered_results
    except Exception as e:
        logger.warning(f"Keyword search failed: {e}")
        keyword_matches = []

    try:
        raw_lower = parsed.raw_message.lower()
        song_search = parsed.song

        remix_match = re.search(r"\((.*?(?:remix|mix|version|edit).*?)\)", raw_lower, re.IGNORECASE)
        if remix_match and parsed.song.lower() in raw_lower:
            song_search = f"{parsed.song} ({remix_match.group(1)})"
            logger.info(f"Using full track name with version info: '{song_search}'")

        releases = await lookup_releases_by_track(
            song_search, parsed.artist, service=discogs_service
        )
        logger.info(f"Found {len(releases)} releases with '{song_search}' on Discogs")

        for release_artist, release_album in releases:
            if parsed.artist and release_album.lower().strip() == parsed.artist.lower().strip():
                logger.debug(f"Skipping '{release_album}' - appears to be artist name, not album")
                continue

            if len(release_album.strip()) < 3:
                continue

            matches = await search_album_fuzzy(db, release_album)

            if matches and parsed.artist:
                filtered_matches = []
                discogs_is_compilation = is_compilation_artist(release_artist)

                for match in matches:
                    if artist_matches_item(match, parsed.artist):
                        filtered_matches.append(match)
                    elif discogs_is_compilation and is_compilation_artist(match.artist or ""):
                        filtered_matches.append(match)
                matches = filtered_matches

            if matches:
                logger.info(
                    f"Found '{parsed.song}' in library on '{matches[0].title}' "
                    f"(matched from Discogs: '{release_album}')"
                )
                for match in matches:
                    if match.id not in seen_ids:
                        results.append(match)
                        seen_ids.add(match.id)
                        discogs_titles[match.id] = release_album

                if len(results) >= MAX_SEARCH_RESULTS:
                    break
    except Exception as e:
        logger.warning(f"Failed to search for track on other releases: {e}")

    if not results and keyword_matches:
        logger.info("Discogs search found nothing, using keyword matches as fallback")
        for item in keyword_matches[:1]:
            if item.id not in seen_ids:
                results.append(item)
                seen_ids.add(item.id)

    if results and parsed.song:
        song_lower = parsed.song.lower()
        results.sort(
            key=lambda r: song_lower in (r.title or "").lower(),
            reverse=True,
        )

    return limit_results(results), discogs_titles


async def search_album_fuzzy(db: LibraryDB, album_title: str) -> list[LibraryItem]:
    """Search for album with fuzzy keyword matching."""
    from rapidfuzz import fuzz

    results = await db.search(query=album_title, limit=MAX_SEARCH_RESULTS)

    if not results:
        words = re.sub(r"[^\w\s]", " ", album_title.lower()).split()
        significant_words = [w for w in words if len(w) > 3 and w not in STOPWORDS]

        if significant_words:
            fuzzy_query = " ".join(significant_words[:4])
            logger.info(f"Exact match failed for '{album_title}', trying fuzzy: '{fuzzy_query}'")
            results = await db.search(query=fuzzy_query, limit=MAX_SEARCH_RESULTS)

            if results:
                album_lower = album_title.lower()
                filtered_results = []
                for result in results:
                    result_title_lower = (result.title or "").lower()

                    keyword_matches = sum(
                        1 for word in significant_words if word in result_title_lower
                    )
                    similarity = fuzz.token_set_ratio(album_lower, result_title_lower)

                    if keyword_matches >= 2 and similarity >= 60:
                        logger.debug(
                            f"Album match: '{result.title}' "
                            f"(keywords={keyword_matches}, similarity={similarity})"
                        )
                        filtered_results.append(result)
                    else:
                        logger.debug(
                            f"Album rejected: '{result.title}' "
                            f"(keywords={keyword_matches}, similarity={similarity})"
                        )

                results = filtered_results

    return results


async def filter_results_by_track_validation(
    results: list[LibraryItem],
    song: str | None,
    artist: str | None,
    discogs_service: DiscogsService | None,
) -> list[LibraryItem] | None:
    """Filter fallback results to only albums that contain the requested track.

    Returns:
        Filtered list, or None if validation isn't possible.
    """
    if not discogs_service or not song or not artist or not results:
        return None

    async def validate_one(item: LibraryItem) -> LibraryItem | None:
        try:
            response = await discogs_service.search(
                DiscogsSearchRequest(album=item.title, artist=artist)
            )
            if not response.results:
                return None

            best_result = response.results[0]
            if best_result.release_id:
                is_valid = await discogs_service.validate_track_on_release(
                    best_result.release_id, song, artist
                )
                if is_valid:
                    logger.info(
                        f"Track validation: '{song}' confirmed on '{item.title}' "
                        f"(release {best_result.release_id})"
                    )
                    return item
        except Exception as e:
            logger.warning(f"Track validation failed for '{item.title}': {e}")
        return None

    validation_results = await asyncio.gather(*[validate_one(item) for item in results])
    validated = [r for r in validation_results if r is not None]

    if validated:
        logger.info(
            f"Track validation filtered {len(results)} albums to {len(validated)} "
            f"containing '{song}'"
        )
        return validated

    logger.info(f"Track validation could not confirm '{song}' on any album")
    return None


async def _resolve_fallback_artwork(discogs_service: DiscogsService, release_id: int) -> str | None:
    """Try artist image, then label image, for a release with no cover art."""
    release = await discogs_service.get_release(release_id)
    if not release:
        return None

    if release.artist_id:
        image = await discogs_service.get_artist_image(release.artist_id)
        if image:
            logger.info(f"Using artist image fallback for release {release_id}")
            return image

    if release.label_id:
        image = await discogs_service.get_label_image(release.label_id)
        if image:
            logger.info(f"Using label image fallback for release {release_id}")
            return image

    return None


async def fetch_artwork_for_items(
    items: list[LibraryItem],
    discogs_service: DiscogsService | None,
    discogs_titles: dict[int, str] | None = None,
) -> list[tuple[LibraryItem, DiscogsSearchResult | None]]:
    """Fetch artwork for multiple library items in parallel."""
    if not discogs_service:
        return [(item, None) for item in items]

    discogs_titles = discogs_titles or {}

    async def fetch_one(item: LibraryItem) -> DiscogsSearchResult | None:
        try:
            album = discogs_titles.get(item.id, item.title)

            artist = item.alternate_artist_name or item.artist or ""
            if is_compilation_artist(artist):
                artist = "Various"

            response = await discogs_service.search(
                DiscogsSearchRequest(album=album, artist=artist)
            )
            if response.results:
                result = response.results[0]
                if not result.artwork_url:
                    fallback = await _resolve_fallback_artwork(discogs_service, result.release_id)
                    if fallback:
                        result = result.model_copy(update={"artwork_url": fallback})
                return result
            return None
        except Exception as e:
            logger.warning(f"Artwork lookup failed for {item.title}: {e}")
            return None

    artwork_results = await asyncio.gather(*[fetch_one(item) for item in items])
    return list(zip(items, artwork_results, strict=True))


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


async def perform_lookup(
    request: LookupRequest,
    db: LibraryDB,
    discogs_service: DiscogsService | None,
    telemetry: RequestTelemetry,
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
    discogs_titles: dict[int, str] = {}
    corrected_artist: str | None = None

    # Step 1: Correct artist spelling
    if parsed.artist:
        corrected = await db.find_similar_artist(parsed.artist)
        if corrected:
            corrected_artist = corrected
            parsed.artist = corrected

    # Step 2: Resolve albums from Discogs
    with telemetry.track_step("album_lookup"):
        if parsed.song and not parsed.album:
            telemetry.record_api_call("discogs")
        albums_for_search, song_not_found = await resolve_albums_for_track(parsed, discogs_service)

    # Step 3: Execute search strategy pipeline
    with telemetry.track_step("library_search"):
        strategies = build_strategies(
            search_library_func=search_library_with_fallback,
            search_alternative_func=search_with_alternative_interpretation,
            search_compilations_func=partial(
                search_compilations_for_track, discogs_service=discogs_service
            ),
            search_song_as_artist_func=partial(
                search_song_as_artist, discogs_service=discogs_service
            ),
        )

        search_state = await execute_search_pipeline(
            parsed=parsed,
            db=db,
            raw_message=request.raw_message,
            strategies=strategies,
            albums_for_search=albums_for_search,
        )

        library_results = limit_results(search_state.results)
        song_not_found = search_state.song_not_found
        found_on_compilation = search_state.found_on_compilation
        discogs_titles = search_state.discogs_titles
        search_type = get_search_type_from_state(search_state)

        if found_on_compilation:
            telemetry.record_api_call("discogs")

    # Step 3b: Validate fallback results against Discogs track data
    if song_not_found and library_results and parsed.song and parsed.artist:
        with telemetry.track_step("track_validation"):
            validated = await filter_results_by_track_validation(
                library_results, parsed.song, parsed.artist, discogs_service
            )
            if validated:
                library_results = validated
                song_not_found = False

    # Step 4: Fetch artwork
    with telemetry.track_step("artwork_fetch"):
        if library_results:
            for _ in library_results:
                telemetry.record_api_call("discogs")
            items_with_artwork = await fetch_artwork_for_items(
                library_results, discogs_service, discogs_titles
            )

    # Step 5: Build context message
    context = build_context_message(
        parsed, found_on_compilation, song_not_found, has_results=bool(library_results)
    )

    # Build response
    result_items = []
    if items_with_artwork:
        for item, artwork in items_with_artwork:
            result_items.append(LookupResultItem(library_item=item, artwork=artwork))
    elif library_results:
        for item in library_results:
            result_items.append(LookupResultItem(library_item=item))

    return LookupResponse(
        results=result_items,
        search_type=search_type,
        song_not_found=song_not_found,
        found_on_compilation=found_on_compilation,
        context_message=context,
        corrected_artist=corrected_artist,
    )
