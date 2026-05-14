"""Integration test for the band-vs-track-credits acceptance criterion.

Motivating case: ``Towers Of Dub`` / ``The Orb`` / Live 93 (release 674529).
On the Discogs API the track is credited to the band members
(Alex Paterson, Kris Weston, Thomas Fehlmann) as per-track ``extraartists``
in the XML, and the discogs-cache ETL stores all per-track credits in
``release_track_artist`` without a role column. Both code paths must fall
back to the release-level artist (``The Orb``) and validate the track.

Marked ``external_api`` — hits the real Discogs API; self-skips if
``DISCOGS_TOKEN`` is unset.
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


@pytest.mark.asyncio
async def test_validate_track_on_release_falls_back_to_band_credit():
    """`Towers Of Dub` by `The Orb` validates against release 674529 (Live 93)
    even though the per-track credits list band members, not the band itself.
    """
    service = DiscogsService(token=DISCOGS_TOKEN)
    try:
        result = await service.validate_track_on_release(674529, "Towers of Dub", "The Orb")
    finally:
        await service.close()
    assert result is True


@pytest.mark.asyncio
async def test_validate_track_on_release_still_rejects_unrelated_artist():
    """Same release must NOT validate for an unrelated artist."""
    service = DiscogsService(token=DISCOGS_TOKEN)
    try:
        result = await service.validate_track_on_release(674529, "Towers of Dub", "Stereolab")
    finally:
        await service.close()
    assert result is False
