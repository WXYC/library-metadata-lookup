"""Unit tests for ``scripts/build_compilation_track_location.py`` (LML#1019).

Pure-logic + mocked-DB coverage. The real matching/credit-fetch/insert path
against a live discogs-cache is exercised separately in
``tests/integration/test_build_compilation_track_location_pg.py``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import aiosqlite
import pytest

from scripts._lib.release_matching import DiscogsMatch
from scripts.build_compilation_track_location import (
    CompCandidate,
    build_compilation_track_location,
    build_rows,
    get_processed_library_ids,
    load_library_compilations,
    match_comp_release,
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
class TestMatchCompRelease:
    """``match_comp_release`` seeds from ``library_release_override`` before the title cascade (LML#1082).

    ``conn`` is a full mock here, so the guard predicate itself (release must
    be in ``va_release`` AND carry a ``release_track`` row) is not re-derived
    in Python -- it's delegated to the SQL in ``_OVERRIDE_SQL`` and proved
    against real tables in ``tests/integration/test_build_compilation_track_location_pg.py``.
    What's covered here is the seeding/precedence/fallback *decision*: a
    library_id the guarded query returns is trusted outright; one it omits
    (whether because it carries no override, or because the guard filtered
    it -- both look identical from this function's perspective) falls
    through to the title cascade.
    """

    LOST_IN_TRANSLATION = CompCandidate(
        library_id=60654,
        title="Lost In Translation (Music From The Motion Picture)",
        artist="Soundtracks - L",
    )
    SOUND_OF_DUB = CompCandidate(
        library_id=28, title="The Sound of Dub", artist="Various Artists - Reggae"
    )

    @staticmethod
    def _conn_with_override_rows(rows):
        conn = AsyncMock()
        conn.fetch = AsyncMock(return_value=rows)
        return conn

    @staticmethod
    def _fail_if_called(name):
        async def _fail(*args, **kwargs):
            raise AssertionError(f"{name} must not run when every comp is override-matched")

        return _fail

    async def test_override_only_comp_is_indexed_without_running_the_cascade(self, monkeypatch):
        conn = self._conn_with_override_rows(
            [{"library_id": 60654, "discogs_release_id": 13468045}]
        )
        monkeypatch.setattr(
            "scripts.build_compilation_track_location.exact_match",
            self._fail_if_called("exact_match"),
        )
        monkeypatch.setattr(
            "scripts.build_compilation_track_location.prefix_strip_match",
            self._fail_if_called("prefix_strip_match"),
        )
        monkeypatch.setattr(
            "scripts.build_compilation_track_location.trigram_match",
            self._fail_if_called("trigram_match"),
        )

        result = await match_comp_release(conn, [self.LOST_IN_TRANSLATION])

        assert result == {60654: 13468045}
        conn.fetch.assert_awaited_once()
        sql, library_ids = conn.fetch.await_args.args
        assert "library_release_override" in sql
        assert "va_release" in sql
        assert "release_track" in sql
        assert library_ids == [60654]

    async def test_override_wins_over_a_conflicting_title_cascade_match(self, monkeypatch):
        """The override-claimed comp never reaches the cascade, so it can't be reassigned."""
        conn = self._conn_with_override_rows(
            [{"library_id": 60654, "discogs_release_id": 13468045}]
        )
        seen_album_ids: list[int] = []

        async def fake_exact_match(conn_arg, albums):
            seen_album_ids.extend(a.id for a in albums)
            # If the override-claimed comp leaked through here, it would
            # resolve to this deliberately-wrong release id.
            matched = [
                DiscogsMatch(
                    comp_id=a.id,
                    comp_title=a.title,
                    discogs_release_id=999999,
                    discogs_title="wrong release",
                    confidence=100.0,
                    track_count=0,
                )
                for a in albums
                if a.id == 60654
            ]
            title_map: dict[str, list[tuple[int, str]]] = {}
            remaining = [a for a in albums if a.id != 60654]
            return matched, remaining, title_map

        async def fake_prefix_strip_match(title_map, albums):
            return [], albums

        async def fake_trigram_match(conn_arg, albums):
            return [], albums

        monkeypatch.setattr(
            "scripts.build_compilation_track_location.exact_match", fake_exact_match
        )
        monkeypatch.setattr(
            "scripts.build_compilation_track_location.prefix_strip_match",
            fake_prefix_strip_match,
        )
        monkeypatch.setattr(
            "scripts.build_compilation_track_location.trigram_match", fake_trigram_match
        )

        result = await match_comp_release(conn, [self.LOST_IN_TRANSLATION, self.SOUND_OF_DUB])

        assert result[60654] == 13468045  # override wins, not the cascade's 999999
        assert 60654 not in seen_album_ids  # override-claimed comp never entered the cascade

    async def test_guard_filtered_comp_falls_back_to_title_cascade(self, monkeypatch):
        """A comp whose override the SQL guard excluded (bad va_release/no tracklist)

        looks, from this function's perspective, identical to "no override at
        all" -- the guarded query simply omits its row -- so it must still get
        a chance at the title cascade.
        """
        conn = self._conn_with_override_rows([])  # guard filtered it out (or no override exists)

        async def fake_exact_match(conn_arg, albums):
            matched = [
                DiscogsMatch(
                    comp_id=a.id,
                    comp_title=a.title,
                    discogs_release_id=555,
                    discogs_title=a.title,
                    confidence=100.0,
                    track_count=0,
                )
                for a in albums
            ]
            return matched, [], {}

        async def fake_prefix_strip_match(title_map, albums):
            return [], albums

        async def fake_trigram_match(conn_arg, albums):
            return [], albums

        monkeypatch.setattr(
            "scripts.build_compilation_track_location.exact_match", fake_exact_match
        )
        monkeypatch.setattr(
            "scripts.build_compilation_track_location.prefix_strip_match",
            fake_prefix_strip_match,
        )
        monkeypatch.setattr(
            "scripts.build_compilation_track_location.trigram_match", fake_trigram_match
        )

        result = await match_comp_release(conn, [self.SOUND_OF_DUB])

        assert result == {28: 555}

    async def test_no_override_at_all_preserves_the_existing_cascade_composition(self, monkeypatch):
        """Regression: zero overrides in play must reproduce the pre-#1082 cascade merge exactly."""
        conn = self._conn_with_override_rows([])

        async def fake_exact_match(conn_arg, albums):
            matched = [
                DiscogsMatch(
                    comp_id=1,
                    comp_title="Exact Hit",
                    discogs_release_id=100,
                    discogs_title="Exact Hit",
                    confidence=100.0,
                    track_count=0,
                )
            ]
            remaining = [a for a in albums if a.id != 1]
            return matched, remaining, {"exact hit": [(100, "Exact Hit")]}

        async def fake_prefix_strip_match(title_map, albums):
            matched = [
                DiscogsMatch(
                    comp_id=2,
                    comp_title="Prefix Hit",
                    discogs_release_id=200,
                    discogs_title="Prefix Hit",
                    confidence=95.0,
                    track_count=0,
                )
            ]
            remaining = [a for a in albums if a.id != 2]
            return matched, remaining

        async def fake_trigram_match(conn_arg, albums):
            matched = [
                DiscogsMatch(
                    comp_id=3,
                    comp_title="Fuzzy Hit",
                    discogs_release_id=300,
                    discogs_title="Fuzzy-ish Hit",
                    confidence=85.0,
                    track_count=0,
                )
            ]
            remaining = [a for a in albums if a.id != 3]
            return matched, remaining

        monkeypatch.setattr(
            "scripts.build_compilation_track_location.exact_match", fake_exact_match
        )
        monkeypatch.setattr(
            "scripts.build_compilation_track_location.prefix_strip_match",
            fake_prefix_strip_match,
        )
        monkeypatch.setattr(
            "scripts.build_compilation_track_location.trigram_match", fake_trigram_match
        )

        comps = [
            CompCandidate(library_id=1, title="Exact Hit", artist="Various Artists"),
            CompCandidate(library_id=2, title="Label - Prefix Hit", artist="Various Artists"),
            CompCandidate(library_id=3, title="Fuzzy Hitt", artist="Various Artists"),
            CompCandidate(library_id=4, title="No Match", artist="Various Artists"),
        ]

        result = await match_comp_release(conn, comps)

        assert result == {1: 100, 2: 200, 3: 300}

    async def test_no_comps_short_circuits_without_querying_the_override_table(self):
        conn = AsyncMock()
        result = await match_comp_release(conn, [])
        assert result == {}
        conn.fetch.assert_not_called()


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
