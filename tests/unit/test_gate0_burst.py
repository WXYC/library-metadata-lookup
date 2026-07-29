"""Tests for ``scripts/gate0_burst.py`` (LML#983 Gate 0 measurement harness).

Gate 0 asks whether ``UVICORN_WORKERS=3`` (LML#747) is still load-bearing now
that #949/PR#899 removed its main p50 justification. This harness fires a
concurrent ``/lookup`` burst and compares latency under 1 vs 3 workers. These
tests cover only the *pure* helpers — the ``Server-Timing`` header parser, the
percentile/aggregation math, the shed-response safety check, and the burst-size
safety rail. The network path (``httpx.AsyncClient`` against a real or mocked
LML) is deliberately untested here; see the harness docstring for how the real
burst is meant to be run, under human supervision, against staging.
"""

from __future__ import annotations

import pytest

from scripts.gate0_burst import (
    GATE0_QUERIES,
    check_burst_size_within_safe_bounds,
    classify_warm_cold,
    is_shed_response,
    parse_server_timing,
    percentile,
    summarize_durations,
)


class TestParseServerTiming:
    def test_extracts_named_legs_among_several(self):
        header = (
            "library_search;dur=12.34, discogs;dur=5, queue_wait;dur=0, "
            "event_loop_lag;dur=3.2, total;dur=20, lml_wall;dur=67.42"
        )
        legs = parse_server_timing(header)
        assert legs["lml_wall"] == pytest.approx(67.42)
        assert legs["event_loop_lag"] == pytest.approx(3.2)
        assert legs["total"] == pytest.approx(20.0)
        assert legs["library_search"] == pytest.approx(12.34)

    def test_missing_leg_is_absent_not_zero(self):
        header = "library_search;dur=12.34, total;dur=20, lml_wall;dur=67"
        legs = parse_server_timing(header)
        assert "event_loop_lag" not in legs
        assert legs["lml_wall"] == pytest.approx(67.0)

    def test_handles_desc_param_before_or_after_dur(self):
        header = 'cache;desc="Cache Read";dur=23.2, lml_wall;dur=67;desc="wall"'
        legs = parse_server_timing(header)
        assert legs["cache"] == pytest.approx(23.2)
        assert legs["lml_wall"] == pytest.approx(67.0)

    def test_malformed_entries_are_skipped_not_raised(self):
        header = "not-a-valid-entry;;;, lml_wall;dur=notanumber, total;dur=42"
        legs = parse_server_timing(header)
        assert "lml_wall" not in legs
        assert legs["total"] == pytest.approx(42.0)

    def test_empty_and_none_header_return_empty_dict(self):
        assert parse_server_timing(None) == {}
        assert parse_server_timing("") == {}
        assert parse_server_timing("   ") == {}

    def test_single_leg_no_trailing_comma(self):
        assert parse_server_timing("lml_wall;dur=100") == {"lml_wall": 100.0}


class TestPercentile:
    def test_known_values_linear_interpolation(self):
        values = [float(v) for v in range(1, 11)]  # 1..10
        assert percentile(values, 50) == pytest.approx(5.5)
        assert percentile(values, 95) == pytest.approx(9.55)
        assert percentile(values, 99) == pytest.approx(9.91)

    def test_single_value_returns_that_value_for_any_percentile(self):
        assert percentile([42.0], 50) == 42.0
        assert percentile([42.0], 99) == 42.0

    def test_unsorted_input_is_sorted_internally(self):
        values = [3.0, 1.0, 2.0]
        assert percentile(values, 50) == pytest.approx(2.0)

    def test_empty_raises_value_error(self):
        with pytest.raises(ValueError):
            percentile([], 50)


class TestSummarizeDurations:
    def test_computes_percentiles_and_bounds(self):
        values = [float(v) for v in range(1, 11)]
        summary = summarize_durations(values)
        assert summary.count == 10
        assert summary.min == 1.0
        assert summary.max == 10.0
        assert summary.mean == pytest.approx(5.5)
        assert summary.p50 == pytest.approx(5.5)
        assert summary.p95 == pytest.approx(9.55)
        assert summary.p99 == pytest.approx(9.91)

    def test_empty_list_yields_zero_count_and_none_stats(self):
        summary = summarize_durations([])
        assert summary.count == 0
        assert summary.p50 is None
        assert summary.p95 is None
        assert summary.p99 is None
        assert summary.min is None
        assert summary.max is None
        assert summary.mean is None

    def test_to_dict_is_json_serializable_shape(self):
        summary = summarize_durations([1.0, 2.0, 3.0])
        d = summary.to_dict()
        assert set(d.keys()) == {"count", "p50", "p95", "p99", "min", "max", "mean"}


class TestClassifyWarmCold:
    def test_splits_by_threshold(self):
        values = [50.0, 60.0, 2400.0, 70.0, 2300.0]
        warm, cold = classify_warm_cold(values, threshold_ms=500.0)
        assert (warm, cold) == (3, 2)

    def test_value_exactly_at_threshold_counts_as_cold(self):
        warm, cold = classify_warm_cold([500.0], threshold_ms=500.0)
        assert (warm, cold) == (0, 1)

    def test_empty_list(self):
        assert classify_warm_cold([], threshold_ms=500.0) == (0, 0)


class TestIsShedResponse:
    def test_true_on_429(self):
        assert is_shed_response(429, None) is True

    def test_true_on_5xx(self):
        assert is_shed_response(500, None) is True
        assert is_shed_response(503, None) is True

    def test_true_on_degraded_upstream_unavailable_body(self):
        body = {"degraded": True, "degraded_reason": "upstream_unavailable", "results": []}
        assert is_shed_response(200, body) is True

    def test_false_on_normal_200(self):
        body = {"degraded": False, "results": []}
        assert is_shed_response(200, body) is False

    def test_false_on_degraded_deadline_exceeded(self):
        """deadline_exceeded is a caller-budget shed, not Discogs saturation --
        must not trip the Gate 0 safety rail."""
        body = {"degraded": True, "degraded_reason": "deadline_exceeded", "results": []}
        assert is_shed_response(200, body) is False

    def test_false_on_200_with_no_body(self):
        assert is_shed_response(200, None) is False


class TestCheckBurstSizeWithinSafeBounds:
    def test_modest_defaults_pass(self):
        assert check_burst_size_within_safe_bounds(concurrency=3, total=12, smoke=False) is None

    def test_oversized_concurrency_rejected(self):
        msg = check_burst_size_within_safe_bounds(concurrency=50, total=12, smoke=False)
        assert msg is not None
        assert "concurrency" in msg.lower()

    def test_oversized_total_rejected(self):
        msg = check_burst_size_within_safe_bounds(concurrency=3, total=500, smoke=False)
        assert msg is not None
        assert "total" in msg.lower()

    def test_smoke_mode_bypasses_bounds(self):
        """--smoke targets /health, not /lookup -- no Discogs risk, so the
        burst-size rail (which exists to protect the shared Discogs budget)
        does not apply."""
        assert check_burst_size_within_safe_bounds(concurrency=50, total=500, smoke=True) is None


class TestGate0Queries:
    def test_includes_the_issue_trace_compilation_query(self):
        """The exact Wave B / compilation-track query from the LML#983 issue
        trace (C. Spencer Yeh / In The Blink Of An Eye) must be present --
        it is the highest cold-worker-cost path (no PG tier, 2 live Discogs
        calls) and the whole point of Gate 0 is observing whether it stays
        bimodal under 1 vs 3 workers."""
        labels = [q["label"] for q in GATE0_QUERIES]
        assert any("spencer yeh" in label.lower() for label in labels)

        compilation_query = next(q for q in GATE0_QUERIES if "spencer yeh" in q["label"].lower())
        assert compilation_query["artist"] == "c spencer yeh"
        assert compilation_query["song"] == "in the blink of an eye"

    def test_all_queries_use_lookup_request_field_names(self):
        allowed_keys = {"label", "artist", "song", "album", "raw_message"}
        for query in GATE0_QUERIES:
            assert set(query.keys()) <= allowed_keys
            assert query["label"]

    def test_at_least_one_ordinary_artist_album_query(self):
        assert any(q.get("album") for q in GATE0_QUERIES)
