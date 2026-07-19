"""Enrichment behavior for a pinned library-release override (LML#850).

The override binds its release in ``fetch_one`` (see
``test_library_release_override_fetch.py``); enrichment then treats that bound
release like any other — there is **deliberately no override carve-out** in the
LML#477/#487 sibling-leak title gate. These tests pin that design decision:

* A genuine library-album lookup (request album == the catalog row title, as
  the dj-site picker and BS flowsheet-linkage send) passes the gate naturally,
  so the pinned release's tracklist + album-derived payload surface.
* A request whose album diverges from the surfaced row's title collapses to the
  ``release_id=0`` sentinel exactly as an unpinned row would — a pin can never
  leak a mismatched row's release identity OR its curated streaming_links.

Reuses the fixtures from ``test_rowless_enrichment.py``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from lookup.enrichment import enrich_artwork_results
from tests.factories import make_discogs_result, make_library_item
from tests.unit.test_rowless_enrichment import (
    ARTIST,
    MISMATCHED_ALBUM,
    RELEASE_ID,
    RELEASE_TITLE,
    SONG,
    _artist_details,
    _resolved_release,
)

_LIBRARY_ID = 12345


@pytest.mark.asyncio
class TestPinnedReleaseEnrichment:
    async def test_pinned_release_surfaces_tracklist_on_matching_lookup(self):
        """Request album == catalog row title (the picker/flowsheet-linkage
        shape): the bound pin passes the title gate naturally and its tracklist
        + album-derived payload surface — no carve-out needed."""
        item = make_library_item(id=_LIBRARY_ID, artist=ARTIST, title=RELEASE_TITLE)
        artwork = make_discogs_result(release_id=RELEASE_ID, artist=ARTIST, album=RELEASE_TITLE)

        discogs_service = AsyncMock()
        discogs_service.get_release.return_value = _resolved_release()
        discogs_service.get_artist_details.return_value = _artist_details()

        results = await enrich_artwork_results(
            [(item, artwork)],
            discogs_service,
            song=SONG,
            album=RELEASE_TITLE,  # == item.title → gate passes naturally
            artist=ARTIST,
            extended=True,
        )

        _, enriched = results[0]
        assert enriched is not None
        assert enriched.release_id == RELEASE_ID
        assert enriched.tracklist is not None
        assert enriched.tracklist[0].title == SONG
        assert enriched.release_year == 2015

    async def test_pinned_release_collapses_on_mismatched_request(self):
        """Request album diverges from the surfaced row's title (sibling-leak
        shape): the pin gets NO exemption, so the row collapses to the sentinel
        exactly as an unpinned row would — no release/streaming_links leak."""
        item = make_library_item(id=_LIBRARY_ID, artist=ARTIST, title=RELEASE_TITLE)
        artwork = make_discogs_result(release_id=RELEASE_ID, artist=ARTIST, album=RELEASE_TITLE)

        discogs_service = AsyncMock()
        discogs_service.get_release.return_value = _resolved_release()
        discogs_service.get_artist_details.return_value = _artist_details()

        results = await enrich_artwork_results(
            [(item, artwork)],
            discogs_service,
            song=SONG,
            album=MISMATCHED_ALBUM,  # != item.title → sibling-leak gate collapses it
            artist=ARTIST,
            extended=True,
        )

        _, enriched = results[0]
        assert enriched is not None
        assert enriched.release_id == 0
        assert enriched.tracklist is None
