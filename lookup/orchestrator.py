"""Lookup orchestrator: the core search logic extracted from request-o-matic.

This module contains the perform_lookup() function that orchestrates the full
search pipeline: artist correction -> album resolution -> search strategies ->
track validation -> artwork fetch -> metadata enrichment -> context message.
"""

import asyncio
import logging
import re
from functools import partial
from urllib.parse import quote

import httpx

from wxyc_etl.text import is_compilation_artist
from wxyc_etl.text import normalize_artist_name as normalize_for_comparison

from core.search import (
    build_strategies,
    execute_search_pipeline,
    get_search_type_from_state,
)
from core.telemetry import RequestTelemetry
from discogs.lookup import lookup_releases_by_artist, lookup_releases_by_track
from discogs.models import DiscogsSearchRequest, DiscogsSearchResult, ReleaseInfo
from discogs.service import DiscogsService
from library.db import STOPWORDS, LibraryDB
from library.models import LibraryItem
from lookup.models import LookupRequest, LookupResponse, LookupResultItem
from services.parser import MessageType, ParsedRequest

logger = logging.getLogger(__name__)

MAX_SEARCH_RESULTS = 5
"""Maximum number of results to return from search operations."""

SELF_TITLED_PATTERNS = frozenset({"s/t", "s.t.", "self-titled", "self titled"})
"""Common abbreviations for self-titled albums (case-insensitive exact match)."""


def is_self_titled(title: str) -> bool:
    """Check if an album title indicates a self-titled release.

    Args:
        title: Album title to check

    Returns:
        True if title is a common self-titled abbreviation (e.g. "S/t", "S.T.")
    """
    return title.strip().lower() in SELF_TITLED_PATTERNS


def map_library_format_to_discogs(fmt: str | None) -> str | None:
    """Map a WXYC library format value to a Discogs API format parameter.

    Library format values like "cd", "vinyl - 12\\"", "cd x 2" are mapped to
    the corresponding Discogs search API format terms ("CD", "12\\"", etc.).

    Returns None if the format is not recognized or is empty.
    """
    if not fmt:
        return None
    normalized = fmt.strip().lower()
    if normalized.startswith("cdr"):
        return "CDr"
    if normalized.startswith("cd"):
        return "CD"
    if 'vinyl - 12"' in normalized or "vinyl - 12" in normalized:
        return '12"'
    if 'vinyl - 7"' in normalized or "vinyl - 7" in normalized:
        return '7"'
    if 'vinyl - 10"' in normalized or "vinyl - 10" in normalized:
        return '10"'
    if normalized.startswith("vinyl"):
        return "Vinyl"
    return None


_FETCH_LIMIT = MAX_SEARCH_RESULTS * 10
"""Internal fetch limit for FTS queries that are post-filtered by artist.

FTS5 ranks results by term frequency, not by artist-prefix relevance, so the
target artist's entries may fall outside a tight SQL LIMIT.  Fetching more rows
ensures enough candidates survive ``filter_results_by_artist`` before we trim
back to ``MAX_SEARCH_RESULTS``.
"""


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
    raw1, raw2 = await asyncio.gather(
        db.search(query=f"{part1} {part2}", limit=_FETCH_LIMIT),
        db.search(query=f"{part2} {part1}", limit=_FETCH_LIMIT),
    )
    results1 = filter_results_by_artist(raw1, part1)
    results2 = filter_results_by_artist(raw2, part2)

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

    results = await db.search(query=song_as_artist, limit=_FETCH_LIMIT)
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

    async def search_album(album_title: str) -> list[LibraryItem]:
        if not album_title:
            return []
        album_results = await db.search(query=album_title, limit=_FETCH_LIMIT)
        return [
            item
            for item in album_results
            if artist_matches_item(item, song_as_artist) or is_compilation_artist(item.artist or "")
        ]

    all_matches = await asyncio.gather(
        *[search_album(album_title) for _, album_title in discogs_releases]
    )

    seen_ids: set[int] = set()
    for matches in all_matches:
        for item in matches:
            if item.id not in seen_ids:
                results.append(item)
                seen_ids.add(item.id)
                logger.info(f"Found '{item.artist} - {item.title}' via Discogs cross-reference")
            if len(results) >= MAX_SEARCH_RESULTS:
                break
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

        async def search_one_album(album: str) -> list[LibraryItem]:
            query = f"{parsed.artist} {album}"
            results = await db.search(query=query, limit=_FETCH_LIMIT)
            results = filter_results_by_artist(results, parsed.artist)

            album_lower = album.lower()
            album_normalized = re.sub(r"[^\w\s]", " ", album_lower)
            album_normalized = " ".join(album_normalized.split())
            album_words = {w for w in album_normalized.split() if len(w) > 2 and w not in STOPWORDS}
            album_is_artist = parsed.artist and normalize_for_comparison(
                album
            ) == normalize_for_comparison(parsed.artist)

            filtered_results = []
            for item in results:
                if album_is_artist and is_self_titled(item.title or ""):
                    filtered_results.append(item)
                    continue

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
            return filtered_results

        album_results = await asyncio.gather(*[search_one_album(a) for a in albums])

        for results in album_results:
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

        # When Discogs found albums but none matched the library, fall through to
        # artist+song and artist-only search.  filter_results_by_track_validation()
        # (called by perform_lookup after the search pipeline) validates fallback
        # results against Discogs tracklists to prevent false positives.
        logger.info(
            f"Discogs found albums {albums} but none matched in library; "
            "falling through to artist search"
        )

    if parsed.artist and parsed.song:
        query = f"{parsed.artist} {parsed.song}"
        results = await db.search(query=query, limit=_FETCH_LIMIT)
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
        results = await db.search(query=parsed.artist, limit=_FETCH_LIMIT)
        results = filter_results_by_artist(results, parsed.artist)
        if results:
            return results, True

    return all_results, bool(parsed.song)


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
            keyword_results = await db.search(query=keyword_query, limit=_FETCH_LIMIT)

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

    discogs_found_releases = False

    try:
        raw_lower = parsed.raw_message.lower()
        song_search = parsed.song

        remix_match = re.search(r"\((.*?(?:remix|mix|version|edit).*?)\)", raw_lower, re.IGNORECASE)
        if remix_match and parsed.song.lower() in raw_lower:
            song_search = f"{parsed.song} ({remix_match.group(1)})"
            logger.info(f"Using full track name with version info: '{song_search}'")

        # Get raw releases from Discogs without per-release validation.
        # We search the library first and only validate releases that match,
        # avoiding expensive API calls for releases not in our catalog.
        raw_releases: list[ReleaseInfo] = []
        if discogs_service:
            # Fire both searches in parallel (speculative: VA search may not be needed)
            response, va_response = await asyncio.gather(
                discogs_service.search_releases_by_track(song_search, parsed.artist),
                discogs_service.search_releases_by_track(
                    song_search, parsed.artist, artist_as_keyword=True
                ),
            )
            raw_releases = list(response.releases)

            # Only merge VA results if the artist-scoped search found no compilations
            has_compilation = any(r.is_compilation for r in raw_releases)
            if not has_compilation:
                seen_album_keys = {r.album.lower() for r in raw_releases}
                for r in va_response.releases:
                    if r.is_compilation and r.album.lower() not in seen_album_keys:
                        raw_releases.append(r)
                        seen_album_keys.add(r.album.lower())
        else:
            # No injected service — fall back to lookup helper (validates all)
            tuples = await lookup_releases_by_track(song_search, parsed.artist, service=None)
            raw_releases = [
                ReleaseInfo(
                    album=album,
                    artist=artist,
                    release_id=0,
                    release_url="",
                    is_compilation=is_compilation_artist(artist),
                )
                for artist, album in tuples
            ]

        logger.info(f"Found {len(raw_releases)} releases with '{song_search}' on Discogs")
        discogs_found_releases = len(raw_releases) > 0

        async def process_release(
            release_info: ReleaseInfo,
        ) -> list[tuple[LibraryItem, str]]:
            """Process one Discogs release: library search, filter, validate."""
            release_album = release_info.album

            album_clean = release_album.lower().replace('"', "").replace("'", "").strip()
            if (
                parsed.artist
                and album_clean == parsed.artist.lower().replace('"', "").replace("'", "").strip()
            ):
                logger.debug(f"Skipping '{release_album}' - appears to be artist name, not album")
                return []

            if len(release_album.strip()) < 3:
                return []

            matches = await search_album_fuzzy(db, release_album)

            # If album-only search failed for a compilation, retry with "Various"
            # to help FTS5 match entries stored as "Various Artists - ..."
            if not matches and release_info.is_compilation:
                matches = await search_album_fuzzy(db, f"Various {release_album}")

            if matches and parsed.artist:
                from rapidfuzz import fuzz as _fuzz

                filtered_matches = []
                discogs_is_compilation = release_info.is_compilation
                release_album_lower = release_album.lower()

                for match in matches:
                    if artist_matches_item(match, parsed.artist):
                        filtered_matches.append(match)
                    elif discogs_is_compilation and is_compilation_artist(match.artist or ""):
                        title_score = _fuzz.ratio(release_album_lower, (match.title or "").lower())
                        if title_score >= 80:
                            filtered_matches.append(match)
                        else:
                            logger.debug(
                                f"Rejected '{match.title}' for '{release_album}' "
                                f"(title_score={title_score:.0f})"
                            )
                matches = filtered_matches

            if not matches:
                return []

            # Validate that the track actually exists on this release.
            # Deferred until after library matching so we only validate
            # releases we might actually return — saving API calls.
            if discogs_service and release_info.release_id and parsed.artist:
                is_valid = await discogs_service.validate_track_on_release(
                    release_info.release_id, song_search, parsed.artist
                )
                if not is_valid:
                    logger.info(
                        f"Skipping '{release_album}' - track/artist not validated on release"
                    )
                    return []

            logger.info(
                f"Found '{parsed.song}' in library on '{matches[0].title}' "
                f"(matched from Discogs: '{release_album}')"
            )
            return [(match, release_album) for match in matches]

        all_release_results = await asyncio.gather(*[process_release(ri) for ri in raw_releases])

        for release_matches in all_release_results:
            for match, discogs_album in release_matches:
                if match.id not in seen_ids:
                    results.append(match)
                    seen_ids.add(match.id)
                    discogs_titles[match.id] = discogs_album
            if len(results) >= MAX_SEARCH_RESULTS:
                break
    except Exception as e:
        logger.warning(f"Failed to search for track on other releases: {e}")

    if not results and keyword_matches and not discogs_found_releases:
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


def album_title_acceptable(query_lower: str, result_lower: str) -> bool:
    """Check if a library album title is an acceptable match for a Discogs album title.

    Uses prefix matching (handles parenthetical suffixes like edition names) and
    length-sensitive fuzz.ratio to reject subset matches that token_set_ratio
    would incorrectly accept.

    Also rejects numbered series albums (e.g., "Chicago V" vs "Chicago 16",
    "Led Zeppelin II" vs "Led Zeppelin IV") by checking that when titles share
    a long common prefix, the short distinguishing suffixes are also similar.
    """
    from rapidfuzz import fuzz

    if query_lower.startswith(result_lower) or result_lower.startswith(query_lower):
        return True

    # Find common prefix length
    common = 0
    for a, b in zip(query_lower, result_lower, strict=False):
        if a != b:
            break
        common += 1

    # Reject numbered series: titles that share a dominant prefix but differ
    # in a short identifier suffix (e.g., "V" vs "16", "II" vs "IV").
    if common > 0:
        remainder_q = query_lower[common:].strip()
        remainder_r = result_lower[common:].strip()
        min_len = min(len(query_lower), len(result_lower))
        if (
            remainder_q
            and remainder_r
            and len(remainder_q) <= 5
            and len(remainder_r) <= 5
            and common >= min_len * 0.5
        ):
            if fuzz.ratio(remainder_q, remainder_r) < 50:
                return False

    return fuzz.ratio(query_lower, result_lower) >= 50


async def search_album_fuzzy(db: LibraryDB, album_title: str) -> list[LibraryItem]:
    """Search for album with fuzzy keyword matching."""
    from rapidfuzz import fuzz

    results = await db.search(query=album_title, limit=MAX_SEARCH_RESULTS)

    # Filter exact FTS5 results by title similarity to reject subset matches
    # (e.g., FTS5 matches "808 State" for query "The Best Of 808 State: Blueprint"
    # because it tokenizes and matches on shared terms "808" and "State")
    if results:
        album_lower = album_title.lower()
        results = [
            r for r in results if album_title_acceptable(album_lower, (r.title or "").lower())
        ]

    if not results:
        words = re.sub(r"[^\w\s]", " ", album_title.lower()).split()
        significant_words = [w for w in words if len(w) > 3 and w not in STOPWORDS]

        if significant_words:
            album_lower = album_title.lower()
            # Require roughly half the keywords to match — lenient enough for
            # abbreviated titles (e.g., "Punk 82-88" vs "Post Punk 1982 - 1988")
            # but strict enough to reject unrelated albums that share a few words
            # (e.g., "20th Anniversary Concert" vs "Trax Records 20th Anniversary Edition").
            # The similarity and album_title_acceptable checks provide additional gating.
            min_keywords = max(2, (len(significant_words) + 1) // 2)

            # Try progressively shorter queries to handle word mismatches between
            # Discogs and library titles (e.g., "Edition" vs "Collection").
            # Filter inside the loop so false positives don't block shorter queries.
            max_words = min(4, len(significant_words))
            for n_words in range(max_words, 1, -1):
                fuzzy_query = " ".join(significant_words[:n_words])
                logger.info(
                    f"Exact match failed for '{album_title}', trying fuzzy: '{fuzzy_query}'"
                )
                raw_results = await db.search(query=fuzzy_query, limit=MAX_SEARCH_RESULTS)
                if not raw_results:
                    continue

                filtered_results = []
                for result in raw_results:
                    result_title_lower = (result.title or "").lower()

                    keyword_matches = sum(
                        1 for word in significant_words if word in result_title_lower
                    )
                    similarity = fuzz.token_set_ratio(album_lower, result_title_lower)
                    title_ok = album_title_acceptable(album_lower, result_title_lower)

                    if keyword_matches >= min_keywords and similarity >= 60 and title_ok:
                        logger.debug(
                            f"Album match: '{result.title}' "
                            f"(keywords={keyword_matches}/{len(significant_words)}, "
                            f"similarity={similarity})"
                        )
                        filtered_results.append(result)
                    else:
                        logger.debug(
                            f"Album rejected: '{result.title}' "
                            f"(keywords={keyword_matches}/{len(significant_words)}, "
                            f"similarity={similarity})"
                        )

                if filtered_results:
                    results = filtered_results
                    break

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
            # Self-titled albums stored as "S/t" should use the artist name
            album_for_search = item.artist if is_self_titled(item.title or "") else item.title
            response = await discogs_service.search(
                DiscogsSearchRequest(album=album_for_search, artist=artist)
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

            # Self-titled albums stored as "S/t" should use the artist name
            # for Discogs search instead of the abbreviation
            if is_self_titled(album or ""):
                album = item.artist

            artist = item.alternate_artist_name or item.artist or ""
            if is_compilation_artist(artist):
                artist = "Various"

            response = await discogs_service.search(
                DiscogsSearchRequest(
                    album=album,
                    artist=artist,
                    label=item.label,
                    format=map_library_format_to_discogs(item.format),
                )
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


def _build_streaming_search_url(base: str, artist: str, term: str) -> str:
    """Build a streaming service search URL from artist + song/album."""
    query = f"{artist} {term}" if term else artist
    return f"{base}{quote(query)}"


async def _fetch_apple_music_url(
    artist: str, song: str, http_client: httpx.AsyncClient | None = None
) -> str | None:
    """Search the iTunes API for an Apple Music link. Free, no auth required."""
    try:
        query = quote(f"{artist} {song}")
        url = f"https://itunes.apple.com/search?term={query}&entity=song&media=music&limit=1"
        if http_client:
            resp = await http_client.get(url)
        else:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(url)
        if resp.status_code != 200:
            return None
        data = resp.json()
        results = data.get("results", [])
        return results[0].get("trackViewUrl") if results else None
    except Exception:
        return None


async def enrich_artwork_results(
    items_with_artwork: list[tuple[LibraryItem, DiscogsSearchResult | None]],
    discogs_service: DiscogsService | None,
    song: str | None = None,
) -> list[tuple[LibraryItem, DiscogsSearchResult | None]]:
    """Enrich artwork results with release year, artist details, and streaming links.

    Fetches supplementary data in parallel (best-effort). Failures are silently
    ignored — enriched fields remain None.
    """
    if not discogs_service:
        return items_with_artwork

    async def enrich_one(
        item: LibraryItem, artwork: DiscogsSearchResult | None
    ) -> tuple[LibraryItem, DiscogsSearchResult | None]:
        if not artwork:
            return (item, artwork)

        artist = item.alternate_artist_name or item.artist or ""
        search_term = song or item.title or ""
        release_id = artwork.release_id

        async def fetch_release_details() -> tuple[int | None, str | None, str | None]:
            """Returns (year, artist_bio, wikipedia_url) from Discogs release + artist."""
            try:
                release = await discogs_service.get_release(release_id)
                if not release:
                    return None, None, None

                year = release.year if isinstance(release.year, int) else None

                # Fetch artist details if we have an artist_id
                artist_id = release.artist_id
                if not isinstance(artist_id, int) or artist_id <= 0:
                    return year, None, None

                details = await discogs_service.get_artist_details(artist_id)
                if not details:
                    return year, None, None

                bio = details.profile if isinstance(details.profile, str) else None
                wiki = next(
                    (
                        url
                        for url in details.urls
                        if isinstance(url, str) and "wikipedia.org" in url
                    ),
                    None,
                )
                return year, bio, wiki
            except Exception:
                return None, None, None

        async def fetch_apple_music() -> str | None:
            if not artist or not search_term:
                return None
            return await _fetch_apple_music_url(artist, search_term)

        release_details_result, apple_music_result = await asyncio.gather(
            fetch_release_details(),
            fetch_apple_music(),
        )

        year_result, artist_bio, wikipedia_url = release_details_result

        # Build streaming search URLs
        spotify_url = None
        youtube_music_url = None
        bandcamp_url = None
        soundcloud_url = None
        if artist and search_term:
            spotify_url = _build_streaming_search_url(
                "https://open.spotify.com/search/", artist, search_term
            )
            youtube_music_url = _build_streaming_search_url(
                "https://music.youtube.com/search?q=", artist, search_term
            )
            bandcamp_url = _build_streaming_search_url(
                "https://bandcamp.com/search?q=", artist, search_term
            )
            soundcloud_url = _build_streaming_search_url(
                "https://soundcloud.com/search?q=", artist, search_term
            )

        updated = artwork.model_copy(
            update={
                "release_year": year_result,
                "artist_bio": artist_bio,
                "wikipedia_url": wikipedia_url,
                "spotify_url": spotify_url,
                "apple_music_url": apple_music_result or None,
                "youtube_music_url": youtube_music_url,
                "bandcamp_url": bandcamp_url,
                "soundcloud_url": soundcloud_url,
            }
        )
        return (item, updated)

    enriched = await asyncio.gather(
        *[enrich_one(item, artwork) for item, artwork in items_with_artwork]
    )
    return list(enriched)


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
            parsed.artist = corrected
    else:
        with telemetry.track_step("album_lookup"):
            albums_for_search, song_not_found = await resolve_albums_for_track(
                parsed, discogs_service
            )

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
            song_not_found=song_not_found,
        )

        library_results = limit_results(search_state.results)
        song_not_found = search_state.song_not_found
        found_on_compilation = search_state.found_on_compilation
        discogs_titles = search_state.discogs_titles
        search_type = get_search_type_from_state(search_state)

        if found_on_compilation:
            telemetry.record_api_call("discogs")

    # Step 3b: Validate results against Discogs track data.
    if library_results and parsed.song and parsed.artist:
        if not found_on_compilation:
            # Normal case: validate all results against Discogs tracklists
            with telemetry.track_step("track_validation"):
                validated = await filter_results_by_track_validation(
                    library_results, parsed.song, parsed.artist, discogs_service
                )
                if validated:
                    library_results = validated
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

    # Step 4: Fetch artwork
    with telemetry.track_step("artwork_fetch"):
        if library_results:
            for _ in library_results:
                telemetry.record_api_call("discogs")
            items_with_artwork = await fetch_artwork_for_items(
                library_results, discogs_service, discogs_titles
            )

    # Step 4b: Enrich with release year, artist details, streaming links
    with telemetry.track_step("metadata_enrichment"):
        if items_with_artwork:
            items_with_artwork = await enrich_artwork_results(
                items_with_artwork, discogs_service, song=parsed.song
            )

    # Step 5: Build context message
    context = build_context_message(
        parsed, found_on_compilation, song_not_found, has_results=bool(library_results)
    )

    # Build response (convert internal models to API contract models)
    result_items = []
    if items_with_artwork:
        for item, artwork in items_with_artwork:
            result_items.append(
                LookupResultItem(
                    library_item=item.to_catalog_item(),
                    artwork=artwork.to_match_result() if artwork else None,
                )
            )
    elif library_results:
        for item in library_results:
            result_items.append(LookupResultItem(library_item=item.to_catalog_item()))

    return LookupResponse(
        results=result_items,
        search_type=search_type,
        song_not_found=song_not_found,
        found_on_compilation=found_on_compilation,
        context_message=context,
        corrected_artist=corrected_artist,
    )
