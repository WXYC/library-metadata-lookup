"""Live-API replay of the LML#784 PR-A acceptance pairs (Bug Fix Protocol).

``docs/testing.md`` requires every lookup-recall bug to carry an integration
test against the real APIs alongside its mocked unit tests. These replay the
category-1 (multi-artist "A & B") and category-4 (query-side "S/T") pairs
through ``_library_miss_discogs_search`` with a real ``DiscogsService`` and
no PG cache attached — the API arm the floor-reject fall-through retries
into. The exact release-id pins assert both protocol halves at once: the
correct release is included, and any floor-clearing false positive would
surface as a different id.

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
from services.parser import MessageType, ParsedRequest

DISCOGS_TOKEN = os.environ.get("DISCOGS_TOKEN")

pytestmark = [
    pytest.mark.external_api,
    pytest.mark.skipif(not DISCOGS_TOKEN, reason="DISCOGS_TOKEN not set"),
]


def _parsed(artist: str, album: str) -> ParsedRequest:
    return ParsedRequest(
        artist=artist,
        album=album,
        message_type=MessageType.REQUEST,
        is_request=True,
    )


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
    of the literal placeholder (which scores 22.2 and can never pass)."""
    result = await _library_miss_discogs_search(
        _parsed("Matmos", "S/T"),
        discogs_service=service,
    )
    assert result is not None, "expected release 63794; got no floor-clearing candidate"
    _, best = result
    assert best.release_id == 63794, f"resolved wrong release {best.release_id}"
    assert (best.album or "").strip().upper() != "S/T", (
        "a candidate literally titled 'S/T' must never clear via the placeholder"
    )
