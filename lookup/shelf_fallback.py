"""Unbound shelf fallback: the final, additive step in ``perform_lookup`` (LML#1391/#1393).

Invariant: a response that is non-empty on ``main`` stays byte-for-byte
unchanged. Only a response whose result list is empty may change.

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

The full rationale (placement, the three ``skip`` conditions, why
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
    library_artist_for,
    normalize_for_comparison,
)
from lookup.models import LookupResultItem
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


def _mark_shelf_fallback_outcome(row_count: int) -> None:
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
            scope.transaction.set_data("lookup.shelf_fallback_rows", row_count)
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
) -> tuple[list[LookupResultItem], str, str | None, str | None, int]:
    """Append the artist's shelf as display-only rows when the response is
    otherwise empty. See the module docstring for the full contract.

    ``skip`` is the caller's ``state.timed_out or state.upstream_shed or
    is_discogs_low_priority()``, plus a spent spine deadline. None of them
    returns early or is visible to this function any other way, so the caller
    has to pass them in.

    Returns ``(result_items, search_type, context_message, external_source,
    shelf_fallback_rows)``. The first four are unchanged when any trigger
    condition fails, or when the search raises: a pure pass-through, so the
    call site in ``perform_lookup`` never needs its own gate.
    ``shelf_fallback_rows`` is 0 on every pass-through and ``len(shelf_items)``
    when this fires; the caller threads it into telemetry so this lane stays
    invisible to the LML#1233 miss classification. See "Telemetry
    invisibility" in ``docs/architecture.md``, "Shelf fallback (step 8):
    design notes".
    """
    unchanged = (result_items, search_type, context_message, external_source, 0)
    lib_artist = library_artist_for(parsed)
    if result_items or skip or not (parsed.album or "").strip() or not lib_artist:
        return unchanged

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
