"""Fuzzy title floors over artist-fallback library rows.

Two ``token_set_ratio`` floors, each scoring a fallback row's catalog title
against one request field:

- the LML#400 album floor (``_ALBUM_MATCH_FLOOR``): does the row's title
  plausibly name the typed **album**? The search layer ranks the song-bearing
  fallback by it (``_partition_results_by_album_match``, LML#1391) and still
  drops by it on the songless lane (``_filter_results_by_album_match``); step
  3b and step 3a read the same primitive (``row_clears_album_floor``) to tell
  "the typed album was found" from "it matched nothing".
- the LML#717 song-as-album-title floor (``_SONG_AS_ALBUM_TITLE_FLOOR``): does
  the row's title name the typed **song**, i.e. did the listener type an album?

These decide ordering and found/not-found. Neither decides what a row may
*serve*: that is the one serve rule in ``lookup/binding_floor.py``.

Leaf module, no I/O. Extracted from ``lookup/matching.py`` and
``lookup/validation.py`` at their line ceilings (LML#1391).
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


def row_clears_album_floor(item: LibraryItem, album: str) -> bool:
    """Does `item`'s title clear `_ALBUM_MATCH_FLOOR` against the typed `album`?

    The one scoring primitive shared by `_partition_results_by_album_match`
    (search-layer ranking) and `apply_track_validation_cascade` (LML#1391 —
    telling "the typed album matched a shelf row" apart from "a song-bearing
    row surfaced but the typed album still didn't match anything"), so the
    two can't drift onto different floors for the same question.
    """
    norm_album = normalize_for_comparison(album)
    title_norm = normalize_for_comparison(item.title or "")
    return fuzz.token_set_ratio(norm_album, title_norm) >= _ALBUM_MATCH_FLOOR


def _partition_results_by_album_match(
    results: list[LibraryItem],
    album: str | None,
) -> list[LibraryItem]:
    """Stable-partition artist-fallback rows by `row_clears_album_floor`:
    floor-clearing rows first, the rest after, each group in its original
    order. No-ops when `album` is empty or whitespace-only.

    LML#1391, song-bearing lane only: a typed album that matched nothing must
    not empty the artist's shelf when a song might still confirm one of its
    rows. The #400 metadata-contamination guard this floor backed is the
    serve rule in `lookup/binding_floor.py`, which withholds a row's release,
    artwork and streaming links per row regardless of ranking.
    The songless lane still drops via `_filter_results_by_album_match` below
    (LML#1393 follow-up).
    """
    if not album or not album.strip():
        return results
    return sorted(results, key=lambda item: not row_clears_album_floor(item, album))


def _filter_results_by_album_match(
    results: list[LibraryItem],
    album: str | None,
) -> list[LibraryItem]:
    """Drop library rows whose title doesn't clear `row_clears_album_floor`
    against the typed album. No-ops when `album` is empty or whitespace-only.

    The songless counterpart to `_partition_results_by_album_match` — see its
    docstring for why the song-bearing lane ranks instead of drops (LML#1391)
    and the songless lane doesn't yet (LML#1393).
    """
    if not album or not album.strip():
        return results
    kept = [item for item in results if row_clears_album_floor(item, album)]
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
