"""Unbound shelf fallback — the final, additive step in ``perform_lookup`` (LML#1391/#1393).

Re-scope of the original LML#1391 fix after PR #1396 was closed: five review
rounds each found a way that attempt changed what Backend-Service binds (a
floor-failing row's own release id/artwork leaking onto a typed-album
request). The corrected scope is narrower and purely additive:

    **Invariant: a response that is non-empty on ``main`` stays byte-for-byte
    unchanged. Only a response whose result list is empty today may change.**

:func:`apply_shelf_fallback` is called once, as the true last step of the
spine — after the location-union fold has had its chance to populate
``results``, not merely after Step 7 (``_step_external_cache_fallback``).
That placement matters: a request the fold would otherwise resolve (e.g. a
recall-index hit on the comp-track lane) is excluded from this fallback
exactly as it is excluded on ``main`` today, because its post-fold
``result_items`` is non-empty and the invariant above forbids touching it.
Every early return in ``perform_lookup`` for the tail-shed/admission-shed
degraded flavors (``_build_degraded_response``) and the spine-deadline
timeout returns before this call site, so those are excluded structurally.
Two conditions still have to be checked explicitly, because each sets its
flag and rides the *normal* return path rather than returning early: the
mid-pipeline hard-cap trip (``state.timed_out``, set inside
``core/search.py``'s strategy loop) and the search-leg Discogs
saturation-breaker shed (``state.upstream_shed``, LML#1126), which is caught
*inside* step 3 and otherwise reaches the final ``LookupResponse`` with
``degraded=True``. The caller collapses both into one ``skip`` flag — there
is nothing left for this function to do differently between them.

When every one of these holds —

* ``result_items`` is empty;
* ``skip`` is false (the caller's ``state.timed_out or state.upstream_shed``);
* the typed ``parsed.album`` is non-empty after stripping whitespace;
* the library artist (``library_artist_for(parsed)``) resolves to at least
  one shelved row — the same cached ``db.search`` + ``filter_results_by_artist``
  call ``ARTIST_PLUS_ALBUM``'s own artist-only fallback issues
  (``lookup/strategies/artist_plus_album.py``);

this returns the artist's shelf as **display-only** rows:
``LookupResultItem(library_item=row.to_catalog_item())``, with no
``artwork`` — hence no release id, artwork URL, year, Discogs URL, or
streaming links, since none of those ever ride anywhere but on ``artwork``
— and no ``matched_via``. Nothing here calls Discogs, fetches artwork, or
runs enrichment.

``search_type`` is forced to ``SEARCH_TYPE_FALLBACK``, never ``direct`` —
so Backend-Service's ``requireSearchType: 'direct'`` callers keep rejecting
this shape, including the album-only request where ``main`` reports
``direct`` with zero rows (LML#1393). ``context_message`` is the
typed-album/typed-artist sentence ``build_context_message`` already uses
for the song-bearing album-miss case (``lookup.matching.album_not_found_message``),
now reachable for the album-only shape too — ``main``'s
``build_context_message`` never reaches it there, since every branch of
its "song not found" message requires ``parsed.song``. ``song_not_found``
and ``found_on_compilation`` are deliberately left untouched: the caller
already computed them correctly before this step runs.

Ordering:

1. rows whose title contains the requested song (case-insensitive), when a
   song was typed;
2. *(not implemented — see below)*;
3. the rest, in the order ``db.search`` returned them.

Capped at ``MAX_SEARCH_RESULTS``.

**Tier 2 (cache-confirmed song) is intentionally not implemented.** The one
cache-only, no-live-API primitive that answers "does a cached Discogs
tracklist already show this song on one of these rows" is
``find_library_albums_with_cached_track`` (``lookup/validation.py``, the
step-3b A4 rescue). It already runs — with this exact ``(artist, song)``
pair — against the artist-fallback rows *before* this function can ever see
them, and when it confirms a row, ``apply_track_validation_cascade``
promotes that row into ``library_results``, making the pipeline's own
result non-empty and keeping this fallback from firing at all. Re-running
that probe here would be a second, redundant cache hit that can only
confirm what the pipeline already would have surfaced and promoted first —
it cannot change this function's output, since this function is only ever
reached in the case where that probe already failed. Wiring in a fresh
Discogs-cache dependency here, only to re-ask the same question with no
chance of a different answer, would add a dependency this step does not
otherwise need, against the re-scope's point of staying small and additive.
"""

import sentry_sdk

from core.search import SEARCH_TYPE_FALLBACK
from library.db import LibraryDB
from library.models import LibraryItem
from lookup.matching import (
    _FETCH_LIMIT,
    MAX_SEARCH_RESULTS,
    album_not_found_message,
    filter_results_by_artist,
    library_artist_for,
)
from lookup.models import LookupResultItem
from services.parser import ParsedRequest


def _order_shelf_rows(rows: list[LibraryItem], song: str | None) -> list[LibraryItem]:
    """Song-in-title rows first (stable), then the rest in query order.

    Tier 2 (cache-confirmed song) is skipped — see the module docstring —
    so this is tiers 1 and 3 only.
    """
    if not song:
        return rows
    needle = song.lower()
    leading_ids = {row.id for row in rows if needle in (row.title or "").lower()}
    if not leading_ids:
        return rows
    leading = [row for row in rows if row.id in leading_ids]
    trailing = [row for row in rows if row.id not in leading_ids]
    return leading + trailing


def _mark_shelf_fallback_outcome() -> None:
    """Project a ``lookup.outcome``-style marker, mirroring the
    ``library_miss_outcome`` convention in ``lookup/orchestrator.py``'s
    ``_step_project_trace_attrs``. Observability must not break the request
    path."""
    try:
        scope = sentry_sdk.get_current_scope()
        if scope.transaction is not None:
            scope.transaction.set_data("lookup.outcome", "unbound_shelf_fallback")
    except Exception:
        pass


async def apply_shelf_fallback(
    parsed: ParsedRequest,
    db: LibraryDB,
    skip: bool,
    result_items: list[LookupResultItem],
    search_type: str,
    context_message: str | None,
    external_source: str | None,
) -> tuple[list[LookupResultItem], str, str | None, str | None]:
    """Append the artist's shelf as display-only rows when the response is
    otherwise empty. See the module docstring for the full contract.

    ``skip`` is the caller's ``state.timed_out or state.upstream_shed`` —
    both set their flag and ride the *normal* return path rather than
    returning early, so both have to be checked here, same as
    ``result_items`` itself; see the module docstring for why.

    Returns ``(result_items, search_type, context_message, external_source)``
    unchanged when any trigger condition fails — a pure pass-through, so the
    call site in ``perform_lookup`` never needs its own gate.
    """
    if result_items or skip:
        return result_items, search_type, context_message, external_source
    album = (parsed.album or "").strip()
    if not album:
        return result_items, search_type, context_message, external_source
    lib_artist = library_artist_for(parsed)
    if not lib_artist:
        return result_items, search_type, context_message, external_source

    rows = filter_results_by_artist(
        await db.search(query=lib_artist, limit=_FETCH_LIMIT), lib_artist
    )
    if not rows:
        return result_items, search_type, context_message, external_source

    ordered = _order_shelf_rows(rows, parsed.song)[:MAX_SEARCH_RESULTS]
    shelf_items = [LookupResultItem(library_item=row.to_catalog_item()) for row in ordered]

    _mark_shelf_fallback_outcome()
    return shelf_items, SEARCH_TYPE_FALLBACK, album_not_found_message(parsed), "library"
