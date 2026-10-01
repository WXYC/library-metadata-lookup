"""The one serve rule: may a library row keep its own Discogs release? (LML#1391)

A row keeps the release bound to it (and the artwork, year and curated
streaming links that ride with it) when any of these holds:

1. its catalog title clears the typed album (the LML#477 ``score_match`` floor);
2. it is a validated row-less carry-through (LML#628);
3. the requested song was track-confirmed on its release: by step-3b
   validation, or by the strategy that bound it on the ``discogs_titles`` seam
   (the LML#684 compilation hit). A track-confirmed release is the right
   metadata for the track whatever album was typed, so LML#400's wrong-album
   contamination cannot recur through this clause.

Anything else is a sibling album surfaced for context, and serves the BS#1185
``release_id=0`` sentinel instead (LML#400). The verdict is always the row's
own, never response-wide.

The rule is ``compute_row_title_matches_requested_album`` (the step-4b serve
gate), fed a per-row ``track_confirmed`` from :func:`track_confirmed_row_ids`.
It is applied after step-3b validation, at three places that therefore cannot
disagree:

- :func:`promotable_stash_rows`: which stashed artist rows step 3b may prepend
  ahead of a compilation;
- the step-4b serve gate itself (``lookup/enrichment/item.py``);
- :func:`floor_row_binding`: the response-assembly chokepoint in
  ``_build_result_items``, which every path builds rows through, so a response
  that skipped step 4b (the degraded tail shed) is held to the same rule.
"""

from collections.abc import Collection, Mapping

from discogs.models import DiscogsSearchResult
from library.models import LibraryItem
from lookup.enrichment.item import compute_row_title_matches_requested_album
from lookup.release_resolution import ResolvedRelease
from lookup.rowless import ROWLESS_LIBRARY_ID


def track_confirmed_row_ids(
    discogs_titles: Mapping[int, ResolvedRelease], track_validated_ids: Collection[int]
) -> frozenset[int]:
    """Library ids of the rows clause 3 covers.

    ``track_validated_ids`` are the rows step-3b validation confirmed the song
    on (a row merely *kept* on a breaker shed is not among them).
    ``discogs_titles`` adds every shelf row a strategy bound to a
    ``track_confirmed`` release; the LML#1318 album-level degrade is excluded by
    that flag, and the row-less key by clause 2 owning it.
    """
    carried = {
        library_id
        for library_id, resolved in discogs_titles.items()
        if library_id != ROWLESS_LIBRARY_ID and resolved.track_confirmed
    }
    return frozenset(track_validated_ids) | carried


def row_keeps_binding(
    album: str | None,
    item: LibraryItem,
    artwork: DiscogsSearchResult | None,
    track_confirmed_ids: Collection[int],
) -> bool:
    """The serve rule for ``item`` bound to ``artwork`` against the typed ``album``.

    A whitespace-only album is read as no album, as every other LML#1391 gate
    reads it.
    """
    if not album or not album.strip():
        return True
    return compute_row_title_matches_requested_album(
        album, item, artwork, track_confirmed=item.id in track_confirmed_ids
    )


def promotable_stash_rows(
    album: str | None, validated: list[LibraryItem], track_confirmed_ids: Collection[int]
) -> list[LibraryItem]:
    """The stashed artist rows that may lead a compilation response.

    Step 3b prepends these ahead of the compilation. The rule is asked before
    step 4 binds anything, so a confirmed row qualifies outright (clause 3 will
    keep whatever release it binds) and any other row only on its title. A row
    the chokepoint would strip to a bare sentinel therefore never leads.
    """
    return [
        item
        for item in validated
        if item.id in track_confirmed_ids or row_keeps_binding(album, item, None, ())
    ]


def floor_row_binding(
    album: str | None,
    item: LibraryItem,
    artwork: DiscogsSearchResult | None,
    track_confirmed_ids: Collection[int],
) -> DiscogsSearchResult | None:
    """Return ``artwork``, or the bare sentinel when the serve rule rejects it.

    An already-collapsed sentinel passes through, since its artwork is the Apple
    probe's pick for the requested album (LML#487), not the row's own.
    """
    if artwork is None or artwork.release_id <= 0:
        return artwork
    if row_keeps_binding(album, item, artwork, track_confirmed_ids):
        return artwork
    return DiscogsSearchResult(release_id=0, release_url="")
