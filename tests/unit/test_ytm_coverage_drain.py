"""Unit tests for scripts/ytm_coverage_drain.py (LML#1056).

Covers the pure/testable seams of the dry-run harness: candidate loading,
concurrent resolution over an injected client, report summarization, and the
gated write path. No network, no ytmusicapi, no database.
"""

from __future__ import annotations

import csv

import pytest

from scripts.ytm_coverage_drain import (
    Candidate,
    DrainOutcome,
    WritePathNotResolvedError,
    execute_write,
    load_candidates_from_csv,
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


class TestWritePathGated:
    def test_execute_write_raises_until_fork_resolved(self):
        # Fill-only persistence is deliberately unimplemented until the
        # warmer-leg-vs-direct-write fork (#1056 / #1052) is settled.
        with pytest.raises(WritePathNotResolvedError):
            execute_write([DrainOutcome(Candidate("Stereolab", "Aluminum Tunes"), _match())])
