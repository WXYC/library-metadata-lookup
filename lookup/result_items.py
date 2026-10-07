"""Response-row construction for ``perform_lookup``: internal models to API contract models.

Moved verbatim out of ``lookup/orchestrator.py`` (LML#1443) so the response-row
build has room to grow without raising the orchestrator's module budget.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from generated.api_models import ReconciledIdentity
from library.models import LibraryItem
from lookup.external_search import build_external_catalog_item
from lookup.models import LookupResultItem

if TYPE_CHECKING:
    from lookup.orchestrator import LookupState


def build_result_items(
    state: LookupState,
    identities_by_artist: dict[str, ReconciledIdentity],
) -> list[LookupResultItem]:
    """Build the response items (convert internal models to API contract models).

    READS: ``items_with_artwork`` (takes precedence when non-empty — the
    canonical rule statement is ``LookupState.result_count``),
    ``library_results``, ``matched_via_by_id``.
    WRITES: nothing.
    """

    def _identity_for(item: LibraryItem) -> ReconciledIdentity | None:
        if not item.artist:
            return None
        return identities_by_artist.get(item.artist)

    result_items = []
    if state.items_with_artwork:
        for item, artwork in state.items_with_artwork:
            # Synthesized items (id=0, from Step 3a) have no library call-number
            # components; build the "(external)" sentinel that Backend-Service
            # already understands (same contract as the Step 7
            # include_external_caches path — one construction site for both,
            # lookup/external_search.py:build_external_catalog_item).
            catalog_item = (
                build_external_catalog_item(artist=item.artist, title=item.title)
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
                    matched_via=None if item.id == 0 else state.matched_via_by_id.get(item.id),
                )
            )
    elif state.library_results:
        for item in state.library_results:
            result_items.append(
                LookupResultItem(
                    library_item=item.to_catalog_item(),
                    reconciled_identity=_identity_for(item),
                    matched_via=state.matched_via_by_id.get(item.id),
                )
            )
    return result_items
