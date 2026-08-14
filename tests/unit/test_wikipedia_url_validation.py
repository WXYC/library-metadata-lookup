"""Unit tests for scripts/wikipedia_url_validation.py — LML#513's empirical
gate: sample 300 discogs-cache artists carrying a wikipedia.org ``artist_url``
row, compare the legacy heuristic pick against the slug-scored pick, and
report agreement / (post-hand-classification) regression rates.

Mirrors ``tests/unit/test_cache_miss_provenance.py``'s fake-cursor pattern for
the PG-facing functions; the scoring/summary functions are pure and tested
directly. No live PG connection anywhere in this file — see
``tests/integration/`` for the ``pg``-marked layer this repo would use if this
script grew a persistent state store (it doesn't; it's read-only and
CSV-in/CSV-out).
"""

from __future__ import annotations

import io
from unittest.mock import MagicMock

from scripts.wikipedia_url_validation import (
    ArtistSample,
    build_comparison_rows,
    compute_ground_truth_summary,
    emit_csv,
    fetch_artist_samples,
    fetch_candidate_artist_ids,
    sample_artist_ids,
)


def _fake_conn(rows: list[tuple]):
    cur = MagicMock()
    cur.fetchall.return_value = rows
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=None)
    conn = MagicMock()
    conn.cursor.return_value = cur
    return conn


class TestFetchCandidateArtistIds:
    def test_returns_ids_from_the_query(self):
        conn = _fake_conn([(1,), (2,), (3,)])
        assert fetch_candidate_artist_ids(conn) == [1, 2, 3]

    def test_empty_result_returns_empty_list(self):
        conn = _fake_conn([])
        assert fetch_candidate_artist_ids(conn) == []


class TestFetchArtistSamples:
    def test_groups_urls_by_artist(self):
        conn = _fake_conn(
            [
                (1, "Stereolab", "https://en.wikipedia.org/wiki/Stereolab"),
                (1, "Stereolab", "https://stereolab.example"),
                (2, "Jessica Pratt", "https://en.wikipedia.org/wiki/Jessica_Pratt"),
            ]
        )
        samples = fetch_artist_samples(conn, [1, 2])
        assert samples == [
            ArtistSample(
                artist_id=1,
                artist_name="Stereolab",
                urls=["https://en.wikipedia.org/wiki/Stereolab", "https://stereolab.example"],
            ),
            ArtistSample(
                artist_id=2,
                artist_name="Jessica Pratt",
                urls=["https://en.wikipedia.org/wiki/Jessica_Pratt"],
            ),
        ]

    def test_empty_artist_ids_does_not_query(self):
        conn = _fake_conn([])
        assert fetch_artist_samples(conn, []) == []
        conn.cursor.assert_not_called()


class TestSampleArtistIds:
    def test_returns_all_ids_when_population_is_at_or_under_sample_size(self):
        assert sample_artist_ids([1, 2, 3], sample_size=5, seed=1) == [1, 2, 3]

    def test_samples_down_to_the_requested_size(self):
        population = list(range(1000))
        sampled = sample_artist_ids(population, sample_size=300, seed=42)
        assert len(sampled) == 300
        assert len(set(sampled)) == 300  # no duplicates
        assert set(sampled) <= set(population)

    def test_seed_is_reproducible(self):
        population = list(range(1000))
        first = sample_artist_ids(population, sample_size=300, seed=42)
        second = sample_artist_ids(population, sample_size=300, seed=42)
        assert first == second

    def test_different_seeds_can_diverge(self):
        population = list(range(1000))
        first = sample_artist_ids(population, sample_size=300, seed=1)
        second = sample_artist_ids(population, sample_size=300, seed=2)
        assert first != second


class TestBuildComparisonRows:
    def test_agreeing_pick_row_shape(self):
        samples = [
            ArtistSample(
                artist_id=1,
                artist_name="Jessica Pratt",
                urls=["https://en.wikipedia.org/wiki/Jessica_Pratt"],
            )
        ]
        rows = build_comparison_rows(samples)
        assert len(rows) == 1
        row = rows[0]
        assert row["artist_id"] == 1
        assert row["artist_name"] == "Jessica Pratt"
        assert row["heuristic_pick"] == "https://en.wikipedia.org/wiki/Jessica_Pratt"
        assert row["slug_pick"] == "https://en.wikipedia.org/wiki/Jessica_Pratt"
        assert row["agreement"] is True
        assert row["clears_floor"] is True
        assert row["heuristic_correct"] == ""
        assert row["slug_correct"] == ""

    def test_diverging_pick_row_shape(self):
        samples = [
            ArtistSample(
                artist_id=2,
                artist_name="Stereolab",
                urls=[
                    "https://en.wikipedia.org/wiki/Tim_Gane",
                    "https://en.wikipedia.org/wiki/Stereolab",
                ],
            )
        ]
        rows = build_comparison_rows(samples)
        row = rows[0]
        assert row["heuristic_pick"] == "https://en.wikipedia.org/wiki/Tim_Gane"
        assert row["slug_pick"] == "https://en.wikipedia.org/wiki/Stereolab"
        assert row["agreement"] is False
        assert row["clears_floor"] is True


class TestEmitCsvRoundTrips:
    def test_round_trip(self):
        samples = [
            ArtistSample(
                artist_id=1,
                artist_name="Jessica Pratt",
                urls=["https://en.wikipedia.org/wiki/Jessica_Pratt"],
            )
        ]
        rows = build_comparison_rows(samples)
        buf = io.StringIO()
        emit_csv(rows, buf)
        buf.seek(0)
        import csv

        read_back = list(csv.DictReader(buf))
        assert read_back[0]["artist_name"] == "Jessica Pratt"
        assert read_back[0]["heuristic_pick"] == "https://en.wikipedia.org/wiki/Jessica_Pratt"


class TestComputeGroundTruthSummary:
    def test_regression_and_improvement_rates(self):
        # Row 1: heuristic correct, slug wrong -> regression.
        # Row 2: heuristic wrong, slug correct -> improvement.
        # Row 3: both correct -> neither.
        # Row 4: blank ground truth -> excluded from the denominator.
        rows = [
            {"heuristic_correct": "TRUE", "slug_correct": "FALSE"},
            {"heuristic_correct": "FALSE", "slug_correct": "TRUE"},
            {"heuristic_correct": "TRUE", "slug_correct": "TRUE"},
            {"heuristic_correct": "", "slug_correct": ""},
        ]
        summary = compute_ground_truth_summary(rows)
        assert summary.classified == 3
        assert summary.regressions == 1
        assert summary.improvements == 1
        assert summary.regression_rate == 1 / 3

    def test_no_classified_rows_yields_zero_rates(self):
        summary = compute_ground_truth_summary([{"heuristic_correct": "", "slug_correct": ""}])
        assert summary.classified == 0
        assert summary.regression_rate == 0.0
