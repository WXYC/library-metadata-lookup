"""Unit tests for the V/A recall-index read helper (LML#1022).

``get_compilation_track_locations`` is the single-query probe the
orchestrator's concurrent multi-location union uses to answer "which library
shelf locations contain track *T* credited to artist *A*?" PG is mocked here;
the real query (and its index use) runs in the integration suite and in
LML#1019's own schema tests.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from entity.compilation_track_location import (
    CompilationTrackLocationProbeError,
    CompilationTrackLocationRow,
    get_compilation_track_locations,
)
from entity.sources import PgSource


@pytest.mark.asyncio
class TestGetCompilationTrackLocations:
    async def test_returns_rows_for_normalized_exact_match(self):
        pg = AsyncMock(spec=PgSource)
        pg.fetchall = AsyncMock(
            return_value=[
                {
                    "library_id": 60654,
                    "track_position": "A3",
                    "track_artist": "brian reitzell",
                    "track_title": "ikebana",
                    "credit_role": "primary",
                    "discogs_release_id": 12345,
                    "artwork_url": "https://example.com/art.jpg",
                }
            ]
        )

        result = await get_compilation_track_locations(
            pg, track_artist="Brian Reitzell", track_title="Ikebana"
        )

        assert result == [
            CompilationTrackLocationRow(
                library_id=60654,
                track_position="A3",
                track_artist="brian reitzell",
                track_title="ikebana",
                credit_role="primary",
                discogs_release_id=12345,
                artwork_url="https://example.com/art.jpg",
            )
        ]
        # Single indexed probe -- no per-candidate fan-out.
        assert pg.fetchall.await_count == 1
        query, *params = pg.fetchall.await_args.args
        assert "compilation_track_location" in query
        assert "track_artist" in query
        assert "track_title" in query
        # The query args are normalized the same way the build script wrote them.
        assert params == ["brian reitzell", "ikebana"]

    async def test_empty_artist_or_title_short_circuits_without_query(self):
        pg = AsyncMock(spec=PgSource)
        pg.fetchall = AsyncMock(return_value=[])

        assert (
            await get_compilation_track_locations(pg, track_artist="", track_title="Ikebana") == []
        )
        assert (
            await get_compilation_track_locations(pg, track_artist="Squarepusher", track_title="")
            == []
        )
        pg.fetchall.assert_not_awaited()

    async def test_no_match_returns_empty_list(self):
        pg = AsyncMock(spec=PgSource)
        pg.fetchall = AsyncMock(return_value=[])

        result = await get_compilation_track_locations(
            pg, track_artist="Squarepusher", track_title="Tommib"
        )

        assert result == []

    async def test_pg_error_raises_typed_probe_error(self):
        # LML#1026 contract inversion: pre-#1026 a PG failure degraded to []
        # (harmless when the only consumer was the additive fold), but the
        # strategy now treats [] as an AUTHORITATIVE index miss that
        # suppresses the legacy live comp pass -- so "could not ask" must
        # raise the typed error instead of masquerading as "index says no".
        # Await sites own the degrade policy (fold -> no locations; strategy
        # -> full legacy pass); /lookup still never 500s.
        pg = AsyncMock(spec=PgSource)
        pg.fetchall = AsyncMock(side_effect=RuntimeError("pool exhausted"))

        with pytest.raises(CompilationTrackLocationProbeError):
            await get_compilation_track_locations(
                pg, track_artist="Squarepusher", track_title="Tommib"
            )

    async def test_slow_pg_probe_raises_typed_probe_error_within_timeout(self, monkeypatch):
        # LML#1026 belt-and-suspenders (parent plan section 7): the probe
        # carries its own timeout so a stuck pool acquire / PG hiccup is
        # bounded here rather than riding the awaiting task as spine latency
        # -- the strategy awaits this task mid-spine post-#1026. The trip
        # raises the typed probe error ("could not ask"), never an empty
        # list (which would read as an authoritative miss).
        import time

        async def _hang(*_args, **_kwargs):
            await asyncio.sleep(30)

        pg = AsyncMock(spec=PgSource)
        pg.fetchall = AsyncMock(side_effect=_hang)
        monkeypatch.setattr("entity.compilation_track_location._PROBE_TIMEOUT_S", 0.02)

        start = time.monotonic()
        with pytest.raises(CompilationTrackLocationProbeError):
            await get_compilation_track_locations(
                pg, track_artist="Brian Reitzell", track_title="Ikebana"
            )
        assert time.monotonic() - start < 5, "timeout bound did not trip"

    async def test_malformed_row_shape_raises_typed_probe_error(self):
        # A recall-index schema drift (renamed/added column) makes the
        # row-dataclass construction fail -- that is a degradation ("could
        # not consult the index"), not a miss, so it raises the typed error
        # like every other probe failure.
        pg = AsyncMock(spec=PgSource)
        pg.fetchall = AsyncMock(return_value=[{"unexpected_column": 1}])

        with pytest.raises(CompilationTrackLocationProbeError):
            await get_compilation_track_locations(
                pg, track_artist="Brian Reitzell", track_title="Ikebana"
            )
