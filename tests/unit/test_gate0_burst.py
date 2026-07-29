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

import asyncio

import pytest

from scripts import gate0_burst
from scripts.gate0_burst import (
    _MAX_SAFE_TOTAL,
    GATE0_QUERIES,
    RequestOutcome,
    build_report,
    check_burst_size_within_safe_bounds,
    classify_warm_cold,
    is_shed_response,
    parse_server_timing,
    percentile,
    render_human,
    run_burst,
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
        assert (
            check_burst_size_within_safe_bounds(concurrency=3, total=12, smoke=False, warm=True)
            is None
        )

    def test_oversized_concurrency_rejected(self):
        msg = check_burst_size_within_safe_bounds(concurrency=50, total=12, smoke=False, warm=True)
        assert msg is not None
        assert "concurrency" in msg.lower()

    def test_oversized_total_rejected(self):
        msg = check_burst_size_within_safe_bounds(concurrency=3, total=500, smoke=False, warm=False)
        assert msg is not None
        assert "total" in msg.lower()

    def test_smoke_mode_bypasses_bounds(self):
        """--smoke targets /health, not /lookup -- no Discogs risk, so the
        burst-size rail (which exists to protect the shared Discogs budget)
        does not apply."""
        assert (
            check_burst_size_within_safe_bounds(concurrency=50, total=500, smoke=True, warm=True)
            is None
        )

    def test_prewarm_counts_toward_total_ceiling(self):
        """Finding 4 (LML#983 review): with --warm on, the prewarm pass fires
        len(GATE0_QUERIES) extra live /lookup calls beyond --total. The
        shared-Discogs-budget ceiling must count them, or it silently
        understates the real live-request volume by that many."""
        n_prewarm = len(GATE0_QUERIES)
        # A --total that sits just under the raw ceiling but goes over once the
        # prewarm pass is counted.
        over_with_prewarm = _MAX_SAFE_TOTAL - n_prewarm + 1
        assert (
            check_burst_size_within_safe_bounds(
                concurrency=3, total=over_with_prewarm, smoke=False, warm=True
            )
            is not None
        )
        # The same --total without a prewarm pass stays within budget.
        assert (
            check_burst_size_within_safe_bounds(
                concurrency=3, total=over_with_prewarm, smoke=False, warm=False
            )
            is None
        )


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


class TestRunBurstAccounting:
    """The prewarm pass must be kept out of the aggregated measurement."""

    def test_prewarm_outcomes_excluded_from_burst_measurement(self, monkeypatch):
        """Finding 1 (LML#983 review): the --warm pass fires deliberately-cold
        requests (one per query) to reproduce the issue trace's warm-one/
        cold-others shape. Those prewarm outcomes must NOT land in the same
        series build_report aggregates, or they inflate p95/p99/max and the
        warm/cold split that the Gate 0 decision reads -- so an N=1 run that
        genuinely collapses to all-warm would still show a false residual cold
        signal from the guaranteed-cold prewarm hits."""

        async def _stub_lookup(client, host, api_key, query, timeout):
            return RequestOutcome(
                label=str(query.get("label", "lookup")),
                status_code=200,
                client_wall_ms=10.0,
                lml_wall_ms=10.0,
            )

        monkeypatch.setattr(gate0_burst, "_fire_lookup", _stub_lookup)

        result = asyncio.run(
            run_burst(
                host="http://x",
                api_key="k",
                concurrency=2,
                total=4,
                warm=True,
                smoke=False,
                timeout=1.0,
            )
        )

        assert len(result.prewarm_outcomes) == len(GATE0_QUERIES)
        assert len(result.outcomes) == 4
        # build_report over the burst series sees only the 4 burst requests.
        report = build_report(result.outcomes, 500.0)
        assert report["total_requests"] == 4


class TestRenderHuman:
    def test_smoke_mode_omits_misleading_server_timing_warning(self):
        """Finding 2 (LML#983 review): GET /health emits no Server-Timing
        header, so server_timing_present is ALWAYS false under --smoke.
        render_human must not tell the operator LML_EMIT_SERVER_TIMING is off
        in that case -- it's expected, not a config fault."""
        report = build_report([], 500.0, smoke=True)
        text = render_human(report)
        assert "LML_EMIT_SERVER_TIMING" not in text

    def test_non_smoke_missing_server_timing_still_warns(self):
        report = build_report([], 500.0, smoke=False)
        text = render_human(report)
        assert "LML_EMIT_SERVER_TIMING" in text
