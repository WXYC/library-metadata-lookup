"""Live-API replay of the LML#784 acceptance pairs (Bug Fix Protocol).

``docs/testing.md`` requires every lookup-recall bug to carry an integration
test against the real APIs alongside its mocked unit tests. These replay the
category-1 (multi-artist "A & B"), category-4 (query-side "S/T"), and
category-2 (V/A-compilation rescue) pairs through
``_library_miss_discogs_search`` with a real ``DiscogsService`` and no PG
cache attached — the API arm the floor-reject fall-through retries into.
The exact release-id pins assert both protocol halves at once: the correct
release is included, and any floor-clearing false positive would surface as
a different id.

Category 3 (typo) deliberately has no live test: the Discogs API returns
zero results for the typo'd title on both the strict and fuzzy arms
(reproduced on the ticket), so the PG trigram tier is the only rescue —
pinned in ``test_search_releases_credits.py``.

Marked ``external_api`` — hits the real Discogs API; self-skips if
``DISCOGS_TOKEN`` is unset.
"""

from __future__ import annotations

import os

import pytest

from discogs.service import DiscogsService
from lookup.strategies.library_miss import _library_miss_discogs_search
from tests.factories import make_parsed_request as _parsed

DISCOGS_TOKEN = os.environ.get("DISCOGS_TOKEN")

pytestmark = [
    pytest.mark.external_api,
    pytest.mark.skipif(not DISCOGS_TOKEN, reason="DISCOGS_TOKEN not set"),
]


@pytest.fixture
def service() -> DiscogsService:
    return DiscogsService(token=DISCOGS_TOKEN)


@pytest.mark.asyncio
async def test_merce_lemon_and_fust_resolves_joined_credit(service):
    """Category 1: the '&' pair floor-passes against the API arm's joined
    "Fust, Merce Lemon" credit (artist 91.4 / album 100)."""
    result = await _library_miss_discogs_search(
        _parsed("Merce Lemon & Fust", "Cup of Loneliness / Choices"),
        discogs_service=service,
    )
    assert result is not None, "expected release 36830641; got no floor-clearing candidate"
    _, best = result
    assert best.release_id == 36830641, f"resolved wrong release {best.release_id}"


@pytest.mark.asyncio
async def test_anadol_and_marie_klock_resolves_either_pressing(service):
    """Category 1: both Manivelles pressings are correct — 37483479 carries the
    '&'-credit (artist 100), 37552383 the comma credit (92.3)."""
    result = await _library_miss_discogs_search(
        _parsed("Anadol & Marie Klock", "Manivelles"),
        discogs_service=service,
    )
    assert result is not None, "expected a Manivelles pressing; got no floor-clearing candidate"
    _, best = result
    assert best.release_id in {37483479, 37552383}, f"resolved wrong release {best.release_id}"


@pytest.mark.asyncio
async def test_matmos_self_titled_placeholder_resolves(service):
    """Category 4: the query-side "S/T" swap searches the artist name instead
    of the literal placeholder (which scores 22.2 and can never pass).

    63794 and 20466 both score 100/100/100 -- a genuine exact tie Discogs
    itself doesn't order consistently. LML#1097 makes ``find_best_typed_match``
    break such ties deterministically by ascending ``release_id`` rather than
    by whichever the API happened to return first, so 20466 (not 63794) is
    the now-guaranteed pick -- both remain acceptable per this test's contract
    (mirrors ``test_anadol_and_marie_klock_resolves_either_pressing`` above).
    """
    result = await _library_miss_discogs_search(
        _parsed("Matmos", "S/T"),
        discogs_service=service,
    )
    assert result is not None, "expected release 63794 or 20466; got no floor-clearing candidate"
    _, best = result
    assert best.release_id in {63794, 20466}, f"resolved wrong release {best.release_id}"
    assert (best.album or "").strip().upper() != "S/T", (
        "a candidate literally titled 'S/T' must never clear via the placeholder"
    )


@pytest.mark.asyncio
async def test_perry_comp_resolves_direct_or_va_substitute(service):
    """Category 2 (PR B): 'Born in the Sky' resolves either the directly
    credited release (316672) via the standard floor or the V/A comp
    632150 via the rescue's embedded-title-segment evidence — the ticket
    sanctions both."""
    result = await _library_miss_discogs_search(
        _parsed("lee scratch perry", "Born in the Sky"),
        discogs_service=service,
    )
    assert result is not None, "expected 316672 or 632150; got no candidate past floor or rescue"
    _, best = result
    assert best.release_id in {316672, 632150}, f"resolved wrong release {best.release_id}"


@pytest.mark.asyncio
async def test_tasquier_comp_resolves_via_tracklist_credit(service):
    """Category 2 (PR B): Lionel Tasquier appears only in the comp's
    per-track credits — the rescue's live ``get_release`` tracklist fetch
    is the evidence path."""
    result = await _library_miss_discogs_search(
        _parsed("Lionel Tasquier", "Prends Le Temps D'écouter"),
        discogs_service=service,
    )
    assert result is not None, "expected release 27518829; got no candidate past floor or rescue"
    _, best = result
    assert best.release_id == 27518829, f"resolved wrong release {best.release_id}"


@pytest.mark.asyncio
async def test_mavi_disambiguation_suffix_resolves(service):
    """LML#1206: the bare name ``"MAVI"`` must resolve *The Pilot*, credited
    under Discogs' disambiguation-suffixed form ``"Mavi (12)"`` — the live-API
    companion to the mocked reproduction in
    ``tests/unit/test_library_miss_discogs.py::TestDisambiguationSuffixRecall``.

    Both *The Pilot* pressings are correct and share master 4206252: 35940307
    is the 2025-11-25 digital release, 37193856 the 2026-02-27 vinyl LP. Their
    tracklists are identical and both carry the ticket's two victim tracks
    (``31 Days`` and ``G-Annis Freestyle``), so either satisfies the enrichment
    the ticket is about. The API arm this module exercises (no PG cache
    attached) surfaces both at a 100/100 tie, which LML#1097 breaks
    deterministically by ascending ``release_id`` — making 35940307 the
    guaranteed pick here, while the ticket's prod measurement named 37193856
    because the PG cache arm holds that pressing. Pinning the set rather than
    one id mirrors ``test_anadol_and_marie_klock_resolves_either_pressing`` and
    ``test_matmos_self_titled_placeholder_resolves`` above.

    The credit assertion is what carries this test's protocol value: the
    resolved candidate must be the *suffixed* credit, which is precisely what
    could not clear the 80/80 floor before this fix
    (``score_match("MAVI", "Mavi (12)")`` = 61.5).
    """
    result = await _library_miss_discogs_search(
        _parsed("MAVI", "The Pilot"),
        discogs_service=service,
    )
    assert result is not None, "expected a The Pilot pressing; got no floor-clearing candidate"
    _, best = result
    assert best.release_id in {35940307, 37193856}, f"resolved wrong release {best.release_id}"
    assert best.artist == "Mavi (12)", (
        f"expected the disambiguation-suffixed credit this fix targets; got {best.artist!r}"
    )
    assert (best.album or "").strip().lower() == "the pilot", (
        f"resolved the wrong album {best.album!r}"
    )
