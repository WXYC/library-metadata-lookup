"""Unit tests for scripts/ytm_coverage_drain.py (LML#1056).

Covers the pure/testable seams of the dry-run harness: candidate loading,
concurrent resolution over an injected client, report summarization, and the
gated write path. No network, no ytmusicapi, no database.
"""

from __future__ import annotations

import csv

import aiosqlite
import pytest

from clients.streaming.matching import normalize_album_title, normalize_artist_name
from scripts.streaming_availability.dedup import DeduplicatedAlbum
from scripts.streaming_availability.results_db import ResultsDB
from scripts.ytm_coverage_drain import (
    Candidate,
    DrainOutcome,
    execute_write,
    load_candidates_from_csv,
    load_candidates_from_db,
    load_candidates_from_rows,
    resolve_candidates,
    summarize,
)
from streaming.models import SourceMatch


def _match(
    url: str = "https://music.youtube.com/browse/MPREb_x", conf: float = 95.0
) -> SourceMatch:
    return SourceMatch(url=url, confidence=conf)


class _FakeClient:
    """Duck-typed YouTubeMusicClient: returns a preset match per (artist, title)."""

    def __init__(self, matches: dict[tuple[str, str], SourceMatch]):
        self._matches = matches
        self.calls: list[tuple[str, str]] = []

    async def find_album_match(self, artist: str, title: str) -> SourceMatch | None:
        self.calls.append((artist, title))
        return self._matches.get((artist, title))


class _RaisingClient:
    """Raises for one (artist, title), matches everything else.

    Mimics the shared matcher's contract of re-raising when every result row
    fails extraction (``clients/streaming/matching.py``, LML#640/#376) -- e.g. a
    YTM result set whose rows all lack an ``artists`` key.
    """

    def __init__(self, raise_on: tuple[str, str]):
        self._raise_on = raise_on
        self.calls: list[tuple[str, str]] = []

    async def find_album_match(self, artist: str, title: str) -> SourceMatch | None:
        self.calls.append((artist, title))
        if (artist, title) == self._raise_on:
            raise KeyError("artists")
        return _match()


class TestSummarize:
    def test_counts_and_hit_rate(self):
        outcomes = [
            DrainOutcome(Candidate("Stereolab", "Aluminum Tunes"), _match()),
            DrainOutcome(Candidate("Juana Molina", "DOGA"), _match(url=".../browse/MPREb_y")),
            DrainOutcome(Candidate("So Kalmery", "Obscure LP"), None),
        ]
        rep = summarize(outcomes)
        assert rep["candidates"] == 3
        assert rep["resolved"] == 2
        assert rep["misses"] == 1
        assert rep["hit_rate"] == pytest.approx(2 / 3)
        assert len(rep["sample_matches"]) == 2
        assert len(rep["sample_misses"]) == 1
        assert rep["sample_matches"][0]["url"].startswith("https://music.youtube.com/browse/")

    def test_sample_size_caps_lists(self):
        outcomes = [DrainOutcome(Candidate(f"A{i}", f"T{i}"), _match()) for i in range(20)]
        rep = summarize(outcomes, sample_size=5)
        assert rep["resolved"] == 20
        assert len(rep["sample_matches"]) == 5

    def test_empty_is_zero_rate_not_div_by_zero(self):
        rep = summarize([])
        assert rep["candidates"] == 0
        assert rep["hit_rate"] == 0.0


class TestResolveCandidates:
    @pytest.mark.asyncio
    async def test_maps_each_candidate_to_its_match(self):
        client = _FakeClient({("Stereolab", "Aluminum Tunes"): _match()})
        cands = [Candidate("Stereolab", "Aluminum Tunes"), Candidate("X", "Y")]
        outcomes = await resolve_candidates(client, cands, concurrency=2)
        by_artist = {o.candidate.artist: o for o in outcomes}
        assert by_artist["Stereolab"].match is not None
        assert by_artist["X"].match is None
        assert len(client.calls) == 2

    @pytest.mark.asyncio
    async def test_preserves_input_order(self):
        client = _FakeClient({})
        cands = [Candidate(f"A{i}", f"T{i}") for i in range(6)]
        outcomes = await resolve_candidates(client, cands, concurrency=3)
        assert [o.candidate.artist for o in outcomes] == [f"A{i}" for i in range(6)]

    @pytest.mark.asyncio
    async def test_isolates_per_candidate_exceptions(self):
        # A single malformed search (the matcher re-raises when every row fails
        # extraction) must become a miss, not abort the whole run and discard
        # every already-resolved match. Mirrors the /streaming-check orchestrator.
        client = _RaisingClient(raise_on=("Boom", "Bad"))
        cands = [Candidate("Boom", "Bad"), Candidate("Stereolab", "Aluminum Tunes")]
        outcomes = await resolve_candidates(client, cands, concurrency=2)
        by_artist = {o.candidate.artist: o for o in outcomes}
        assert by_artist["Boom"].match is None  # raise -> miss
        assert by_artist["Stereolab"].match is not None  # run continued
        assert len(client.calls) == 2


class TestLoaders:
    def test_load_from_csv_reads_artist_title_header(self, tmp_path):
        p = tmp_path / "sample.csv"
        with p.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["artist", "title"])
            w.writerow(["Stereolab", "Aluminum Tunes"])
            w.writerow(["Juana Molina", "DOGA"])
        cands = load_candidates_from_csv(str(p))
        assert [c.artist for c in cands] == ["Stereolab", "Juana Molina"]
        assert cands[0].title == "Aluminum Tunes"

    def test_load_from_rows_adapts_arbitrary_keys(self):
        rows = [{"canon_artist": "Sessa", "canon_title": "Grandeza", "discogs_release_id": 42}]
        cands = load_candidates_from_rows(
            rows, artist_key="canon_artist", title_key="canon_title", id_key="discogs_release_id"
        )
        assert cands[0].artist == "Sessa"
        assert cands[0].discogs_release_id == 42

    def test_load_from_rows_skips_blank_or_null_artist_title(self):
        rows = [
            {"a": "X", "t": None},
            {"a": "  ", "t": "Y"},
            {"a": "Sessa", "t": "Grandeza"},
        ]
        cands = load_candidates_from_rows(rows, artist_key="a", title_key="t")
        assert [c.artist for c in cands] == ["Sessa"]


class TestLoadCandidatesFromDb:
    """The #1070 from-db candidate source: a pending-scan over albums, resumable
    for free because get_pending("youtube_music") excludes found + not_found rows.
    """

    async def _seed(self, db_path: str) -> dict[str, int]:
        db = ResultsDB(db_path)
        await db.connect()
        try:
            await db.insert_albums(
                [
                    DeduplicatedAlbum(
                        normalized_artist=normalize_artist_name(artist),
                        normalized_title=normalize_album_title(title),
                        display_artist=artist,
                        display_title=title,
                        library_ids=[1],
                        formats=["cd"],
                    )
                    for artist, title in [
                        ("Stereolab", "Aluminum Tunes"),
                        ("Autechre", "Confield"),
                        ("Juana Molina", "DOGA"),
                    ]
                ]
            )
            rows = await db.get_pending("spotify", limit=10)
            by_artist = {r["display_artist"]: r["id"] for r in rows}
            await db.update_youtube_music_url(
                by_artist["Stereolab"], "https://music.youtube.com/browse/MPREb_stereolab"
            )
            await db.mark_youtube_music_not_found(by_artist["Autechre"])
            return by_artist
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_load_candidates_from_db_yields_only_pending_with_album_id(self, tmp_path):
        db_path = str(tmp_path / "sa.db")
        by_artist = await self._seed(db_path)

        db = ResultsDB(db_path)
        await db.connect()
        try:
            candidates = await load_candidates_from_db(db, limit=10)
        finally:
            await db.close()

        assert len(candidates) == 1
        assert candidates[0].artist == "Juana Molina"
        assert candidates[0].title == "DOGA"
        assert candidates[0].album_id == by_artist["Juana Molina"]

    @pytest.mark.asyncio
    async def test_blank_display_name_rows_are_marked_not_found_not_left_stalling(self, tmp_path):
        # get_pending has no ORDER BY/OFFSET, so an unsearchable blank-name row
        # left 'pending' would reappear in every future pending-scan page and
        # could stall pagination for real candidates behind it. It must be
        # marked not_found (like any other attempted-but-unusable candidate)
        # instead of silently dropped.
        db_path = str(tmp_path / "sa.db")
        db = ResultsDB(db_path)
        await db.connect()
        try:
            await db.insert_albums(
                [
                    DeduplicatedAlbum(
                        normalized_artist="blank artist",
                        normalized_title="blank title",
                        display_artist="",
                        display_title="Something",
                        library_ids=[1],
                        formats=["cd"],
                    ),
                    DeduplicatedAlbum(
                        normalized_artist=normalize_artist_name("Sessa"),
                        normalized_title=normalize_album_title("Grandeza"),
                        display_artist="Sessa",
                        display_title="Grandeza",
                        library_ids=[2],
                        formats=["cd"],
                    ),
                ]
            )
            candidates = await load_candidates_from_db(db, limit=10)
            assert [c.artist for c in candidates] == ["Sessa"]

            rows = await db.get_all_results()
            blank_row = next(r for r in rows if r["display_artist"] == "")
            assert blank_row["youtube_music_status"] == "not_found"
        finally:
            await db.close()


class TestExecuteWritePersists:
    """execute_write fill-only-persists verified YTM links into the
    streaming_availability results DB -- Option A of the #1056 write-path fork.
    Each match maps to its albums row by normalized (artist, title) and fills
    youtube_music_url only when NULL, so a resolved link is never clobbered.
    """

    async def _seed_album(self, db_path: str, artist: str, title: str) -> None:
        db = ResultsDB(db_path)
        await db.connect()
        try:
            await db.insert_albums(
                [
                    DeduplicatedAlbum(
                        normalized_artist=normalize_artist_name(artist),
                        normalized_title=normalize_album_title(title),
                        display_artist=artist,
                        display_title=title,
                        library_ids=[1],
                        formats=["cd"],
                    )
                ]
            )
        finally:
            await db.close()

    async def _read_url(self, db_path: str) -> str | None:
        db = ResultsDB(db_path)
        await db.connect()
        try:
            rows = await db.get_all_results()
            return rows[0]["youtube_music_url"]
        finally:
            await db.close()

    async def _read_row(self, db_path: str) -> aiosqlite.Row:
        db = ResultsDB(db_path)
        await db.connect()
        try:
            rows = await db.get_all_results()
            return rows[0]
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_fills_matched_url_into_albums_row(self, tmp_path):
        db_path = str(tmp_path / "sa.db")
        await self._seed_album(db_path, "Stereolab", "Aluminum Tunes")
        url = "https://music.youtube.com/browse/MPREb_stereolab"
        outcome = DrainOutcome(Candidate("Stereolab", "Aluminum Tunes"), _match(url=url))
        tally = await execute_write([outcome], db_path=db_path)
        assert tally == {"written": 1, "already_present": 0, "unmatched": 0, "not_found": 0}
        assert await self._read_url(db_path) == url

    @pytest.mark.asyncio
    async def test_is_fill_only_on_second_run(self, tmp_path):
        db_path = str(tmp_path / "sa.db")
        await self._seed_album(db_path, "Stereolab", "Aluminum Tunes")
        first = "https://music.youtube.com/browse/MPREb_first"
        await execute_write(
            [DrainOutcome(Candidate("Stereolab", "Aluminum Tunes"), _match(url=first))],
            db_path=db_path,
        )
        second = "https://music.youtube.com/browse/MPREb_second"
        tally = await execute_write(
            [DrainOutcome(Candidate("Stereolab", "Aluminum Tunes"), _match(url=second))],
            db_path=db_path,
        )
        assert tally == {"written": 0, "already_present": 1, "unmatched": 0, "not_found": 0}
        assert await self._read_url(db_path) == first  # original preserved

    @pytest.mark.asyncio
    async def test_unmatched_candidate_is_tallied_not_written(self, tmp_path):
        db_path = str(tmp_path / "sa.db")
        await self._seed_album(db_path, "Stereolab", "Aluminum Tunes")
        outcome = DrainOutcome(
            Candidate("No Such Artist", "No Such Album"),
            _match(url="https://music.youtube.com/browse/MPREb_ghost"),
        )
        tally = await execute_write([outcome], db_path=db_path)
        assert tally == {"written": 0, "already_present": 0, "unmatched": 1, "not_found": 0}
        assert await self._read_url(db_path) is None

    @pytest.mark.asyncio
    async def test_execute_write_marks_miss_as_not_found_when_album_id_present(self, tmp_path):
        db_path = str(tmp_path / "sa.db")
        await self._seed_album(db_path, "Stereolab", "Aluminum Tunes")
        row = await self._read_row(db_path)
        outcome = DrainOutcome(
            Candidate("Stereolab", "Aluminum Tunes", album_id=row["id"]), match=None
        )
        tally = await execute_write([outcome], db_path=db_path)
        assert tally == {"written": 0, "already_present": 0, "unmatched": 0, "not_found": 1}
        row = await self._read_row(db_path)
        assert row["youtube_music_status"] == "not_found"
        assert row["youtube_music_url"] is None

    @pytest.mark.asyncio
    async def test_execute_write_miss_when_already_resolved_counts_already_present(self, tmp_path):
        # The row got resolved out-of-band between the pending-scan read and
        # this write landing (e.g. a second run over an overlapping window).
        # mark_youtube_music_not_found's fill-only guard no-ops -- the tally
        # must reflect that as already_present, not double-count it as a
        # genuine not_found.
        db_path = str(tmp_path / "sa.db")
        await self._seed_album(db_path, "Stereolab", "Aluminum Tunes")
        row = await self._read_row(db_path)
        url = "https://music.youtube.com/browse/MPREb_x"
        seed_db = ResultsDB(db_path)
        await seed_db.connect()
        try:
            await seed_db.update_youtube_music_url(row["id"], url)
        finally:
            await seed_db.close()

        outcome = DrainOutcome(
            Candidate("Stereolab", "Aluminum Tunes", album_id=row["id"]), match=None
        )
        tally = await execute_write([outcome], db_path=db_path)
        assert tally == {"written": 0, "already_present": 1, "unmatched": 0, "not_found": 0}
        row = await self._read_row(db_path)
        assert row["youtube_music_status"] == "found"
        assert row["youtube_music_url"] == url

    @pytest.mark.asyncio
    async def test_execute_write_miss_without_album_id_is_skipped(self, tmp_path):
        db_path = str(tmp_path / "sa.db")
        await self._seed_album(db_path, "Stereolab", "Aluminum Tunes")
        outcome = DrainOutcome(Candidate("Stereolab", "Aluminum Tunes"), match=None)
        tally = await execute_write([outcome], db_path=db_path)
        assert tally == {"written": 0, "already_present": 0, "unmatched": 0, "not_found": 0}
        row = await self._read_row(db_path)
        assert row["youtube_music_status"] == "pending"

    @pytest.mark.asyncio
    async def test_execute_write_writes_found_by_album_id_without_names_join(self, tmp_path):
        db_path = str(tmp_path / "sa.db")
        await self._seed_album(db_path, "Stereolab", "Aluminum Tunes")
        row = await self._read_row(db_path)
        url = "https://music.youtube.com/browse/MPREb_stereolab"
        # Names deliberately wouldn't resolve via get_album_id_by_names -- the
        # album_id write path must not need the names-join at all.
        outcome = DrainOutcome(
            Candidate("Nonmatching Artist", "Nonmatching Title", album_id=row["id"]),
            _match(url=url),
        )
        tally = await execute_write([outcome], db_path=db_path)
        assert tally == {"written": 1, "already_present": 0, "unmatched": 0, "not_found": 0}
        assert await self._read_url(db_path) == url

    @pytest.mark.asyncio
    async def test_reuses_a_provided_connection_instead_of_opening_a_second_one(self, tmp_path):
        # --from-db --execute would otherwise open a second ResultsDB against
        # the same file just to write, paying a redundant connect/migrate
        # round-trip. Passing an already-connected db must be honored, and
        # execute_write must not close a connection it doesn't own.
        db_path = str(tmp_path / "sa.db")
        await self._seed_album(db_path, "Stereolab", "Aluminum Tunes")
        row = await self._read_row(db_path)
        db = ResultsDB(db_path)
        await db.connect()
        try:
            url = "https://music.youtube.com/browse/MPREb_reused"
            outcome = DrainOutcome(
                Candidate("Stereolab", "Aluminum Tunes", album_id=row["id"]), _match(url=url)
            )
            tally = await execute_write([outcome], db_path=db_path, db=db)
            assert tally == {"written": 1, "already_present": 0, "unmatched": 0, "not_found": 0}
            # Still open and usable -- execute_write did not close it.
            rows = await db.get_all_results()
            assert rows[0]["youtube_music_url"] == url
        finally:
            await db.close()


class TestRerunResumability:
    """AC #3: a re-run over an overlapping candidate set re-searches ~0 already-
    attempted albums. This is the entire point of the #1070 from-db drain --
    the rate-limited YTM endpoint must never be re-hit for a prior miss.
    """

    @pytest.mark.asyncio
    async def test_rerun_over_overlapping_set_researches_zero_attempted(self, tmp_path):
        db_path = str(tmp_path / "sa.db")
        seed_db = ResultsDB(db_path)
        await seed_db.connect()
        try:
            await seed_db.insert_albums(
                [
                    DeduplicatedAlbum(
                        normalized_artist=normalize_artist_name(artist),
                        normalized_title=normalize_album_title(title),
                        display_artist=artist,
                        display_title=title,
                        library_ids=[1],
                        formats=["cd"],
                    )
                    for artist, title in [
                        ("Stereolab", "Aluminum Tunes"),
                        ("Juana Molina", "DOGA"),
                        ("So Kalmery", "Obscure LP"),
                    ]
                ]
            )
        finally:
            await seed_db.close()

        client = _FakeClient({("Stereolab", "Aluminum Tunes"): _match()})

        # First run: --from-db, --execute. Marks one found, two not_found.
        run1_db = ResultsDB(db_path)
        await run1_db.connect()
        try:
            candidates = await load_candidates_from_db(run1_db, limit=10)
        finally:
            await run1_db.close()
        assert len(candidates) == 3
        outcomes = await resolve_candidates(client, candidates, concurrency=2)
        assert len(client.calls) == 3
        tally = await execute_write(outcomes, db_path=db_path)
        assert tally == {"written": 1, "already_present": 0, "unmatched": 0, "not_found": 2}

        # Re-run over the same (overlapping) set: 0 new candidates, 0 new searches.
        rerun_db = ResultsDB(db_path)
        await rerun_db.connect()
        try:
            rerun_candidates = await load_candidates_from_db(rerun_db, limit=10)
        finally:
            await rerun_db.close()
        assert rerun_candidates == []
