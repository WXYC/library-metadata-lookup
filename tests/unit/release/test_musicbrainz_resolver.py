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

from unittest.mock import AsyncMock, patch

import pytest

from release.musicbrainz_resolver import (
    _format_duration_ms,
    resolve_tracklist_via_musicbrainz,
)


def _make_capturing_transaction():
    """Return a Sentry-transaction stand-in whose ``set_data`` calls are
    appended to ``._calls`` as ``(args, kwargs)`` tuples.

    Shared by the LML#506 telemetry tests so the pattern doesn't drift
    across the five test sites that need it. The mock has no ``__init__``,
    so ``_calls`` is assigned right after construction and is guaranteed
    present before any caller invokes ``set_data``. If a future maintainer
    adds an ``__init__`` to this class, they must initialize ``_calls``
    inside it before any ``set_data`` call can occur.
    """
    instance = type(
        "MockTransaction",
        (),
        {"set_data": lambda self, *args, **kwargs: self._calls.append((args, kwargs))},
    )()
    instance._calls = []
    return instance


def _captured_attrs(transaction) -> dict[str, object]:
    """Return ``{key: value}`` for the positional-arg form of ``set_data``."""
    return {args[0]: args[1] for args, _ in transaction._calls}


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


@pytest.mark.asyncio
async def test_returns_none_on_pg_timeout():
    # A slow MB query must not push the response past Backend-Service's
    # 30s AbortController; the timeout coerces it to a quiet None.
    import asyncio

    async def hang(*_args, **_kwargs):
        await asyncio.sleep(60)
        return []

    mb_pg = AsyncMock()
    mb_pg.fetchall = hang

    with patch("release.musicbrainz_resolver._PG_TIMEOUT_S", 0.05):
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


@pytest.mark.asyncio
async def test_projects_requested_vs_returned_album_on_sentry_trace():
    """LML#506 telemetry: the resolver projects the requested album and the
    returned candidate's title onto the active Sentry transaction so the
    trace explorer can size the Deluxe-vs-Original drift cohort — needed to
    justify the resolver-side top-K follow-up.
    """
    mb_pg = AsyncMock()
    mb_pg.fetchall = AsyncMock(
        return_value=[
            {
                "release_id": 42,
                "release_title": "Emperor Tomato Ketchup",  # actually returned
                "album_score": 0.95,
                "artist_score": 0.99,
                "medium_position": 1,
                "position": 1,
                "title": "Brakhage",
                "length_ms": 252000,
            }
        ]
    )
    txn = _make_capturing_transaction()

    with patch("release.musicbrainz_resolver.sentry_sdk") as sentry:
        sentry.get_current_scope.return_value.transaction = txn
        await resolve_tracklist_via_musicbrainz(
            "Stereolab", "Emperor Tomato Ketchup (Deluxe Edition)", mb_pg=mb_pg
        )

    values = _captured_attrs(txn)
    keys = list(values.keys())
    assert "lookup.mb_resolver.requested_album" in keys
    assert "lookup.mb_resolver.returned_album" in keys
    assert values["lookup.mb_resolver.requested_album"] == "Emperor Tomato Ketchup (Deluxe Edition)"
    assert values["lookup.mb_resolver.returned_album"] == "Emperor Tomato Ketchup"


@pytest.mark.asyncio
async def test_projects_returned_album_none_when_resolver_misses():
    """When the trigram cutoff returns zero rows, telemetry still fires with
    ``returned_album=None`` so the trace explorer can distinguish the
    no-candidate cohort from the candidate-rejected cohort.
    """
    mb_pg = AsyncMock()
    mb_pg.fetchall = AsyncMock(return_value=[])
    txn = _make_capturing_transaction()

    with patch("release.musicbrainz_resolver.sentry_sdk") as sentry:
        sentry.get_current_scope.return_value.transaction = txn
        await resolve_tracklist_via_musicbrainz("Stereolab", "Aluminum Tunes", mb_pg=mb_pg)

    values = _captured_attrs(txn)
    assert values["lookup.mb_resolver.requested_album"] == "Aluminum Tunes"
    assert values["lookup.mb_resolver.returned_album"] is None


@pytest.mark.asyncio
async def test_projects_telemetry_on_pg_timeout():
    """LML#506 review: telemetry must fire on the timeout path too. Without
    this, a PG outage silently empties the trace-explorer denominator and
    the Option 2 cohort-sizing analysis is biased toward happy paths only.
    """
    import asyncio

    async def hang(*_args, **_kwargs):
        await asyncio.sleep(60)
        return []

    mb_pg = AsyncMock()
    mb_pg.fetchall = hang
    txn = _make_capturing_transaction()

    with (
        patch("release.musicbrainz_resolver.sentry_sdk") as sentry,
        patch("release.musicbrainz_resolver._PG_TIMEOUT_S", 0.05),
    ):
        sentry.get_current_scope.return_value.transaction = txn
        result = await resolve_tracklist_via_musicbrainz("Stereolab", "Aluminum Tunes", mb_pg=mb_pg)

    assert result is None
    values = _captured_attrs(txn)
    assert values["lookup.mb_resolver.requested_album"] == "Aluminum Tunes"
    assert values["lookup.mb_resolver.returned_album"] is None


@pytest.mark.asyncio
async def test_projects_telemetry_on_pg_exception():
    """LML#506 review: telemetry must also fire when PG raises so the
    exception cohort is filterable in the trace explorer.
    """
    mb_pg = AsyncMock()
    mb_pg.fetchall = AsyncMock(side_effect=RuntimeError("connection refused"))
    txn = _make_capturing_transaction()

    with patch("release.musicbrainz_resolver.sentry_sdk") as sentry:
        sentry.get_current_scope.return_value.transaction = txn
        result = await resolve_tracklist_via_musicbrainz("Stereolab", "Aluminum Tunes", mb_pg=mb_pg)

    assert result is None
    values = _captured_attrs(txn)
    assert values["lookup.mb_resolver.requested_album"] == "Aluminum Tunes"
    assert values["lookup.mb_resolver.returned_album"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("release_title_in_row", "expected_projected"),
    [
        pytest.param(None, None, id="null-column-projects-none"),
        pytest.param(
            "",
            "",
            id="empty-string-column-projects-empty-string",
        ),
    ],
)
async def test_release_title_three_state_projection(release_title_in_row, expected_projected):
    """LML#506 review: pin the three-state contract for
    ``lookup.mb_resolver.returned_album``:

    * ``None`` projection means 'no candidate row at all' (resolver miss,
      timeout, exception, or row with NULL ``release_title``).
    * ``''`` projection means 'candidate row found but its title was
      literally empty' — a real corruption signal distinct from the
      no-candidate cohort.

    A future refactor adding ``or None`` to the projection would silently
    flatten both buckets and break operator queries that join on this
    attribute to size the Option-2 follow-up cohort.
    """
    mb_pg = AsyncMock()
    mb_pg.fetchall = AsyncMock(
        return_value=[
            {
                "release_id": 42,
                "release_title": release_title_in_row,
                "album_score": 0.95,
                "artist_score": 0.99,
                "medium_position": 1,
                "position": 1,
                "title": "Brakhage",
                "length_ms": 252000,
            }
        ]
    )
    txn = _make_capturing_transaction()

    with patch("release.musicbrainz_resolver.sentry_sdk") as sentry:
        sentry.get_current_scope.return_value.transaction = txn
        await resolve_tracklist_via_musicbrainz("Stereolab", "Aluminum Tunes", mb_pg=mb_pg)

    values = _captured_attrs(txn)
    assert values["lookup.mb_resolver.returned_album"] == expected_projected
