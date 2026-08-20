"""Integration test for `_resolve_fallback_artwork` against the real Discogs API.

Companion to the unit test
``tests/unit/test_orchestrator_helpers.py::TestFetchArtworkFallback::test_uses_release_artwork_when_search_misses``.

The unit test mocks ``DiscogsService.get_release`` to assert the function
prefers ``release.artwork_url`` over the artist/label image fallback. This
integration test confirms the same fix end-to-end against a real Discogs
release that has ``images[0].uri`` populated.

Marked ``external_api`` — hits the real Discogs API; self-skips if
``DISCOGS_TOKEN`` is unset.
"""

from __future__ import annotations

import os

import pytest

from discogs.models import DiscogsSearchRequest
from discogs.service import DiscogsService
from lookup.artwork import _resolve_fallback_artwork

DISCOGS_TOKEN = os.environ.get("DISCOGS_TOKEN")

pytestmark = [
    pytest.mark.external_api,
    pytest.mark.skipif(not DISCOGS_TOKEN, reason="DISCOGS_TOKEN not set"),
]


@pytest.mark.asyncio
async def test_resolves_to_release_images_uri_when_present():
    """Given a real Discogs release whose ``images[0].uri`` is populated,
    ``_resolve_fallback_artwork`` must return that URL. Picks the top search
    hit for Stereolab / Aluminum Tunes (the project-wide example fixture) so
    the test stays anchored to a real catalog entry rather than a hard-coded
    release_id that could be deleted upstream."""
    service = DiscogsService(token=DISCOGS_TOKEN)

    search = await service.search(DiscogsSearchRequest(artist="Stereolab", album="Aluminum Tunes"))
    assert search.results, "Stereolab / Aluminum Tunes search returned no hits"
    release_id = search.results[0].release_id

    release = await service.get_release(release_id)
    assert release is not None, f"get_release({release_id}) returned None"
    assert release.artwork_url, (
        f"release {release_id} has no images[0].uri — pick a different fixture; "
        "test premise is that the release object carries an artwork URL"
    )

    resolved = await _resolve_fallback_artwork(service, release_id)
    assert resolved == release.artwork_url, (
        f"expected fallback to return release.artwork_url={release.artwork_url!r}; "
        f"got {resolved!r}. The function must prefer release.images[0].uri over "
        "the artist/label image fallback."
    )


@pytest.mark.parametrize(
    ("release_id", "label"),
    [
        (1573110, "Autechre - Confield"),
        (2496, "Autechre - Chiastic Slide"),
    ],
)
@pytest.mark.asyncio
async def test_resolves_own_cover_for_previously_misdiagnosed_releases(release_id, label):
    """LML#1237 regression pin. #687 was reopened because it misdiagnosed
    these two releases as coverless (arguing from a *different*, imageless
    Autechre pressing, release 28138) when in fact both carry their own
    Discogs cover -- LML just could not see it, because the cache row for
    each was in the "bulk-loaded, never asked about artwork" state and the
    LML#542 widened `get_release` predicate read that NULL as absence.
    Pins that `_resolve_fallback_artwork` surfaces the release's own cover
    for both, without degrading to the artist-image rung."""
    service = DiscogsService(token=DISCOGS_TOKEN)

    resolved = await _resolve_fallback_artwork(service, release_id)

    assert resolved, f"{label} (release {release_id}) resolved to no artwork at all"
    release = await service.get_release(release_id)
    assert release is not None, f"get_release({release_id}) returned None for {label}"
    assert resolved == release.artwork_url, (
        f"{label} (release {release_id}): _resolve_fallback_artwork returned {resolved!r}, "
        f"but the release's own cover is {release.artwork_url!r} -- expected the release-cover "
        "rung to win, not a degraded artist-image fallback."
    )
