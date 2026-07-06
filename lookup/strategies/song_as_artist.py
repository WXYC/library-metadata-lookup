"""SONG_AS_ARTIST — treat the parsed song as an artist name and re-search.

Fires when the parser produced a song but no artist — typically because the
parser misread an artist as a song title (e.g. "Laid Back" parsed as song
instead of artist). The execute func may also cross-reference Discogs for
releases by the candidate name and intersect with the library. Its tuple
second element carries the row-less ``discogs_titles`` seam (LML#631) when a
non-library artist resolved on Discogs (``{0: ResolvedRelease}``), and is
``None`` on the library-backed paths.

Strictly upstream of :class:`lookup.strategies.song_as_track.SongAsTrack` —
the parser-misread-artist case gets first crack at song-only queries; the
track fallback only runs if this strategy returned empty.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, ClassVar

from wxyc_etl.text import is_compilation_artist

from config.settings import get_settings
from core.search import (
    Outcome,
    SearchState,
    SearchStrategyType,
    no_results_and_song_but_no_artist,
)
from discogs.lookup import lookup_releases_by_artist
from discogs.service import DiscogsService
from library.db import LibraryDB
from library.models import LibraryItem
from lookup.matching import (
    _FETCH_LIMIT,
    MAX_SEARCH_RESULTS,
    artist_matches_item,
    filter_results_by_artist,
    limit_results,
)
from lookup.release_resolution import ResolvedRelease
from lookup.rowless import ROWLESS_LIBRARY_ID, _select_rowless_artist_release
from services.parser import ParsedRequest

logger = logging.getLogger(__name__)

SongAsArtistExecute = Callable[
    [LibraryDB, str | None],
    Awaitable[tuple[list[LibraryItem], Any]],
]


@dataclass(frozen=True)
class SongAsArtist:
    """Search the library treating ``parsed.song`` as an artist name."""

    name: ClassVar[SearchStrategyType] = SearchStrategyType.SONG_AS_ARTIST

    db: LibraryDB
    execute: SongAsArtistExecute
    """Production: ``functools.partial(search_song_as_artist,
    discogs_service=discogs_service)``."""

    def should_attempt(self, parsed: ParsedRequest, state: SearchState, raw_message: str) -> bool:
        return no_results_and_song_but_no_artist(parsed, state, raw_message)

    async def attempt(self, parsed: ParsedRequest, state: SearchState, raw_message: str) -> Outcome:
        items, discogs_titles = await self.execute(self.db, parsed.song)
        if not items:
            return Outcome.empty()
        return Outcome.found(items, discogs_titles=discogs_titles or None)


async def search_song_as_artist(
    db: LibraryDB,
    song_as_artist: str,
    discogs_service: DiscogsService | None = None,
    *,
    allow_release_resolution_fallback: bool = True,
) -> tuple[list[LibraryItem], dict[int, ResolvedRelease] | None]:
    """Try searching using the parsed song title as an artist name.

    Primarily serves request-o-matic listener requests. When the typed token is
    an artist WXYC owns, the library cross-reference returns those rows. When it
    resolves on Discogs but has no ``library.db`` row, LML#631 surfaces a
    *row-less* result — ``LibraryItem(id=0)`` paired with the resolved release on
    the ``discogs_titles`` seam — so rom can post the Discogs context to Slack.
    The row-less path is gated on ``LML_RESOLVE_NONLIBRARY_RELEASE`` (shared with
    the #628 carry-through) and on the typed token normalizing-equal to the
    resolved Discogs artist name; the second tuple element carries that release
    (``None`` on the library-backed paths, which are unchanged).

    ``allow_release_resolution_fallback`` is the bulk kill switch (LML#652):
    ``False`` on /lookup/bulk suppresses the row-less surface (no row-less item,
    hence no per-row ``bind_carried`` artwork fetch downstream), parity with the
    #628 carry-through and #604's lazy fallback. Unlike those, this path does no
    resolve fan-out or cache write — the gate only suppresses the row-less pick
    over releases the artist probe already fetched.
    """
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
        *[search_album(release.album or "") for release in discogs_releases]
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

    # LML#631 — no WXYC catalog row for this artist. If the token resolves
    # cleanly on Discogs, surface the best-representative release row-less so rom
    # can post Discogs context to Slack. Gated behind the shared non-library flag
    # and, per LML#652, the per-request bulk kill switch — /lookup/bulk passes
    # ``allow_release_resolution_fallback=False`` so a song-only backfill item
    # never surfaces a row-less result (and never pays the downstream per-row
    # ``bind_carried`` artwork fetch).
    if get_settings().lml_resolve_nonlibrary_release and allow_release_resolution_fallback:
        rowless = _select_rowless_artist_release(song_as_artist, discogs_releases)
        if rowless is not None:
            item, resolved = rowless
            logger.info(
                f"Surfacing row-less Discogs release {resolved.release_id} for "
                f"non-library artist '{song_as_artist}'"
            )
            return [item], {ROWLESS_LIBRARY_ID: resolved}

    return [], None
