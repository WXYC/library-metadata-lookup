"""Unit tests for ``scripts/build_compilation_track_location.py`` (LML#1019).

Pure-logic + mocked-DB coverage. The real matching/credit-fetch/insert path
against a live discogs-cache is exercised separately in
``tests/integration/test_build_compilation_track_location_pg.py``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import aiosqlite
import pytest

from scripts.build_compilation_track_location import (
    CompCandidate,
    build_compilation_track_location,
    build_rows,
    get_processed_library_ids,
    load_library_compilations,
    tier_credit_role,
)


class TestTierCreditRole:
    def test_extra_zero_is_primary(self):
        assert tier_credit_role(0, None) == "primary"
        assert tier_credit_role(0, "Vocals") == "primary"

    @pytest.mark.parametrize(
        "role",
        ["Featuring", "featuring", "Vocals", "Voice", "Lead Vocals", "Featuring [uncredited]"],
    )
    def test_extra_one_with_featured_marker_is_featured(self, role):
        assert tier_credit_role(1, role) == "featured"

    @pytest.mark.parametrize("role", ["Producer", "Written-By", "Remix", "Mixed By", None, ""])
    def test_extra_one_without_featured_marker_is_extra(self, role):
        assert tier_credit_role(1, role) == "extra"


class TestBuildRows:
    def test_normalizes_artist_and_title_and_tiers_role(self):
        credits = [
            {
                "position": "A1",
                "sequence": 1,
                "artist_name": "The Bug",
                "track_title": "Pressure",
                "extra": 0,
                "role": None,
            },
            {
                "position": "A1",
                "sequence": 1,
                "artist_name": "Flowdan",
                "track_title": "Pressure",
                "extra": 1,
                "role": "Featuring",
            },
        ]
        rows = build_rows(
            library_id=42,
            discogs_release_id=12345,
            credits=credits,
            artwork_url="https://example.com/cover.jpg",
        )
        assert rows == [
            (42, "A1", "the bug", "pressure", "primary", 12345, "https://example.com/cover.jpg"),
            (42, "A1", "flowdan", "pressure", "featured", 12345, "https://example.com/cover.jpg"),
        ]

    def test_falls_back_to_sequence_when_position_is_blank(self):
        credits = [
            {
                "position": None,
                "sequence": 7,
                "artist_name": "Some Artist",
                "track_title": "Some Title",
                "extra": 0,
                "role": None,
            }
        ]
        rows = build_rows(library_id=1, discogs_release_id=99, credits=credits, artwork_url=None)
        assert rows == [(1, "7", "some artist", "some title", "primary", 99, None)]


@pytest.mark.asyncio
class TestLoadLibraryCompilations:
    async def test_filters_to_compilation_shelf_rows_only(self, tmp_path):
        db_path = tmp_path / "library.db"
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                "CREATE TABLE library (id INTEGER PRIMARY KEY, title TEXT, artist TEXT)"
            )
            await db.executemany(
                "INSERT INTO library (id, title, artist) VALUES (?, ?, ?)",
                [
                    (1, "Aluminum Tunes", "Stereolab"),
                    (2, "The Sound of Dub", "Various Artists - Reggae"),
                    (3, "Local Hero", "Soundtracks - L"),
                ],
            )
            await db.commit()

        comps = await load_library_compilations(str(db_path))

        assert comps == [
            CompCandidate(
                library_id=2, title="The Sound of Dub", artist="Various Artists - Reggae"
            ),
            CompCandidate(library_id=3, title="Local Hero", artist="Soundtracks - L"),
        ]


@pytest.mark.asyncio
class TestGetProcessedLibraryIds:
    async def test_returns_distinct_library_ids(self):
        conn = AsyncMock()
        conn.fetch = AsyncMock(
            return_value=[{"library_id": 2}, {"library_id": 3}, {"library_id": 2}]
        )
        result = await get_processed_library_ids(conn)
        assert result == {2, 3}


@pytest.mark.asyncio
class TestBuildCompilationTrackLocationOrchestration:
    """Mocked-collaborator coverage of the incremental/full/dry-run branching."""

    @pytest.fixture
    def comps(self):
        return [
            CompCandidate(
                library_id=2, title="The Sound of Dub", artist="Various Artists - Reggae"
            ),
            CompCandidate(library_id=3, title="Local Hero", artist="Soundtracks - L"),
        ]

    async def _run(
        self,
        monkeypatch,
        comps,
        *,
        full,
        processed_ids,
        matches,
        credits_by_release,
        dry_run=False,
        limit=None,
    ):
        conn = AsyncMock()
        conn.executemany = AsyncMock()

        async def fake_load(db_path):
            return comps

        async def fake_processed(conn_arg):
            assert conn_arg is conn
            return processed_ids

        async def fake_match(conn_arg, candidates):
            assert conn_arg is conn
            return {
                c.library_id: matches[c.library_id] for c in candidates if c.library_id in matches
            }

        async def fake_credits(conn_arg, release_ids):
            assert conn_arg is conn
            return {rid: credits_by_release.get(rid, []) for rid in release_ids}

        monkeypatch.setattr(
            "scripts.build_compilation_track_location.load_library_compilations", fake_load
        )
        monkeypatch.setattr(
            "scripts.build_compilation_track_location.get_processed_library_ids", fake_processed
        )
        monkeypatch.setattr(
            "scripts.build_compilation_track_location.match_comp_release", fake_match
        )
        monkeypatch.setattr(
            "scripts.build_compilation_track_location.fetch_track_credits", fake_credits
        )

        stats = await build_compilation_track_location(
            library_db_path="unused.db",
            discogs_conn=conn,
            discogs_service=None,
            full=full,
            limit=limit,
            dry_run=dry_run,
        )
        return conn, stats

    async def test_incremental_skips_already_processed_library_ids(self, monkeypatch, comps):
        conn, stats = await self._run(
            monkeypatch,
            comps,
            full=False,
            processed_ids={2},
            matches={3: 555},
            credits_by_release={
                555: [
                    {
                        "position": "A1",
                        "sequence": 1,
                        "artist_name": "Some Artist",
                        "track_title": "Some Title",
                        "extra": 0,
                        "role": None,
                    }
                ]
            },
        )
        assert stats["candidates"] == 1
        assert stats["matched"] == 1
        assert stats["rows_inserted"] == 1
        conn.executemany.assert_awaited_once()
        rows = conn.executemany.await_args.args[1]
        assert rows == [(3, "A1", "some artist", "some title", "primary", 555, None)]

    async def test_full_mode_includes_already_processed_library_ids(self, monkeypatch, comps):
        conn, stats = await self._run(
            monkeypatch,
            comps,
            full=True,
            processed_ids={2},
            matches={2: 111, 3: 222},
            credits_by_release={},
        )
        assert stats["candidates"] == 2
        # Both matched, but neither has cached tracklist data -> zero rows.
        assert stats["matched"] == 2
        assert stats["rows_inserted"] == 0

    async def test_unmatched_comp_is_skipped_and_retryable(self, monkeypatch, comps):
        conn, stats = await self._run(
            monkeypatch,
            comps,
            full=False,
            processed_ids=set(),
            matches={2: 111},  # library_id 3 never matches
            credits_by_release={111: []},
        )
        assert stats["candidates"] == 2
        assert stats["matched"] == 1
        assert stats["rows_inserted"] == 0
        conn.executemany.assert_not_awaited()

    async def test_dry_run_never_writes(self, monkeypatch, comps):
        conn, stats = await self._run(
            monkeypatch,
            comps,
            full=False,
            processed_ids=set(),
            matches={2: 111},
            credits_by_release={
                111: [
                    {
                        "position": "A1",
                        "sequence": 1,
                        "artist_name": "Some Artist",
                        "track_title": "Some Title",
                        "extra": 0,
                        "role": None,
                    }
                ]
            },
            dry_run=True,
        )
        assert stats["rows_inserted"] == 1
        conn.executemany.assert_not_awaited()

    async def test_limit_caps_candidate_count(self, monkeypatch, comps):
        conn, stats = await self._run(
            monkeypatch,
            comps,
            full=True,
            processed_ids=set(),
            matches={},
            credits_by_release={},
            limit=1,
        )
        assert stats["candidates"] == 1

    async def test_no_candidates_short_circuits_without_matching(self, monkeypatch):
        conn = AsyncMock()

        async def fake_load(db_path):
            return []

        match_called = False

        async def fake_match(conn_arg, candidates):
            nonlocal match_called
            match_called = True
            return {}

        monkeypatch.setattr(
            "scripts.build_compilation_track_location.load_library_compilations", fake_load
        )
        monkeypatch.setattr(
            "scripts.build_compilation_track_location.match_comp_release", fake_match
        )

        stats = await build_compilation_track_location(
            library_db_path="unused.db",
            discogs_conn=conn,
            discogs_service=None,
            full=True,
        )
        assert stats == {"candidates": 0, "matched": 0, "rows_inserted": 0}
        assert match_called is False
