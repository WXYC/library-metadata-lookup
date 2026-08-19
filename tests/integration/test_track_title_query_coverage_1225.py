"""Integration test for the query-coverage gate acceptance criteria (LML#1225).

Motivating case: Discogs release 6194406 (Population One -- *Theater Of A
Confused Mind*), track B2 "Battle For Space". Production, 2026-08-18: a
listener's song-only request "Space Lizzard Battle Star hell cat" validated
against this release because ``token_set_ratio('space lizzard battle star
hell cat', 'battle for space') == 85.71`` cleared the (pre-fix) 85 fuzzy
floor -- the metric structurally ignores the four query tokens that match
nothing. The query-coverage gate in ``discogs/matching.py`` closes this by
also requiring that most of the QUERY's own tokens land in the title.

Marked ``external_api`` -- hits the real Discogs API; self-skips if
``DISCOGS_TOKEN`` is unset. See ``tests/integration/test_validate_band_track_credits.py``
for the sibling pattern this follows (LML#334's live-Discogs anchor).
"""

from __future__ import annotations

import os

import pytest

from discogs.service import DiscogsService

DISCOGS_TOKEN = os.environ.get("DISCOGS_TOKEN")

pytestmark = [
    pytest.mark.external_api,
    pytest.mark.skipif(not DISCOGS_TOKEN, reason="DISCOGS_TOKEN not set"),
]

_POPULATION_ONE_RELEASE_ID = 6194406


@pytest.mark.asyncio
async def test_noise_padded_query_no_longer_validates():
    """LML#1225's production repro, replayed against the real Discogs release.

    Query-token coverage is 2/6 (33%, well below the 0.8 floor): only
    'battle' and 'space' land in the title; 'lizzard', 'star', 'hell', 'cat'
    match nothing. Must now return False.
    """
    service = DiscogsService(token=DISCOGS_TOKEN)
    try:
        result = await service.validate_track_on_release(
            _POPULATION_ONE_RELEASE_ID, "Space Lizzard Battle Star hell cat", "Population One"
        )
    finally:
        await service.close()
    assert result is False


@pytest.mark.asyncio
async def test_clean_query_still_validates_no_recall_regression():
    """No-regression control: the track's real title, un-padded, must still
    validate against the same real release -- the fix narrows precision
    without costing recall on a genuine match."""
    service = DiscogsService(token=DISCOGS_TOKEN)
    try:
        result = await service.validate_track_on_release(
            _POPULATION_ONE_RELEASE_ID, "Battle For Space", "Population One"
        )
    finally:
        await service.close()
    assert result is True
