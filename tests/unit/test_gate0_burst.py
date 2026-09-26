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
import json
from pathlib import Path

import pytest

from scripts import gate0_burst
from scripts.gate0_burst import (
    _MAX_SAFE_TOTAL,
    _PACED_RUN_MIN_SECONDS,
    GATE0_QUERIES,
    RequestOutcome,
    build_report,
    check_burst_size_within_safe_bounds,
    classify_warm_cold,
    is_shed_response,
    load_queries_file,
    parse_args,
    parse_server_timing,
    percentile,
    render_human,
    resolve_run_shape,
    run_burst,
    summarize_durations,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
GOLDEN_CORPUS = REPO_ROOT / "tests" / "e2e" / "golden" / "cases.json"


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


def _write_cases(tmp_path: Path, payload: object) -> str:
    path = tmp_path / "cases.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


class TestLoadQueriesFile:
    """``--queries-file`` reads the LML#1233 golden corpus (C1b).

    The corpus is a top-level JSON *list* of case objects; the driver wants
    each case's ``query`` sub-object and its ``id`` as the report label.
    """

    def test_reads_the_real_golden_corpus(self):
        queries = load_queries_file(str(GOLDEN_CORPUS))
        assert len(queries) == 143
        assert all(q["label"] for q in queries)
        allowed = {"label", "artist", "song", "album", "raw_message"}
        for query in queries:
            assert set(query) <= allowed
            # Every case carries at least one lookup field, never a bare label.
            assert set(query) - {"label"}

    def test_uses_the_case_id_as_the_label(self, tmp_path):
        path = _write_cases(
            tmp_path,
            [{"id": "case-juana", "query": {"artist": "Juana Molina", "album": "DOGA"}}],
        )
        assert load_queries_file(path) == [
            {"label": "case-juana", "artist": "Juana Molina", "album": "DOGA"}
        ]

    def test_drops_corpus_only_keys_that_are_not_lookup_fields(self, tmp_path):
        """A case carries ``shape``/``expect``/``requires_rows`` siblings and the
        ``query`` itself is already clean -- but nothing outside the
        ``LookupRequest`` field names may reach the POST body."""
        path = _write_cases(
            tmp_path,
            [
                {
                    "id": "c1",
                    "shape": "artist_song",
                    "expect": {"miss_kind": "hit"},
                    "query": {"artist": "Jessica Pratt", "song": "Back, Baby"},
                }
            ],
        )
        assert load_queries_file(path) == [
            {"label": "c1", "artist": "Jessica Pratt", "song": "Back, Baby"}
        ]

    def test_missing_file_raises_value_error_naming_the_path(self, tmp_path):
        missing = str(tmp_path / "nope.json")
        with pytest.raises(ValueError) as excinfo:
            load_queries_file(missing)
        assert missing in str(excinfo.value)

    def test_malformed_json_raises_value_error(self, tmp_path):
        path = tmp_path / "cases.json"
        path.write_text("[{'not': 'json'},", encoding="utf-8")
        with pytest.raises(ValueError):
            load_queries_file(str(path))

    def test_non_list_top_level_raises(self, tmp_path):
        with pytest.raises(ValueError):
            load_queries_file(_write_cases(tmp_path, {"cases": []}))

    def test_case_without_a_query_object_raises(self, tmp_path):
        with pytest.raises(ValueError):
            load_queries_file(_write_cases(tmp_path, [{"id": "c1", "shape": "artist_only"}]))

    def test_case_whose_query_has_no_lookup_fields_raises(self, tmp_path):
        with pytest.raises(ValueError):
            load_queries_file(_write_cases(tmp_path, [{"id": "c1", "query": {"genre": "Rock"}}]))

    def test_empty_corpus_raises(self, tmp_path):
        with pytest.raises(ValueError):
            load_queries_file(_write_cases(tmp_path, []))


class TestPacedRunLiftsTheTotalCap:
    """``--pace-seconds`` >= 10 lifts ``_MAX_SAFE_TOTAL``, and nothing else.

    The cap exists to protect the shared Discogs rate budget from a *burst*.
    A run paced at 10 s or more is not a burst -- it is at most 6 requests a
    minute per worker, well inside staging's limiter -- so a full 143-case
    corpus cycle is allowed. Below 10 s the cap still applies.
    """

    @pytest.mark.parametrize("pace_seconds", [0.0, 1.0, 9.0, 9.999])
    def test_cap_still_enforced_below_the_threshold(self, pace_seconds):
        msg = check_burst_size_within_safe_bounds(
            concurrency=1,
            total=_MAX_SAFE_TOTAL + 1,
            smoke=False,
            warm=False,
            pace_seconds=pace_seconds,
        )
        assert msg is not None
        assert "total" in msg.lower()

    @pytest.mark.parametrize("pace_seconds", [10.0, 30.0])
    def test_cap_lifted_at_and_above_the_threshold(self, pace_seconds):
        assert (
            check_burst_size_within_safe_bounds(
                concurrency=1, total=143, smoke=False, warm=False, pace_seconds=pace_seconds
            )
            is None
        )

    def test_threshold_constant_is_ten_seconds(self):
        assert _PACED_RUN_MIN_SECONDS == 10.0

    def test_concurrency_cap_is_not_lifted_by_pacing(self):
        """Pacing bounds the per-worker rate, not the fan-out. A paced run at
        high concurrency is still a burst in aggregate, so that rail stays."""
        msg = check_burst_size_within_safe_bounds(
            concurrency=50, total=143, smoke=False, warm=False, pace_seconds=30.0
        )
        assert msg is not None
        assert "concurrency" in msg.lower()

    def test_prewarm_count_follows_the_supplied_query_set(self):
        """The prewarm pass fires one live /lookup per *distinct query in use*.
        With a 143-case corpus that is 143 calls, not len(GATE0_QUERIES) -- so
        the ceiling must be told the real count or it understates the load."""
        msg = check_burst_size_within_safe_bounds(
            concurrency=1, total=1, smoke=False, warm=True, prewarm_count=143
        )
        assert msg is not None
        assert "prewarm" in msg.lower()

    def test_default_prewarm_count_is_the_builtin_query_set(self):
        assert (
            check_burst_size_within_safe_bounds(concurrency=3, total=12, smoke=False, warm=True)
            is None
        )

    def test_pacing_does_not_exempt_the_unpaced_prewarm_pass(self):
        """``--pace-seconds`` vouches for the burst leg only. The prewarm pass
        is unpaced by construction -- one sequential live /lookup per distinct
        query, back to back -- so 143 of them is an oversized burst however
        slowly the requests after it are spaced."""
        msg = check_burst_size_within_safe_bounds(
            concurrency=1, total=143, smoke=False, warm=True, pace_seconds=30.0, prewarm_count=143
        )
        assert msg is not None
        assert "prewarm" in msg.lower()

    def test_paced_run_with_a_small_prewarm_pass_is_still_allowed(self):
        assert (
            check_burst_size_within_safe_bounds(
                concurrency=1, total=143, smoke=False, warm=True, pace_seconds=30.0
            )
            is None
        )


class TestResolveRunShape:
    """``--total`` and ``--warm`` change default with ``--queries-file``."""

    def test_without_a_queries_file_the_builtin_defaults_hold(self):
        queries, total, warm = resolve_run_shape(queries_file=None, total=12, warm=None)
        assert queries is GATE0_QUERIES
        assert total == 12
        assert warm is True

    def test_builtin_total_still_defaults_to_twelve(self):
        """Only ``--queries-file`` moves the ``--total`` default.

        Resolving the built-in path to ``len(GATE0_QUERIES)`` would silently
        drop the documented Gate 0 burst from 12 requests to 3 -- a p95 over
        three samples instead of twelve -- while ``--help`` and
        ``docs/scripts.md`` both still promise 12.
        """
        queries, total, warm = resolve_run_shape(queries_file=None, total=None, warm=None)
        assert queries is GATE0_QUERIES
        assert total == 12
        assert warm is True

    def test_total_defaults_to_the_corpus_length(self):
        _, total, _ = resolve_run_shape(queries_file=str(GOLDEN_CORPUS), total=None, warm=None)
        assert total == 143

    def test_prewarm_defaults_off_for_a_corpus_replay(self):
        """The prewarm pass is *unpaced*. Three hardcoded probes is the LML#983
        warm-one/cold-others setup; 143 of them is an unpaced burst straight
        through the budget the pacing exists to respect."""
        _, _, warm = resolve_run_shape(queries_file=str(GOLDEN_CORPUS), total=None, warm=None)
        assert warm is False

    def test_explicit_flags_win_over_both_defaults(self):
        _, total, warm = resolve_run_shape(queries_file=str(GOLDEN_CORPUS), total=5, warm=True)
        assert (total, warm) == (5, True)

    def test_builtin_total_default_is_unset_on_the_parser(self):
        """``--total`` must reach resolve_run_shape as None when the operator
        did not type it, or the corpus-length default can never fire."""
        args = parse_args(["--host", "http://x"])
        assert args.total is None
        assert args.warm is None
        assert args.queries_file is None
        assert args.pace_seconds == 0.0


class TestPacing:
    """``--pace-seconds`` sleeps *between* requests, via an injected sleep.

    No test here may sleep for real -- the injected callable records the
    requested delay and returns immediately.
    """

    @staticmethod
    def _stub_lookup_recording(seen: list[str]):
        async def _stub(client, host, api_key, query, timeout):
            seen.append(str(query.get("label")))
            return RequestOutcome(
                label=str(query.get("label")),
                status_code=200,
                client_wall_ms=1.0,
                lml_wall_ms=1.0,
            )

        return _stub

    def test_sleeps_between_requests_but_not_before_the_first_or_after_the_last(self, monkeypatch):
        slept: list[float] = []

        async def _fake_sleep(seconds: float) -> None:
            slept.append(seconds)

        monkeypatch.setattr(gate0_burst, "_fire_lookup", self._stub_lookup_recording([]))

        result = asyncio.run(
            run_burst(
                host="http://x",
                api_key="k",
                concurrency=1,
                total=3,
                warm=False,
                smoke=False,
                timeout=1.0,
                pace_seconds=30.0,
                sleep=_fake_sleep,
            )
        )

        assert len(result.outcomes) == 3
        assert slept == [30.0, 30.0]

    def test_no_pacing_by_default(self, monkeypatch):
        slept: list[float] = []

        async def _fake_sleep(seconds: float) -> None:
            slept.append(seconds)

        monkeypatch.setattr(gate0_burst, "_fire_lookup", self._stub_lookup_recording([]))

        asyncio.run(
            run_burst(
                host="http://x",
                api_key="k",
                concurrency=1,
                total=3,
                warm=False,
                smoke=False,
                timeout=1.0,
                sleep=_fake_sleep,
            )
        )

        assert slept == []

    def test_pacing_stops_at_the_shed_abort_rail(self, monkeypatch):
        """The abort-on-shed rail is unchanged by pacing: a shed stops the run
        rather than sleeping on into the next request."""

        slept: list[float] = []

        async def _fake_sleep(seconds: float) -> None:
            slept.append(seconds)

        async def _stub_shedding(client, host, api_key, query, timeout):
            return RequestOutcome(
                label=str(query.get("label")), status_code=429, client_wall_ms=1.0, shed=True
            )

        monkeypatch.setattr(gate0_burst, "_fire_lookup", _stub_shedding)

        result = asyncio.run(
            run_burst(
                host="http://x",
                api_key="k",
                concurrency=1,
                total=5,
                warm=False,
                smoke=False,
                timeout=1.0,
                pace_seconds=30.0,
                sleep=_fake_sleep,
            )
        )

        assert result.aborted_on_shed is True
        assert len(result.outcomes) == 1
        assert slept == []

    def test_supplied_queries_are_fired_round_robin(self, monkeypatch):
        seen: list[str] = []
        monkeypatch.setattr(gate0_burst, "_fire_lookup", self._stub_lookup_recording(seen))

        asyncio.run(
            run_burst(
                host="http://x",
                api_key="k",
                concurrency=1,
                total=4,
                warm=False,
                smoke=False,
                timeout=1.0,
                queries=[
                    {"label": "a", "artist": "Stereolab"},
                    {"label": "b", "artist": "Cat Power"},
                ],
            )
        )

        assert seen == ["a", "b", "a", "b"]

    def test_a_shed_during_another_workers_wait_stops_it_before_it_fires_again(self, monkeypatch):
        """A worker asleep in its pacing wait must not fire when it wakes into
        an already-aborted run.

        This needs ``concurrency > 1`` to reach at all: with a single worker the
        ``while`` condition catches a self-shed before the wait is entered, so
        the abort check *after* the wait is dead code from one worker's point of
        view. It is the check that matters most in a paced run, because the
        window it closes is a whole ``--pace-seconds`` wide -- 30 seconds during
        the LML#1354 soak.
        """
        fired: list[str] = []
        shed_seen = asyncio.Event()

        async def _stub(client, host, api_key, query, timeout):
            label = str(query.get("label"))
            fired.append(label)
            if len(fired) == 2:
                # The second worker's opening request sheds, while the first
                # worker is parked in its inter-request wait.
                shed_seen.set()
                return RequestOutcome(label=label, status_code=429, client_wall_ms=1.0, shed=True)
            return RequestOutcome(label=label, status_code=200, client_wall_ms=1.0, lml_wall_ms=1.0)

        async def _fake_sleep(seconds: float) -> None:
            # Stands in for a 30s wait that the shed lands in the middle of,
            # without spending it.
            await shed_seen.wait()

        monkeypatch.setattr(gate0_burst, "_fire_lookup", _stub)

        result = asyncio.run(
            run_burst(
                host="http://x",
                api_key="k",
                concurrency=2,
                total=6,
                warm=False,
                smoke=False,
                timeout=1.0,
                pace_seconds=30.0,
                queries=[{"label": "a", "artist": "Stereolab"}],
                sleep=_fake_sleep,
            )
        )

        assert result.aborted_on_shed is True
        assert fired == ["a", "a"], "the parked worker fired after the run had aborted"

    def test_supplied_queries_are_fired_round_robin_across_workers(self, monkeypatch):
        seen: list[str] = []
        monkeypatch.setattr(gate0_burst, "_fire_lookup", self._stub_lookup_recording(seen))

        asyncio.run(
            run_burst(
                host="http://x",
                api_key="k",
                concurrency=2,
                total=4,
                warm=False,
                smoke=False,
                timeout=1.0,
                queries=[{"label": "a", "artist": "Stereolab"}],
            )
        )

        assert seen == ["a", "a", "a", "a"]
