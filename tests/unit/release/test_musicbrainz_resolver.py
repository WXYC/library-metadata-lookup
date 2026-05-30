"""Unit tests for release/musicbrainz_resolver.py.

`mb_pg.fetchall` is mocked — no real PostgreSQL traffic. Tests cover:
- happy path: candidate hits, similarity floors pass, tracklist projection
- no mb_pg / blank input → None (caller skips silently)
- empty rows from PG → None (no candidate match)
- candidate below artist or album similarity floor → None
- DB error from mb_pg → None (caller handles missing tracklist)
- length-ms → "M:SS" formatting; NULL length → None duration
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from release.musicbrainz_resolver import (
    _format_duration_ms,
    resolve_tracklist_via_musicbrainz,
)


@pytest.mark.asyncio
async def test_returns_none_when_mb_pg_is_none():
    result = await resolve_tracklist_via_musicbrainz("Stereolab", "Aluminum Tunes", mb_pg=None)
    assert result is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("artist", "album"),
    [("", "X"), ("   ", "X"), ("X", ""), ("X", "   "), (None, "X"), ("X", None)],
)
async def test_returns_none_on_blank_input(artist, album):
    mb_pg = AsyncMock()
    mb_pg.fetchall = AsyncMock(return_value=[])

    result = await resolve_tracklist_via_musicbrainz(artist, album, mb_pg=mb_pg)

    assert result is None
    mb_pg.fetchall.assert_not_called()


@pytest.mark.asyncio
async def test_returns_none_when_pg_returns_empty():
    mb_pg = AsyncMock()
    mb_pg.fetchall = AsyncMock(return_value=[])

    result = await resolve_tracklist_via_musicbrainz("Stereolab", "Aluminum Tunes", mb_pg=mb_pg)

    assert result is None
    mb_pg.fetchall.assert_called_once()


@pytest.mark.asyncio
async def test_projects_track_rows_to_track_items():
    # The release matched cleanly (both similarity scores above floor) and PG
    # returned three tracks in medium/position order.
    mb_pg = AsyncMock()
    mb_pg.fetchall = AsyncMock(
        return_value=[
            {
                "release_id": 42,
                "album_score": 0.95,
                "artist_score": 0.99,
                "medium_position": 1,
                "position": 1,
                "title": "Brakhage",
                "length_ms": 252000,
            },
            {
                "release_id": 42,
                "album_score": 0.95,
                "artist_score": 0.99,
                "medium_position": 1,
                "position": 2,
                "title": "Cybele's Reverie",
                "length_ms": 245000,
            },
            {
                "release_id": 42,
                "album_score": 0.95,
                "artist_score": 0.99,
                "medium_position": 1,
                "position": 3,
                "title": "Olv 26",
                "length_ms": None,
            },
        ]
    )

    result = await resolve_tracklist_via_musicbrainz(
        "Stereolab", "Emperor Tomato Ketchup", mb_pg=mb_pg
    )

    assert result is not None
    assert len(result) == 3
    assert result[0].position == "1"
    assert result[0].title == "Brakhage"
    assert result[0].duration == "4:12"
    assert result[1].position == "2"
    assert result[1].duration == "4:05"
    assert result[2].duration is None
    # Per-track artists left empty; controllers fall back to release-level
    # artist on the picker side.
    assert result[0].artists == []


@pytest.mark.asyncio
async def test_returns_none_when_artist_score_below_floor():
    mb_pg = AsyncMock()
    mb_pg.fetchall = AsyncMock(
        return_value=[
            {
                "release_id": 1,
                "album_score": 0.95,
                "artist_score": 0.55,  # below 0.70 floor
                "medium_position": 1,
                "position": 1,
                "title": "Track",
                "length_ms": 180000,
            }
        ]
    )

    result = await resolve_tracklist_via_musicbrainz("Stereolab", "Aluminum Tunes", mb_pg=mb_pg)

    assert result is None


@pytest.mark.asyncio
async def test_returns_none_when_album_score_below_floor():
    mb_pg = AsyncMock()
    mb_pg.fetchall = AsyncMock(
        return_value=[
            {
                "release_id": 1,
                "album_score": 0.40,  # below 0.70 floor
                "artist_score": 0.95,
                "medium_position": 1,
                "position": 1,
                "title": "Track",
                "length_ms": 180000,
            }
        ]
    )

    result = await resolve_tracklist_via_musicbrainz("Stereolab", "Aluminum Tunes", mb_pg=mb_pg)

    assert result is None


@pytest.mark.asyncio
async def test_returns_none_on_pg_exception():
    # DB unavailability must never propagate; the synth path stays valid
    # with tracklist=None.
    mb_pg = AsyncMock()
    mb_pg.fetchall = AsyncMock(side_effect=RuntimeError("connection refused"))

    result = await resolve_tracklist_via_musicbrainz("Stereolab", "Aluminum Tunes", mb_pg=mb_pg)

    assert result is None


@pytest.mark.parametrize(
    ("length_ms", "expected"),
    [
        (None, None),
        (0, "0:00"),
        (59000, "0:59"),
        (60000, "1:00"),
        (245000, "4:05"),
        (3600000, "60:00"),  # MB length spans rare hour-long tracks; format degrades
    ],
)
def test_format_duration_ms(length_ms, expected):
    assert _format_duration_ms(length_ms) == expected
