"""Unit tests for `POST /api/v1/identity/pinned-tracklists` (LML#1503).

The route answers one entry per requested `legacy_release_id`, in request
order, with the row's Discogs pin and grouped tracklist. PG access goes
through a fake `PgSource` override; the pins reader and the tracklist loader
are monkeypatched at the router, so these tests cover the route, the id
validation and the grouping only. The pins reader's real SQL is covered by
`tests/integration/test_library_release_override.py` (`pg` marker).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from asyncpg.exceptions import PostgresError
from httpx import ASGITransport, AsyncClient

from entity.library_release_override import get_library_release_pins_strict
from entity.release_tracklist import ReleaseTracklistRow
from entity.sources import PgSource
from tests.unit.conftest import override_deps

URL = "/api/v1/identity/pinned-tracklists"


def _row(release_id, sequence, title, credit=None, position="A1"):
    return ReleaseTracklistRow(
        release_id=release_id,
        sequence=sequence,
        position=position,
        title=title,
        credit_artist_name=credit,
    )


@pytest.fixture
def client_app(mock_settings, monkeypatch):
    import identity.router as router_module
    from config.settings import get_settings
    from core.dependencies import (
        get_discogs_cache_pg,
        get_discogs_service,
        get_library_db,
        get_posthog_client,
    )
    from main import app

    state = {"pins": {}, "tracklists": {}, "pin_calls": 0, "tracklist_calls": 0}

    async def fake_pins(pg, ids):
        state["pin_calls"] += 1
        state["pin_ids"] = list(ids)
        if isinstance(state["pins"], Exception):
            raise state["pins"]
        return {i: p for i, p in state["pins"].items() if i in ids}

    async def fake_tracklists(pg, release_ids):
        state["tracklist_calls"] += 1
        return {r: state["tracklists"][r] for r in release_ids if r in state["tracklists"]}

    monkeypatch.setattr(router_module, "get_library_release_pins_strict", fake_pins)
    monkeypatch.setattr(router_module, "load_release_tracklists_by_release_id", fake_tracklists)
    with override_deps(
        app,
        {
            get_library_db: AsyncMock(),
            get_discogs_service: None,
            get_posthog_client: None,
            get_settings: mock_settings,
            get_discogs_cache_pg: AsyncMock(spec=PgSource),
        },
    ):
        app.state_for_test = state
        yield app, state


async def _post(app, body):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        return await ac.post(URL, json=body)


@pytest.mark.asyncio
class TestPinnedTracklists:
    async def test_no_pin_is_null(self, client_app):
        app, _ = client_app
        resp = await _post(app, {"legacy_release_ids": [5]})
        assert resp.status_code == 200
        assert resp.json() == {"results": [{"legacy_release_id": 5, "pin": None}]}

    async def test_pin_in_cache_with_tracks(self, client_app):
        app, state = client_app
        state["pins"] = {5: (900, "alex-l-2026")}
        state["tracklists"] = {900: [_row(900, 2, "B", "Cat"), _row(900, 1, "A", "Cat")]}
        pin = (await _post(app, {"legacy_release_ids": [5]})).json()["results"][0]["pin"]
        assert pin == {
            "discogs_release_id": 900,
            "source": "alex-l-2026",
            "in_cache": True,
            "tracks": [
                {"sequence": 1, "position": "A1", "title": "A", "artist_credits": ["Cat"]},
                {"sequence": 2, "position": "A1", "title": "B", "artist_credits": ["Cat"]},
            ],
        }

    async def test_pin_in_cache_with_no_tracks(self, client_app):
        app, state = client_app
        state["pins"] = {5: (900, "s")}
        state["tracklists"] = {900: []}
        pin = (await _post(app, {"legacy_release_ids": [5]})).json()["results"][0]["pin"]
        assert pin["in_cache"] is True and pin["tracks"] == []

    async def test_pin_outside_cache(self, client_app):
        app, state = client_app
        state["pins"] = {5: (900, "s")}
        pin = (await _post(app, {"legacy_release_ids": [5]})).json()["results"][0]["pin"]
        assert pin["in_cache"] is False and pin["tracks"] == []

    async def test_no_artist_identity_needed_and_blank_position_is_null(self, client_app):
        """A track with no credit and an empty position still ships (the
        bulk-resolve composer would drop it for lack of an artist name)."""
        app, state = client_app
        state["pins"] = {5: (900, "s")}
        state["tracklists"] = {900: [_row(900, 1, "Intro", None, position="")]}
        track = (await _post(app, {"legacy_release_ids": [5]})).json()["results"][0]["pin"][
            "tracks"
        ][0]
        assert track["artist_credits"] == [] and track["position"] is None

    async def test_va_record_credits_per_track(self, client_app):
        app, state = client_app
        state["pins"] = {5: (900, "s")}
        state["tracklists"] = {
            900: [_row(900, 1, "One", "Sessa"), _row(900, 2, "Two", "Juana Molina")]
        }
        tracks = (await _post(app, {"legacy_release_ids": [5]})).json()["results"][0]["pin"][
            "tracks"
        ]
        assert [t["artist_credits"] for t in tracks] == [["Sessa"], ["Juana Molina"]]

    async def test_split_credit_is_one_entry_with_two_credits(self, client_app):
        app, state = client_app
        state["pins"] = {5: (900, "s")}
        state["tracklists"] = {
            900: [_row(900, 1, "Split", "Sessa"), _row(900, 1, "Split", "Cat Power")]
        }
        tracks = (await _post(app, {"legacy_release_ids": [5]})).json()["results"][0]["pin"][
            "tracks"
        ]
        assert len(tracks) == 1 and tracks[0]["artist_credits"] == ["Cat Power", "Sessa"]

    async def test_repeated_ids_fan_out_by_position_with_one_set_of_queries(self, client_app):
        app, state = client_app
        state["pins"] = {5: (900, "s")}
        state["tracklists"] = {900: [_row(900, 1, "A")]}
        resp = await _post(app, {"legacy_release_ids": [5, 7, 5]})
        results = resp.json()["results"]
        assert [r["legacy_release_id"] for r in results] == [5, 7, 5]
        assert results[0]["pin"] == results[2]["pin"] and results[1]["pin"] is None
        assert state["pin_ids"] == [5, 7]
        assert state["pin_calls"] == 1 and state["tracklist_calls"] == 1

    async def test_db_error_is_503_never_no_pin(self, client_app):
        app, state = client_app
        state["pins"] = PostgresError("down")
        assert (await _post(app, {"legacy_release_ids": [5]})).status_code == 503

    async def test_pool_unavailable_is_503(self, client_app):
        from core.dependencies import get_discogs_cache_pg

        app, _ = client_app
        app.dependency_overrides[get_discogs_cache_pg] = lambda: None
        assert (await _post(app, {"legacy_release_ids": [5]})).status_code == 503

    @pytest.mark.parametrize(
        ("body", "status"),
        [
            ({"legacy_release_ids": []}, 422),
            ({"legacy_release_ids": list(range(1001))}, 422),
            ({"legacy_release_ids": ["x"]}, 422),
            ({}, 422),
            # Out of the int4 range of library.legacy_release_id: rejected at
            # validation, not left to fail inside asyncpg as a permanent 503.
            ({"legacy_release_ids": [2**31]}, 422),
            ({"legacy_release_ids": [-(2**31) - 1]}, 422),
            ({"legacy_release_ids": [5, 3_000_000_000]}, 422),
            # Non-integers must not be coerced by Pydantic's lax mode.
            ({"legacy_release_ids": ["5"]}, 422),
            ({"legacy_release_ids": [5.0]}, 422),
            ({"legacy_release_ids": [True]}, 422),
        ],
    )
    async def test_invalid_bodies_are_422(self, client_app, body, status):
        app, _ = client_app
        assert (await _post(app, body)).status_code == status

    @pytest.mark.parametrize("legacy_id", [2**31 - 1, -(2**31), 0, 1])
    async def test_int4_range_edges_are_accepted(self, client_app, legacy_id):
        app, state = client_app
        resp = await _post(app, {"legacy_release_ids": [legacy_id]})
        assert resp.status_code == 200
        assert state["pin_ids"] == [legacy_id]

    async def test_malformed_json_is_400(self, client_app):
        app, _ = client_app
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.post(
                URL, content=b"{nope", headers={"content-type": "application/json"}
            )
        assert resp.status_code == 400


@pytest.mark.asyncio
class TestPinsReader:
    async def test_returns_release_and_source_in_one_query(self):
        pg = AsyncMock(spec=PgSource)
        pg.fetchall = AsyncMock(
            return_value=[{"library_id": 42, "discogs_release_id": 1000, "source": "alex"}]
        )
        assert await get_library_release_pins_strict(pg, [42, 43]) == {42: (1000, "alex")}
        assert pg.fetchall.await_count == 1

    async def test_empty_short_circuits_and_errors_propagate(self):
        pg = AsyncMock(spec=PgSource)
        assert await get_library_release_pins_strict(pg, []) == {}
        pg.fetchall = AsyncMock(side_effect=PostgresError("x"))
        with pytest.raises(PostgresError):
            await get_library_release_pins_strict(pg, [1])
