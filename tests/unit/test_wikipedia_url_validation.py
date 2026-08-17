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
from unittest.mock import MagicMock, PropertyMock, patch

from lookup.wikipedia_url import ExtractorComparison
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

    def test_clears_floor_reads_the_single_owner_property_not_a_hand_rolled_copy(self):
        # LML#1192 review round 6, pass 3, A4: ExtractorComparison.clears_floor
        # already declares itself "the single owner" (round 3, finding 9) --
        # this script must call it, not hand-rewrite the same predicate
        # locally. Proven by patching the PROPERTY and confirming the row
        # reflects the patched value: if the script still hand-derived
        # clears_floor from slug_pick/slug_score itself, patching the
        # property here would have no effect on the row at all.
        samples = [
            ArtistSample(
                artist_id=1,
                artist_name="Jessica Pratt",
                urls=["https://en.wikipedia.org/wiki/Jessica_Pratt"],
            )
        ]
        with patch.object(
            ExtractorComparison, "clears_floor", new_callable=PropertyMock
        ) as mock_clears_floor:
            mock_clears_floor.return_value = "PATCHED_SENTINEL"
            rows = build_comparison_rows(samples)
        assert rows[0]["clears_floor"] == "PATCHED_SENTINEL"

    def test_agreement_reads_the_single_owner_property_not_a_hand_rolled_copy(self):
        samples = [
            ArtistSample(
                artist_id=1,
                artist_name="Jessica Pratt",
                urls=["https://en.wikipedia.org/wiki/Jessica_Pratt"],
            )
        ]
        with patch.object(
            ExtractorComparison, "agreement", new_callable=PropertyMock
        ) as mock_agreement:
            mock_agreement.return_value = "PATCHED_SENTINEL"
            rows = build_comparison_rows(samples)
        assert rows[0]["agreement"] == "PATCHED_SENTINEL"


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
        # Row 1: heuristic correct, slug wrong, clears the floor -> regression.
        # Row 2: heuristic wrong, slug correct, clears the floor -> improvement.
        # Row 3: both correct, clears the floor -> neither.
        # Row 4: blank ground truth -> excluded from the denominator.
        rows = [
            {"heuristic_correct": "TRUE", "slug_correct": "FALSE", "clears_floor": "True"},
            {"heuristic_correct": "FALSE", "slug_correct": "TRUE", "clears_floor": "True"},
            {"heuristic_correct": "TRUE", "slug_correct": "TRUE", "clears_floor": "True"},
            {"heuristic_correct": "", "slug_correct": "", "clears_floor": "True"},
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

    def test_below_floor_row_is_excluded_even_when_fully_classified(self):
        # LML#1192 review (A6): a below-floor slug pick is NEVER actually
        # served once the flag flips -- the heuristic wins regardless -- so
        # marking it "wrong" is not a real regression. Ignoring
        # clears_floor here over-counts regressions and dilutes the
        # denominator with no-op rows the flip can't affect either way.
        rows = [
            {"heuristic_correct": "TRUE", "slug_correct": "FALSE", "clears_floor": "False"},
            {"heuristic_correct": "FALSE", "slug_correct": "TRUE", "clears_floor": "False"},
        ]
        summary = compute_ground_truth_summary(rows)
        assert summary.classified == 0
        assert summary.regressions == 0
        assert summary.improvements == 0

    def test_mixed_floor_status_counts_only_the_above_floor_rows(self):
        rows = [
            # Above floor, genuine regression.
            {"heuristic_correct": "TRUE", "slug_correct": "FALSE", "clears_floor": "True"},
            # Below floor: heuristic wins regardless -- must not count.
            {"heuristic_correct": "TRUE", "slug_correct": "FALSE", "clears_floor": "False"},
        ]
        summary = compute_ground_truth_summary(rows)
        assert summary.classified == 1
        assert summary.regressions == 1
        assert summary.regression_rate == 1.0

    def test_missing_clears_floor_column_defaults_to_excluded(self):
        # A row lacking the column at all (e.g. a hand-edited CSV) must
        # fail safe -- excluded, not counted as a regression.
        rows = [{"heuristic_correct": "TRUE", "slug_correct": "FALSE"}]
        summary = compute_ground_truth_summary(rows)
        assert summary.classified == 0

    def test_a_short_csv_row_fails_safe_to_excluded_not_a_crash(self):
        # LML#1192 review round 4, P1-11: csv.DictReader's restval for a
        # column a SHORT row doesn't reach is None, not "" -- unlike a
        # column simply absent from the dict (test_missing_clears_floor_
        # column_defaults_to_excluded above), the key IS present here, so
        # row.get(key, "")'s default never kicks in and .strip() is called
        # on None. This is the realistic case for a hand-edited CSV: an
        # operator deletes a trailing column's value (or a stray newline
        # truncates a row) rather than removing the column entirely.
        import csv
        import io

        csv_text = "heuristic_correct,slug_correct,clears_floor\nTRUE,FALSE\n"
        rows = list(csv.DictReader(io.StringIO(csv_text)))
        assert rows[0]["clears_floor"] is None  # confirms the DictReader restval premise

        summary = compute_ground_truth_summary(rows)  # must not raise

        assert summary.classified == 0
