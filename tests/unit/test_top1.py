"""Unit tests for lookup/enrichment/top1.py's Wikipedia-URL extractor swap (LML#513).

``fetch_top1_release_details``'s inline ``next(...)`` first-substring match
is replaced by a call to ``lookup.wikipedia_url.pick_artist_wikipedia_url``;
the returned ``wiki`` tuple element widens from ``str | None`` to
``lookup.wikipedia_url.PickedWikiUrl | None`` (same 5-tuple arity). See
``docs/plans/lml-1192-wikipedia-artist-bio.md`` Phase A.

The broader ``fetch_top1_release_details`` behavior (lean reads, breaker-shed
degrade, non-shed full degrade) stays pinned by ``test_enrichment.py``'s
``TestTop1LeanHydration`` / ``TestTop1ArtistBreakerShed`` — this file covers
only the widened ``wiki`` element.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from discogs.models import ArtistDetails, ReleaseMetadataResponse
from lookup.enrichment.top1 import fetch_top1_release_details
from lookup.wikipedia_url import PickedWikiUrl
from tests.factories import make_discogs_result


def _artwork():
    return make_discogs_result(release_id=555, artist="Stereolab", album="Aluminum Tunes")


def _release():
    return ReleaseMetadataResponse(
        release_id=555,
        title="Aluminum Tunes",
        artist="Stereolab",
        year=1998,
        artist_id=99,
        release_url="https://discogs.com/release/555",
    )


@pytest.mark.asyncio
class TestFetchTop1WikipediaWidening:
    async def test_wiki_element_is_a_picked_wiki_url_when_a_wikipedia_url_is_present(self):
        discogs_service = AsyncMock()
        discogs_service.get_release.return_value = _release()
        discogs_service.get_artist_details.return_value = ArtistDetails(
            artist_id=99,
            name="Stereolab",
            profile="French-British band.",
            urls=["https://en.wikipedia.org/wiki/Stereolab", "https://stereolab.example"],
        )

        _year, _bio, wiki, _release_obj, _details = await fetch_top1_release_details(
            discogs_service, _artwork()
        )

        assert isinstance(wiki, PickedWikiUrl)
        assert wiki.url == "https://en.wikipedia.org/wiki/Stereolab"

    async def test_wiki_element_is_none_when_no_wikipedia_url_present(self):
        discogs_service = AsyncMock()
        discogs_service.get_release.return_value = _release()
        discogs_service.get_artist_details.return_value = ArtistDetails(
            artist_id=99,
            name="Stereolab",
            profile="French-British band.",
            urls=["https://stereolab.example"],
        )

        _year, _bio, wiki, _release_obj, _details = await fetch_top1_release_details(
            discogs_service, _artwork()
        )

        assert wiki is None

    async def test_wiki_element_is_none_when_details_urls_is_empty(self):
        discogs_service = AsyncMock()
        discogs_service.get_release.return_value = _release()
        discogs_service.get_artist_details.return_value = ArtistDetails(
            artist_id=99, name="Stereolab", profile="French-British band.", urls=[]
        )

        _year, _bio, wiki, _release_obj, _details = await fetch_top1_release_details(
            discogs_service, _artwork()
        )

        assert wiki is None
