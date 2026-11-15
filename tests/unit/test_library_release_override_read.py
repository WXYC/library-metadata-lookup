"""Unit tests for the library-release override PG read helpers (LML#850).

``get_library_release_pins`` is the single-query prefetch the orchestrator
uses to map a request's library ids to their pinned Discogs releases, each
tagged with the batch ``source`` that wrote it (LML#1514). PG is mocked here;
the real query runs in the integration suite.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from asyncpg.exceptions import PostgresError

from entity.library_release_override import (
    PinnedRelease,
    get_library_release_overrides_strict,
    get_library_release_pins,
    get_library_release_pins_strict,
)
from entity.sources import PgSource


class TestPinnedRelease:
    """Only a music director's pin (``source`` ``md-<user id>``) skips the floor."""

    @pytest.mark.parametrize(
        ("source", "exempt"),
        [
            ("md-42", True),
            ("md-abc123", True),
            ("alex-l-2026", False),
            ("alex-l-2026-masters-api", False),
            ("MD-42", False),
            ("xmd-42", False),
            ("md", False),
            ("", False),
        ],
    )
    def test_exempt_from_floor(self, source, exempt):
        assert PinnedRelease(release_id=5000, source=source).exempt_from_floor is exempt

    def test_is_a_release_source_pair(self):
        # A tuple, so the pinned-tracklists route's ``(release_id, source)``
        # unpacking keeps working on the strict reader's values.
        release_id, source = PinnedRelease(release_id=5000, source="md-42")
        assert (release_id, source) == (5000, "md-42")


@pytest.mark.asyncio
class TestGetLibraryReleasePins:
    async def test_returns_id_to_tagged_pin_map(self):
        pg = AsyncMock(spec=PgSource)
        pg.fetchall = AsyncMock(
            return_value=[
                {"library_id": 42, "discogs_release_id": 1000, "source": "alex-l-2026"},
                {"library_id": 77, "discogs_release_id": 2000, "source": "md-12"},
            ]
        )

        result = await get_library_release_pins(pg, [42, 77, 99])

        assert result == {
            42: PinnedRelease(release_id=1000, source="alex-l-2026"),
            77: PinnedRelease(release_id=2000, source="md-12"),
        }
        # Single query, keyed by the id list (no per-item fan-out).
        assert pg.fetchall.await_count == 1
        query = pg.fetchall.await_args.args[0]
        assert "library_release_override" in query
        assert "source" in query
        assert "ANY(" in query

    async def test_empty_id_list_short_circuits_without_query(self):
        pg = AsyncMock(spec=PgSource)
        pg.fetchall = AsyncMock(return_value=[])

        result = await get_library_release_pins(pg, [])

        assert result == {}
        pg.fetchall.assert_not_awaited()

    async def test_pg_error_degrades_to_empty_map(self):
        # Best-effort posture (mirrors the streaming / resolution caches): a PG
        # failure must not break the lookup path — it degrades to "no overrides"
        # and the fuzzy pick runs as before.
        pg = AsyncMock(spec=PgSource)
        pg.fetchall = AsyncMock(side_effect=RuntimeError("pool exhausted"))

        result = await get_library_release_pins(pg, [42])

        assert result == {}


@pytest.mark.asyncio
class TestGetLibraryReleaseOverridesStrict:
    """LML#1138: the bulk-resolve arm needs the SAME single-query prefetch
    with the OPPOSITE failure posture — PG errors propagate so the router
    can degrade to the unvisited `(False, [])` state AND emit the
    `single_artist_track_read_fail_open` counter. The swallowing reader
    above would silently map an outage to "no pins", indistinguishable
    from the routine un-pinned case, blinding the counter."""

    async def test_returns_id_to_release_map(self):
        pg = AsyncMock(spec=PgSource)
        pg.fetchall = AsyncMock(
            return_value=[
                {"library_id": 42, "discogs_release_id": 1000},
                {"library_id": 77, "discogs_release_id": 2000},
            ]
        )

        result = await get_library_release_overrides_strict(pg, [42, 77, 99])

        assert result == {42: 1000, 77: 2000}
        assert pg.fetchall.await_count == 1
        query = pg.fetchall.await_args.args[0]
        assert "library_release_override" in query
        assert "ANY(" in query

    async def test_empty_id_list_short_circuits_without_query(self):
        pg = AsyncMock(spec=PgSource)
        pg.fetchall = AsyncMock(return_value=[])

        result = await get_library_release_overrides_strict(pg, [])

        assert result == {}
        pg.fetchall.assert_not_awaited()

    async def test_pg_error_propagates(self):
        pg = AsyncMock(spec=PgSource)
        pg.fetchall = AsyncMock(side_effect=PostgresError("pool exhausted"))

        with pytest.raises(PostgresError):
            await get_library_release_overrides_strict(pg, [42])


@pytest.mark.asyncio
class TestGetLibraryReleasePinsStrict:
    async def test_returns_tagged_pins(self):
        pg = AsyncMock(spec=PgSource)
        pg.fetchall = AsyncMock(
            return_value=[{"library_id": 42, "discogs_release_id": 1000, "source": "md-12"}]
        )

        result = await get_library_release_pins_strict(pg, [42])

        assert result == {42: PinnedRelease(release_id=1000, source="md-12")}
        assert result[42].exempt_from_floor
