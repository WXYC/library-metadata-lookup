"""Unbound shelf fallback: the final, additive step in ``perform_lookup`` (LML#1391/#1393).

Invariant: every row of a non-empty response stays exactly as the pipeline
produced it, in the same order, and ``results[0]`` is never displaced. An empty
response may gain the artist's shelf; a non-empty one may gain only the
self-titled companion rows described at the end of this docstring.

:func:`apply_shelf_fallback` runs once, after the location-union fold. It
fires only when ``result_items`` is empty, the caller's ``skip`` flag is false
(timed out, search-leg shed, low-priority caller, or spine deadline spent), an
album was typed, and the library artist has shelved rows of its own. It then
returns up to ``MAX_SEARCH_RESULTS`` display-only rows
(``LookupResultItem(library_item=...)`` with no ``artwork``, so no release id,
artwork URL, year, Discogs URL or streaming links), forces ``search_type`` to
``SEARCH_TYPE_FALLBACK`` (never ``direct``), sets ``external_source`` to
``"library"``, and sets the "not found in the library, but here are other
albums by X:" context line, where X is the library artist whose shelf is shown.
Rows whose title contains the requested song come first; the rest keep
``db.search`` order.

A row is kept only when its artist EQUALS the library artist under
``normalize_for_comparison``. ``filter_results_by_artist``'s prefix rung is too
loose for a lane that names the artist: "Can" would list a Canibus row. When no
row survives, or the search raises, the step does not fire and the response
stays empty, as on ``main``.

It makes no Discogs call, fetches no artwork and runs no enrichment. Its last
return value is the number of rows appended, which the caller threads into
miss telemetry so a shelf-only response still reports ``miss_clean`` with
``results_count`` 0 (``lookup/miss_kind.py``).

Self-titled companion (LML#1405): when the response is NOT empty and the
typed album is a request-side self-titled placeholder ("Epon.", "S/T"), rows
titled the artist's name that the pipeline did not return are inserted at index
1 as the same display-only rows, up to the room left under
``MAX_SEARCH_RESULTS``. ``search_type``, the context line, ``external_source``
and the netted row count are untouched, and the same ``skip`` flag applies, so
a low-priority caller never sees one. See :func:`_self_titled_companions`.

The full rationale (placement, the four ``skip`` conditions, why
``/lookup/bulk`` is excluded, telemetry invisibility, and why a
cache-confirmed ordering tier is not implemented) is in
``docs/architecture.md``, "Shelf fallback (step 8): design notes".
"""

import logging

import sentry_sdk

from core.search import SEARCH_TYPE_FALLBACK
from library.db import LibraryDB
from library.models import LibraryItem
from lookup.matching import (
    _FETCH_LIMIT,
    MAX_SEARCH_RESULTS,
    album_not_found_message,
    filter_results_by_artist,
    is_self_titled,
    is_self_titled_request_placeholder,
    library_artist_for,
    normalize_for_comparison,
)
from lookup.models import LookupResultItem
from lookup.name_folding import fold_punctuation_for_comparison
from services.parser import ParsedRequest

logger = logging.getLogger(__name__)


def _rows_by_artist(rows: list[LibraryItem], artist: str) -> list[LibraryItem]:
    """Rows whose artist equals ``artist`` after normalization (never a prefix)."""
    key = normalize_for_comparison(artist).strip()
    if not key:
        return []
    return [row for row in rows if normalize_for_comparison(row.artist or "").strip() == key]


def _order_shelf_rows(rows: list[LibraryItem], song: str | None) -> list[LibraryItem]:
    """Song-in-title rows first (stable), then the rest in query order.

    Tier 2 (cache-confirmed song) is skipped, so this is tiers 1 and 3 only.
    ``docs/architecture.md``, "Shelf fallback (step 8): design notes", says why.
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


def _mark_shelf_fallback_outcome(row_count: int, key: str = "lookup.shelf_fallback_rows") -> None:
    """Project the ONE new, separate Sentry trace attr for this lane.

    Deliberately NOT ``lookup.outcome`` -- that key is the pre-existing
    ``library_miss_outcome`` projection (``_step_project_trace_attrs``), and
    overwriting it here would make this lane indistinguishable from whatever
    step 3a already recorded, violating the "existing trace attrs stay
    unchanged" half of the telemetry-invisibility contract
    (``docs/architecture.md``, "Shelf fallback (step 8): design notes"). A
    fresh key name, only ever set when this lane actually fires, is additive
    by construction. Observability must not break the request path.
    """
    try:
        scope = sentry_sdk.get_current_scope()
        if scope.transaction is not None:
            scope.transaction.set_data(key, row_count)
    except Exception:
        pass


async def _self_titled_companions(
    parsed: ParsedRequest, db: LibraryDB, lib_artist: str, result_items: list[LookupResultItem]
) -> list[LookupResultItem]:
    """Display-only rows titled the artist's name, for a typed self-titled
    placeholder the pipeline answered with some other album (LML#1405).

    Empty unless the typed album is a request-side placeholder ("Epon.",
    "S/T"), the response has room under ``MAX_SEARCH_RESULTS``, and no shelved
    title is itself the typed album or a placeholder (the LML#1392
    literal-title guard, as in ``runs_album_resolution``). Artist and title
    both match by equality, never a prefix or word overlap, so a numbered
    sibling ("X II") is not a companion. Rows already returned are left out.
    """
    room = MAX_SEARCH_RESULTS - len(result_items)
    typed = parsed.album or ""
    if room <= 0 or not is_self_titled_request_placeholder(typed):
        return []
    found = await db.search(query=lib_artist, limit=_FETCH_LIMIT)
    typed_folded = fold_punctuation_for_comparison(typed.lower())
    if any(
        is_self_titled(row.title or "")
        or fold_punctuation_for_comparison((row.title or "").lower()) == typed_folded
        for row in filter_results_by_artist(found, lib_artist)
    ):
        return []
    name = normalize_for_comparison(lib_artist).strip()
    returned = {item.library_item.id for item in result_items}
    return [
        LookupResultItem(library_item=row.to_catalog_item())
        for row in _rows_by_artist(found, lib_artist)
        if row.id not in returned and normalize_for_comparison(row.title or "").strip() == name
    ][:room]


async def apply_shelf_fallback(
    parsed: ParsedRequest,
    db: LibraryDB,
    skip: bool,
    result_items: list[LookupResultItem],
    search_type: str,
    context_message: str | None,
    external_source: str | None,
) -> tuple[list[LookupResultItem], str, str | None, str | None, int]:
    """Append the artist's shelf as display-only rows when the response is
    otherwise empty, or insert the self-titled companion rows when it is not.
    See the module docstring for the full contract.

    ``skip`` is the caller's ``state.timed_out or state.upstream_shed or
    is_discogs_low_priority()``, plus a spent spine deadline. None of them
    returns early or is visible to this function any other way, so the caller
    has to pass them in.

    Returns ``(result_items, search_type, context_message, external_source,
    shelf_fallback_rows)``. The first four are unchanged when any trigger
    condition fails, or when the search raises: a pure pass-through, so the
    call site in ``perform_lookup`` never needs its own gate.
    ``shelf_fallback_rows`` is 0 on every pass-through and on the companion
    lane (those responses are hits either way), and ``len(shelf_items)`` when
    the shelf lane fires; the caller threads it into telemetry so this lane stays
    invisible to the LML#1233 miss classification. See "Telemetry
    invisibility" in ``docs/architecture.md``, "Shelf fallback (step 8):
    design notes".
    """
    unchanged = (result_items, search_type, context_message, external_source, 0)
    lib_artist = library_artist_for(parsed)
    if skip or not (parsed.album or "").strip() or not lib_artist:
        return unchanged
    if result_items:
        # LML#1405: the one change to a non-empty response. ``results[0]`` and
        # every row already present stay as they are; companions go in at 1.
        try:
            companions = await _self_titled_companions(parsed, db, lib_artist, result_items)
        except Exception as exc:
            logger.warning("Self-titled companion failed for %r: %s", lib_artist, exc)
            return unchanged
        if not companions:
            return unchanged
        _mark_shelf_fallback_outcome(len(companions), "lookup.self_titled_companion_rows")
        merged = [result_items[0], *companions, *result_items[1:]]
        return (merged, search_type, context_message, external_source, 0)

    # On ``main`` this path returned an empty 200 with no further I/O, so a
    # failure here must leave that response exactly as it was.
    try:
        found = await db.search(query=lib_artist, limit=_FETCH_LIMIT)
        rows = _order_shelf_rows(_rows_by_artist(found, lib_artist), parsed.song)
        shelf_items = [
            LookupResultItem(library_item=row.to_catalog_item())
            for row in rows[:MAX_SEARCH_RESULTS]
        ]
    except Exception as exc:
        logger.warning("Shelf fallback failed for %r; response stays empty: %s", lib_artist, exc)
        return unchanged
    if not shelf_items:
        return unchanged

    _mark_shelf_fallback_outcome(len(shelf_items))
    return (
        shelf_items,
        SEARCH_TYPE_FALLBACK,
        album_not_found_message(parsed, lib_artist),
        "library",
        len(shelf_items),
    )
