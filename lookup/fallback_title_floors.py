"""Fuzzy title floors over artist-fallback library rows.

Two ``token_set_ratio`` floors, each scoring a fallback row's catalog title
against one request field:

- the LML#400 album floor (``_ALBUM_MATCH_FLOOR``): does the row's title
  plausibly name the typed **album**? ``_filter_results_by_album_match`` drops
  the artist-fallback rows that do not.
- the LML#717 song-as-album-title floor (``_SONG_AS_ALBUM_TITLE_FLOOR``): does
  the row's title name the typed **song**, i.e. did the listener type an album?

Leaf module, no I/O. Moved verbatim out of ``lookup/matching.py`` and
``lookup/validation.py``, both near their line ceilings.
"""

import logging

from rapidfuzz import fuzz
from wxyc_etl.text import to_match_form as normalize_for_comparison

from library.models import LibraryItem

logger = logging.getLogger(__name__)

# Minimum fuzzy score (0-100) for accepting a library-row title as a genuine
# album match for the DJ-typed album. Mirrors the 80-floor in
# `clients/streaming/apple_music._APPLE_MUSIC_MATCH_FLOOR` and the
# streaming-availability batch matcher. When the artist-fallback branches
# of `search_library_with_fallback` surface a row whose title doesn't clear
# this floor against the typed album, the row would otherwise carry the
# matched Discogs release's `release_year` / `apple_music_url` / `spotify_url`
# / `discogs_url` / `artwork_url` onto a flowsheet row tagged with a
# completely different album — the contamination shape documented in #400
# (~184k rows; 16,532 distinct Discogs URLs each attached to many distinct
# DJ-typed `(artist, album)` pairs). #390 / #398 tightened the result
# verification; this tightens the LML lookup result itself.
_ALBUM_MATCH_FLOOR = 80.0

# Minimum fuzzy score for promoting an artist-fallback row whose *title*
# matches the requested "song" — i.e. recognising the parsed song as the
# album the user actually wanted. Set higher than the album-match floor
# because the consequence here is asserting the user's intent (album,
# not track); a borderline match should *not* override the song-not-found
# message.
_SONG_AS_ALBUM_TITLE_FLOOR = 90.0


def _filter_results_by_album_match(
    results: list[LibraryItem],
    album: str | None,
) -> list[LibraryItem]:
    """Drop library rows whose title doesn't clear `_ALBUM_MATCH_FLOOR` against
    the typed album. No-ops when `album` is empty or whitespace-only.
    """
    if not album or not album.strip():
        return results
    norm_album = normalize_for_comparison(album)
    kept: list[LibraryItem] = []
    for item in results:
        title_norm = normalize_for_comparison(item.title or "")
        if fuzz.token_set_ratio(norm_album, title_norm) >= _ALBUM_MATCH_FLOOR:
            kept.append(item)
    if len(kept) < len(results):
        logger.info(
            f"Album-match floor dropped {len(results) - len(kept)} of {len(results)} "
            f"artist-fallback candidates against typed album '{album}'"
        )
    return kept


def _filter_results_by_song_as_album_title(
    results: list[LibraryItem],
    song: str | None,
) -> list[LibraryItem]:
    """Pick artist-fallback rows whose title matches the requested song.

    Handles the request shape "on patrol, sun araw" — request-o-matic routes
    it as ``song="On Patrol"`` / ``artist="Sun Araw"``, but the user typed
    an album name. The artist+song FTS branch of
    ``search_library_with_fallback`` surfaces the matching album because
    the album title contains the song words; per-result track validation
    then reasonably comes back empty (no track titled "On Patrol" exists on
    that album — it IS the album) and ``song_not_found`` stays set,
    producing the misleading 'not on any album' context message about a
    result sitting in its own list.

    Floor is ``_SONG_AS_ALBUM_TITLE_FLOOR`` (>= 90 via
    ``rapidfuzz.fuzz.token_set_ratio``) — high enough that a coincidental
    word overlap won't override the song-not-found path.

    Returns the subset of ``results`` whose normalised title clears the
    floor against the normalised song. Empty input or whitespace-only song
    returns ``[]`` cleanly.
    """
    if not song or not song.strip() or not results:
        return []
    norm_song = normalize_for_comparison(song)
    matches: list[LibraryItem] = []
    for item in results:
        title_norm = normalize_for_comparison(item.title or "")
        if fuzz.token_set_ratio(norm_song, title_norm) >= _SONG_AS_ALBUM_TITLE_FLOOR:
            matches.append(item)
    return matches
